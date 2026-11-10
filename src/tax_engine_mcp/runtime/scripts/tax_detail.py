#!/usr/bin/env python3
"""
Tax Policy Detail — fetch metadata and download law documents from NPC API.

Usage:
  python tax_detail.py --info <bbbs_id>
  python tax_detail.py --download <bbbs_id> [--format docx|pdf] [output_path]
  python tax_detail.py --preview <bbbs_id>
  python tax_detail.py --cache-stats / --cache-clear   # 详情缓存默认开，TTL 1h
  python tax_detail.py --info <bbbs_id> --no-cache     # 本次强制现拉，不读也不写

详情元数据默认缓存 1 小时（键前缀 detail）。DOCX/PDF 下载件不缓存，每次现下。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from io import BytesIO
import zipfile

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# NPC 串行闸与 tax_search 共用同一把跨进程锁：详情接口和检索接口打的是同一个
# 站，两边各持一把锁等于没锁。锁的实现见 tax_search.NpcSerialGate。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tax_http  # noqa: E402
from tax_search import npc_gate  # noqa: E402
from tax_cache import CacheManager  # noqa: E402
# 发请求统一走 tax_http：本模块原有的 3 处 requests.get 各写了一遍 timeout，
# 与 tax_search / tax_fgk 的写法也不一致。VERIFY_SSL 在这里再导出一次，
# 因为 tests/eval_answer.py 读的是 tax_detail.VERIFY_SSL。
from tax_http import VERIFY_SSL  # noqa: E402,F401

BASE_URL = "https://flk.npc.gov.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://flk.npc.gov.cn/",
    "Accept": "application/json, text/plain, */*",
}
SXX_MAP = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效"}


# ── 详情元数据缓存：交给共享实现，1h TTL，默认开 ───────────────────────────
# 缓存的实现只在 tax_cache.py 那一份，这里不另建一份。
# 默认开是有意的差异：详情接口比检索慢，且元数据变动频率低。
# 键带 "detail" 前缀，与 search / fgk 两个命名空间的键天然不撞；
# namespace 只用来划定 clear() / stats() 的作用范围：清检索缓存不会带走这里的详情
# 缓存，按目录清 *.json 才是越界。
DETAIL_CACHE_TTL = 3600
_detail_cache = CacheManager(enabled=True, namespace="detail")

# 详情接口与检索接口同一套限流策略，最小间隔与重试次数照抄 tax_search
_MIN_INTERVAL = 0.6
_last_request_at = 0.0


def _is_challenge_page(r) -> bool:
    """限流时对方回 HTTP 200 + 一份 HTML 挑战页（Please enable JavaScript）。"""
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "json" in ctype:
        return False
    head = r.text[:600].lower()
    return "please enable" in head or "<!doctype html" in head


def _request(url: str, max_retries: int = 4):
    """带节流与重试的 GET，覆盖 429、断连、5xx 与挑战页。"""
    global _last_request_at
    for attempt in range(max_retries):
        gap = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if gap > 0:
            time.sleep(gap)
        try:
            # 与检索接口共用同一把 NPC 串行闸（跨进程）
            with npc_gate:
                r = tax_http.get(url, headers=HEADERS, verify=VERIFY_SSL, timeout=15)
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
            continue
        finally:
            _last_request_at = time.monotonic()
        if r.status_code == 429 or r.status_code in {500, 502, 503}:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
        elif _is_challenge_page(r) and attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        return r
    return r


def fetch_detail(bbbs_id: str) -> dict:
    """Get law detail metadata from NPC API.

    响应里 ossWordPath / ossPdfPath 嵌在 data.ossFile 对象下，不在 data 顶层，
    按顶层取会静默拿到空串——下载功能因此报"No download URL"之外的问题却
    毫无提示。content 是单个节点对象而非数组，取 body 时要按节点展开。
    """
    cache_key = _detail_cache._key("detail", bbbs_id)
    cached = _detail_cache.get(cache_key, max_age=DETAIL_CACHE_TTL)
    if cached:
        # 留痕：命中时 fetched_at 是"第一次抓取的时刻"，不打标记会被当成刚刚抓的
        cached["_from_cache"] = True
        age = _detail_cache.age(cache_key)
        if age is not None:
            cached["_cache_age_s"] = round(age, 1)
        return cached

    url = f"{BASE_URL}/law-search/search/flfgDetails?bbbs={bbbs_id}"
    r = _request(url)
    try:
        data = r.json()
    except ValueError as e:
        # 限流时对方回 HTTP 200 + 一份 HTML 挑战页，.json() 报的是
        # "Expecting value: line 1 column 1"，看不出真实原因。
        raise RuntimeError(
            f"NPC 详情接口返回的不是 JSON（HTTP {r.status_code}，"
            f"Content-Type {r.headers.get('Content-Type')}，"
            f"{len(r.content)} 字节）：{r.text[:120]!r}；原异常 {e}") from e
    detail = data.get("data", data)
    oss = detail.get("ossFile") or {}

    result = {
        "id": bbbs_id,
        "title": detail.get("title", ""),
        "category": detail.get("flxz", ""),
        "publish_date": detail.get("gbrq", ""),
        "effective_date": detail.get("sxrq", ""),
        "status_code": detail.get("sxx", 0),
        "status": SXX_MAP.get(detail.get("sxx", 0), "未知"),
        "issuing_authority": detail.get("zdjgName", ""),
        "oss_files": {
            "docx": oss.get("ossWordPath", ""),
            "pdf": oss.get("ossPdfPath", ""),
        },
        "related": {
            "amendments": detail.get("xgwj", []),
            "interpretation": detail.get("lsyg", []),
            "drafts": detail.get("xgzl", []),
            "legal_basis": detail.get("flfg", []),
        },
        "content_tree": _flatten_content(detail.get("content")),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "_from_cache": False,
    }

    _detail_cache.set(cache_key, result)
    return result


def _flatten_content(node, out: Optional[list] = None) -> list:
    """把 content 节点树拍平成 [{level,title,content}...] 列表。

    接口返回的是单个根节点（带 children），不是数组。
    """
    if out is None:
        out = []
    if isinstance(node, list):
        for n in node:
            _flatten_content(n, out)
        return out
    if not isinstance(node, dict):
        return out
    title = node.get("title") or node.get("name") or ""
    text = node.get("content") or ""
    if title or text:
        out.append({
            "level": node.get("level", 0),
            "title": title,
            "content": text,
        })
    _flatten_content(node.get("children") or node.get("childList"), out)
    return out


def get_download_url(bbbs_id: str, fmt: str = "docx") -> Optional[str]:
    """Get a signed download URL for a law document.

    走 _request，不直接 tax_http.get：这个端点就在 flk.npc.gov.cn 上，与检索、
    详情接口同一套限流。绕过节流与串行闸就是闸外的一条缝——两个进程
    各拿一个下载地址都会打进同一个站。
    """
    url = f"{BASE_URL}/law-search/download/pc?format={fmt}&bbbs={bbbs_id}"
    r = _request(url)
    data = r.json()
    return data.get("data", {}).get("url")


def download_bytes(bbbs_id: str, fmt: str = "docx") -> bytes:
    """下载法规文件并返回字节，不落盘。

    下载环节也在串行闸内：评测逐题取依据时会连发几十次，而"评测时不要并行
    打 NPC"这条规则靠人记迟早会忘——放进闸里就不必记了。
    """
    dl_url = get_download_url(bbbs_id, fmt)
    if not dl_url:
        raise ValueError(f"No download URL returned for {bbbs_id}")
    with npc_gate:
        r = tax_http.get(dl_url, headers=HEADERS, verify=VERIFY_SSL, timeout=60)
    r.raise_for_status()
    return r.content


def download_file(bbbs_id: str, fmt: str = "docx", output_path: Optional[str] = None) -> str:
    """Download a law document and save to disk."""
    content = download_bytes(bbbs_id, fmt)

    detail = fetch_detail(bbbs_id)
    safe_title = re.sub(r"[^\w一-鿿]", "_", detail["title"])[:50]
    ext = "docx" if fmt == "docx" else "pdf"

    if not output_path:
        output_path = f"{safe_title}_{bbbs_id[:8]}.{ext}"

    with open(output_path, "wb") as f:
        f.write(content)

    return output_path


def _parse_docx_from_bytes(data: bytes) -> list[str]:
    """Parse DOCX bytes and return non-empty paragraph texts (stdlib, zero-dependency)."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for p in tree.findall(".//w:p", ns):
            texts = [t.text or "" for t in p.findall(".//w:t", ns)]
            para = "".join(texts).strip()
            if para:
                paragraphs.append(para)
        return paragraphs
    except Exception:
        return []


