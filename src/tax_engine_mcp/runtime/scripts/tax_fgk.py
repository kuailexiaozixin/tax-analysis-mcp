#!/usr/bin/env python3
"""
国家税务总局政策法规库 (fgk.chinatax.gov.cn) — 检索清单并取正文。

取正文的关键是编码，不是 JS 渲染。该站详情页不声明 charset，requests 按
HTTP 头的 ISO-8859-1 解码，于是中文全部变成乱码，看起来"正文不在 HTML 里"
（检索词在原文里 0 次命中）。显式按 UTF-8 解码字节流即可拿到完整正文，
容器是 <div class="zscont">（注释）与 <div class="arc_cont">（正文），
正文内的法规名带 <a href> 指向关联文件。

页面头部还带一组 meta：ArticleTitle、PubDate、ContentSource、articleId
（关联文件查询接口 queryManuscriptAssociation 的入参）。

检索要翻页，否则筛不出法规文件。该接口把 pageSize 卡在 10，传更大的值也只回
10 条（实测传 20/60/100 一律返回 10 条），所以只能按页取。翻页基准：总局
search5 的 pageNum 从 0 起算，收在 tax_web_search.search_chinatax 里（本模块
的 page 参数从 1 起算）。基准用错时实测很隐蔽——「特别纳税调整实施办法」
按 1 起算发出去，取回的是真实第二屏，目标文件落在第 5 屏；按 0 起算后它就是
第 1 屏的首条。

检索范围默认锁在文件类标签（tax_web_search.FILE_LABELS），不是全站。这一维
决定翻页窗口里装的是什么：「转让定价」按全站取，前 3 屏一条法规库条目都没有
（全是新闻与各地动态，自适应就此收尾，得 0 条），收窄到文件类标签后 18 条
命中全部取回；「小微企业」全站 4822 条、首屏十条无一条是文件，收窄后 184 条。
代价是会漏掉标在「视频政策解读」「图片政策解读」上的法规库条目，那些页面回的是
media_only 空正文，本来就引不了条文；要连它们一起搜，命令行给 --all-labels。

Usage:
  python tax_fgk.py "研发费用" --size 10
  python tax_fgk.py "增值税" --size 5 --json
  python tax_fgk.py "资产评估减值" --size 1 --body
  python tax_fgk.py "转让定价" --size 3 --pages 8       # 严格翻 8 页（关自适应）
  python tax_fgk.py "增值税" --size 10 --cache          # 清单缓存(TTL 1h)，正文仍现拉
  python tax_fgk.py --cache-stats / --cache-clear       # 查看 / 清空缓存

翻页约定：默认按需自适应——连续 IDLE_PAGE_LIMIT(3) 页没捞到新的法规库条目
就收尾，不再往后翻（范围已是文件类标签，还连着几页空手，剩下的只是沾词的
别的文件）。显式给 --pages 则关掉自适应，严格翻满该页数。

缓存约定：只缓存"检索清单"（标题/文号/日期/URL），**正文永不缓存**——
条文必须每次现拉，避免把已废止/被修订的旧条文当现行有效引用。
"""

import argparse
import copy
import html as htmllib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import tax_http
import tax_evidence
from tax_cache import CacheManager
from tax_web_search import (FGK_MARKER, CHINATAX_HOST, FGK_HOST,
                            add_filter_args, aging_of, filters_from_args,
                            label_scope_text, order_text, search_chinatax,
                            search_opts_from_args)


def filters_token(filters: dict) -> str:
    """把收窄维度收成一段稳定的缓存键后缀（None 与空 dict 都返回空串）。

    用分号分隔、按字典序，避开 CacheManager._key 里 join 用的竖线，也保证维度
    的写入顺序不影响键值。
    """
    if not filters:
        return ""
    return "flt:" + ";".join(f"{k}={filters[k]}" for k in sorted(filters))


def scope_token(file_only: bool, order: str) -> str:
    """把非默认的检索范围收成缓存键段；默认范围返回空串。

    清单的内容由 (label 白名单, orderBy) 决定，与 filters 是两个维度：白名单关掉
    之后同一检索词回的是另一个集合的排序，两种清单绝不能共用一条缓存。默认
    （file_only=True + relevance）不追加键段，让键回到 LIST_KEY_REV 那一条，
    只有偏离默认才另立键。
    """
    parts = []
    if not file_only:
        parts.append("labels=all")
    if order != "relevance":
        parts.append(f"order={order}")
    return ";".join(parts)


# 总局检索接口把 pageSize 卡在 10 条，传更大的值无效，只能按页翻。
PAGE_SIZE = 10
# 翻页上限。默认范围收窄到文件类标签后，法规库条目集中得很靠前（2026-10-02 实测
# 「转让定价」18 条命中只占 2 页，第 3 页就空了；「研发费用加计扣除」62 条命中，
# 取满 30 条只用掉 3 页）；留到 20 页是给「增值税」这类命中上千条的宽词兜底
# （1908 条要 191 页，只能取回前一截）。命中量极大的关键词用 --pages 自行收敛。
MAX_PAGES = 20

# 清单缓存 TTL（秒）。只缓存检索清单，正文永远现拉。默认关闭，用 --cache 打开。
# 清单/元数据变动频率低（与手册“详情元数据 1 小时”一致），故 TTL 取 1 小时。
CACHE_TTL = 3600

# 按需自适应收尾：连续这么多页都没捞到新的法规库条目，就不再往后翻。
# 默认范围已经是文件类标签，还在连着几页捞不到新条目，说明剩下的只是与检索词
# 沾边的别的文件，继续翻只是白烧请求。要严格翻满某个页数，用 --pages 显式指定
# （那时不做自适应）。
IDLE_PAGE_LIMIT = 3

