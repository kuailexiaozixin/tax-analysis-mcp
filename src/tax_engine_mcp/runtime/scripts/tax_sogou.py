#!/usr/bin/env python3
"""搜狗手机端单页检索：site 限定、直链解析与整页元数据缓存。"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_cache  # noqa: E402
import tax_http  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SEARCH_URL = "https://m.sogou.com/web/searchList.jsp?keyword=%s"
SOURCE_NAME = "搜狗搜索 (m.sogou.com)"
UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://m.sogou.com/",
}
TIMEOUT = 20
# 重试只针对网络异常与 5xx：反爬页是 200，重发只会把封禁时间拖长。
MAX_RETRIES = 2

# 限流实测记录见 references/source_defects.md；冷却期快速返回，不等待到期。
SEARCH_MIN_GAP = 3.0
BUDGET_MAX = 4
BUDGET_WINDOW = 600.0
BLOCK_SECONDS = 600.0
CACHE_TTL = 1800

_LOCK_PATH = Path(tempfile.gettempdir()) / "tax-analysis-sogou-web.lock"
_STATE_PATH = Path(tempfile.gettempdir()) / "tax-analysis-sogou-web-state.json"
_LOCK_TIMEOUT = float(os.getenv("TAX_SOGOU_WEB_LOCK_TIMEOUT", "180"))
_sogou_gate = tax_http.SerialGate(path=_LOCK_PATH, timeout=_LOCK_TIMEOUT)
_sogou_gate.site = "搜狗"
_cache = tax_cache.CacheManager(
    enabled=os.getenv("TAX_SOGOU_CACHE", "1") != "0", namespace="sogou")

# 反爬页的落点；HTTP 状态仍是 200，所以判据看原地址的 Location 与最终地址。
_ANTISPIDER_MARKS = ("/antispider/", "antip=web_hd", "antip=wap_hd")
# 零结果页的文案（"站内没有找到能和“…”匹配的内容"）。有结果块时不按它判，
# 免得摘要里出现同一句话被误读成 0 条。
_NO_RESULT_MARK = "没有找到"
# 手机入口 site: 限定下站点自报「站内无定制结果」那一档的 class（2026-11-26 量到：
# 结果区里那枚 div 写成 class="vrResult site-vr-none"，可见文本「抱歉，未找到
# “shui5.cn”站内包含“增值税”的内容」）。注意手机页说的是「未找到」，上面那枚
# 桌面文案「没有找到」在手机上一个字都不出现——所以它认不出这一档。
_SITE_NONE_CLASS = "site-vr-none"
_VRWRAP_RE = re.compile(r'(?=<div[^>]*class="[^"]*\bvrwrap\b)')
_DATA_URL_RE = re.compile(r'data-url="(https?://[^"]+)"')
_TITLE_RE = re.compile(r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>(.*?)</h3>', re.S)
_SNIPPET_RE = re.compile(
    r'<div[^>]*class="[^"]*fz-mid[^"]*space-txt[^"]*"[^>]*>(.*?)</div>', re.S)
_CITE_RE = re.compile(
    r'<a[^>]*class="[^"]*citeLinkClass[^"]*"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_CONT_PAGE_RE = re.compile(r"\$\d+(?=\.html(?:$|[?#]))")
# 2026-11-26 量到的手机入口新形状：结果卡片整体搬进
# `<script type="application/json">…</script>` 数据岛，HTML 侧只剩壳（`a.resultLink`
# 只剩百科卡一枚，`data-url` 0 处）。这一枚正则只认带 type="application/json" 的容器，
# 不认裸 <script>：脚本正文里出现过一段 url/title 字样不算结果（判据
# test_a_json_payload_outside_an_island_tag_is_not_a_result）。
# 注意别拿 script 的 id 当锚——那是 `data-<会话 uuid>`，每轮都换。
_ISLAND_JSON_RE = re.compile(
    r'<script[^>]*\btype=["\']application/json["\'][^>]*>(.*?)</script>',
    re.S | re.I)
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class _Unavailable(Exception):
    """预算、熔断或跨进程排队失败。blocked 只表示"被站点反爬"这一种。"""

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


def _read_state() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    """状态盘只影响保护强度，写不进去就继续跑，不让检索因此失败。"""
    tmp = _STATE_PATH.with_name(_STATE_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _reserve_slot() -> None:
    """在闸内调用：查熔断与预算，按共享时间戳等间隔，再把本次请求记进预算。"""
    now = time.time()
    state = _read_state()
    blocked_until = float(state.get("blocked_until") or 0)
    if blocked_until > now:
        raise _Unavailable(
            "搜狗熔断中：约 %d 秒后到期（上一条请求触发反爬，冷却期内不发请求）"
            % (int(blocked_until - now) + 1), blocked=True)

    recent = [t for t in (state.get("recent") or [])
              if isinstance(t, (int, float)) and now - t < BUDGET_WINDOW]
    if len(recent) >= BUDGET_MAX:
        raise _Unavailable(
            "搜狗请求预算已用尽：%d 分钟内最多 %d 次，本轮没有发请求"
            % (int(BUDGET_WINDOW // 60), BUDGET_MAX))

    last = float(state.get("last_request_at") or 0)
    wait = SEARCH_MIN_GAP - (now - last)
    if wait > 0:
        time.sleep(min(wait, SEARCH_MIN_GAP))

    now = time.time()
    recent.append(now)
    state["recent"] = recent
    state["last_request_at"] = now
    if blocked_until and blocked_until <= now:
        state["blocked_until"] = 0.0
    _write_state(state)


def _trip_breaker_locked() -> None:
    state = _read_state()
    state["blocked_until"] = max(float(state.get("blocked_until") or 0),
                                 time.time() + BLOCK_SECONDS)
    _write_state(state)


def _trip_breaker() -> None:
    with _sogou_gate:
        _trip_breaker_locked()


def _is_antispider_location(resp) -> bool:
    if resp.status_code not in _REDIRECT_CODES:
        return False
    location = (getattr(resp, "headers", {}) or {}).get("Location", "")
    return any(mark in location for mark in _ANTISPIDER_MARKS)


def _get(sess: requests.Session, url: str) -> requests.Response:
    """打搜狗的请求统一走这里：预算、熔断、跨进程间隔都在闸内判。"""
    with _sogou_gate:
        _reserve_slot()
        resp = sess.get(url, timeout=TIMEOUT, verify=False, allow_redirects=False)
        if _is_antispider_location(resp):
            # 不跟随反爬跳转：跟随本身就是再一次请求，而且会把 302 变成 200。
            _trip_breaker_locked()
        return resp


def _looks_like_verify_page(html: str) -> bool:
    """200 但既无结果区、又带验证码特征的页面。不能把摘要里的词当判据。"""
    if _VRWRAP_RE.search(html):
        return False
    if "vrResult" in html:
        parser = _MobileResultsParser()
        parser.feed(html)
        if parser.has_results:
            return False
    head = html[:6000]
    return ("antispider" in head or "seccodeImage" in head
            or ("验证码" in head and _NO_RESULT_MARK not in head))


def _fetch(query: str) -> tuple[str, str, str, bool]:
    """取一页检索结果，返回 (页面, 最终地址, 错误, 是否被反爬)。

    错误非空时页面为空。反爬页不重试——重发只会把封禁时间拖长；网络异常与
    5xx 才按 MAX_RETRIES 重试。
    """
    url = SEARCH_URL % quote(query)
    sess = requests.Session()
    sess.headers.update(HEADERS)
    last = ""
    try:
        for attempt in range(MAX_RETRIES):
            try:
                r = _get(sess, url)
            except _Unavailable as e:
                return "", "", str(e), e.blocked
            except TimeoutError as e:
                # 串行闸等不到（TAX_SOGOU_LOCK_TIMEOUT 到点）＝另有进程正在打搜狗。
                # 2026-11-27 修报告 §5.10：这枚异常此前直接穿透 `sogou_search`，
                # 调用方拿到的是裸异常而不是「这一轮没发请求」的信封；也不重试——
                # 闸被占着的时候重试只会把等待叠上去。
                return "", "", str(e), False
            except requests.RequestException as e:
                last = tax_http.short_reason(e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue

            final_url = getattr(r, "url", "") or url
            location = ((getattr(r, "headers", {}) or {}).get("Location", "")
                        if r.status_code in _REDIRECT_CODES else "")
            if any(mark in final_url for mark in _ANTISPIDER_MARKS) or \
                    any(mark in location for mark in _ANTISPIDER_MARKS):
                _trip_breaker()
                return "", final_url, (
                    "触发搜狗反爬（antispider），0 条不代表没有匹配结果"), True
            if r.status_code in _REDIRECT_CODES:
                return "", final_url, (
                    "搜狗返回 HTTP %d 跳转到非检索页（%s），本轮不跟随"
                    % (r.status_code, location[:80] or "无 Location")), False
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                if r.status_code in (403, 429):
                    _trip_breaker()
                    return "", final_url, last, True
                if not 500 <= r.status_code < 600:
                    return "", final_url, last, False
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue

            html = r.content.decode("utf-8", errors="replace")
            if _looks_like_verify_page(html):
                _trip_breaker()
                return "", final_url, (
                    "搜狗返回验证页（HTTP 200），0 条不代表没有匹配结果"), True
            return html, final_url, "", False
        return "", "", f"请求失败：{last}", False
    finally:
        sess.close()


def _clean(fragment: str) -> str:
    text = unescape(_TAG_RE.sub("", fragment))
    return re.sub(r"\s+", " ", text).strip()


def _normalize_url(url: str) -> str:
    """把文章第 2 页起的 $N 地址归回首页，避免正文从中间读起。"""
    return _CONT_PAGE_RE.sub("", url)


def _matches_site(url: str, site: str) -> bool:
    needle = site.lower().removeprefix("www.")
    host = _domain_of(url)
    return host == needle or host.endswith("." + needle)


def _domain_of(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return ""
        return (parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _title_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\$\d+", "", tail)
    tail = re.sub(r"\.(html?|shtml|jsp|aspx)$", "", tail, flags=re.IGNORECASE)
    return tail or url


def _cite_date(text: str) -> str:
    for value in reversed(_DATE_RE.findall(text)):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            continue
    return ""


def _date_of(block: str) -> str:
    """日期只从引用行取；摘要里的日期不是发布日，不能拿来顶。"""
    cite = _CITE_RE.search(block)
    return _cite_date(_clean(cite.group(1))) if cite else ""


class _MobileResultsParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.card = None
        self.card_depth = 0
        self.cards = []
        self.has_results = False
        self.site_none = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if _SITE_NONE_CLASS in classes:
            self.site_none = True
        if tag == "div" and "vrResult" in classes and self.card is None:
            self.has_results = True
            self.card_depth = len(self.stack)
            self.card = {"title": [], "snippet": [], "cite": [], "url": ""}
        field = self.stack[-1][1] if self.stack else None
        if self.card is not None:
            if tag in ("script", "style"):
                field = None
            elif tag == "h3":
                field = "title"
            elif "citeurl" in classes:
                field = "cite"
            elif any(c in classes for c in ("txt-summary", "text-layout", "str_info", "fz-mid")):
                field = "snippet"
            if tag == "a" and "resultLink" in classes and field == "title":
                try:
                    values = parse_qs(urlsplit(attrs.get("href") or "").query)
                    target = values.get("url", [""])[0]
                except ValueError:
                    target = ""
                if _domain_of(target) and not _matches_site(target, "sogou.com"):
                    self.card["url"] = _normalize_url(target)
        if tag not in self._VOID:
            self.stack.append((tag, field))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                if self.card is not None and len(self.stack) <= self.card_depth:
                    self.cards.append(self.card)
                    self.card = None
                break

    def handle_data(self, text):
        if self.card is not None and self.stack:
            field = self.stack[-1][1]
            if field:
                self.card[field].append(text)


def _island_title_hit(query: str, *fields: str) -> bool:
    """这张岛与检索词有没有关系（按空白分词，命中任一分词即算）。

    检索词为空时一律算命中——调用方没给词就不要拿这条尺去筛人。
    """
    if not query:
        return True
    hay = "\n".join(str(field or "") for field in fields).lower()
    return any(token.lower() in hay for token in query.split() if token)


def _island_rows(html: str, site: str, query: str = "") -> tuple[list, int]:
    """从 JSON 数据岛里读结果卡片，返回 (结果, 带直链的原始卡片数)。

    2026-11-26 真页（`tests/fixtures/msogou_dataisland_20261126.html`）里九块岛中
    六块是结果卡片，形状是顶层带 `url`+`title` 的对象（另有 `content` 摘要与
    `showSiteAndDate`）；剩下三块分别是全局配置、只有标题的新闻聚合、带翻页 URL
    的导航块——它们没有顶层 `url`+`title`，所以只认这一对键就自动筛掉了。
    日期一律留空：岛上的 `showSiteAndDate.time` 语义说不清（本轮同一页里两枚卡片
    标的是抓取当天 2026-11-26，另外几枚标的是站点收录/更新时间），拿它当发布日印进
    答案就是把猜测写成事实。

    2026-11-27 修报告 §5.5 加了两道闸：
    - **归属**：只认带 `cardInfo`（dict）的岛。真页九块岛每块都有 28 键的 `cardInfo`，
      结果岛的 `classId` 是 30010582；而诊断里那枚合成推荐载荷
      `{"url":…,"title":…,"content":…}` 根本没有 `cardInfo`——旧判据只看 `url+title`，
      这种载荷会直接当成一条结果交付出去（假命中），带 `site:` 时又被过滤掉（假零命中）。
      这里不把 `classId` 写死成白名单：那枚值只在一份真页上量过，站点换个卡片模板号
      就会把真结果筛成 0 条，那种 0 比现在的假命中更难被发现。
    - **相关性**：`query` 传进来时，与检索词毫不相干的岛不计入 `raw`，因此也不会
      被上层拿去当「站内没货」的证据。不传 query 的调用（含既有用例）行为不变。
    """
    rows = []
    seen = set()
    raw = 0
    for body in _ISLAND_JSON_RE.findall(html):
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            # 畸形 JSON 不许把「页面改版」升级成崩溃。
            continue
        if not isinstance(payload, dict):
            continue
        if not isinstance(payload.get("cardInfo"), dict):
            # 没有 cardInfo 的载荷不是渲染出来的结果卡（§5.5 的合成推荐位就是这形）。
            continue
        url = (payload.get("url") or "").strip()
        title = _clean(payload.get("title") or "")
        if not url or not title:
            continue
        if not _domain_of(url) or _matches_site(url, "sogou.com"):
            continue
        if not _island_title_hit(query, title, payload.get("content")):
            continue
        raw += 1
        url = _normalize_url(url)
        if url in seen:
            continue
        seen.add(url)
        if site and not _matches_site(url, site):
            continue
        rows.append({
            "title": title,
            "url": url,
            "date": "",
            "snippet": _clean(payload.get("content") or ""),
            "source": _domain_of(url),
            "site": site,
        })
    return rows, raw


def _parse_mobile(html: str, site: str, size: int) -> tuple[list, int, bool]:
    parser = _MobileResultsParser()
    parser.feed(html)
    parser.close()
    rows = []
    seen = set()
    raw = 0
    for card in parser.cards:
        url = card["url"]
        title = re.sub(r"\s+", " ", "".join(card["title"])).strip()
        if not url or not title:
            continue
        raw += 1
        if (site and not _matches_site(url, site)) or url in seen:
            continue
        seen.add(url)
        rows.append({
            "title": title,
            "url": url,
            "date": _cite_date(" ".join(card["cite"])),
            "snippet": re.sub(r"\s+", " ", "".join(card["snippet"])).strip(),
            "source": _domain_of(url),
            "site": site,
        })
    return rows[:size], raw, parser.has_results


def _parse(html: str, site: str = "", size: int = 10,
           query: str = "") -> tuple[list, int, bool]:
    """解析结果块，返回 (结果, 带直链的原始块数, 是否出现结果区)。

    原始块数用来区分"站点确实 0 条"与"页面改版、结果块还在但取不到直链"：
    后者要报 `_error`，不能静默当 0 条。
    """
    if "vrResult" in html:
        rows, raw, has_mobile = _parse_mobile(html, site, size)
        if has_mobile:
            if raw:
                return rows, raw, has_mobile
            # 2026-11-26 起结果卡片整体搬进 JSON 数据岛，HTML 侧只剩壳：这一支
            # 先按 HTML 读到 0 枚直链，再回落到岛层。岛层也读不出才让上层报
            # 「页面结构可能改版」——「有结果区但一条直链都取不出」这个语义
            # 仍然留着，不能因为加了岛层就退化成静默 0 条。
            island_rows, island_raw = _island_rows(html, site, query)
            if island_raw:
                return island_rows[:size], island_raw, has_mobile
            return rows, raw, has_mobile
    blocks = _VRWRAP_RE.split(html)
    rows = []
    seen = set()
    raw = 0
    for block in blocks[1:]:
        m = _DATA_URL_RE.search(block)
        if not m:
            continue
        raw += 1
        url = _normalize_url(unescape(m.group(1)))
        if site and not _matches_site(url, site):
            continue
        if url in seen:
            continue
        seen.add(url)
        title_m = _TITLE_RE.search(block)
        snippet_m = _SNIPPET_RE.search(block)
        rows.append({
            "title": (_clean(title_m.group(1)) if title_m else "") or _title_from_url(url),
            "url": url,
            "date": _date_of(block),
            "snippet": _clean(snippet_m.group(1)) if snippet_m else "",
            "source": _domain_of(url),
            "site": site,
        })
    return rows[:size], raw, bool(blocks[1:])


def _declares_no_site_results(html: str) -> bool:
    """站点在手机入口自报「这个站内没有匹配内容」（class 带 site-vr-none）。

    判据走 HTMLParser 的 class 词元，不拿正则裸匹这个类名：脚本里出现过
    这四个字不算数（同一枚理由已在 test_mobile_class_name_in_script_is_not_
    a_result_area 上钉着）。
    """
    parser = _MobileResultsParser()
    parser.feed(html)
    parser.close()
    return parser.site_none


def _payload(keyword: str, site: str, rows: list, searched_at: str,
             from_cache: bool = False) -> dict:
    out = {
        "keyword": keyword,
        "site": site,
        "total": len(rows),
        "results": rows,
        "searched_at": searched_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE_NAME,
        "_from_cache": from_cache,
    }
    # 空清单一律带 _zero_hit，缓存命中那一路也不例外：否则同一轮"0 条"
    # 第一次有键、第二次从缓存回来没键，消费面两次读出的性质不一样。
    if not rows:
        out["_zero_hit"] = True
    return out


def _empty(keyword: str, site: str, error: str = "", blocked: bool = False) -> dict:
    """空结果信封，按统一出口标记契约分流：带 error（含反爬 blocked）的是
    请求层面的失败，打 `_fetch_failed`；不带 error 的是这一轮跑成了、答案就是
    0 条，打 `_zero_hit`。消费面（`_rows_and_error`、聚合层、`tax_cases`）
    从此认这两枚键判失败，不再拿 `_error` 文本猜。"""
    out = _payload(keyword, site, [], "")
    if error:
        # `_payload` 见空清单就打了 _zero_hit，这一支是失败，必须把它摘掉：
        # 两枚键同时挂在一只信封上，判据无论先看哪枚都说明契约被写坏了。
        # 2026-11-25 用例 test_sogou_breaker_is_a_failure 就是撞在这上面报红的。
        out.pop("_zero_hit", None)
        out["_error"] = error
        out["_fetch_failed"] = True
    if blocked:
        out["_blocked"] = True
    return out


def _cache_key(query: str) -> str:
    raw = f"sogou-mobile-page|{query}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _remember(key: str, out: dict) -> None:
    if "_error" in out:
        return
    # 只存结果元数据；检索词在键里是哈希，不落盘。
    _cache.set(key, {"results": out["results"],
                     "searched_at": out["searched_at"]})


def sogou_search(keyword: str, site: str = "", size: int = 10) -> dict:
    """
    在搜狗网页检索，可选 site: 限定。

    Args:
        keyword: 检索词
        site: 目标域名，如 shui5.cn。空字符串表示全网检索
        size: 返回条数上限

    Returns:
        {"keyword","site","total","results","searched_at","source","_from_cache",
         "_error"?,"_fetch_failed"?,"_zero_hit"?,"_blocked"?}
        results 每项含 title/url/date/snippet/source/site。
        被反爬、熔断或预算用尽时 `_error` 非空并带 `_fetch_failed=True`；被反爬时
        另带 `_blocked=True`。0 条且页面给出无结果提示时不带 `_error` 而带
        `_zero_hit=True`——这两种在调用方那里要分开处理，3.62.26 起统一出口标记
        契约把它们钉成两枚键，判据见 `tax_http.is_fetch_failure`。
    """
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("size 必须是正整数")
    query = f"site:{site} {keyword}" if site else keyword
    key = _cache_key(query)
    cached = _cache.get(key, max_age=CACHE_TTL)
    if isinstance(cached, dict) and isinstance(cached.get("results"), list):
        return _payload(keyword, site, cached["results"][:size],
                        cached.get("searched_at", ""), from_cache=True)

    html, final_url, err, blocked = _fetch(query)
    if err:
        return _empty(keyword, site, err, blocked)
    if any(mark in final_url for mark in _ANTISPIDER_MARKS):
        return _empty(keyword, site,
                      "触发搜狗反爬（antispider），0 条不代表没有匹配结果", True)

    rows, raw, has_vrwrap = _parse(html, site=site, size=sys.maxsize, query=keyword)
    if rows:
        out = _payload(keyword, site, rows, "")
        _remember(key, out)
        return _payload(keyword, site, rows[:size], out["searched_at"])
    if _declares_no_site_results(html):
        # 2026-11-26 量到：手机入口 site: 限定下，站点对真 0 条自报的形状是
        # class="vrResult site-vr-none" +「未找到」（桌面那枚「没有找到」一个字
        # 都不出现）。旧出口把这一档落进「结构可能改版」（_fetch_failed），把
        # 「没搜到」与「取数失败」语义写反了——判据与重测见
        # search_source/sogou/NOTE.md。
        out = _empty(keyword, site)
        _remember(key, out)
        return out
    if _NO_RESULT_MARK in html and not has_vrwrap:
        out = _empty(keyword, site)
        _remember(key, out)
        return out
    # 2026-11-27 修报告 §5.5：岛上的卡带着 cardInfo 也不等于这一轮的答案——
    # 整批与检索词毫不相干时那是推荐位。它既不能交付成结果，更推不出
    # 「站内没有这个词」。这一支必须排在「改版」那支之前：推荐位同样表现为
    # 「有结果区、零直链」，不先分出来就被读成页面改版（3.62.49 注入证伪时，
    # 撤掉岛层相关性闸，全族用例没有一枚报红，就是因为这个落点写在了末支）。
    if _island_rows(html, "", "")[1] and not _island_rows(html, "", keyword)[1]:
        return _empty(keyword, site, "结果区里的卡片没有一张与检索词相关（像是推荐位）："
                                     "不能据此判站内没有结果")
    if raw == 0 and has_vrwrap:
        return _empty(keyword, site,
                      "搜狗返回了结果区，但没有解析出可用直链：页面结构可能改版")
    if not has_vrwrap:
        return _empty(keyword, site,
                      "搜狗返回的页面既没有结果区也没有无结果提示：可能落在验证页")
    # 走到这里意味着 raw > 0：卡片与检索词相关（相关性计数在 `_island_rows` 里
    # 就是发生在 raw 之前），只是没有一张落在 site: 指定的域名上。这一轮确实
    # 没有目标站点的结果，不是取数失败——旧语义保留（判据上移那一支只管
    # 「整批都是推荐位」那种 0 条，不能顺带把 site: 过滤也算成通道故障）。
    out = _empty(keyword, site)
    _remember(key, out)
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="搜狗网页检索（site: 限定）")
    p.add_argument("keyword", help="检索词")
    p.add_argument("--site", default="", help="目标域名，如 shui5.cn")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = sogou_search(args.keyword, site=args.site, size=args.size)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    scope = f"site:{args.site}" if args.site else "全网"
    print(f"🔍 搜狗搜索 [{scope}] \"{args.keyword}\" | {result['searched_at']}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    print(f"共 {result['total']} 条\n")
    for item in result["results"]:
        print(f"  {item['title'][:70]}")
        print(f"     {item['url']}")
        if item.get("date"):
            print(f"     {item['date']}")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