def extract_text_from_docx(filepath: str) -> list:
    """Extract paragraphs from a DOCX file using stdlib (ZIP + XML)."""
    with open(filepath, "rb") as f:
        return _parse_docx_from_bytes(f.read())


_CN_DATE = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
_CN_DATE_RE = re.compile(_CN_DATE)
_ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百千\d]+条")
# 只认「本法/本条例/本细则…自X日起施行」这一句式。末条里常紧跟一句
# 「《某暂行条例》同时废止」，那句里的日期说的是被废止的那份，不是本篇。
_EFFECTIVE_RE = re.compile(r"^第[一二三四五六七八九十百千\d]+条.*?本"
                           r"(?:法|条例|细则|规定|办法|决定)"
                           r"自\s*" + _CN_DATE + r"[^。]*施行")
_CAPTION_ACTIONS = ("公布", "通过", "修订", "修正")


def _iso_date(match) -> str:
    y, m, d = match.groups()[:3]
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _caption_of(paragraphs: list) -> str:
    """取第一条条文之前的题注段；没有题注时返回空串。

    判据是这一段自载「某日公布/通过/修订/修正」，不是"排在第一条前面"：
    目录、章名也在第一条之前，而《增值税法实施条例》那份正文根本没有题注，
    按位置取会把「第一章 总则」当成版本信息读。
    """
    for para in paragraphs:
        if _ARTICLE_RE.match(para):
            break
        for seg in re.split(r"[\s;；]+", para.strip("（）()")):
            # 用 search 不用 match：修订段的写法是「根据2026年8月8日《…》修订」，
            # 日期不在段首。段尾那个动作词已经把范围收住了。
            if seg.endswith(_CAPTION_ACTIONS) and _CN_DATE_RE.search(seg):
                return para
    return ""