# 第几页之内算"浅页"。总局检索按相关度排序，越往后越松：第 1 页基本都在主题
# 上，第 2 页起开始出现只沾一个词的条目。浅页之外的条目逐条带 `_reliability:
# medium` 与一句写明"深页排序变松、引用前要回上位库核对"的说明，这一句会原样
# 进 `tax_evidence.grade` 的 `caveats`、界面卡面和聚合输出——定级层不再把它折算
# 成分数，所以说明必须由来源自己写清，档位名承载不了原因。
FGK_SHALLOW_PAGES = 1

FGK_DEEP_NOTE = (
    "取自总局检索第 {page} 页：翻得越深排序越松，深页条目可能只是沾了检索词。"
    "仅用于定位法规，引用具体条文前必须回上一级数据库核对上位法。")

# 清单缓存键的翻页基准版本号。总局接口的 pageNum 原是从 0 起算、我们按 1
# 起算发送，等于每轮检索都丢掉相关度最高的首屏；按 1 起算后，用旧基准写下的
# 清单缺的就是这一屏，必须让它整体失效重抓，所以把基准写进键里。
# pn0 之后追加 files：清单默认改为只在 FILE_LABELS 那十类文件标签里检索。
# 实测同一批检索词在两种范围下的差距：「转让定价」全站前 3 页一条法规库条目都
# 没有（自适应收尾，得 0 条），收窄后 18 条全部取回；「小微企业」全站 4822 条
# 命中、首屏十条无一条是文件，收窄后 184 条。旧键写下的清单是按全站排序取的，
# 不能继续顶替新默认，故一并作废。
LIST_KEY_REV = "pn0-files"

_cache = CacheManager(enabled=False, namespace="fgk")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

_ZS_OPEN_RE = re.compile(r'<div class="zscont"[^>]*>', re.I)
_ARC_OPEN_RE = re.compile(r'<div class="arc_cont"[^>]*>', re.I)

# ── 正文取回状态 ────────────────────────────────────────────────────────────
# 原先"拿到了"看 content、"没拿到"看 _error 里的措辞：调用方要看两个键、还要拿
# 句子做子串比对才能判（search_fgk 就曾靠 "视频/图片" in _error 认视频条目）。
# 这一组值把判定收进一个键：
#   ok      —— 清洗后有文字条文
#   dynamic —— HTTP 200、返回体短到装不下条文、且带着页面框架特征：正文靠脚本渲染
#   empty   —— HTTP 200、正文容器取到、清洗后没有字（视频/图片型正文归这一类）
#   error   —— 请求本身没成：连不上、非 200、本会话已判定不可用的通道
BODY_OK = "ok"
BODY_DYNAMIC = "dynamic"
BODY_EMPTY = "empty"
BODY_ERROR = "error"
BODY_STATUSES = (BODY_OK, BODY_DYNAMIC, BODY_EMPTY, BODY_ERROR)

#: 判定动态壳页的返回体上限（字符数）。实测两类壳页的静态 HTML 是 1,731 字
#: （`c100009/c5193055`）与 939 字（`c100024/c5245218`），同站带条文的页面在万字级。
DYNAMIC_SHELL_MAX_CHARS = 3000
#: 框架特征：外链脚本。正文靠脚本渲染的页面必然是这个形态，带条文的静态页不是。
_SHELL_SCRIPT_RE = re.compile(r"<script\b[^>]+\bsrc\s*=", re.I)

#: 检索词里带着法规库标识时的直达通道：形如 `c100009/c5237365`（栏目段/文章段），
#: 详情页地址是能拼出来的，不必等 search5 活着。上一轮 search5 多次 SSLError，
#: 手里已经有标识却回空清单，等于把取数失败当成"库里没有"。
_FGK_ID_PAIR_RE = re.compile(r"\b(c\d{5,9})/(c\d{5,9})\b", re.I)
# 导航栏里也含 arc_cont，按顺序取最后一处才是正文
_ZS_END_RE = re.compile(r'<div class="arc_cont"[^>]*>|</body>', re.I)
_ARC_END_RE = re.compile(
    r'<div class="(?:arc_cont|bot-btns-box)"[^>]*>|</body>', re.I)
