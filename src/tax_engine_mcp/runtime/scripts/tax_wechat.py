#!/usr/bin/env python3
"""
微信公众号文章检索 — 搜狗微信 + 移动端 UA 取正文。

三段流程（每段都经实测确认）：
  1. 检索：GET https://weixin.sogou.com/weixin?type=2&query=<关键词>
     type=2 检索文章，type=1 检索公众号（实测 type=1 返回 0 条，不用）。
  2. 还原真实地址：搜狗给的是 weixin.sogou.com/link?url=... 跳转链接。
     带 Referer 请求该链接，返回的是 200 的 JS 片段页而不是 302，
     真实地址被拆成 url += '...' 的若干段拼出，拼接后还要去掉 "@"。
     不带 Referer 会被重定向到 /antispider/ 反爬页。
  3. 取正文：GET mp.weixin.qq.com/s?... 用手机版 UA + Referer 请求，
     正文容器是 id="js_content"。

归属说明：本源是"实务解读"层，权威性低于 NPC 法规与税务总局原文。

Usage:
  python tax_wechat.py "研发费用加计扣除" --size 5
  python tax_wechat.py "研发支出资本化" --size 3 --read --json
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_http  # noqa: E402

SOGOU_SEARCH = "https://weixin.sogou.com/weixin"
SOGOU_REFERER = "https://weixin.sogou.com/"
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
HEADERS_SOGOU = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": SOGOU_REFERER,
}
HEADERS_WX = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://mp.weixin.qq.com/",
}
TIMEOUT = 20
# 单次检索最多还原多少个跳转链接（搜狗对高频请求敏感）
MAX_RESTORE = 5
RESTORE_INTERVAL = 1.2
READ_INTERVAL = 2.0

# ── 搜狗串行闸（跨进程） ───────────────────────────────────────────────────
# 搜狗对高频请求敏感：一加压就跳 /antispider/，而且链接还原要靠检索那一次的
# 会话 cookie，两个进程各查一次等于各持一份会话互相搅。所以跟 NPC 用同一套闸
# （实现在 tax_http.SerialGate，锁文件分开）。
# "并发加压会触发反爬"光写在文档里全靠人记；进了闸就不必记了。
_SOGOU_LOCK_PATH = Path(tempfile.gettempdir()) / "tax-analysis-skill-sogou.lock"
_SOGOU_LOCK_TIMEOUT = float(os.getenv("TAX_SOGOU_LOCK_TIMEOUT", "180"))
sogou_gate = tax_http.SerialGate(path=_SOGOU_LOCK_PATH, timeout=_SOGOU_LOCK_TIMEOUT)
sogou_gate.site = "搜狗微信"        # 只改超时报错里的那句提示

# 闸只保证"同一时刻一个请求"，保证不了"别发太快"，所以另加一个最小间隔。
_SOGOU_MIN_INTERVAL = 1.0
_last_sogou_at = 0.0


def _sogou_get(sess: "requests.Session", url: str, **kwargs) -> "requests.Response":
    """打搜狗的请求统一走这里：先限速，再进串行闸。

    只约束 weixin.sogou.com。mp.weixin.qq.com 的正文读取是另一台机器，
    没有实测到它也有同样的反爬阈值，且读取本身已按 READ_INTERVAL 拉开间隔，
    所以不往这把闸里塞——不给自己没验证过的结论。
    """
    global _last_sogou_at
    gap = _SOGOU_MIN_INTERVAL - (time.monotonic() - _last_sogou_at)
    if gap > 0:
        time.sleep(gap)
    try:
        with sogou_gate:
            return sess.get(url, timeout=TIMEOUT, verify=False, **kwargs)
    finally:
        _last_sogou_at = time.monotonic()

_ITEM_RE = re.compile(r'<div class="txt-box">.*?</div>\s*</div>', re.DOTALL)
_LINK_RE = re.compile(r'href="(/link\?url=[^"]+)"')
_ACCOUNT_RE = re.compile(r'<a[^>]*class="account"[^>]*>(.*?)</a>', re.DOTALL)
_TITLE_RE = re.compile(r'<h3>\s*<a[^>]*>(.*?)</a>', re.DOTALL)
_DATE_RE = re.compile(r"timeConvert\('(\d+)'\)")


def search_wechat(keyword: str, size: int = 5, read_body: bool = False) -> dict:
    """
    检索微信公众号文章。

    Args:
        keyword: 检索词
        size: 返回条数上限
        read_body: True 时再逐篇取正文

    Returns:
        {"keyword","total","results","searched_at","source","_error"?}
    """
    url = f"{SOGOU_SEARCH}?type=2&query={quote(keyword)}"
    # 检索与还原必须共用一个 Session：还原链接要靠检索时拿到的
    # SNUID/SUID cookie，脱离会话会被判成爬虫跳到 antispider。
    sess = requests.Session()
    sess.headers.update(HEADERS_SOGOU)
    try:
        r = _sogou_get(sess, url)
    except requests.RequestException as e:
        return _empty(keyword, str(e))

    if r.status_code != 200:
        return _empty(keyword, f"HTTP {r.status_code}")
    if "/antispider/" in r.url:
        return _empty(keyword, "触发搜狗反爬（antispider）")

    blocks = _ITEM_RE.findall(r.text)
    if not blocks:
        return _empty(keyword, "检索结果为空或页面结构已变")

    results = []
    for block in blocks:
        link = _LINK_RE.search(block)
        if not link:
            continue
        title_m = _TITLE_RE.search(block)
        account_m = _ACCOUNT_RE.search(block)
        date_m = _DATE_RE.search(block)

        results.append({
            "title": _clean(title_m.group(1)) if title_m else "",
            "account": _clean(account_m.group(1)) if account_m else "",
            "date": _fmt_ts(date_m.group(1)) if date_m else "",
            "_link": "https://weixin.sogou.com" + link.group(1),
        })
        if len(results) >= size:
            break

    # 还原真实地址（逐个请求，带间隔）
    for i, item in enumerate(results):
        real, err = _restore_url(item.pop("_link"), sess)
        item["url"] = real
        if err:
            item["_error"] = err
        if i < len(results) - 1:
            time.sleep(RESTORE_INTERVAL)

    if read_body:
        for i, item in enumerate(results):
            if not item.get("url"):
                continue
            content, err = read_article(item["url"])
            item["content"] = content
            if err:
                item["_error"] = err
            if i < len(results) - 1:
                time.sleep(READ_INTERVAL)

    for item in results:
        item["source"] = "微信公众号"
        item["source_label"] = "实务解读"
        if not item.get("url"):
            continue
    return {
        "keyword": keyword,
        "total": len(results),
        "results": results,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "微信公众号 (搜狗微信)",
        "_from_cache": False,
    }


def _restore_url(link: str, sess: "requests.Session") -> tuple[str, str]:
    """
    还原搜狗跳转链接对应的真实 mp.weixin.qq.com 地址。

    Args:
        link: 搜狗 /link?url=... 地址
        sess: 与检索共用、已带上 SNUID/SUID cookie 的会话

    Returns:
        (真实地址, 错误信息)。成功时错误信息为空串。
    """
    try:
        r = _sogou_get(sess, link, headers=HEADERS_SOGOU, allow_redirects=False)
    except requests.RequestException as e:
        return "", str(e)

    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"
    if "/antispider/" in r.url:
        return "", "触发搜狗反爬（antispider）"

    # 页面把真实地址拆成 url += '片段' 的形式，逐段拼出
    parts = re.findall(r"url \+= '([^']*)'", r.text)
    if not parts:
        return "", "未在跳转页中找到 url 片段（页面结构可能已变）"
    return "".join(parts).replace("@", ""), ""


def read_article(url: str) -> tuple[str, str]:
    """
    取公众号文章正文纯文本。

    Returns:
        (正文纯文本, 错误信息)。成功时错误信息为空串。
    """
    try:
        r = requests.get(url, headers=HEADERS_WX, timeout=TIMEOUT, verify=False)
    except requests.RequestException as e:
        return "", str(e)

    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"

    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*(?:<script|</div>)',
                  r.text, re.DOTALL)
    if not m:
        if "环境异常" in r.text or "验证" in r.text:
            return "", "微信返回验证页，正文需浏览器环境"
        return "", "未找到正文容器 js_content"

    body = m.group(1)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.DOTALL)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", body, re.DOTALL)
    lines = [_clean(p) for p in paras]
    text = "\n".join(l for l in lines if l)
    if not text:
        return "", "正文段落为空"
    return text, ""


def _clean(fragment: str) -> str:
    """去标签、解实体、压空白。"""
    text = re.sub(r"<[^>]+>", "", fragment)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fmt_ts(ts: str) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, OSError):
        return ""


def _empty(keyword: str, error: str = "") -> dict:
    return {
        "keyword": keyword,
        "total": 0,
        "results": [],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "微信公众号 (搜狗微信)",
        "_error": error,
        "_from_cache": False,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="微信公众号文章检索（搜狗微信）")
    p.add_argument("keyword", help="检索词")
    p.add_argument("--size", type=int, default=5)
    p.add_argument("--read", action="store_true", help="同时取回正文")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = search_wechat(args.keyword, size=min(args.size, MAX_RESTORE),
                           read_body=args.read)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"🔍 微信公众号检索 \"{args.keyword}\" | {result['searched_at']}")
    if result.get("_error"):
        print(f"⚠️  {result['_error']}")
    print(f"共 {result['total']} 条\n")
    for item in result["results"]:
        print(f"  {item.get('title', '')[:70]}")
        if item.get("account"):
            print(f"     公众号: {item['account']}")
        if item.get("date"):
            print(f"     日期: {item['date']}")
        print(f"     {item.get('url') or '（未还原）'}")
        if item.get("_error"):
            print(f"     ⚠️ {item['_error']}")
        elif item.get("content"):
            print(f"     正文 {len(item['content'])} 字，节选：{item['content'][:100]}...")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
