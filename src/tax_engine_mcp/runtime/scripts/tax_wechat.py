#!/usr/bin/env python3
"""微信公众号文章检索：元宝搜索取清单，微信正文页取全文。

元宝入口返回服务端渲染 HTML，结果自带 mp.weixin.qq.com 文章直链，不需要还原
搜狗跳转页。结果页、登录墙与验证码墙分别归类；只有明确的空结果页才算零命中。
本源是实务解读层，权威性低于 NPC 法规与税务总局原文。

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
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_http  # noqa: E402

YUANBAO_SEARCH = "https://so.qq.com/isearch"
YUANBAO_HOME = "https://so.qq.com/"
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
BLOCK_MARKERS = ("环境异常", "验证码", "请完成验证", "系统检测到")
BLOCK_ANCHORS = ("/antispider/", "antispider.min.js", "anti.min.css")
LOGIN_WALL_ANCHORS = ("linkid&quot;:&quot;login_page", "请登录后继续查看内容")
ZERO_RESULT_MARKS = ("js-results", "result-card")

_LOCK_PATH = Path(tempfile.gettempdir()) / "tax-engine-yuanbao.lock"
_LOCK_TIMEOUT = float(os.getenv("TAX_YUANBAO_LOCK_TIMEOUT", "180"))
yuanbao_gate = tax_http.SerialGate(path=_LOCK_PATH, timeout=_LOCK_TIMEOUT)
yuanbao_gate.site = "元宝搜索"
YUANBAO_MIN_INTERVAL = 1.5
_last_yuanbao_at = 0.0


def _yuanbao_get(sess: "requests.Session", url: str, **kwargs) -> "requests.Response":
    global _last_yuanbao_at
    gap = YUANBAO_MIN_INTERVAL - (time.monotonic() - _last_yuanbao_at)
    if gap > 0:
        time.sleep(gap)
    try:
        with yuanbao_gate:
            return sess.get(url, timeout=TIMEOUT, verify=False, **kwargs)
    finally:
        _last_yuanbao_at = time.monotonic()


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
        chunks.append(chunk[:room])
        total += len(chunk[:room])
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


def search_wechat(keyword: str, size: int = 5, read_body: bool = False) -> dict:
    """检索公众号文章；read_body=True 时再逐篇读取微信正文。"""
    if not keyword.strip():
        return _empty(keyword, "检索词不能为空")
    if size < 1:
        return _empty(keyword, "size 必须大于 0")

    url = f"{YUANBAO_SEARCH}?tab=weixin_article&query={quote(keyword, safe='')}"
    sess = requests.Session()
    sess.headers.update(HEADERS_YUANBAO)
    try:
        response = _yuanbao_get(sess, url, stream=True)
    except requests.RequestException as exc:
        return _empty(keyword, f"元宝请求失败：{exc}")

    try:
        if response.status_code != 200:
            return _empty(keyword, f"元宝返回 HTTP {response.status_code}")
        landed = response.url or ""
        if "/antispider/" in landed or "antip=web_hd" in landed or "antip=wap_hd" in landed:
            return _empty(keyword, "元宝跳转到反爬或验证页")
        try:
            raw, truncated = _read_capped(response)
        finally:
            response.close()
    except requests.RequestException as exc:
        return _empty(keyword, f"元宝响应读取失败：{exc}")

    if truncated:
        return _empty(keyword, f"元宝响应超过 {MAX_BODY_BYTES} 字节上限并被截断")
    if not raw:
        return _empty(keyword, "元宝返回 HTTP 200 但正文 0 字节：这一路当前未提供检索页面")

    page_html = _decode(raw, response.headers.get("Content-Type", ""))
    parsed = _parse_results(page_html)
    if not parsed:
        if any(anchor in page_html for anchor in LOGIN_WALL_ANCHORS):
            return _empty(keyword, "元宝返回登录墙：当前会话未提供公众号检索结果；"
                                  "本模块不读取浏览器登录态")
        blocked = _blocked_reason(page_html)
        if blocked:
            return _empty(keyword, f"元宝返回验证或拦截页（{blocked}）")
        if any(marker in page_html for marker in ZERO_RESULT_MARKS):
            return _empty(keyword)
        return _empty(keyword, "元宝页面有内容，但没有结果锚点、登录墙、验证码或空结果标记："
                              "页面结构可能已变")

    results = parsed[:size]
    lost_link = sum(1 for item in results if not item["url"])
    if lost_link == len(results):
        return _empty(keyword, f"元宝给出 {len(results)} 条结果，但没有一条有效的微信文章直链")

    if read_body:
        readable = [item for item in results if item["url"]]
        for index, item in enumerate(readable):
            content, error = read_article(item["url"])
            item["content"] = content
            if error:
                item["_error"] = error
            if index < len(readable) - 1:
                time.sleep(READ_INTERVAL)

    lost_body = sum(1 for item in results
                    if item["url"] and read_body and not item.get("content"))
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
        parts.append(f"{lost_link}/{len(results)} 条没有有效微信直链")
    if lost_body:
        parts.append(f"{lost_body}/{len(results)} 条没取到正文")
    if parts:
        out["_partial_error"] = ("元宝给出了 %d 条，但%s（逐枚原因见各条 `_error`）："
                                 "这些条目不能当成完整的一轮用" % (len(results), "、".join(parts)))
    return out


def read_article(url: str) -> tuple[str, str]:
    """读取 mp.weixin.qq.com 文章正文纯文本。"""
    try:
        response = requests.get(url, headers=HEADERS_WX, timeout=TIMEOUT, verify=False)
    except requests.RequestException as exc:
        return "", str(exc)
    if response.status_code != 200:
        return "", f"HTTP {response.status_code}"
    if "appmsgcaptcha" in response.url:
        return "", ("微信验证码门（落地 wappoc_appmsgcaptcha，HTTP 200 非正文）："
                    "需登录态或人工过验证，浏览器同样被拦，降级用检索摘要")

    match = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*(?:<script|</div>)',
                      response.text, re.DOTALL)
    if not match:
        if "环境异常" in response.text or "验证" in response.text:
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = search_wechat(args.keyword, size=args.size, read_body=args.read)
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
