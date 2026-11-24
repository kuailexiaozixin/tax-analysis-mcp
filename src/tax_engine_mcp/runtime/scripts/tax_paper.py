# -*- coding: utf-8 -*-
"""中国税务报网络报（数字报）第七源。

这不是第六源（中国税网 www.ctaxnews.com.cn，`tax_ctax.py` 与 search_source/ctaxnews/）
的翻版，三读数定死差别（2026-11-24 八轮只读勘察，探针与报告在
D:\\DeepSeek Harness WorkSpace\\.tmp-inspect\\netpaper-probe23{,b..o}.txt）：
 * **主机不同**：本文件打 `www.ctaxnews.net.cn`（中国税务报网络报·数字报），
   第六源打 `www.ctaxnews.com.cn`（中国税网）；
 * **内容重叠仅 6/29**：当日 29 枚有标题的稿，拿标题原样、前 12 字、带标点
   三式去第六源检索只定位到 6 枚。反证也做了：另取 8 枚"三式全空"的稿，用
   「即墨税务」「邗江税务」「雄安新区离境」这类专名配 11-20..11-24 的日期窗
   全文检索，**8 枚全 total=0**——数字报大面积独占。证据强度如实记：单日样，
   且第六源的 `q` 不分词使"搜不到"不等于"库里必无"；
 * **同稿异题**：第六源常是全题「本溪高新区税务分局强化医药行业税收监管——从医
   药推广费用风险核查入手」，数字报的 `<title>` 只留后半截「从医药推广费用风险
   核查入手」⇒ 聚合层按标题去重对这一源失效，行里必须带 url 供下游按地址合并。

模板也不共用，抽取器各写各的：第六源稿是 `<article>`/`article-main`，
`founder-content/newsdetatext/TRS_Editor/id="zoom"/Custom_UnText/detail_content`
实测**全不存在**；数字报稿 8589 字节里 `founder-content` 与 `newsdetatext` 都在、
没有 `<article>`。

取数通路唯一：按日期扫版面。页面自带检索表单
`<form action="https://180.76.243.174:8081/servlet/SearchServlet.do" method="POST">`
（字段 contentKey / op=single / siteID=1 / sort=date），本机直打五种打法全灭：
https 无论 verify 与否都是 `SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))`，
http 明文回 502/0 字节——不是证书配对问题，是 TLS 直接掐断。版面页 19 枚
`<script src>` 全抓过逐一读过，唯一接口线索是 SSO 登录口
（`https://sso.ctaxnews.net.cn:9080/SSOv2/`），**没有版面清单/检索 API**。
所以本层不做站内检索，只按日期扫版面再本地比文。

地址形状（注意都有日段，缺日段实测 404；GitHub 上 blueadark/tax2.0 那条硬编
`/paper/pc/layout/202604/node_03.html` 少了日段，是一枚坏线索，别照抄）：
    版面  https://www.ctaxnews.net.cn/paper/pc/layout/YYYYMM/DD/node_NN.html
    文章  https://www.ctaxnews.net.cn/paper/pc/con/YYYYMM/DD/content_NNNNNN.html
版面枚数恒定 8（node_01..node_08），但**栏目名逐日漂移**：同一枚 node_06 在
11-17 是「税案传真」、11-18 是「税务研究」、11-20 是「实践探索」⇒ 栏目只能从
「第NN版：xxx」字面现读，绝不按位置硬编。

出版日（2026-11-01..11-24 连扫 node_01：30 天窗口里 18 天回 200）：
Mon/Tue/Wed/Fri 出报，**Thu/Sat/Sun 全 404**（周四不发、周末不发）。404 在这里
是"当天没出报"的诚实读数，不是取数失败——把它说成"取不到"就是把两件事说成一件
（红线⑦）。`fetch_edition` 因此单开一档 `_status="no-issue：…"`。

正文与标题：`<founder-content>` 抽正文（43/43 非空）；标题在
`<!--enpproperty … <title>x</title>/enpproperty-->` 注释块（43/43），`<h3>` 兜底
（两种摆法在样本里同时成立、无一互斥）。**图片稿 14/43**：正文空、`<title>` 是
「一版图一」这类版面占位名、图注在 `div.pic><p>`——这类行打 `_image_piece` 标记，
content 给图注而不是空串，免得下游把"图没字"读成"这稿是空的"。
"""

