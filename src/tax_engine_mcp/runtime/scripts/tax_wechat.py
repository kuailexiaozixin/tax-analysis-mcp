#!/usr/bin/env python3
"""微信公众号文章检索：元宝搜索取清单，微信正文页取全文。

3.62.49 起主路换成站点自己的取数接口：`POST /searchweb/api/search/next-page`，
公众号那一档由 `sp.tab=weixin_vertical` 加请求头 `x-original-uri: /iweixin?...`
两枚一起决定（缺一回 0 张卡）。旧的 `GET /isearch?tab=weixin_article` 那条路
**SSR 根本不渲染结果**——它回一份 6 万字的骨架页，正文里只有埋点串，没有结果卡；
老实现据此报「登录墙」是误判（那句「请登录后继续查看内容」在站点自己的代码里是
限流标题 `getRateLimitTitle("429_outer")`，不是账号墙）。现在仍保留 GET 那一路作
第三级兜底，因为低频时它会直接渲染出结果（实测 30 条全带微信直链）。

三档阶梯：垂直档（全直链）→ 综合档（按 mp.weixin.qq.com 过滤）→ 旧 GET。
每档之后都做相关性过滤：整批没有一张含检索词的任一 token，就判站点那批是推词
兜底（`doc_from=q2q`），不当结果交回。响应里 `timings.search` 只有十几毫秒那一形
是服务端熔断（上游检索没被调用），报「通道短接」并冷却重发一次，不报零命中。
本源是实务解读层，权威性低于 NPC 法规与税务总局原文。

Usage:
  python tax_wechat.py "研发费用加计扣除" --size 5
  python tax_wechat.py "研发支出资本化" --size 3 --read --json
  python tax_wechat.py "留抵退税" --time-filter 1m
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_http  # noqa: E402

YUANBAO_SEARCH = "https://so.qq.com/isearch"
YUANBAO_HOME = "https://so.qq.com/"
# 站点自己的取数接口（chunk[20] `0eph8svu9ikse.js` 里 fetchNextPage 逐字读出的形状）：
# fetch(`${APP_BASE_PATH}/api/search/next-page`, {method:"POST", headers:{Content-Type:
# application/json, ...passThroughHeaders}, body:JSON.stringify({query,page,variant,sp,
# suuid,originalUri})})。APP_BASE_PATH="/searchweb"。
YUANBAO_NEXT_PAGE = "https://so.qq.com/searchweb/api/search/next-page"
YUANBAO_APPROVE = "https://so.qq.com/newanti/approve"
# 公众号档的 tab 值是别名 weixin_vertical（chunk 里 WEIXIN_ARTICLE_TAB_ALIAS），
# 不是页面 URL 上那枚 weixin_article——把后者发进 sp 会回 0 张卡（实测 112 字节）。
VERTICAL_TAB = "weixin_vertical"
IWEIXIN_PREFIX = "/iweixin"
# 站点客户端每次拿到 cards 都补一发（module 693653 @13340 逐字），本层照做但只当
# 尽力而为：实测补与不补都可能撞熔断形，它不是通道开关。
TIME_FILTERS = ("1d", "1w", "1m", "1y")
# timings.search 是上游检索的毫秒数：满档实测 744/745/749，熔断形只有 19/22/23/25。
# 低于这条就判「通道短接」（服务端根本没查），不能读成「站里没这个词」。
SEARCH_ALIVE_MS = 120
DEAD_COOLDOWN = 12.0
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
HEADERS_YUANBAO = {
    "User-Agent": UA_DESKTOP,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": YUANBAO_HOME,
}
HEADERS_WX = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://mp.weixin.qq.com/",
}
TIMEOUT = 25
MAX_BODY_BYTES = 4 * 1024 * 1024
READ_INTERVAL = 2.0
MP_HOST = "mp.weixin.qq.com"

TITLE_BLOCK = re.compile(
    r'<a\s+([^>]*?linkid&quot;:&quot;title&quot;[^>]*?)>(.*?)</a>', re.S)
SOURCE_BLOCK = re.compile(
    r'<a\s+([^>]*?linkid&quot;:&quot;source&quot;[^>]*?)>(.*?)</a>', re.S)
HREF_RX = re.compile(r'href=["\']([^"\']+)["\']')
TAG_RX = re.compile(r"<[^>]+>")
SPAN_RX = re.compile(r'<span[^>]*>([^<]+)</span>')
SUMMARY_RX = re.compile(r'class="[^"]*sa-text[^"]*"[^>]*>(.*?)</div>', re.S)
DATE_RX = re.compile(
    r"(刚刚|昨天|前天|\d+(?:\.\d+)?\s*(?:分钟|小时|天|周|个月|月|年)前|"
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)")
ABSOLUTE_DATE_RX = re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$")
# 站点的关键词高亮边界符：JSON 卡片的 title/summary 里直接插 U+E40A/U+E40B
# （2026-11-27 真响应 25 张里 23 张带）。GET 那一路是 HTML，标签被 TAG_RX 吃掉；
# JSON 这一路不剥就把私有区脏字符交付给下游。判据与交付字段都先过 _strip_hl。
HL_RX = re.compile("[\ue400-\ue4ff]")
BLOCK_MARKERS = ("环境异常", "验证码", "请完成验证", "系统检测到")
BLOCK_ANCHORS = ("/antispider/", "antispider.min.js", "anti.min.css")
LOGIN_WALL_ANCHORS = ("linkid&quot;:&quot;login_page", "请登录后继续查看内容")
# 站点自己的空结果页正文锚（chunk module 868170 的 <h2> 逐字）。有它在，
# 「0 条」就是真零命中，不是通道坏了。
SITE_EMPTY_MARK = "抱歉，没有找到相关网页"
ZERO_RESULT_MARKS = ("js-results", "result-card")

_LOCK_PATH = Path(tempfile.gettempdir()) / "tax-engine-yuanbao.lock"
_LOCK_TIMEOUT = float(os.getenv("TAX_YUANBAO_LOCK_TIMEOUT", "180"))
yuanbao_gate = tax_http.SerialGate(path=_LOCK_PATH, timeout=_LOCK_TIMEOUT)
yuanbao_gate.site = "元宝搜索"
YUANBAO_MIN_INTERVAL = 1.5
_last_yuanbao_at = 0.0
_PACE_PATH = Path(tempfile.gettempdir()) / "tax-engine-yuanbao-pace.json"
# 元宝这条链上站点自己产出的跳转码集合（与 tax_sogou 的 _REDIRECT_CODES 同源），
# 正文那一头要靠它判断「被 302 走了」这件事（§5.1 的重定向边界）。
REDIRECT_CODES = (301, 302, 303, 307, 308)
# 同主机跳转的上限：微信的 http→https 升级只有一跳，再多的就是环。
MAX_REDIRECT_HOPS = 2


def _read_pace_stamp() -> float:
    """盘上那枚最近发请求的时刻。读不到就当作 0——不能让记账文件把通道打死。"""
    try:
        payload = json.loads(_PACE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    value = payload.get("last_request_at") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _write_pace_stamp(at: float) -> None:
    try:
        _PACE_PATH.write_text(json.dumps({"last_request_at": at}), encoding="utf-8")
    except OSError:
        pass


def _pace_reserve() -> None:
    """在**闸内**为这一发留出最小间隔。

    2026-11-27 修报告 §5.9 的两处：①旧写法把 sleep 放在进闸之前，两个进程各自
    算各自的 gap，实测 configured=1.0 秒 / observed=0.0019 秒就一起打了出去；②
    时间戳是进程内变量，另一个进程根本看不见。现在间隔在闸里等、时间戳上盘，
    拿锁的一方一定看得见对方刚写下的时刻。写法照 `tax_sogou._reserve_slot`。
    """
    global _last_yuanbao_at
    now = time.time()
    last = max(_last_yuanbao_at, _read_pace_stamp())
    wait = YUANBAO_MIN_INTERVAL - (now - last)
    if wait > 0:
        time.sleep(min(wait, YUANBAO_MIN_INTERVAL))
    _last_yuanbao_at = time.time()
    _write_pace_stamp(_last_yuanbao_at)


def _yuanbao_get(sess: "requests.Session", url: str, **kwargs) -> "requests.Response":
    """走闸的 GET。串行闸等不到时抛的 `TimeoutError` 由调用层收成失败信封
    （见 `_GATE_TIMEOUT_NOTE`），不许穿透公共函数。"""
    with yuanbao_gate:
        _pace_reserve()
        return sess.get(url, timeout=TIMEOUT, verify=False, **kwargs)


def _yuanbao_post(sess: "requests.Session", url: str, **kwargs) -> "requests.Response":
    """取数接口那一路的发。与 `_yuanbao_get` 同一条闸与同一个最小间隔——两档共用
    同一个频次预算，分开记账就等于绕过自己定的冷却。"""
    with yuanbao_gate:
        _pace_reserve()
        return sess.post(url, timeout=TIMEOUT, verify=False, **kwargs)


_GATE_TIMEOUT_NOTE = "串行闸等待超时（另有进程正在打元宝），这一轮没有发请求"


def _read_capped(response: "requests.Response") -> tuple[bytes, bool]:
    chunks = []
    total = 0
    truncated = False
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        if total >= MAX_BODY_BYTES:
            truncated = True
            break
        room = MAX_BODY_BYTES - total
        if len(chunk) > room:
            # 旧写法只在这一枚块之前看一眼总额，末块跨过上限时交回去的字节数正好
            # 等于上限而 truncated 仍是 False——调用层据此以为拿到了完整页面
            # （2026-11-27 修报告 §5.11，实测 chunk 4,194,294+20）。
            truncated = True
        chunks.append(chunk[:room])
        total += len(chunk[:room])
        if truncated:
            break
    return b"".join(chunks), truncated


def _decode(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    for encoding in ([match.group(1)] if match else []) + ["utf-8", "gbk"]:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RX.sub("", fragment or ""))).strip()


def _strip_hl(text: str) -> str:
    """剥站点的高亮边界符（见 HL_RX 注释）。剥完再比较、再交付。"""
    return HL_RX.sub("", text or "")


def _safe_mp_link(href: str) -> str:
    if not href:
        return ""
    try:
        parts = urlsplit(href.strip())
        if (parts.scheme not in ("http", "https") or parts.hostname != MP_HOST
                or parts.port is not None or parts.username is not None
                or parts.password is not None):
            return ""
    except ValueError:
        return ""
    return href.strip()


def _account(inner: str) -> str:
    for text in SPAN_RX.findall(inner):
        value = text.strip()
        if value and not value.startswith("http") and not DATE_RX.search(value):
            return value
    return ""


def _date_text(inner: str) -> str:
    for text in SPAN_RX.findall(inner):
        value = text.strip()
        if DATE_RX.search(value):
            return value
    return ""


def _date_epoch(value: str, now: float) -> int | None:
    if not value:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(分钟|小时|天|周|个月|月|年)前$", value)
    if match:
        scale = {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800,
                 "个月": 2592000, "月": 2592000, "年": 31536000}
        return int(now - float(match.group(1)) * scale[match.group(2)])
    if value == "刚刚":
        return int(now)
    if value == "昨天":
        return int(now - 86400)
    if value == "前天":
        return int(now - 2 * 86400)
    if ABSOLUTE_DATE_RX.match(value):
        normalized = value.replace("年", "-").replace("月", "-").replace("日", "")
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%d")
            return int(time.mktime(parsed.timetuple()))
        except ValueError:
            return None
    return None


def _parse_results(page_html: str) -> list[dict]:
    titles = list(TITLE_BLOCK.finditer(page_html))
    items = []
    now = time.time()
    for index, title_match in enumerate(titles):
        source_region = page_html[(titles[index - 1].end() if index else 0):title_match.start()]
        summary_region = page_html[title_match.end():(
            titles[index + 1].start() if index + 1 < len(titles) else len(page_html))]
        href_match = HREF_RX.search(title_match.group(1))
        raw_link = unescape(href_match.group(1)) if href_match else ""
        title = _clean(title_match.group(2))
        if not title:
            continue
        source_match = SOURCE_BLOCK.search(source_region)
        source_inner = source_match.group(2) if source_match else ""
        date_text = _date_text(source_inner)
        epoch = _date_epoch(date_text, now)
        summary_match = SUMMARY_RX.search(summary_region)
        link = _safe_mp_link(raw_link)
        item = {
            "title": title,
            "account": _account(source_inner),
            "summary": _clean(summary_match.group(1)) if summary_match else "",
            "date": time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch else "",
            "date_text": date_text,
            "date_approx": bool(date_text and not ABSOLUTE_DATE_RX.match(date_text)),
            "timestamp": epoch,
            "url": link,
        }
        if not link:
            item["_error"] = "元宝结果链接不是 mp.weixin.qq.com 文章直链"
        items.append(item)
    return items


def _blocked_reason(page_html: str) -> str:
    hits = [marker for marker in BLOCK_MARKERS if marker in page_html]
    if any(anchor in page_html.lower() for anchor in BLOCK_ANCHORS):
        hits.append("antispider 结构锚")
    return "、".join(hits)


def _query_tokens(keyword: str) -> tuple[str, ...]:
    """检索词切 token（按空白），去重保序。相关性过滤只在 token 上判，不在整串上判——
    实测站点按子串给结果，整串「财政部 税务总局公告2026年第29号」逐字命中会被写成
    空批。"""
    return tuple(dict.fromkeys(tok for tok in keyword.split() if tok))


def _relevant_only(items: list[dict], keyword: str) -> tuple[list[dict], bool]:
    """返回（保留的条目, 是否整批都是推词兜底）。

    站点在检索词没有直接命中时照样回一整批卡（实测不存在的词「zzz不存在税种名称
    zzz」回 26 张，整批 `doc_from=q2q`、0/26 含词）。注意 **doc_from 不能当判据**：
    真词的 25 张同样是全批 q2q（2026-11-27 现读），它只是这一档的固定来源标记。
    可用的判据是 token 命中：条目 title+summary（先剥高亮边界符）含检索词的任一
    token 就算真结果——真词批命中 19/25，兜底批 0/26，分得开。整批 0 命中不当
    结果交回，报「站内没这个词」而不是「有 26 条」。
    """
    tokens = _query_tokens(keyword)
    if not tokens or not items:
        return items, False
    lowered = [tok.lower() for tok in tokens]
    kept = []
    for item in items:
        hay = _strip_hl(str(item.get("title", "")) + "\n"
                        + str(item.get("summary", ""))).lower()
        if any(needle in hay for needle in lowered):
            kept.append(item)
    return kept, (len(kept) == 0)


def _parse_vertical_cards(payload: dict) -> tuple[list[dict], int]:
    """垂直档（POST next-page）的卡片解析，返回（条目, 被直链白名单挡掉的卡数）。

    形状（yb59shape.py 对真响应逐层核对）：cards[].data.vm =
    {title, summary, authorName, publishTime, jumpUrl, host, docId, imgUrl,
    isIweixin, isWxmpTab, cardInfo}；精确时间戳在 cards[].data.item.jsonData.pub_time
    （epoch 秒，整数）。jumpUrl 是 mp.weixin.qq.com 文章直链（http）。

    链接不在白名单的卡**不进清单**（2026-11-27 修报告 §5.2）。旧写法把无链的卡也
    带 title 交下去，这类卡通常不含检索词的分词，于是先被相关性过滤吃掉，整轮就读
    成「站内没这个词」的零命中——把「站点给了货但链形不对」这件通道故障伪装成了没
    货，故障再也不会被任何人看见。计数交给上层报失败。
    """
    items = []
    bad_link = 0
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        data = card.get("data") or {}
        vm = data.get("vm") or {}
        title = _strip_hl(str(vm.get("title") or "")).strip()
        # 只认 jumpUrl：`host` 是频道首页（http://mp.weixin.qq.com），能过白名单却
        # 点不进任何一篇文章。2026-11-27 离线自测暴露：卡片缺 jumpUrl 时旧写法把首页
        # 当文章直链交付出去，下游 read_article 取回来的不是正文。
        link = _safe_mp_link(str(vm.get("jumpUrl") or ""))
        if not link:
            if title:
                bad_link += 1
            continue
        item = {
            "title": title,
            "account": _strip_hl(str(vm.get("authorName") or "")).strip(),
            "summary": _strip_hl(str(vm.get("summary") or "")).strip(),
            "date": str(vm.get("publishTime") or "").strip(),
            "date_text": str(vm.get("publishTime") or "").strip(),
            "date_approx": False,
            "timestamp": None,
            "url": link,
        }
        inner = data.get("item") or {}
        json_data = inner.get("jsonData") if isinstance(inner, dict) else None
        if isinstance(json_data, dict):
            pub = json_data.get("pub_time")
            if isinstance(pub, int) and not isinstance(pub, bool):
                item["timestamp"] = pub
                if not item["date"]:
                    item["date"] = time.strftime("%Y-%m-%d", time.localtime(pub))
        items.append(item)
    return items, bad_link


def _approve_handshake(sess: "requests.Session", payload: dict) -> None:
    """站点客户端每次拿到 cards 都补一发（chunk module 693653 @13340 逐字）。
    本层照做但只当尽力而为：实测补与不补都可能撞熔断形，它不是通道开关。"""
    uuid = payload.get("uuid")
    if not uuid:
        return
    params = {"uuid": uuid}
    token = payload.get("antispiderToken")
    if token:
        params["token"] = token
    # 旧写法裸 `sess.get` 绕开串行闸、也不关响应（离线复现 direct_get_calls=1 /
    # response_closed=false）。它也是一发真请求，占了同一份频次预算就必须走闸。
    try:
        response = _yuanbao_get(sess, YUANBAO_APPROVE, params=params)
    except (requests.RequestException, TimeoutError):
        return
    try:
        response.close()
    except (AttributeError, requests.RequestException):
        pass


def _alive_ms(payload: dict) -> int:
    """上游检索那一发的毫秒数。站点偶尔会把数值给成字符串（`"19"`），旧写法直接
    比较就抛 TypeError，整轮检索崩在判据里（2026-11-27 修报告 §6.4）。取不到数值
    就当 0——0 意味着「没有证据表明上游跑了」，落到短接那一支是安全的一侧。
    """
    timings = payload.get("timings")
    value = timings.get("search") if isinstance(timings, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except (TypeError, ValueError):
            return 0
    return int(value)


def _vertical_search(sess: "requests.Session", keyword: str,
                     time_filter: str = "") -> tuple[dict | None, str]:
    """主路：POST /searchweb/api/search/next-page 的公众号档。

    返回（payload, 落地说明）；payload=None 表示这一发没拿到 JSON（网络/HTTP 层
    或熔断形），落地说明给出原因。body 只有 query/page/sp 三枚——实测多给
    passThroughHeaders/suuid/variant 任何一枚都可能整体清空结果（112 字节 0 卡）。
    `sp.tab=weixin_vertical` 与请求头 `x-original-uri: /iweixin?...` 两枚缺一不可。
    """
    sp = {"tab": VERTICAL_TAB}
    if time_filter and time_filter != "all":
        sp["timeFilter"] = time_filter
    body = {"query": keyword, "page": 1, "sp": sp}
    headers = {
        "Content-Type": "application/json",
        "x-original-uri": f"{IWEIXIN_PREFIX}?tab=weixin_article&query={quote(keyword, safe='')}",
    }
    try:
        response = _yuanbao_post(sess, YUANBAO_NEXT_PAGE,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers)
    except TimeoutError:
        return None, _GATE_TIMEOUT_NOTE
    except requests.RequestException as exc:
        return None, f"元宝取数请求失败：{exc}"
    try:
        if response.status_code != 200:
            return None, f"元宝取数返回 HTTP {response.status_code}"
        raw, truncated = _read_capped(response)
    except requests.RequestException as exc:
        return None, f"元宝取数响应读取失败：{exc}"
    finally:
        try:
            response.close()
        except (AttributeError, requests.RequestException):
            pass
    if truncated:
        # 旧写法把这枚旗标扔进 `_truncated` 不用，于是「被截断的 JSON」要么解析
        # 失败报读取错误、要么（少见的截在边界上）当成一份完整清单用（§6.5）。
        return None, f"元宝取数响应超过 {MAX_BODY_BYTES} 字节上限并被截断"
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        return None, f"元宝取数响应读取失败：{exc}"
    if not isinstance(payload, dict):
        return None, "元宝取数返回的不是 JSON 对象"
    alive = _alive_ms(payload)
    if not payload.get("cards") and alive < SEARCH_ALIVE_MS:
        # 熔断形：上游检索根本没被调用（实测 search=19~25 vs 满档 744+），
        # 不能读成「站里没这个词」。
        return None, f"元宝通道短接（timings.search={alive}ms，上游检索未执行）"
    _approve_handshake(sess, payload)
    return payload, ""


def _plain_search(sess: "requests.Session", keyword: str,
                  time_filter: str = "") -> tuple[dict | None, str]:
    """第二档：同一个 POST 接口的综合档（sp 不带 tab）。返回的卡混着全网站点，
    只有 `vm.host` 指向 mp.weixin.qq.com 的才收。

    `time_filter` 必须一起带下来（2026-11-27 修报告 §5.6）。旧写法只给垂直档，
    落到这一档就把读者的时间限定悄悄丢了——拿到的是一堆旧文，还以为筛过了。
    这一枚参数在综合档同样生效（probe58tf 真发两发对照：带 `timeFilter:"1m"`
    ⇒ 112,835B/17 卡，15 枚日期全落 2026-10-28…11-27；不带 ⇒ 212,964B/17 卡，
    里面出现 2019-05-14、2022-07-27 的旧文）。
    """
    sp = {}
    if time_filter and time_filter != "all":
        sp["timeFilter"] = time_filter
    body = {"query": keyword, "page": 1, "sp": sp}
    headers = {"Content-Type": "application/json"}
    try:
        response = _yuanbao_post(sess, YUANBAO_NEXT_PAGE,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers)
    except TimeoutError:
        return None, _GATE_TIMEOUT_NOTE
    except requests.RequestException as exc:
        return None, f"元宝综合档请求失败：{exc}"
    try:
        if response.status_code != 200:
            return None, f"元宝综合档返回 HTTP {response.status_code}"
        raw, truncated = _read_capped(response)
    except requests.RequestException as exc:
        return None, f"元宝综合档响应读取失败：{exc}"
    finally:
        try:
            response.close()
        except (AttributeError, requests.RequestException):
            pass
    if truncated:
        return None, f"元宝综合档响应超过 {MAX_BODY_BYTES} 字节上限并被截断"
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        return None, f"元宝综合档响应读取失败：{exc}"
    if not isinstance(payload, dict):
        return None, "元宝综合档返回的不是 JSON 对象"
    alive = _alive_ms(payload)
    if not payload.get("cards") and alive < SEARCH_ALIVE_MS:
        return None, f"元宝综合档通道短接（timings.search={alive}ms）"
    _approve_handshake(sess, payload)
    return payload, ""


def _parse_mp_cards(payload: dict) -> list[dict]:
    """综合档卡片：vm 形与垂直档不同，逐张按 mp 直链过滤。

    2026-11-27 现读：带微信直链的那形是 `card-common/card30010608`，键为
    `{title, content, author, publishTime("2026-09-16 17:31:10"), h5Url, host,
    showname("微信公众平台"), sourceType("wechat"), cover, docId, classId, favicon}`；
    其余 `card-pc/card_normal_result` 是外部站点（host 不是 mp，被 _safe_mp_link 挡掉），
    它那形才有 `date`/`url`/`sourcename`。所以取值顺序是 publishTime 在前、author 在前。
    """
    items = []
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        vm = (card.get("data") or {}).get("vm") or {}
        title = _strip_hl(str(vm.get("title") or "")).strip()
        link = _safe_mp_link(str(vm.get("h5Url") or vm.get("url") or vm.get("host") or ""))
        if not link:
            continue
        day = str(vm.get("publishTime") or vm.get("date") or vm.get("showdate") or "").strip()
        item = {
            "title": title,
            "account": _strip_hl(str(vm.get("author") or vm.get("showname")
                                  or vm.get("sourcename") or "")).strip(),
            "summary": _strip_hl(str(vm.get("content") or vm.get("summary") or "")).strip(),
            "date": day[:10],
            "date_text": day,
            "date_approx": False,
            "timestamp": None,
            "url": link,
        }
        if title:
            items.append(item)
    return items


TIME_FILTER_DAYS = {"1d": 1, "1w": 7, "1m": 30, "1y": 365}


def _item_day(item: dict):
    """条目的发布日：优先 `item.jsonData.pub_time`（epoch 秒，精确），回落日期串前 10 位。"""
    ts = item.get("timestamp")
    if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    day = str(item.get("date") or "")[:10]
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        return ""
    return day


def _apply_time_filter(items: list[dict], time_filter: str, tier: str,
                       site_ignored: bool = False) -> tuple[list[dict], str]:
    """按发布日本地筛时间档，返回（留下的条目, 给读者看的那一句）。

    为什么要有这一枚（2026-11-27 定性，yb76tf/yb78tf3 真网）：**垂直档不执行
    `sp.timeFilter`**。同一关键词带 `timeFilter:"1m"` 与不带，返回的是同一批卡、
    同一顺序（「增值税 即征即退」25 张逐枚相同、复跑两轮一致；「留抵退税 制造业」
    28 张同样逐枚相同）。参数照发、站点照忽略，于是 `--time-filter` 在读者那边
    等于没生效——报告 §5.6 的验收句是「结果满足筛选，或明确返回筛选未生效」，
    旧实现两样都没做到：既没筛，也没说。

    这里选择兑现语义而不是只加一句说明：站点这一档既然不筛，就由我们按发布日期筛。
    日期读不出来的条目**不计入**（宁可少给，不拿一条不知道日期的冒充近一月），
    但要把这个数告诉读者。一张都没滤掉时**不记账**——「本地筛了近 30 天：4 张里留
    4 张」这种话只在真滤掉东西时才有信息量，否则是给读者的噪声。
    """
    days = TIME_FILTER_DAYS.get(time_filter)
    if not days or not items:
        return items, ""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    kept, undated = [], 0
    for item in items:
        day = _item_day(item)
        if not day:
            undated += 1
            continue
        if day >= cutoff:
            kept.append(item)
    if len(kept) == len(items):
        return kept, ""
    if site_ignored:
        note = ("%s不执行站点时间筛（实测带与不带 `timeFilter` 回同一批卡），"
                "已按发布日期本地筛近 %d 天：%d 张里留 %d 张"
                % (tier, days, len(items), len(kept)))
    else:
        note = ("%s已按发布日期本地复核时间窗（近 %d 天）：%d 张里留 %d 张"
                % (tier, days, len(items), len(kept)))
    if undated:
        note += "，另有 %d 张读不出发布日期未计入" % undated
    return kept, note


def _finalize(keyword: str, results: list[dict], size: int, read_body: bool,
              caveat: str = "") -> dict:
    """共用的收尾：先按直链有效性挑出能交付的那批、再截 size、逐篇读正文、拼返回
    （含 _partial_error）。

    截断必须在过滤之后（2026-11-27 修报告 §5.7）：旧写法先 `results[:size]` 再数
    无效链，size=1 而第一条恰好无链、第二条有链时，整轮被读成「一条有效直链都没有」，
    把本来能交付的那条挡掉了。
    """
    if not results:
        return _empty(keyword, caveat or "三档阶梯都没有给出可用的结果")
    usable = [item for item in results if item["url"]]
    dropped = [item for item in results if not item["url"]]
    if not usable:
        return _empty(keyword, f"元宝给出 {len(results)} 条结果，但没有一条有效的微信文章直链")
    results = usable[:size]
    lost_link = len(dropped)

    if read_body:
        for index, item in enumerate(results):
            content, error = read_article(item["url"])
            item["content"] = content
            if error:
                item["_error"] = error
            if index < len(results) - 1:
                time.sleep(READ_INTERVAL)

    lost_body = sum(1 for item in results
                    if read_body and not item.get("content"))
    for item in results:
        item["source"] = "微信公众号"
        item["source_label"] = "实务解读"

    out = {
        "keyword": keyword,
        "total": len(results),
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "微信公众号 (元宝搜索)",
        "_from_cache": False,
    }
    parts = []
    if lost_link:
        parts.append("%d 条没有有效微信直链、已剔除" % lost_link)
    if lost_body:
        parts.append(f"{lost_body}/{len(results)} 条没取到正文")
    if parts:
        out["_partial_error"] = ("元宝给出 %d 条，交付 %d 条，但%s（逐枚原因见各条 `_error`）："
                                 "这些条目不能当成完整的一轮用"
                                 % (lost_link + len(results), len(results), "、".join(parts)))
    if caveat:
        out["_caveat"] = caveat
    return out


def search_wechat(keyword: str, size: int = 5, read_body: bool = False,
                  time_filter: str = "") -> dict:
    """检索公众号文章；read_body=True 时再逐篇读取微信正文。

    三档阶梯（3.62.49 起）：
    1. 公众号垂直档：`POST /searchweb/api/search/next-page`，body 的
       `sp.tab=weixin_vertical` 加请求头 `x-original-uri: /iweixin?...` 两枚一起决定
       档位（缺一回 0 张卡）；卡片全带 mp.weixin.qq.com 直链。
    2. 综合档：同一个 POST、`sp` 不带 tab，按 mp.weixin.qq.com 过滤。
    3. 旧 `GET /isearch?tab=weixin_article` 兜底——低频时它会直接渲染出结果，
       高频时只回一份不含结果的 SSR 骨架页，不可依赖。

    页型判据（来自 2026-11-27 实测与站点自己的 chunk 代码）：
    - 熔断形：响应 JSON 的 `timings.search` 只有十几毫秒＝上游检索根本没被调用
      （满档实测 744/745/749，熔断 19/22/23/25）。报「通道短接」并冷却
      `DEAD_COOLDOWN` 秒重发一次，**不报零命中**——那是把熔断读成没货。
    - 推词兜底：整批卡片的 title+summary 不含检索词的任一 token（站点侧
      `doc_from=q2q`）。当零命中如实交回，不冒充结果。
    - 「请登录后继续查看内容」是站点自己的限流标题（chunk
      `getRateLimitTitle("429_outer")`），报限流、不报账号墙。
    - 验证/拦截：落地含 `/antispider/`、`/captcha/`，或 `antip=web_hd`／
      `antip=wap_hd`／**`antip=new_hd`**（本站实测产出的是 new_hd）。
    """
    if not keyword.strip():
        return _empty(keyword, "检索词不能为空")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        # 2026-11-27 修报告 §6.4：旧写法 `if size < 1` 在 size="10"/size=1.5 上抛
        # TypeError，size=True 还被当成 1 放行——参数写错要报错回来，不该是异常。
        return _empty(keyword, "size 必须是正整数：收到 %r" % (size,))
    if time_filter and time_filter != "all" and time_filter not in TIME_FILTERS:
        return _empty(keyword, "time_filter 只认 %s（all 等于不筛）：收到 %r"
                      % ("、".join(TIME_FILTERS), time_filter))

    # 2026-11-27 修报告 §6.2：三档阶梯整段进 try，退出必关——旧写法建完 Session
    # 再没有 close 过，一轮检索（最多五次 HTTP）的连接就这么留在进程里。
    sess = requests.Session()
    try:
        sess.headers.update(HEADERS_YUANBAO)
        notes = []
        site_empty_note = ""
        # 「有相关条目、只是都不在所选时间窗内」是零命中的另一种正当成因，与
        # 「站内没这个词」不同义，所以各记各的，收尾时一起写进 _caveat。
        window_notes = []

        # ---- 第一档：公众号垂直档 ----
        try:
            payload, why = _vertical_search(sess, keyword, time_filter)
            if payload is None and "短接" in why:
                time.sleep(DEAD_COOLDOWN)
                payload, why = _vertical_search(sess, keyword, time_filter)
        except TimeoutError:
            # 串行闸等不到（TAX_YUANBAO_LOCK_TIMEOUT 到点）：收成失败信封，不许穿透
            # 公共函数（2026-11-27 修报告 §5.10，离线复现 TimeoutError: lock timeout）。
            payload, why = None, _GATE_TIMEOUT_NOTE
        if payload is not None:
            parsed, bad_link = _parse_vertical_cards(payload)
            kept, _ = _relevant_only(parsed, keyword)
            in_window = kept
            if kept:
                kept, tf_note = _apply_time_filter(kept, time_filter, "垂直档",
                                                   site_ignored=True)
                if tf_note:
                    notes.append(tf_note)
            if kept:
                dropped = len(parsed) - len(in_window)
                if dropped:
                    notes.append("垂直档 %d 张里 %d 张与检索词无关，已按 token 相关性滤掉"
                                 % (len(parsed), dropped))
                notes.extend(_tier_notes(payload))
                if bad_link:
                    notes.append("另有 %d 张卡被微信直链白名单挡掉" % bad_link)
                return _finalize(keyword, kept, size, read_body, "；".join(notes))
            if in_window:
                # 相关性过了、时间窗把它清空了：站点这一档是有货的，只是不在窗内。
                # 这一笔不能落进下面「整批推词兜底」那支，否则通道被读成「站内没这个词」。
                window_notes.append("垂直档 %d 张相关条目都不在所选时间窗内" % len(in_window))
            elif bad_link:
                # 2026-11-27 修报告 §5.2：站点给了货，但链接全不在白名单里——这是通道
                # 故障。旧写法把无链的卡继续交给相关性过滤，整批 0 命中就落进零命中
                # 信封，「链接形不对」这件故障被伪装成「站内没这个词」，永远看不见。
                return _empty(keyword, "元宝垂直档给出 %d 张卡，但其中 %d 张的链接不是 "
                                       "mp.weixin.qq.com 文章直链"
                                       % (len(parsed) + bad_link, bad_link))
            elif parsed:
                # 满批推词兜底＝站点自己的「没找到」表达方式（实测不存在的词照样回
                # 26 张 q2q 卡）。记下这一笔，仍往下两档试——综合档偶尔有真结果。
                site_empty_note = ("垂直档 %d 张卡没有一张含检索词的分词（整批是站点的"
                                   "推词兜底）" % len(parsed))
                notes.append(site_empty_note)
            else:
                # 整批 0 张卡**不算**站内没货（2026-11-27 修报告 §5.4）：0 张可能是
                # 熔断、也可能是空响应，旧写法拿「垂直档回 0 张卡」当空证据，后面两档全
                # 失败时就把通道故障交付成一份干净的零命中。记一笔进说明，但不作空证据。
                notes.append("垂直档回 0 张卡（不作站内没货的证据）")
        else:
            notes.append(why)

        # ---- 第二档：综合档 ----
        try:
            payload2, why2 = _plain_search(sess, keyword, time_filter)
        except TimeoutError:
            payload2, why2 = None, _GATE_TIMEOUT_NOTE
        if payload2 is not None:
            parsed2 = _parse_mp_cards(payload2)
            kept2, _ = _relevant_only(parsed2, keyword)
            in_window2 = kept2
            if kept2:
                # 综合档站点自己会筛（probe58tf 那组样本），但 yb77tf2 那轮两发都薄、
                # 没能复现——不能当保证。再过一遍本地筛是幂等的：真滤掉才记账。
                kept2, tf_note2 = _apply_time_filter(kept2, time_filter, "综合档")
                if tf_note2:
                    notes.append(tf_note2)
            if kept2:
                dropped2 = len(parsed2) - len(kept2)
                # 上一档为什么不算（整批推词兜底／0 张卡）已经在 notes 里记过一笔，
                # 这里 join 出来就跟着这批一起交出去：读者得知道这一批是从综合档来的。
                notes.append("改用综合档取到 %d 条微信直链" % len(parsed2))
                if dropped2:
                    notes.append("其中 %d 条与检索词无关已滤掉" % dropped2)
                return _finalize(keyword, kept2, size, read_body, "；".join(notes))
            if in_window2:
                window_notes.append("综合档 %d 张相关条目都不在所选时间窗内"
                                    % len(in_window2))
            if parsed2:
                notes.append("综合档 %d 张卡里没有一条带微信直链的相关结果" % len(parsed2))
        else:
            notes.append(why2)

        # ---- 第三档：旧 GET 兜底 ----
        try:
            results3, note3, kind3 = _get_fallback(sess, keyword)
        except TimeoutError:
            results3, note3, kind3 = [], _GATE_TIMEOUT_NOTE, ""
        if results3:
            in_window3 = len(results3)
            results3, tf_note3 = _apply_time_filter(results3, time_filter, "GET 兜底")
            if tf_note3:
                notes.append(tf_note3)
            if results3:
                notes.append(note3)
                return _finalize(keyword, results3, size, read_body, "；".join(notes))
            window_notes.append("GET 兜底 %d 条相关条目都不在所选时间窗内" % in_window3)
        if kind3 == "site-empty":
            # 上一档的成因不能覆盖掉：报零命中时要把「为什么三档都空」一次说清。
            site_empty_note = (site_empty_note + "；" + note3) if site_empty_note else note3
        if note3:
            notes.append(note3)

        if window_notes and not site_empty_note:
            # 窗口把相关条目清空了，而没有任何一档给出「站内没这个词」的正面证据：
            # 报零命中（站点有货、只是不在窗内），并把成因写进 _caveat。这既不是
            # 通道故障，也不是站内没货——报告 §5.6 要的是「结果满足筛选或明确说明」。
            return _zero_with_note(keyword, "；".join(window_notes))
        if window_notes:
            # 两种正当成因都有：窗内空 + 站内没词。窗是读者自己限定的，写在前面。
            site_empty_note = "；".join(window_notes) + "；" + site_empty_note
        if site_empty_note:
            # 至少有一档给出了「站内确实没这个词」的正面证据（空结果页锚／整批推词
            # 兜底／有容器无卡），那就报零命中，不报通道坏了。
            return _zero_with_note(keyword, site_empty_note)
        return _empty(keyword, "元宝三档阶梯都没取到结果：" + "；".join(n for n in notes if n))
    finally:
        sess.close()


def _zero_with_note(keyword: str, note: str) -> dict:
    """真零命中：`_zero_hit=True`、`_error` 留空（与「通道坏了」严格互斥），
    但把判据写进 `_caveat`，读者知道这一轮为什么是空。"""
    out = _empty(keyword)
    out["_caveat"] = note
    return out


def _tier_notes(payload: dict) -> list:
    """垂直档返回里的附加说明：只有一页这件事值得告诉读者（hasMore=False 实测）。"""
    out = []
    if payload.get("hasMore") is False:
        out.append("公众号垂直档只出一页（站点 hasMore=False，翻页那一发实测回空）")
    return out


def _get_fallback(sess: "requests.Session", keyword: str) -> tuple[list, str, str]:
    """第三档：旧的 GET 结果页。返回（条目, 说明, 类别），类别取
    `""`（通道问题）／`"site-empty"`（站内确实没结果——只认站点自己的空结果文案
    或整批推词兜底这两种正面证据）／`"ok"`（有结果）。"""
    url = f"{YUANBAO_SEARCH}?tab=weixin_article&query={quote(keyword, safe='')}"
    try:
        response = _yuanbao_get(sess, url, stream=True)
    except TimeoutError:
        return [], _GATE_TIMEOUT_NOTE, ""
    except requests.RequestException as exc:
        return [], f"GET 兜底请求失败：{exc}", ""
    try:
        if response.status_code != 200:
            return [], f"GET 兜底返回 HTTP {response.status_code}", ""
        landed = response.url or ""
        if ("/antispider/" in landed or "/captcha/" in landed
                or "antip=web_hd" in landed or "antip=wap_hd" in landed
                or "antip=new_hd" in landed):
            return [], "GET 兜底跳转到反爬或验证页", ""
        try:
            raw, truncated = _read_capped(response)
        finally:
            response.close()
    except requests.RequestException as exc:
        return [], f"GET 兜底响应读取失败：{exc}", ""
    if truncated:
        return [], f"GET 兜底响应超过 {MAX_BODY_BYTES} 字节上限并被截断", ""
    if not raw:
        return [], "GET 兜底返回 HTTP 200 但正文 0 字节：这一路当前未提供检索页面", ""

    page_html = _decode(raw, response.headers.get("Content-Type", ""))
    parsed = _parse_results(page_html)
    if parsed:
        kept, _ = _relevant_only(parsed, keyword)
        if kept:
            dropped = len(parsed) - len(kept)
            note = "GET 兜底这一路直接渲染出了结果（低频时才这样）"
            if dropped:
                note += "；%d 条与检索词无关已滤掉" % dropped
            return kept, note, "ok"
        return [], "GET 兜底整批与检索词无关（推词兜底）", "site-empty"

    if "请登录后继续查看内容" in page_html:
        # 站点自己的限流标题（chunk getRateLimitTitle("429_outer")），不是账号墙。
        return [], ("GET 兜底撞限流页（站点文案「请登录后继续查看内容」是 429_outer "
                    "限流标题，不是账号墙）"), ""
    blocked = _blocked_reason(page_html)
    if blocked:
        return [], f"GET 兜底返回验证或拦截页（{blocked}）", ""
    if SITE_EMPTY_MARK in page_html:
        return [], "GET 兜底是站点自己的空结果页：站内确实没有这个词的结果", "site-empty"
    if any(marker in page_html for marker in ZERO_RESULT_MARKS):
        # 2026-11-27 修报告 §5.3：这一枚只说明页面上有结果容器，不说明站里没货——
        # 容器同时也是客户端渲染骨架页的组成部分（实测 60,751 字节的骨架页里
        # js-results/result-card 各 0 处，但站点改版后这类壳完全可能出现）。
        # 判「站内没这个词」只认 SITE_EMPTY_MARK 或整批推词兜底这两种正面证据。
        return [], "GET 兜底有结果容器但一张卡都没解析出来", ""
    return [], ("GET 兜底页面有内容但没有结果锚（SSR 骨架页，或页面结构可能已变）"), ""


def read_article(url: str) -> tuple[str, str]:
    """读取 mp.weixin.qq.com 文章正文纯文本。

    出网这一发先过 `_safe_mp_link`（2026-11-27 修报告 §5.1：旧写法对任何 URL 照
    发，`http://127.0.0.1/private` 与 `mp.weixin.qq.com.evil.test` 都成了合法输入，
    SSRF 边界失控）；`allow_redirects=False` 把重定向边界攥在自己手里——但**只拒
    异主机的跳转**。真网实测（yb74e2e.py 14:57）：检索给出 `http://mp.weixin.qq.com/s?…`，
    微信对它 301 升级到同主机的 https 版；一律拒跳等于正文这一路全断。所以这里自己
    跟、只跟 `MP_HOST`、最多 MAX_REDIRECT_HOPS 跳，每一跳重新过白名单；被带去别的
    主机立刻拒（报告 §11「微信 URL 跳到非微信主机⇒拒绝读最终页」）；正文受
    `_read_capped` 的 4MB 上限约束，响应逐枚关掉。
    """
    link = _safe_mp_link(url)
    if not link:
        return "", "不是 mp.weixin.qq.com 文章直链（或含端口/用户信息），已拒绝读取"
    for hop in range(MAX_REDIRECT_HOPS + 1):
        try:
            response = requests.get(link, headers=HEADERS_WX, timeout=TIMEOUT,
                                    verify=False, allow_redirects=False)
        except requests.RequestException as exc:
            return "", str(exc)
        if response.status_code not in REDIRECT_CODES:
            break
        target = str((response.headers or {}).get("Location", ""))
        nxt = _safe_mp_link(urljoin(link, target))
        code = response.status_code
        try:
            response.close()
        except (AttributeError, requests.RequestException):
            pass
        if nxt and hop < MAX_REDIRECT_HOPS:
            link = nxt
            continue
        return "", ("微信文章链接被 HTTP %d 重定向（Location: %s），"
                    "落地主机不是微信或跳转次数超限，本轮不跟随" % (code, target[:120]))
    try:
        if response.status_code != 200:
            return "", f"HTTP {response.status_code}"
        if "appmsgcaptcha" in (response.url or link):
            return "", ("微信验证码门（落地 wappoc_appmsgcaptcha，HTTP 200 非正文）："
                        "需登录态或人工过验证，浏览器同样被拦，降级用检索摘要")
        try:
            raw, truncated = _read_capped(response)
        except requests.RequestException as exc:
            return "", str(exc)
        if truncated:
            return "", f"正文超过 {MAX_BODY_BYTES} 字节上限并被截断"
        page_html = _decode(raw, (response.headers or {}).get("Content-Type", ""))
    finally:
        try:
            response.close()
        except (AttributeError, requests.RequestException):
            pass

    match = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*(?:<script|</div>)',
                      page_html, re.DOTALL)
    if not match:
        if "环境异常" in page_html or "验证" in page_html:
            return "", "微信返回验证页（正文含环境异常/验证），需登录态或人工过验证"
        return "", "未找到正文容器 js_content"
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", match.group(1), flags=re.DOTALL)
    lines = [_clean(paragraph) for paragraph in re.findall(r"<p[^>]*>(.*?)</p>", body, re.DOTALL)]
    text = "\n".join(line for line in lines if line)
    if not text:
        return "", "正文段落为空"
    return text, ""


def _empty(keyword: str, error: str = "") -> dict:
    out = {
        "keyword": keyword,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "微信公众号 (元宝搜索)",
        "_error": error,
        "_from_cache": False,
    }
    out["_fetch_failed" if error else "_zero_hit"] = True
    return out


def main():
    parser = argparse.ArgumentParser(description="微信公众号文章检索（元宝搜索）")
    parser.add_argument("keyword", help="检索词")
    parser.add_argument("--size", type=int, default=5)
    parser.add_argument("--read", action="store_true", help="同时取回正文")
    parser.add_argument("--time-filter", default="", metavar="档",
                        help="站点的时间档，只认 %s（不传等于 all）" % "、".join(TIME_FILTERS))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = search_wechat(args.keyword, size=args.size, read_body=args.read,
                           time_filter=args.time_filter)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"微信公众号检索（元宝） \"{args.keyword}\" | {result['searched_at']}")
    if result.get("_error"):
        print(f"错误: {result['_error']}")
    if result.get("_partial_error"):
        print(f"部分失败: {result['_partial_error']}")
    print(f"共 {result['total']} 条\n")
    for item in result["results"]:
        print(f"  {item.get('title', '')[:70]}")
        if item.get("account"):
            print(f"     公众号: {item['account']}")
        if item.get("date"):
            qualifier = "（相对时间折算）" if item.get("date_approx") else ""
            print(f"     日期: {item['date']}{qualifier}")
        print(f"     {item.get('url') or '（无有效微信直链）'}")
        if item.get("_error"):
            print(f"     错误: {item['_error']}")
        elif item.get("content"):
            print(f"     正文 {len(item['content'])} 字，节选：{item['content'][:100]}...")
        print()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