_META_RE = re.compile(r'<meta\s+name="([^"]+)"\s+content="([^"]*)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_PARA_RE = re.compile(r"</(?:p|div|li|tr)\s*>|<br\s*/?>", re.I)
# 表格单独处理：摊平写法丢的是行列对应——Word 粘贴来的表每格包着 <p>，摊平成
# "一格一行"（c5194303 的表头摊平成 序号/产品种类/产品范围/征收标准 四行）；
# 没有 <p> 的表整行各格首尾相接成一串字。所以先按表切段。
_TABLE_RE = re.compile(r"(?is)<table\b[^>]*>.*?</table>")
_ROW_RE = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")
_CELL_RE = re.compile(r"(?is)<(t[hd])([^>]*)>(.*?)</\1>")
_SPAN_RE = re.compile(r"(?i)\b(colspan|rowspan)\s*=\s*[\"']?(\d+)")


def _span_of(attrs: str, name: str) -> int:
    """取单元格上的 colspan/rowspan，没有或不是正整数就按 1 算。"""
    for key, value in _SPAN_RE.findall(attrs):
        if key.lower() == name:
            return max(1, int(value))
    return 1


def _table_lines(html: str) -> list:
    """一张 HTML 表铺成 Markdown 行，colspan 补空列、rowspan 把值带到它盖住的每一行。

    rowspan 的延续行填的是同一个文本而不是空串：合并格的意思是"这几行都算这个值"，
    留空会让读者以为那几行没有这一列。同一个值重复出现不影响下游判据——执行期限
    与文号都是按去重后的集合计数，重复值不新增一段期限。
    """
    carry = {}          # 列号 -> [还剩几行, 文本]
    lines = []
    for row in _ROW_RE.findall(html):
        cells = _CELL_RE.findall(row)
        line, col, idx = [], 0, 0
        while idx < len(cells) or any(v[0] > 0 for v in carry.values()):
            pending = carry.get(col)
            if pending and pending[0] > 0:
                line.append(pending[1])
                pending[0] -= 1
                col += 1
                continue
            if idx >= len(cells):
                break
            _tag, attrs, inner = cells[idx]
            idx += 1
            text = _plain_text(inner).replace("\n", " ").strip()
            text = re.sub(r"\s+", " ", text)
            for k in range(_span_of(attrs, "colspan")):
                line.append(text if k == 0 else "")
                over = _span_of(attrs, "rowspan")
                if over > 1:
                    carry[col] = [over - 1, text]
                col += 1
        lines.append(line)
    lines = [ln for ln in lines if any(x for x in ln)]
    if not lines:
        return []
    width = max(len(ln) for ln in lines)
    lines = [ln + [""] * (width - len(ln)) for ln in lines]
    out = ["| " + " | ".join(ln) + " |" for ln in lines]
    if len(out) > 1:
        out.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return out


def _end_at(pattern, page: str, start: int) -> int:
    """正文容器右边界的位置：找得到结束标记就用它，找不到就取到页尾。

    arc_cont 开标签在、却缺 </body> 与后续 arc_cont/bot-btns-box 时，`.search()`
    回 None，原先直接 `.start()` 会抛 AttributeError，把"页面截断/改版"这类
    本该归 empty 或 error 的情形变成整个取数崩溃。取到页尾后交给下面的四态判定：
    容器里有字就 ok、没字就 empty，与完整页面走同一条收口。
    """
    m = pattern.search(page, start)
    return m.start() if m else len(page)


def _text_of(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    parts, pos = [], 0
    for match in _TABLE_RE.finditer(fragment):
        head = fragment[pos:match.start()]
        if head.strip():
            parts.append(_plain_text(head))
        parts.extend(_table_lines(match.group(0)))
        pos = match.end()
    tail = fragment[pos:]
    if tail.strip():
        parts.append(_plain_text(tail))
    return "\n".join(x for x in parts if x)


def _plain_text(fragment: str) -> str:
    fragment = _PARA_RE.sub("\n", fragment)
    fragment = _TAG_RE.sub("", fragment)
    fragment = htmllib.unescape(fragment)
    fragment = fragment.replace(" ", " ").replace("　", " ")
    lines = [ln.strip() for ln in fragment.splitlines()]
    return "\n".join(ln for ln in lines if ln)


_MEDIA_SRC_RE = re.compile(
    r"(?is)<(?:img|video|audio|source)\b[^>]*\bsrc=[\"']([^\"']+)[\"']")


def _media_urls(fragment: str, page_url: str) -> list:
    """列出正文容器里的图片/视频地址。

    没有文字可引时，读者要的是"那去看原文的那张图"，只回一句"正文是图片"等于
    把人挡在外面。页面里的图片多用相对地址（形如「<文章id>/images/x.png」），
    拼在详情页所在目录后面才是可点开的链接。
    """
    base = page_url.rsplit("/", 1)[0]
    root = base.split("//", 1)[-1].split("/", 1)[0]
    scheme = base.split("//", 1)[0] if "//" in base else ""
    out, seen = [], set()
    for src in _MEDIA_SRC_RE.findall(fragment):
        src = src.strip()
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            # 协议相对写法 //host/path，沿用详情页的协议；落到下面 "/" 分支会
            # 把 host 当成详情页目录下的第一段路径，拼出一个不存在的域名。
            full = f"{scheme}{src}"
        elif src.startswith("http"):
            full = src
        elif src.startswith("/"):
            # 以 / 开头是站根相对地址，拼在详情页目录后面会多出一层路径
            full = f"{scheme}//{root}{src}"
        else:
            full = f"{base}/{src}"
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def fetch_fgk_body(url: str) -> dict:
    """
    取法规库详情页正文。

    请求经 `tax_http.attempt`：瞬时失败（断连、429、5xx）退避后重发，最多
    `tax_http.DEFAULT_ATTEMPTS` 次，每一次尝试都记进返回值的 `_attempts`。
    末次原因写在 `_error` 里并带着次数（"3 次尝试仍失败：HTTP 502"），
    所以"试了几次都不通"与"一次不通"（那句就是光秃秃的 `HTTP 404`）在返回值上
    就能分开；同一 host 连续两次 TLS 握手失败后本会话不再发第三个请求，
    `_error` 回"该通道本会话已判定不可用"。

    Returns:
        {"url","body_status","_attempts","title"?,"pub_date"?,"content"?,
         "meta"?,"media_only"?,"media_urls"?,"_error"?}
        `body_status` 是判定键（值域见 `BODY_STATUSES`），`_error` 是给人看的那句
        原因；视频/图片型正文归 `empty` 并另带 `media_only=True` 与素材地址，
        与"取失败"分开——前者重试无用。
    """
    out = {"url": url, "body_status": BODY_ERROR, "_attempts": tax_http.new_log()}
    # verify 显式传 True：这一路用 requests 的默认校验（tax_detail
    # 与 tax_search 用的则是默认关闭的 VERIFY_SSL），这里不跟着关。
    r, err, _blocked = tax_http.attempt(
        lambda: tax_http.get(url, headers=HEADERS, timeout=25, verify=True),
        url, out["_attempts"])
    if err:
        out["_error"] = err
        return out
    if r.status_code != 200:
        out["_error"] = f"HTTP {r.status_code}"
        return out

    # 不声明 charset，必须显式按 UTF-8 解码，否则中文全是乱码
    page = r.content.decode("utf-8", errors="replace")
    meta = {k.lower(): v for k, v in _META_RE.findall(page)}
    out["meta"] = meta
    out["title"] = meta.get("articletitle", "")

    # 按位置切，不用嵌套正则：zscont 内部嵌套 div，贪婪/懒惰都拿不准边界，
    # 且 arc_cont 在导航栏里也出现，必须取 zscont 之后的第一处。
    note = raw_body = ""
    located = False     # 是否定位到正文容器（与"定位到但里面没字"是两回事）
    zs = list(_ZS_OPEN_RE.finditer(page))
    arcs = list(_ARC_OPEN_RE.finditer(page))
    if zs and arcs:
        z = zs[-1]
        note = page[z.end():_end_at(_ZS_END_RE, page, z.end())]
        a = next((m for m in arcs if m.start() > z.start()), None)
        if a is not None:
            raw_body = page[a.end():_end_at(_ARC_END_RE, page, a.end())]
            located = True
    elif arcs:
        a = arcs[-1]
        raw_body = page[a.end():_end_at(_ARC_END_RE, page, a.end())]
        located = True

    if not located:
        # 页面回了 200 但静态 HTML 里没有正文容器。两种成因要在返回值上分开：
        # 短页面 + 框架脚本 = 正文靠 JS 渲染（重发无用，得换取数路径）；
        # 其余 = 页面结构与预期不符（改版或拿错了地址）。
        if len(page) < DYNAMIC_SHELL_MAX_CHARS and _SHELL_SCRIPT_RE.search(page):
            out["body_status"] = BODY_DYNAMIC
            out["_error"] = ("正文为脚本渲染：静态 HTML 只有页面框架"
                             f"（{len(page)} 字，无条文容器）")
        else:
            out["body_status"] = BODY_ERROR
            out["_error"] = "未取到正文（页面结构与预期不符）"
        return out

    if not raw_body:
        out["body_status"] = BODY_EMPTY
        out["_error"] = "正文容器为空"
        return out

    body = _text_of(raw_body)
    # 税法小课堂等栏目的正文是视频/图片，容器取得到但没有文字
    if not body and re.search(r"<(video|img|audio)\b", raw_body, re.I):
        out["body_status"] = BODY_EMPTY
        out["media_only"] = True
        out["_error"] = "该条正文为视频/图片，无文字内容"
        media = _media_urls(raw_body, url)
        if media:
            out["media_urls"] = media
        return out

    out["content"] = "\n".join(x for x in (_text_of(note), body) if x)
    out["pub_date"] = (meta.get("pubdate", "") or "")[:10]
    out["body_status"] = BODY_OK if out["content"] else BODY_EMPTY
    if not out["content"]:
        out["_error"] = "正文容器为空"
    return out


def fgk_detail_url(column_id: str, article_id: str) -> str:
    """把法规库标识拼成详情页地址（`c100009/c5237365` → 可直接取的 URL）。"""
    return f"{FGK_HOST}/zcfgk/{column_id.lower()}/{article_id.lower()}/content.html"


#: 详情页取正文的并发上限。实测口径：fgk.chinatax.gov.cn 详情页 4 条串行 1.69s、
#: 4 线程并发 0.42s，两边各 4 条全部 ok、0 条 error（同日同端点，每次 GET 间隔 0）。
#: 站上有串行闸的是 NPC 与搜狗，法规库详情页没有闸；这个数就是实测的那个上限，
#: 未经重测不再往上加。
BODY_MAX_WORKERS = 4


def _fetch_one_body(url: str) -> dict:
    """取一条正文；单条异常收成与 fetch_fgk_body 同形的失败结果，不拖垮整批。

    返回形状与 `fetch_fgk_body` 一致（url/body_status/_attempts/_error），
    调用点按同一套键处理，并发与串行走的是同一条收口。
    """
    try:
        return fetch_fgk_body(url)
    except Exception as e:                      # 单条失败只影响这一条
        return {"url": url, "body_status": BODY_ERROR, "_attempts": [],
                "_error": f"{type(e).__name__}：{e}"[:200]}


def _fetch_bodies(urls: list) -> list:
    """取若干条正文，返回与入参同序的结果列表（有界并发，结果按下标对齐）。

    `ThreadPoolExecutor.map` 本身保序；0/1 条时走直调，不起线程池。
    台账仍是每条自己的 `_attempts`，并发不改变"谁发了哪几次请求"的记账口径。
    """
    if len(urls) <= 1:
        return [_fetch_one_body(u) for u in urls]
    with ThreadPoolExecutor(max_workers=min(BODY_MAX_WORKERS, len(urls))) as pool:
        return list(pool.map(_fetch_one_body, urls))


def detail_ids_from_text(text: str) -> list:
    """从一段文本里抠出法规库标识对，按出现顺序去重。

    只认"栏目段/文章段"成对出现的写法（`c100009/c5237365`）：单给一个 `c5237365`
    拼不出地址——栏目段决定页面挂在哪个栏目下，缺了它只能猜，猜错就是 404。
    """
    out, seen = [], set()
    for column, article in _FGK_ID_PAIR_RE.findall(text or ""):
        pair = (column.lower(), article.lower())
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _direct_entries(keyword: str) -> tuple[list, list, list]:
    """检索接口失败时的退路：按标识直达详情页。

    Returns:
        `(条目, 台账, 未取回的说明)`。条目只带详情页 meta 里的标题与成文日期，
        正文一律不落进这一层——`_scan_list` 的产出会进清单缓存，而缓存旧条文会把
        已废止的当现行有效引用（正文永远现拉这条规矩不因走直达通道而松动）。
        取回状态仍随 `_body_status` 交出去：动态壳页、空正文与取失败是三种
        不同的下一步。
    """
    entries, attempts, failed = [], [], []
    pairs = detail_ids_from_text(keyword)
    urls = [fgk_detail_url(column, article) for column, article in pairs]
    # 正文有界并发取，结果与 pairs 同序；每条仍各记自己的尝试台账
    bodies = _fetch_bodies(urls)
    for (column, article), body in zip(pairs, bodies):
        url = fgk_detail_url(column, article)
        attempts.extend(body.get("_attempts") or [])
        entry = {
            "title": body.get("title", ""),
            "url": url,
            "date": body.get("pub_date", ""),
            "source": "税务总局法规库",
            "source_label": "税务总局法规库",
            # page=0 标明这一条不是检索翻页取回的，是按标识直达的
            "page": 0,
            "_direct_hit": True,
            "_body_status": body.get("body_status", BODY_ERROR),
        }
        entry["body_in_attachment"] = tax_evidence.has_body_in_attachment(entry)
        if body.get("media_only"):
            entry["media_only"] = True
        if body.get("_error"):
            entry["_body_error"] = body["_error"]
            failed.append(f"{column}/{article}：{body['_error']}")
        else:
            entries.append(entry)
    return entries, attempts, failed


# 详情页 URL 末段就是文章 id（/zcfgk/c{栏目}/c{文章id}/content.html），法律类
# 页面可能没有 articleId meta，所以从 URL 抠比只读 meta 稳。
_ARTICLE_ID_RE = re.compile(r"/c(\d+)/content\.html", re.I)

# 关联接口的四个分组：接口键名 -> 输出字段名
_ASSOC_GROUPS = {
    "policyDocument": "files",               # 关联文件（含同一政策的现行/废止版）
    "policyInterpretation": "interpretations",  # 关联解读
    "policyGuidance": "guidances",           # 政策指引
    "policyQA": "qas",                       # 政策问答
}


def article_id_from_url(url: str) -> str:
    """从详情页 URL 取文章 id（queryManuscriptAssociation 的入参）。

    实测 meta 的 articleId 与 URL 末段一致（c5247431 → 5247431），法律类页面
    meta 可能缺，URL 末段总在，所以以 URL 为准、取 content.html 前最后一个 c 段。
    """
    ids = _ARTICLE_ID_RE.findall(unquote(urlparse(url or "").path))
    return ids[-1] if ids else ""


def _assoc_title(raw: str) -> str:
    """关联条目标题：去高亮标签、解实体、压空白。"""
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG_RE.sub("", raw or ""))).strip()