def law_date_lineage(paragraphs: list, publish_date: str = "",
                     effective_date: str = "", body_error: str = "") -> dict:
    """从正文里读出版本沿革与施行日期，并与录入项逐项对照。

    Args:
        paragraphs: docx 解出的段落文本（`_parse_docx_from_bytes` 的产出）。
        publish_date / effective_date: `fetch_detail` 给的录入项日期，可为空。
        body_error: 正文没取到时的原因。给了它就照原因说，不去评价这份正文。

    Returns:
        {"caption","original_publish_date","amendments","effective_clause",
         "effective_clause_date","date_note"}。amendments 每项是
        {"date","kind","document"}，document 取题注里那对书名号内的名字，
        题注没写名字时留空串（2001 年那次征管法修订就是这种写法）。

    为什么要读这两处：NPC 的 content_tree 只有标题、content 恒空，日期只在
    下载的 docx 里。而录入项那两个日期与正文里的日期常常不相等，且多半不是
    哪个错了——实测《碳排放权交易管理暂行条例》录入公布 2026-08-08、施行
    2026-08-15，正文题注写「2024年1月25日国务院令第775号公布　根据2026年8月8日
    《国务院关于修改和废止部分行政法规的决定》修订」，末条写「自2024年5月1日起
    施行」：录入项跟的是最新修订版，末条那句跟的是原初版。《税收征管法》同理
    （录入 2015-04-24，末条 2001-05-01，题注列了三次修正加一次修订）。所以这里
    只把两处日期各自的出处摆出来，不判哪一处对，也不拿正文覆盖录入项。
    """
    pub = (publish_date or "").strip()
    eff = (effective_date or "").strip()
    caption = _caption_of(paragraphs or [])
    original, amendments = "", []
    if caption:
        for seg in re.split(r"[\s;；]+", caption.strip("（）()")):
            m = _CN_DATE_RE.search(seg)
            if not m or not seg.endswith(_CAPTION_ACTIONS):
                continue
            day = _iso_date(m)
            if seg.endswith(("修订", "修正")):
                name = re.search(r"《([^》]*)》", seg)
                amendments.append({"date": day, "kind": seg[-2:],
                                   "document": name.group(1) if name else ""})
            elif seg.endswith(("公布", "通过")) and not original:
                original = day
    clause, clause_date = "", ""
    for para in paragraphs or []:
        m = _EFFECTIVE_RE.match(para)
        if m:
            clause, clause_date = para, _iso_date(m)
    latest = max((a["date"] for a in amendments), default="")

    if body_error:
        note = f"正文没取到（{body_error}）：题注与末条施行句都读不到，日期只剩录入项这一处"
    elif not paragraphs:
        note = "正文段落没取到（下载件解不出文本）：题注与末条施行句都读不到，日期只剩录入项这一处"
    else:
        parts = []
        if amendments:
            parts.append(f"题注载明最近一次{amendments[-1]['kind']}在 {latest}"
                         + (f"（{amendments[-1]['document']}）"
                            if amendments[-1]["document"] else "")
                         + (f"；录入项公布日 {pub}、施行日 {eff or '空'} 跟的是这一版"
                            if pub else "；录入项未录公布日")
                         + (f"，正文施行句的 {clause_date} 是原初版的起日，两者不是同一次"
                            if clause_date else "，正文没读到施行句"))
        elif not caption:
            parts.append("正文未附题注，版本沿革无从核对；日期按录入项与正文施行句两处看")
        elif original and pub and original < pub:
            parts.append(f"题注最新日期 {original} 早于录入项公布日 {pub}，"
                         f"而题注没写修订：这一条收录的是哪一版要回公布件核对")
        if clause_date and not eff:
            parts.append(f"施行日期取正文自载的 {clause_date}（{clause[:12]}…），录入项未录")
        elif clause_date and eff and clause_date != eff and not amendments:
            # 有修订记录时上面那句已经把差异解释到底了，再追一句"核对是否指同一
            # 次施行"是把同一件事说两遍，还反过来要求读者去查刚给出的结论。
            parts.append(f"正文施行句写 {clause_date}、录入项施行日 {eff}，"
                         f"引用前核对两者是否指同一次施行")
        note = "；".join(parts)
    return {"caption": caption, "original_publish_date": original,
            "amendments": amendments, "effective_clause": clause,
            "effective_clause_date": clause_date, "date_note": note}