import datetime
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import requests

import tax_cache
import tax_http

PAPER_BASE = "https://www.ctaxnews.net.cn"
PAPER_SITE = "ctaxnews.net.cn"
LAYOUT_URL = PAPER_BASE + "/paper/pc/layout/{yyyymm}/{dd}/node_{nn}.html"
CON_URL = PAPER_BASE + "/paper/pc/con/{yyyymm}/{dd}/content_{aid}.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": PAPER_BASE + "/paper/pc/layout/",
}

TIMEOUT_PAGE = 20
TIMEOUT_ARTICLE = 25
# 版面与整版文章各算一枚缓存项；一天六七十枚请求，缓存六小时是礼节也是自保
PAPER_CACHE_TTL = 6 * 3600
PAPER_CACHE = tax_cache.CacheManager(enabled=True, namespace="paper")

_MAX_EDITIONS = 8
# 站点没实测到限流，这是逐篇读页之间的礼节间隔
_READ_INTERVAL = 1.0

_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_EDITION_NAME_RE = re.compile(r"第\s*0?\d+\s*版[：:]\s*([^<\s|]{2,14})")
_ARTICLE_HREF_RE = re.compile(r"""href=["']([^"'#]*content_\d+\.html)["']""", re.I)
_ENP_BLOCK_RE = re.compile(r"enpproperty(.*?)/enpproperty", re.S)
_ENP_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_FOUNDER_RE = re.compile(r"<founder-content>(.*?)</founder-content>", re.S)
# 图片稿的图注：实测形状是 <div class="pic"><img …><p>图注</p></div>（一枚闭合 div，
# 后面直接跟 founder-content），所以捕获到第一个 </div> 为止——写成两枚 </div> 实测配不上。
_PIC_RE = re.compile(r'<div class="pic"[^>]*>(.*?)</div>', re.S)
_PIC_CAP_RE = re.compile(r"<p>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_PARA_RE = re.compile(r"</(?:p|div|li|tr|table|h[1-6])\s*>|<br\s*/?>", re.I)
# 图片稿的占位题名：「一版图一」「第05版：税收法治专刊 五版图二」这类
_IMAGE_TITLE_RE = re.compile(r"^第?[一二三四五六七八九十\d]*版?图[一二三四五六七八九十\d]+$")


def _norm_date(date_str) -> tuple:
    """'2026-11-24'/'20261124' -> ('202611', '24')；认不出来交回 ('', '')。"""
    s = re.sub(r"\s+", "", str(date_str or ""))
    m = _DATE_RE.match(s)
    if m:
        return "%04d%02d" % (int(m.group(1)), int(m.group(2))), "%02d" % int(m.group(3))
    if re.fullmatch(r"\d{8}", s):
        return s[:6], s[6:]
    return "", ""


def _to_text(fragment: str) -> str:
    """剥标签、留段落：块级结束标签与 <br> 先换行再剥剩下的标签。

    顺序不能颠倒：`_TAG_RE` 把 `</p>` 一起剥掉之后 `_PARA_RE` 就再也找不到东西，
    整篇正文会粘成一行。（同 tax_ctax._to_text 那条课。）
    """
    txt = _TAG_RE.sub("", _PARA_RE.sub("\n", fragment or ""))
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    lines = [ln.strip() for ln in txt.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _edition_name(node_html: str) -> str:
    """从版面页现读「第NN版：栏目名」；读不到交回空串（页面就是证据，别猜）。"""
    m = _EDITION_NAME_RE.search(node_html or "")
    return _to_text(m.group(1)) if m else ""


def _article_links(node_html: str, base_url: str) -> list:
    """版面页里的文章地址（绝对化 + 去重 + 按地址排序，保持页面顺序的可比性）。"""
    return sorted({urljoin(base_url, m.group(1)) for m in _ARTICLE_HREF_RE.finditer(node_html or "")})


def _title_of(article_html: str) -> str:
    """enpproperty 注释块里的 <title>（43/43 中）；退一步用 <h3>。"""
    html = article_html or ""
    block = _ENP_BLOCK_RE.search(html)
    if block:
        t = _ENP_TITLE_RE.search(block.group(1))
        if t:
            return _to_text(t.group(1))
    h3 = _H3_RE.search(html)
    return _to_text(h3.group(1)) if h3 else ""


def _is_image_piece(title: str) -> bool:
    return bool(_IMAGE_TITLE_RE.match((title or "").strip()))


def _body_of(article_html: str) -> str:
    """正文：<founder-content> 里的文字；enpproperty 注释块整段剔掉（它是元信息不是正文）。"""
    fc = _FOUNDER_RE.search(article_html or "")
    if not fc:
        return ""
    inner = _ENP_BLOCK_RE.sub("", fc.group(1))
    return _to_text(inner)


def _caption_of(article_html: str) -> str:
    """图片稿的图注：`div.pic` 里那张 <img> 之后的 <p>。"""
    pic = _PIC_RE.search(article_html or "")
    if not pic:
        return ""
    cap = _PIC_CAP_RE.search(pic.group(1))
    return _to_text(cap.group(1)) if cap else ""


def _row_of(article_html: str, url: str) -> dict:
    title = _title_of(article_html)
    body = _body_of(article_html)
    row = {
        "title": title,
        "url": url,
        "source": "中国税务报网络报",
        "_site": PAPER_SITE,
    }
    if not body:
        cap = _caption_of(article_html)
        if cap or _is_image_piece(title):
            row["_image_piece"] = True
            row["content"] = cap
            row["_note"] = "图片稿：正文空，content 是图注（标题是版面占位名）"
        else:
            row["content"] = ""
            row["_empty_body"] = True
            row["_note"] = "正文取空：这一页既没有 founder-content 文字也没有图注"
    else:
        row["content"] = body
    return row


def _http_get(url: str, session=None, timeout: int = TIMEOUT_PAGE):
    """取一页，重试与留痕都交 `tax_http.attempt`。

    交回 `(status, final_url, text, err)`：status=404 时 text 空、err 空（当天没
    出报是读数不是错误）；err 非空才是真取不到。正文页不带 charset，requests 会
    按 ISO-8859-1 解出乱码且不报错，所以按响应头补 utf-8（同 tax_ctax._http_get）。
    """
    s = session or requests.Session()
    if session is None:
        s.headers.update(HEADERS)

    def once():
        return s.get(url, timeout=timeout, verify=False)

    r, err, _blocked = tax_http.attempt(once, url, tax_http.new_log())
    if err:
        return None, url, "", err
    if r is None:
        return None, url, "", "没有拿到响应"
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "charset" not in ctype and (r.encoding or "").lower() in ("iso-8859-1", "latin-1"):
        r.encoding = "utf-8"
    if r.status_code == 404:
        return 404, r.url, "", ""
    if r.status_code != 200:
        return r.status_code, r.url, "", "HTTP %d" % r.status_code
    return 200, r.url, r.text or "", ""


def fetch_edition(date_str: str, edition: int = 6, *, use_cache: bool = True,
                  session=None) -> dict:
    """一天一枚版面：{date, edition, edition_name, url, articles, _status}。

    `_status` 四档，四件事不许混成一件事：
      ""                     整版取全
      "no-issue：<日期> 当天没出报（版面页 404）"   —— 404 是出版日历，不是失败
      "partial｜…"            版面取到、个别文章页失败（lost 逐枚写明是哪篇）
      "failed｜…"             版面页本身取不到（网络/非 200）
    只有全绿（`_status==""`）才落缓存：partial 落进去会把"少几篇"钉六小时。
    """
    ym, dd = _norm_date(date_str)
    iso = "%s-%s-%s" % (ym[:4], ym[4:6], dd) if ym else str(date_str)
    if not ym:
        return {"date": iso, "edition": edition, "edition_name": "", "url": "",
                "articles": [], "network": 0,
                "_status": "failed｜日期形状不认（只收 2026-11-24 / 20261124 两式）"}

    # 键必须过 _key() 做哈希：那是 sha256 前 16 位，落盘名合法。直接把
    # "edition|202611|24|6" 这种带竖线的原文当键传给 set()，在 Windows 上是
    # 非法文件名，tax_cache 的写盘会抛 OSError 并被"缓存写不进不该让查询失败"
    # 那段静默吞掉——缓存就成了无声的空转，每次调用都重打七次站。
    key = PAPER_CACHE._key("edition", ym, dd, str(edition))
    if use_cache and PAPER_CACHE.enabled:
        hit = PAPER_CACHE.get(key, max_age=PAPER_CACHE_TTL)
        if hit:
            hit["_from_cache"] = True
            # 命中这趟一次站都没打，network 必须归零；留着缓存里那次抓站的 7，
            # 等于向调用方宣称本趟做了七次抓取——汇总层的网络计数就是这么虚高的。
            hit["network"] = 0
            return hit

    lay = LAYOUT_URL.format(yyyymm=ym, dd=dd, nn="%02d" % int(edition))
    st, furl, html, err = _http_get(lay, session=session)
    net = 1
    out = {"date": iso, "edition": edition, "edition_name": "", "url": lay,
           "articles": [], "network": net}
    if st == 404:
        out["_status"] = "no-issue：%s 当天没出报（版面页 404）" % iso
        if PAPER_CACHE.enabled:
            PAPER_CACHE.set(key, out)          # 日历事实，落缓存；六小时内不再打站
        return out
    if err or st != 200:
        # 两道都判：_http_get 现在的形状里"取不到"必带 err，但只认 err 的话，
        # 一枚 status=500/err 空的形状会被读成"整版取全"、把空页面钉进缓存六小时。
        out["_status"] = "failed｜" + (err or ("版面页 HTTP %s" % st))
        return out

    name = _edition_name(html)
    out["edition_name"] = name
    links = _article_links(html, furl or lay)
    articles, lost = [], []
    for u in links:
        s2, _f2, art, e2 = _http_get(u, session=session, timeout=TIMEOUT_ARTICLE)
        net += 1
        if e2:
            lost.append("%s：%s" % (u.rsplit("/", 1)[-1], e2))
        else:
            row = _row_of(art, u)
            row["date"] = iso
            row["_edition"] = edition
            row["_edition_name"] = name
            articles.append(row)
        if _READ_INTERVAL:
            time.sleep(_READ_INTERVAL)
    out["network"] = net
    out["articles"] = articles
    if lost:
        out["_status"] = "partial｜%d 篇取到、%d 篇没取到：%s" % (
            len(articles), len(lost), "；".join(lost[:3]))
        out["_lost"] = lost
    else:
        out["_status"] = ""
        if PAPER_CACHE.enabled:
            PAPER_CACHE.set(key, out)
    return out


def _to_date(date_str):
    ym, dd = _norm_date(date_str)
    if not ym:
        return None
    return datetime.date(int(ym[:4]), int(ym[4:6]), int(dd))


def search_paper(keyword: str, days: int = 10, size: int = 6, *,
                 editions=(6,), date_from: str = "", date_to: str = "") -> dict:
    """关键词扫近 N 天的指定版面——这一源没有可用的站内检索，只能扫版面再本地比文。

    `editions` 默认 `(6,)`：**栏目名逐日漂移**，"税案传真"不固定在第 6 版；要全覆盖
    就传 `tuple(range(1, 9))`（代价是一天八次版面页 + 当天全部文章页）。
    命中判据是"关键词出现在标题或正文里"，`in` 字面匹配（站点没有分词接口可借）。
    交回 {query, results, total(恒 None), _status, _note, _days_scanned,
    _no_issue_days, _failed_days, _editions}。
    """
    kw = (keyword or "").strip()
    eds = tuple(int(x) for x in (editions or (6,)))
    if not kw:
        return {"query": "", "results": [], "total": None,
                "_status": "没走成：关键词为空（这一源没有站内检索接口，"
                           "按版面扫再比文需要关键词）",
                "_days_scanned": [], "_no_issue_days": [], "_editions": list(eds)}

    end = _to_date(date_to) or (_to_date(date_from) or datetime.date.today())
    start = _to_date(date_from) or (end - datetime.timedelta(days=max(1, int(days)) - 1))
    if start > end:
        start, end = end, start

    results, scanned, noissue, failed = [], [], [], []
    seen = set()
    d = end
    while d >= start:
        iso = d.isoformat()
        scanned.append(iso)
        for ed in eds:
            page = fetch_edition(iso, edition=ed)
            st = page.get("_status") or ""
            if st.startswith("no-issue"):
                if ed == eds[0] and iso not in noissue:
                    noissue.append(iso)
                continue
            if st.startswith("failed"):
                if ed == eds[0] and iso not in failed:
                    failed.append("%s：%s" % (iso, st[len("failed｜"):][:80]))
                continue
            for row in page["articles"]:
                if kw in row.get("title", "") or kw in row.get("content", ""):
                    if row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    results.append(row)
                    if len(results) >= size:
                        break
            if len(results) >= size:
                break
        if len(results) >= size:
            break
        d -= datetime.timedelta(days=1)

    bits = ["扫了 %d 天 × %d 个版面，命中 %d 篇" % (len(scanned), len(eds), len(results))]
    if noissue:
        bits.append("没出报 %d 天（%s）" % (len(noissue), "、".join(noissue[:5])))
    if failed:
        bits.append("取数失败 %d 天（%s）" % (len(failed), "；".join(failed[:3])))
    if len(results) >= size:
        bits.append("已到 size=%d 上限，后面可能还有" % size)
    status = "" if not failed else "partial｜" + "；".join(bits[1:])
    # total 恒 None：这一源没有可用的站内检索，拿不到"库里共多少条"，
    # 只有"扫了这些天这些版面、命中这些篇"。写成 len(results) 会让汇总层
    # 把命中数当总数（tax_ctax 的 total 是站点报的总数，两种语义不能同形）。
    return {"query": kw, "results": results, "total": None, "_status": status,
            "_note": "；".join(bits),
            "_days_scanned": scanned, "_no_issue_days": noissue,
            "_failed_days": failed, "_editions": list(eds)}


def days_with_issue(date_from: str, date_to: str, edition: int = 1) -> list:
    """日期窗内出报的日子（只打版面页，不读文章）——先问哪天有报再逐日读，省一半请求。"""
    start = _to_date(date_from)
    end = _to_date(date_to) or start
    if not start:
        return []
    if start > end:
        start, end = end, start
    out, d = [], start
    while d <= end:
        page = fetch_edition(d.isoformat(), edition=edition)
        st = page.get("_status") or ""
        if not st.startswith("no-issue") and not st.startswith("failed"):
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 人读 ----

def render(out: dict) -> str:
    """取数结果的人读形态（CLI 与适配器共用）。

    零命中也要把"扫了哪几天、哪几天没出报、哪几天取数失败"写出来：这一源没有
    站内检索，没命中只说明扫过的范围里没有，不等于报上没有。
    """
    status = out.get("_status", "")
    if "date" in out and "articles" in out:          # fetch_edition 的形状
        L = ["中国税务报网络报 %s 第%02d版：%s" % (
            out["date"], int(out.get("edition") or 0),
            out.get("edition_name") or "（栏目名没读到）")]
        if status:
            L.append(status)
        arts = out["articles"]
        hit = "打了 %s 次站" % out.get("network", 0)
        if out.get("_from_cache"):
            hit += "，这一趟是六小时内的缓存"
        L.append("取到 %d 篇（%s）：" % (len(arts), hit))
        for i, a in enumerate(arts, 1):
            tag = "［图片稿，正文是图注］" if a.get("_image_piece") else ""
            L.append("  %d. %s%s" % (i, a.get("title", ""), tag))
            L.append("     %s｜%s" % (a.get("date", ""), a.get("url", "")))
        for lost in out.get("_lost", [])[:5]:
            L.append("     没取到：「%s」" % lost)
        return "\n".join(L)

    L = ["中国税务报网络报 关键词「%s」" % out.get("query", "")]
    if status:
        L.append(status)
    rows = out.get("results", [])
    L.append("命中 %d 篇：扫了 %s 天（版面 %s）" % (
        len(rows), out.get("_days_scanned", 0),
        ",".join(str(x) for x in out.get("_editions", []))))
    if out.get("_no_issue_days"):
        L.append("这些日子没出报：%s" % "、".join(out["_no_issue_days"]))
    if out.get("_failed_days"):
        L.append("这些日子取数失败（失败与没有是两件事）：%s" % "、".join(out["_failed_days"]))
    for i, a in enumerate(rows, 1):
        L.append("  %d. %s" % (i, a.get("title", "")))
        L.append("     %s｜第%s版 %s｜%s" % (
            a.get("date", ""), a.get("_edition", ""),
            a.get("_edition_name", ""), a.get("url", "")))
    L.append("\n局限：这一源没有站内检索，靠逐日扫版面再本地比文；没命中不等于报上"
             "没有，只说明扫过的日子与版面里没有。栏目名逐日漂移，按版面号筛会漏。")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI ----

def _cli(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="中国税务报网络报（数字报）按日期扫版面取稿")
    ap.add_argument("keyword", nargs="?", default="",
                    help="关键词（不给就 --date/--edition 直接看一天那一版）")
    ap.add_argument("--date", default="", help="版面日期 2026-11-24 / 20261124")
    ap.add_argument("--edition", type=int, default=6, help="版面号 1-8（默认 6）")
    ap.add_argument("--days", type=int, default=10, help="关键词回溯天数（默认 10）")
    ap.add_argument("--from", dest="date_from", default="", help="起日（含）")
    ap.add_argument("--to", dest="date_to", default="", help="止日（含）")
    ap.add_argument("--editions", default="6",
                    help="关键词扫的版面号，逗号分隔（默认 6；全版面 1,2,3,4,5,6,7,8）")
    ap.add_argument("--size", type=int, default=6)
    ap.add_argument("--list-days", action="store_true", help="只列日期窗内出报的日子")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)

    if a.no_cache:
        PAPER_CACHE.enabled = False

    if a.list_days:
        if not a.date_from:
            print("--list-days 需要 --from（可给 --to）")
            return 2
        print(json.dumps(days_with_issue(a.date_from, a.date_to, edition=a.edition),
                         ensure_ascii=False, indent=1))
        return 0
    if a.date and not a.keyword:
        out = fetch_edition(a.date, edition=a.edition, use_cache=not a.no_cache)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if a.keyword:
        eds = tuple(int(x) for x in str(a.editions).split(",") if x.strip())
        out = search_paper(a.keyword, days=a.days, size=a.size, editions=eds,
                           date_from=a.date_from, date_to=a.date_to)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