def _assoc_row(it: dict) -> dict:
    """把关联接口的一条原始条目归一成 {title,url,document_number,status,effect_level}。"""
    u = (it.get("url") or "").strip()
    if u.startswith("/"):
        # 接口给的是相对路径，必须拼 fgk 域才取到正文（拼 www 回 404，实测）。
        u = FGK_HOST + u
    row = {
        "title": _assoc_title(it.get("title")),
        "url": u,
        "document_number": (it.get("writtentext") or "").strip(),
        "effect_level": (it.get("effectlevel") or "").strip(),
    }
    aging = aging_of(it.get("aging"))
    if aging:
        row["status"] = aging
    return row


def fetch_associations(article_id: str) -> dict:
    """查一份文件的关联文件/解读/指引/问答。

    走 POST queryManuscriptAssociation（表单参数 id=articleId）。静态详情页
    HTML 里这几组是空的（实测 glwjlist/gljdlist 为空、正文 <a href> 只有零星
    线索），必须调接口——这正是主线④"同一文件的现行版/被废止版"的线索来源：
    返回的 policyDocument 每条带 status（时效），据此能看出关联的是全文有效
    还是已废止的旧版。

    域名两处不能混：POST 只在 www 域返回 200（同一 id 打 fgk 域回 404），而它
    返回的 /zcfgk/… 相对链接要拼 fgk 域才取到正文（拼 www 回 404，实测）。

    Args:
        article_id: 详情页文章 id，用 article_id_from_url 从 URL 取

    Returns:
        {"article_id","files":[…],"interpretations":[…],"guidances":[…],
         "qas":[…],"_error"?}；_error 非空时四组均为空列表。
    """
    out = {"article_id": article_id, "files": [], "interpretations": [],
           "guidances": [], "qas": [], "_attempts": tax_http.new_log()}
    if not article_id:
        out["_error"] = "URL 里取不到 articleId，无法查关联"
        return out
    assoc_url = f"{CHINATAX_HOST}/queryManuscriptAssociation"
    r, err, _blocked = tax_http.attempt(
        lambda: tax_http.request("POST", assoc_url, headers=HEADERS, timeout=25,
                                 verify=False, data={"id": article_id}),
        assoc_url, out["_attempts"])
    if err:
        out["_error"] = err
        return out
    if r.status_code != 200:
        out["_error"] = f"HTTP {r.status_code}"
        return out
    try:
        payload = r.json()
    except ValueError as e:
        out["_error"] = f"响应不是 JSON: {e}"
        return out

    results = ((payload.get("results") or {}).get("data") or {}).get("results") or []
    # results[0] 是文章本体，关联分组在含 policyDocument 键的那一段（实测是 [1]）
    block = next((g for g in results
                  if isinstance(g, dict) and "policyDocument" in g), None)
    if block is None:
        out["_error"] = "响应里没有关联分组（接口结构变了或该文件无关联）"
        return out
    for key, field in _ASSOC_GROUPS.items():
        out[field] = [_assoc_row(it) for it in (block.get(key) or [])
                      if isinstance(it, dict)]
    return out