def preview_law(bbbs_id: str) -> dict:
    """Preview a law: 法条数、编号形态、前十条，外加正文里的日期沿革。

    正文只下一次、只在内存里解，不落盘再删：日期沿革（题注与末条施行句）与
    法条计数用的是同一份段落，分成 `--info` 加 `--download` 两趟等于把同一个
    docx 下两遍。下不到时把原因写进 date_note，不写成"这份正文没有题注"。
    """
    detail = fetch_detail(bbbs_id)
    if not detail["title"]:
        raise ValueError(f"Law not found: {bbbs_id}")

    paragraphs, body_error = [], ""
    try:
        paragraphs = _parse_docx_from_bytes(download_bytes(bbbs_id, "docx"))
    except Exception as e:
        body_error = f"{type(e).__name__}: {e}"

    chinese_nums = sum(1 for p in paragraphs if re.match(r"^第[一二三四五六七八九十百千]+条", p))
    arabic_nums = sum(1 for p in paragraphs if re.match(r"^第\d+条", p))
    numbering = "chinese" if chinese_nums > arabic_nums else "arabic"
    pattern = r"^第[一二三四五六七八九十百千]+条" if numbering == "chinese" else r"^第\d+条"
    articles = [p for p in paragraphs if re.match(pattern, p)]
    lineage = law_date_lineage(paragraphs, detail.get("publish_date") or "",
                               detail.get("effective_date") or "", body_error)

    out = {
        **detail,
        "article_count": len(articles),
        "numbering_pattern": numbering,
        "first_articles": articles[:10],
        "date_lineage": lineage,
        "date_note": lineage["date_note"],
    }
    if body_error:
        out["_body_error"] = body_error
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Fetch tax law detail and download documents")
    p.add_argument("--info", help="Fetch detail metadata for a law ID")
    p.add_argument("--download", help="Download a law document by ID")
    p.add_argument("--preview", help="Preview a law: title, article count, numbering")
    p.add_argument("--format", choices=["docx", "pdf"], default="docx")
    p.add_argument("--output", "-o", help="Output file path")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument("--no-cache", action="store_true",
                   help=f"本次不读也不写详情缓存（详情缓存默认开，TTL {DETAIL_CACHE_TTL}s）")
    p.add_argument("--cache-stats", action="store_true", help="查看详情缓存统计")
    p.add_argument("--cache-clear", action="store_true",
                   help="清空详情缓存（不动检索与法规库缓存）")

    args = p.parse_args()

    global _detail_cache
    if args.no_cache:
        _detail_cache = CacheManager(enabled=False, namespace="detail")

    if args.cache_stats:
        print(json.dumps({"cache": _detail_cache.stats()}, ensure_ascii=False, indent=2))
        return
    if args.cache_clear:
        print(f"已清理详情缓存 {_detail_cache.clear()} 条")
        return

    if args.info:
        detail = fetch_detail(args.info)
        if args.json:
            print(json.dumps(detail, ensure_ascii=False, indent=2))
        else:
            print(f"📋 {detail['title']}")
            if detail.get("_from_cache"):
                age = detail.get("_cache_age_s")
                print(f"   [详情缓存{' ' + str(int(age)) + 's 前' if age is not None else ''}]")
            print(f"   分类: {detail['category']}")
            print(f"   状态: [{detail['status']}]")
            print(f"   公布: {detail['publish_date']}  施行: {detail['effective_date']}")
            print(f"   发布机关: {detail['issuing_authority']}")
            print("   这两个日期是收录版本的录入项；正文题注与末条施行句里的日期"
                  "常与它不同（修订件尤其如此）。要一趟看清用 --preview，"
                  "它下正文时把版本沿革一起读出来，不必再 --download 一遍")

    elif args.download:
        path = download_file(args.download, args.format, args.output)
        print(f"✅ 已下载: {path}")

    elif args.preview:
        preview = preview_law(args.preview)
        if args.json:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
        else:
            print(f"📋 {preview['title']}")
            print(f"   状态: [{preview['status']}]")
            print(f"   法条数: {preview['article_count']} 条")
            print(f"   编号格式: {'中文数字' if preview['numbering_pattern'] == 'chinese' else '阿拉伯数字'}")
            lin = preview["date_lineage"]
            if lin["caption"]:
                print(f"   题注: {lin['caption']}")
                print(f"   原公布/通过: {lin['original_publish_date'] or '题注未写'}")
                for a in lin["amendments"]:
                    print(f"   {a['kind']}: {a['date']}"
                          + (f"　《{a['document']}》" if a["document"] else "　题注未写文件名"))
            if lin["effective_clause"]:
                print(f"   正文施行句: {lin['effective_clause']}")
            if preview.get("date_note"):
                print(f"   日期对照: {preview['date_note']}")
            if preview.get("first_articles"):
                print(f"   前 {min(5, len(preview['first_articles']))} 条:")
                for a in preview["first_articles"][:5]:
                    print(f"     {a[:100]}")

    else:
        p.print_help()


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出里的 emoji 与法规名会炸 UnicodeEncodeError
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
