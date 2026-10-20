#!/usr/bin/env python3
"""
360 站内搜索 — 通过 m.so.com 的 site: 检索补齐搜索引擎层能力。

为什么不用 Bing：www.bing.com 对 site: 查询返回 0 个结果块，
cn.bing.com/m.bing.com 虽偶发返回 10 个 <li class="b_algo">，但内容与查询无关
（查 chinatax.gov.cn 企业所得税法 返回"元气壁纸"），属于不可信降级，故已移除。
m.so.com 会命中目标站子域名，代价是它对被限流的 IP 返回一份"访问异常出错"页
（见 _BLOCK_MARKER），所以本模块把"被拦截"和"没结果"分成两种返回值。

360 把真实地址放在 m.so.com/jump?u=<urlencoded> 中，需要解出 u 参数。

Usage:
  python tax_so360.py "企业所得税法" --site chinatax.gov.cn --size 10
  python tax_so360.py "研发费用加计扣除" --site shui5.cn --json
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import quote, unquote
from html import unescape

import requests
import urllib3

import tax_http

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SEARCH_URL = "https://m.so.com/s"
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
HEADERS = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://m.so.com/",
}
TIMEOUT = 20
# m.so.com 偶发读超时，实测同一关键词连抓两次至少一次成功。
# 单次失败就返回空结果，会让上层误判"没搜到"，所以这里重试两次。
MAX_RETRIES = 2

# 360 的每条结果是 <div class="g-card res-list ...">，真实地址放在 data-pcurl
_CARD_RE = re.compile(r'data-pcurl="(https?://[^"]+)"[^>]*class="[^"]*res-list', re.DOTALL)
_TITLE_RE = re.compile(r'<h3[^>]*class="res-title"[^>]*>(.*?)</h3>', re.DOTALL)
_SNIPPET_RE = re.compile(r'<div[^>]*class="res-con"[^>]*>(.*?)</div>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# 360 自身的推荐/再搜索链接不是检索结果
_SELF_SEARCH_RE = re.compile(r"^https?://m\.so\.com/")
# 被限流时 360 回一份约 5KB 的"访问异常出错"页：HTTP 200、一张结果卡都没有。
# 必须识别出来，否则上层把 0 条当成"该法规没有解读文件"，前端会据此给出假结论。
_BLOCK_MARKER = "访问异常出错"


def _clean(fragment: str) -> str:
    text = unescape(_TAG_RE.sub("", fragment))
    return re.sub(r"\s+", " ", text).strip()


def _matches_site(url: str, site: str) -> bool:
    needle = site.replace("www.", "").lower()
    return needle in url.replace("www.", "").lower()


def so360_search(keyword: str, site: str = "", size: int = 10,
                 page: int = 1) -> dict:
    """
    在 360 移动版检索，可选 site: 限定。

    Args:
        keyword: 检索词
        site: 目标域名，如 chinatax.gov.cn。空字符串表示全网检索
        size: 返回条数上限
        page: 页码，从 1 开始

    Returns:
        {"keyword","site","total","results","searched_at","source","_error"?}
    """
    query = f"site:{site} {keyword}" if site else keyword
    url = f"{SEARCH_URL}?q={quote(query)}&pn={page}"

    r, err = None, ""
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            break
        except requests.RequestException as e:
            err = tax_http.short_reason(e)
            r = None
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))

    if r is None:
        return _empty(keyword, site, f"请求失败：{err}")

    if r.status_code != 200:
        return _empty(keyword, site, f"HTTP {r.status_code}")

    if _BLOCK_MARKER in r.text:
        return _empty(keyword, site,
                      "360 返回访问异常页（本机 IP 被限流），0 条不代表没有匹配结果")

    # 按 data-pcurl 切分出每张结果卡，再在卡内取标题与摘要
    cards = re.split(r'(?=<div[^>]*data-pcurl=")', r.text)
    results = []
    seen = set()
    for card in cards:
        pcurl = re.search(r'data-pcurl="(https?://[^"]+)"', card)
        if not pcurl:
            continue
        target = unescape(pcurl.group(1))
        if _SELF_SEARCH_RE.match(target):
            continue
        if site and not _matches_site(target, site):
            continue
        if target in seen:
            continue
        seen.add(target)

        title_m = _TITLE_RE.search(card)
        snippet_m = _SNIPPET_RE.search(card)
        results.append({
            "title": _clean(title_m.group(1)) if title_m else _title_from_url(target),
            "url": target,
            "snippet": _clean(snippet_m.group(1))[:200] if snippet_m else "",
            "source": _domain_of(target),
            "site": site,
        })
        if len(results) >= size:
            break

    return {
        "keyword": keyword,
        "site": site,
        "total": len(results),
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "360 搜索 (m.so.com)",
        "_from_cache": False,
    }


def _title_from_url(url: str) -> str:
    """360 的 jump 链接不含标题，用路径末段兜底。"""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.(html?|shtml|jsp|aspx)$", "", tail, flags=re.IGNORECASE)
    return tail or url


def _domain_of(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else ""


def _empty(keyword: str, site: str, error: str = "") -> dict:
    return {
        "keyword": keyword,
        "site": site,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "360 搜索 (m.so.com)",
        "_error": error,
        "_from_cache": False,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="360 站内搜索（site: 限定）")
    p.add_argument("keyword", help="检索词")
    p.add_argument("--site", default="", help="目标域名，如 chinatax.gov.cn")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = so360_search(args.keyword, site=args.site, size=args.size, page=args.page)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    scope = f"site:{args.site}" if args.site else "全网"
    print(f"🔍 360 搜索 [{scope}] \"{args.keyword}\" | {result['searched_at']}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    print(f"共 {result['total']} 条\n")
    for item in result["results"]:
        print(f"  {item['title'][:70]}")
        print(f"     {item['url']}")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