def attach_associations(entries: list) -> list:
    """给每条清单条目现拉一份关联（每条多一次 POST，不缓存）。

    关联里的政策文件也带时效，缓存久了可能把"当时废止、现已改回来"的旧关系
    当成现状，所以与正文一样每次都现拉。就地写 entry["associations"] 并返回
    同一个列表。
    """
    for entry in entries:
        aid = article_id_from_url(entry.get("url", ""))
        entry["associations"] = fetch_associations(aid)
    return entries


def _scan_list(keyword: str, size: int, max_pages: int,
               adaptive: bool = True, filters: dict = None,
               file_only: bool = True, order: str = "relevance") -> dict:
    """只翻检索清单，不取正文（这一层才可缓存）。

    adaptive=True 时按需收尾：连续 IDLE_PAGE_LIMIT 页没捞到新的法规库条目
    就停。要严格翻满就用 adaptive=False（CLI 显式给 --pages 时）。

    filters 是 tax_web_search.build_filters 产出的收窄维度，逐页原样带进
    search_chinatax。把 column=政策法规 / xxgkSonTaxPolicy=<税种> 下推到检索
    侧，才是把翻页窗口对准法规文件的做法（否则自适应那 200 条窗口够不到散在
    深处的法规）。

    file_only 与 order 是 search_chinatax 的检索选项，同样逐页带下去：
    file_only=True 时只在文件类标签里搜，翻页窗口对准的就是文件本身；实测
    「小微企业」全站 4822 条首屏十条没有一条文件，收窄后 184 条首屏全是
    文件。代价是会漏掉标在「视频政策解读」「图片政策解读」上的法规库条目
    （2026-10-02 实测「研发费用加计扣除」前 3 页有 7 条），那两类回的是
    media_only 空正文，本来就引不了条文。要连它们一起拿，传 file_only=False。
    """
    results = []
    seen = set()
    pages = 0
    idle_pages = 0          # 连续多少页没新增法规库条目
    stopped_early = False   # 是否因自适应而提前收尾
    first_error = ""
    empty_reason = ""
    filter_note = ""
    total_hits = 0
    attempts = []           # 每一次网络尝试的四元组台账
    direct_note = ""
    for page in range(1, max(1, max_pages) + 1):
        found = search_chinatax(keyword, page=page, size=PAGE_SIZE,
                                filters=filters, file_only=file_only,
                                order=order)
        attempts.extend(found.get("_attempts") or [])
        if not first_error and found.get("_error"):
            first_error = found["_error"]
        if page == 1:
            total_hits = found.get("total", 0)
            empty_reason = found.get("_empty_reason", "")
            filter_note = found.get("_filter_note", "")
        page_items = found.get("results", [])
        pages += 1
        if not page_items:
            break
        before = len(results)
        for item in page_items:
            if FGK_MARKER not in item.get("url", ""):
                continue
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            entry = {
                "title": item["title"],
                "url": item["url"],
                "date": item.get("date", ""),
                "document_number": item.get("document_number", ""),
                "publisher": item.get("publisher", ""),
                "snippet": item.get("snippet", ""),
                "source": "税务总局法规库",
                "source_label": "税务总局法规库",
                # 记下取自第几页：下游要靠它判断"这条是主题命中还是深页凑数"
                "page": page,
            }
            # 文号、时效、效力级别都从接口的录入项带下来。缺了这三栏，法规库
            # 条目每条都只能报"时效未标明"，定级环节就没法把 2026 年新发的
            # 公告和已被废止的公告分开。
            for k in ("status", "status_from", "effect_level", "publish_date"):
                if item.get(k):
                    entry[k] = item[k]
            if item.get("attachments"):
                entry["attachments"] = item["attachments"]
            # 带着附件的条目在文本层之外还有一份可点的原文（税率表、清单常只做成
            # 附件）。正文取回来是空的时，这个键把"这条规定不存在"与"正文在附件里"
            # 分开——判据在 tax_evidence.has_body_in_attachment，与详情层共用一份。
            entry["body_in_attachment"] = tax_evidence.has_body_in_attachment(entry)
            if entry.get("effect_level"):
                entry["category"] = entry["effect_level"]
            if page > FGK_SHALLOW_PAGES:
                entry["_reliability"] = "medium"
                entry["_reliability_note"] = FGK_DEEP_NOTE.format(page=page)
            results.append(entry)
            if len(results) >= size:
                break
        if len(results) >= size:
            break
        if adaptive:
            if len(results) == before:
                idle_pages += 1
                if idle_pages >= IDLE_PAGE_LIMIT:
                    stopped_early = True
                    break
            else:
                idle_pages = 0

    result = {
        "keyword": keyword,
        "total": len(results),
        "total_hits": total_hits,
        "pages_scanned": pages,
        "stopped_early": stopped_early,
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "税务总局法规库 (fgk.chinatax.gov.cn)",
        "_from_cache": False,
        # 每一次尝试的 (host, 异常类名或状态码, 耗时 ms, 第几次)，见 tax_http
        "_attempts": attempts,
    }
    if filters:
        result["filters"] = filters
    if file_only is False:
        result["label_scope"] = label_scope_text(file_only)
    # 检索本身失败要透出错误，不要和"库里没有"混为一谈
    if first_error:
        result["_error"] = first_error
        result["_fetch_failed"] = True
        # 检索接口不通、但检索词里就带着法规库标识（形如 c100009/c5237365）：
        # 详情页地址是能拼出来的，直接取详情页，而不是把这一轮交回空清单。
        # 直达取到条目时把标记换成 _search_failed：清单这一路确实失败了，但手里
        # 有条目，而消费方（tax_answer._fgk_lookup_row）见到 _fetch_failed 会把整轮
        # 读成"什么都没有"。一条都没取到时 _fetch_failed 原样留着，绝不清零——
        # "取数失败"与"0 条命中"必须在返回值上分得开（红线⑦）。
        if detail_ids_from_text(keyword):
            direct, direct_attempts, direct_failed = _direct_entries(keyword)
            attempts.extend(direct_attempts)
            result["_search_failed"] = True
            tail = ("；".join(direct_failed) if direct_failed else
                    "详情页回的是空正文或未定位到正文容器")
            if direct:
                results.extend(direct[:size])
                result["total"] = len(results)
                result["direct_from_ids"] = True
                result.pop("_fetch_failed", None)
                result["_error"] = (
                    f"{first_error}；检索词里带着法规库标识，已改按标识直达详情页，"
                    f"取回 {len(direct)} 条"
                    + (f"，另有 {len(direct_failed)} 个标识未取回：" + tail
                       if direct_failed else ""))
            else:
                result["_error"] = f"{first_error}；按标识直达详情页也未取回：{tail}"
    elif empty_reason:
        # 接口给了命中数、这一页没给清单：不能写成"翻完 N 页未筛出法规库条目"，
        # 那是把接口的返回形态当成库的内容。
        result["_error"] = empty_reason
        result["_empty_reason"] = empty_reason
    elif not results:
        # 带了收窄维度却一条法规库条目都没有：优先把"维度拼窄"那句递出去，
        # 它比"翻完 N 页未筛出"更接近真相——0 可能是维度拼的，不是库里没有。
        if filter_note:
            result["_error"] = filter_note
            result["_filter_note"] = filter_note
        else:
            tail = (f"（连续 {idle_pages} 页无新法规库条目，已自适应收尾）"
                    if stopped_early else "")
            result["_error"] = (f"翻完前 {pages} 页总局检索结果（共 {total_hits} 条命中）"
                                f"未筛出法规库条目{tail}"
                                # 默认范围只在文件类标签里搜，视频/图片解读那两类
                                # 法规库条目本来就不在窗口内；这句话必须写出来，
                                # 否则"未筛出"会被读成"库里没有这份文件"。
                                + ("（检索范围限于文件类标签，视频与图片解读类条目不在内，"
                                   "要连它们一起搜用 --all-labels）" if file_only else ""))
    return result


