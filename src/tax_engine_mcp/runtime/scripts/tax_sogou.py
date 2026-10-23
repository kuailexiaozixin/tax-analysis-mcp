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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
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


def _parse(html: str, site: str = "", size: int = 10) -> tuple[list, int, bool]:
    """解析结果块，返回 (结果, 带直链的原始块数, 是否出现结果区)。

    原始块数用来区分"站点确实 0 条"与"页面改版、结果块还在但取不到直链"：
    后者要报 `_error`，不能静默当 0 条。
    """
    if "vrResult" in html:
        rows, raw, has_mobile = _parse_mobile(html, site, size)
        if has_mobile:
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


def _payload(keyword: str, site: str, rows: list, searched_at: str,
             from_cache: bool = False) -> dict:
    return {
        "keyword": keyword,
        "site": site,
        "total": len(rows),
        "results": rows,
        "searched_at": searched_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE_NAME,
        "_from_cache": from_cache,
    }


def _empty(keyword: str, site: str, error: str = "", blocked: bool = False) -> dict:
    out = _payload(keyword, site, [], "")
    if error:
        out["_error"] = error
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
         "_error"?,"_blocked"?}
        results 每项含 title/url/date/snippet/source/site。
        被反爬、熔断或预算用尽时 `_error` 非空；被反爬时另带 `_blocked=True`。
        0 条且页面给出无结果提示时不带 `_error`——这两种在调用方那里要分开处理。
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

    rows, raw, has_vrwrap = _parse(html, site=site, size=sys.maxsize)
    if rows:
        out = _payload(keyword, site, rows, "")
        _remember(key, out)
        return _payload(keyword, site, rows[:size], out["searched_at"])
    if _NO_RESULT_MARK in html and not has_vrwrap:
        out = _empty(keyword, site)
        _remember(key, out)
        return out
    if raw == 0 and has_vrwrap:
        return _empty(keyword, site,
                      "搜狗返回了结果区，但没有解析出可用直链：页面结构可能改版")
    if not has_vrwrap:
        return _empty(keyword, site,
                      "搜狗返回的页面既没有结果区也没有无结果提示：可能落在验证页")
    # 有直链但都被 site: 过滤掉：这一轮确实没有目标站点的结果，不是取数失败
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