def search_fgk(keyword: str, size: int = 10, with_body: bool = False,
               max_pages: int = MAX_PAGES, adaptive: bool = True,
               filters: dict = None, file_only: bool = True,
               order: str = "relevance") -> dict:
    """
    在税务总局法规库检索法规文件清单。

    缓存策略：**只缓存清单，正文永不缓存**。清单按
    (keyword, size, max_pages, adaptive, filters, 检索范围) 缓存 CACHE_TTL 秒；
    命中缓存时直接返回清单，正文（with_body）仍逐条现拉。

    Args:
        keyword: 检索词
        size: 返回条数上限
        with_body: 逐条取详情页正文（每条多一次请求，正文不走缓存）
        max_pages: 最多翻几页总局检索结果（每页固定 10 条）
        adaptive: 连续 IDLE_PAGE_LIMIT 页无新法规库条目即收尾（默认开）；
                  要严格翻满 max_pages 就传 False
        filters: build_filters 产出的收窄维度；并入缓存键，不同维度不会共用缓存
        file_only: 检索范围，True（默认）只在文件类标签里搜，见 _scan_list；
                   False 连新闻/视频/各地动态一起搜。两种范围是不同集合，各自入键
        order: 传给 search_chinatax 的排序，relevance（默认）/ date_desc /
               category / date_asc

    Returns:
        {"keyword","total","total_hits","pages_scanned","stopped_early","results",
         "searched_at","source","_from_cache","_attempts","_cache_age_s"?,"_error"?,
         "filters"?}
        每项含 title/document_number/date/publisher/url；with_body 时另有
        body（正文）与 `body_status`（取值见 `BODY_STATUSES`，是"拿到了/页面是
        动态壳/清洗后没字/取失败"的唯一判定键，`body_error` 只是给人看的原因句）。
        正文是视频/图片的条目另带 media_only=True——表示"本来就没有文字"，
        与取失败的 body_status=error 区分开，上层据此判断无需重试。
        `_attempts` 是这一轮发过的每一次请求 `(host, 异常类名或状态码, 耗时 ms,
        第几次)`；清单命中缓存时那几条是写下这份清单那一轮发过的请求，正文的
        尝试仍照本轮真实发出追加在后面。
        检索接口失败但检索词里带着法规库标识（`c100009/c5237365` 这种）时，
        改按标识直达详情页取条目，带 `direct_from_ids=True`；直达也没取到时
        `_fetch_failed` 保持 True，绝不清零成"0 条命中"。
    """
    # 不带维度时不追加键段，旧基准（LIST_KEY_REV）写下的无过滤清单继续命中，
    # 不必整体重抓；带维度时把排序后的键值拼进去，None 与 {} 视作同一种"无过滤"。
    # 检索范围与排序只有偏离默认时才追加（scope_token），否则白名单默认一改，
    # 旧的全站清单就会顶着新默认被取出来。
    key_parts = ["fgk", LIST_KEY_REV, keyword, str(size), str(max_pages),
                 str(adaptive)]
    token = filters_token(filters)
    if token:
        key_parts.append(token)
    token = scope_token(file_only, order)
    if token:
        key_parts.append(token)
    cache_key = _cache._key(*key_parts)
    result = _cache.get(cache_key, max_age=CACHE_TTL)
    if result is not None:
        # 深拷贝，避免下面写 body 时污染缓存文件
        result = copy.deepcopy(result)
        result["_from_cache"] = True
        cached_age = _cache.age(cache_key)
        if cached_age is not None:
            result["_cache_age_s"] = round(cached_age, 1)
    else:
        result = _scan_list(keyword, size, max_pages, adaptive, filters,
                            file_only=file_only, order=order)
        _cache.set(cache_key, result)  # 缓存的是"无正文"的清单

    # 正文永远现拉，绝不缓存（避免引用过期条文）
    if with_body:
        # 先收集要取正文的条目，再按实测允许的上限有界并发取，结果与条目同序套回。
        # 各站自己的串行闸（NPC、搜狗）不在这一条路径上，这条只并发法规库详情页。
        targets = [entry for entry in result["results"] if entry.get("url")]
        bodies = _fetch_bodies([entry["url"] for entry in targets])
        for entry, body in zip(targets, bodies):
            entry["body"] = body.get("content", "")
            # 判定只认这一个键；body_error 留给人读的那句原因
            entry["body_status"] = body.get("body_status", BODY_ERROR)
            # 清单层已带 `_attempts`（直达通道的那几条在里面），正文的尝试续在后面
            entry["_attempts"] = (entry.get("_attempts") or []) + \
                (body.get("_attempts") or [])
            if body.get("pub_date") and not entry["date"]:
                entry["date"] = body["pub_date"]
            if body.get("_error"):
                entry["body_error"] = body["_error"]
                # 视频/图片条目单独标出来：这不是"取失败"，是"本来就没有文字"。
                # 上层见到 media_only 就知道不该去引条文，也不必重试。标记由
                # fetch_fgk_body 直接给，不再靠比对错误句子里的字。
                if body.get("media_only"):
                    entry["media_only"] = True
                if body.get("media_urls"):
                    entry["media_urls"] = body["media_urls"]
    # 条目级的尝试台账汇总一份到轮次级：整轮发过几次请求、都败在哪，一眼能数出来
    agg = list(result.get("_attempts") or [])
    for entry in result["results"]:
        agg.extend(entry.pop("_attempts", None) or [])
    result["_attempts"] = agg
    return result


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="国家税务总局政策法规库检索")
    p.add_argument("keyword", nargs="?", help="检索词")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--body", action="store_true",
                   help="同时取详情页正文（每条多一次请求，正文不走缓存）")
    p.add_argument("--pages", type=int, default=None,
                   help=f"最多翻几页总局检索结果，每页固定 {PAGE_SIZE} 条。"
                        f"不指定时按需自适应（连续 {IDLE_PAGE_LIMIT} 页无新条目"
                        f"即收尾，上限 {MAX_PAGES} 页）；显式指定则严格翻满")
    p.add_argument("--json", action="store_true")
    p.add_argument("--cache", action="store_true",
                   help=f"启用清单缓存（TTL {CACHE_TTL}s）；正文仍现拉")
    p.add_argument("--no-cache", action="store_true", help="禁用缓存（默认）")
    p.add_argument("--cache-stats", action="store_true", help="查看缓存统计")
    p.add_argument("--cache-clear", action="store_true", help="清空缓存")
    p.add_argument("--assoc", action="store_true",
                   help="逐条查关联文件/关联解读（每条多一次请求，走 "
                        "queryManuscriptAssociation；不依赖 --body）")
    add_filter_args(p)
    args = p.parse_args()

    global _cache
    if args.cache:
        _cache = CacheManager(enabled=True, namespace="fgk")

    if args.cache_stats:
        print(json.dumps({"cache": _cache.stats()}, ensure_ascii=False, indent=2))
        return
    if args.cache_clear:
        _cache.clear()
        print("Cache cleared.")
        return

    if not args.keyword:
        p.error("需要检索词（仅 --cache-stats / --cache-clear 可省略）")

    max_pages = args.pages if args.pages is not None else MAX_PAGES
    adaptive = args.pages is None      # 显式给了页数就别自作主张提前收尾

    try:
        filters = filters_from_args(args)
        opts = search_opts_from_args(args)
    except ValueError as e:
        p.error(str(e))

    result = search_fgk(args.keyword, size=args.size, with_body=args.body,
                        max_pages=max_pages, adaptive=adaptive, filters=filters,
                        **opts)
    if args.assoc:
        attach_associations(result["results"])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    cache_tag = ""
    if result.get("_from_cache"):
        age = result.get("_cache_age_s")
        cache_tag = f" [清单缓存{' ' + str(int(age)) + 's 前' if age is not None else ''}]"
    print(f"🔍 税务总局法规库 \"{args.keyword}\" | {result['searched_at']}{cache_tag}")
    print(f"   检索范围: {label_scope_text(opts['file_only'])} | "
          f"排序: {order_text(opts['order'])}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    early = (f"（连续 {IDLE_PAGE_LIMIT} 页无新法规库条目，已自适应收尾）"
             if result.get("stopped_early") else "")
    print(f"总局检索命中 {result['total_hits']} 条，翻了 {result['pages_scanned']} 页{early}，"
          f"筛出法规文件 {result['total']} 条\n")
    for item in result["results"]:
        print(f"  📋 {item['title']}")
        if item.get("document_number"):
            print(f"     文号: {item['document_number']}")
        if item.get("date"):
            print(f"     日期: {item['date']}")
        if item.get("publisher"):
            print(f"     发文机关: {item['publisher']}")
        print(f"     {item['url']}")
        for att in item.get("attachments") or []:
            print(f"     附件: {att['name']}（{att['url']}）")
        if item.get("body"):
            print(f"     正文 {len(item['body'])} 字:")
            for ln in item["body"].splitlines():
                print(f"       {ln}")
        if item.get("media_only"):
            print("     🎬 该条正文是视频/图片，没有文字可引（不是取失败，重试也无用）")
            for src in item.get("media_urls") or []:
                print(f"       原文素材: {src}")
        elif item.get("body_error"):
            print(f"     ⚠️ {item['body_error']}")
        assoc = item.get("associations")
        if assoc:
            if assoc.get("_error"):
                print(f"     关联: ⚠️ {assoc['_error']}")
            else:
                labels = {"files": "关联文件", "interpretations": "关联解读",
                          "guidances": "政策指引", "qas": "政策问答"}
                for field, label in labels.items():
                    for row in assoc.get(field, []):
                        bits = []
                        if row.get("document_number"):
                            bits.append(row["document_number"])
                        if row.get("status"):
                            bits.append(row["status"])
                        if row.get("effect_level"):
                            bits.append(row["effect_level"])
                        tail = f"（{' / '.join(bits)}）" if bits else ""
                        print(f"     {label}: {row['title']}{tail}")
                        if row.get("url"):
                            print(f"       {row['url']}")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
