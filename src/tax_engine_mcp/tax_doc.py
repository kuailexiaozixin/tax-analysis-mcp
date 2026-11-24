#!/usr/bin/env python3
"""全文下载、段落解析与官方解读检索。

引擎里这两件事写在 tax_server 的 Flask 路由层内，适配器要用就得连带把
Flask 拉进来。本副本只 import 随包携带的运行时取数脚本（tax_detail／
tax_sogou），不 import tax_server，所以 CLI、API、MCP 都拿得到全文与
官方解读，又都不依赖 Flask。

3.62.21 起本文件不再有第二份"实务解读源"清单：那份 TAX_PRACTICE_SOURCES
与它配套的检索函数是 tax_server 路由层的老抄本，全仓无人引用，而活的那份
（带每一源失败说明的通道）在 runtime/scripts/tax_related.py，由各仓
tax_service 的 web_related 走它。留着一份没人调用的旧抄本，等于给下一个
改这处的人准备一次无声的无效修复——它改了、生效的不是它。

本文件是逐项目副本：CLI、API、MCP 各自带一份，三份内容要求逐字节相同，
由各自的测试用 SHA-256 校验。全文段落缓存是本进程内的字典；解读复用取数层缓存。

本模块只做取数与解析，不生成解读、不调用外部模型。
"""
import re
import time

import tax_http
from tax_detail import download_bytes, _parse_docx_from_bytes
from tax_sogou import sogou_search


_text_cache = {}

# ── Content filter: domains/keywords that indicate non-tax garbage results ──
_BLOCKED_DOMAINS = [
    "game", "4399", "7k7k", "xiaoyouxi", "youxi", "yxdown", "3dmgame",
    "gamersky", "duowan", "17173", "52pk", "sina.com.cn/game",
    "bilibili.com/video", "douyin.com", "kuaishou.com", "weibo.com",
    "zhihu.com/question", "tieba.baidu.com", "baike.baidu.com/item",
]
_BLOCKED_TITLE_WORDS = [
    "游戏", "小游戏", "手游", "网游", "页游", "棋牌", "捕鱼",
    "娱乐", "八卦", "明星", "综艺", "电影", "电视剧",
    "体育", "彩票", "竞彩", "博彩",
    "小说", "漫画", "动漫",
]


def _is_garbage_result(url: str = "", title: str = "") -> bool:
    """Filter out clearly irrelevant/non-tax content."""
    lower_url = url.lower()
    for kw in _BLOCKED_DOMAINS:
        if kw in lower_url:
            return True
    lower_title = title.lower()
    for kw in _BLOCKED_TITLE_WORDS:
        if kw in lower_title:
            return True
    return False


# ── Interpretation Search Engine ─────────────────────────────────────────

def _search_one_source(site: str, query: str, n: int = 5) -> tuple[list, str]:
    """Search a specific site for policy interpretations, via Sogou site: search.

    返回 (结果, 拦截说明)。说明非空表示这一路根本没取到数据，调用方不能把
    0 条当成"该站点没有解读"。

    Bing 在此处已被移除：www.bing.com 对 site: 查询返回 0 个结果块，
    cn/m.bing.com 虽偶发返回 10 个 <li class="b_algo">，但内容与查询无关
    （查 chinatax.gov.cn 企业所得税法 返回"元气壁纸"），属于不可信降级。
    """
    found = sogou_search(query, site=site, size=n)
    if found.get("_error"):
        return [], found["_error"]

    results = []
    for item in found["results"]:
        title = item.get("title", "")
        if len(title) < 5 or _is_garbage_result(item.get("url", ""), title):
            continue
        results.append({
            "title": title,
            "url": item["url"],
            "date": item.get("date", ""),
            "source": site,
            "source_label": _source_label(site),
            "snippet": item.get("snippet", ""),
        })
    return results, ""


def _source_label(site: str) -> str:
    labels = {
        "fgk.chinatax.gov.cn": "税务法规库",
        "chinatax.gov.cn": "国家税务总局",
        "mof.gov.cn": "财政部",
        "npc.gov.cn": "全国人大",
        "gov.cn": "中国政府网",
    }
    for k, v in labels.items():
        if k in site:
            return v
    # Handle provincial subdomain: shanghai.chinatax.gov.cn → "上海税务"
    m = re.match(r"([a-z]+)\.chinatax\.gov\.cn", site)
    if m:
        province_map = {
            "beijing":"北京税务","shanghai":"上海税务","tianjin":"天津税务",
            "chongqing":"重庆税务","guangdong":"广东税务","shenzhen":"深圳税务",
            "zhejiang":"浙江税务","jiangsu":"江苏税务","shandong":"山东税务",
            "sichuan":"四川税务","hubei":"湖北税务","hunan":"湖南税务",
            "henan":"河南税务","hebei":"河北税务","fujian":"福建税务",
            "xiamen":"厦门税务","anhui":"安徽税务","liaoning":"辽宁税务",
            "dalian":"大连税务","jilin":"吉林税务","heilongjiang":"黑龙江税务",
            "jiangxi":"江西税务","shanxi":"山西税务","shaanxi":"陕西税务",
            "gansu":"甘肃税务","qinghai":"青海税务","yunnan":"云南税务",
            "guizhou":"贵州税务","guangxi":"广西税务","hainan":"海南税务",
            "neimenggu":"内蒙古税务","ningxia":"宁夏税务","xinjiang":"新疆税务",
            "xizang":"西藏税务","qingdao":"青岛税务","ningbo":"宁波税务",
        }
        return province_map.get(m.group(1), f"{m.group(1)}税务")
    return site


def search_interpretations(law_title: str, keyword: str = "",
                           sources: list[str] = None,
                           province: str = "") -> dict:
    """Search **official** policy interpretations ONLY from .gov.cn sources.
    Practice/public-account/web results belong in /api/web-related, NOT here."""
    if sources is None:
        if province:
            sources = [f"{province}.chinatax.gov.cn", "chinatax.gov.cn", "www.gov.cn"]
        else:
            sources = ["chinatax.gov.cn", "mof.gov.cn", "www.gov.cn"]
    sources = list(dict.fromkeys(sources))

    title_short = law_title.replace("中华人民共和国", "").strip()

    base_query = f"{title_short} 政策解读"
    alt_query = (f"{keyword} 官方解读" if keyword and keyword != law_title
                 else f"{title_short} 解读")
    search_plan = []
    for i, site in enumerate(sources):
        q_text = alt_query if i == 0 and alt_query != base_query else base_query
        search_plan.append((site, q_text))

    all_results = []
    seen_urls = set()
    blocked = []

    for site, q_text in search_plan:
        try:
            items, why = _search_one_source(site, q_text, 100)
            if why:
                blocked.append(why)
            for item in items:
                if isinstance(item, dict) and item.get("url"):
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        all_results.append(item)
        except Exception as exc:
            blocked.append(f"{site}: {tax_http.short_reason(exc)}")

    result = {
        "law_title": law_title,
        "keyword": keyword,
        "total": len(all_results),
        "sources": all_results[:15],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if blocked:
        result["engine_error"] = "; ".join(dict.fromkeys(blocked))
        result["queries_blocked"] = f"{len(blocked)}/{len(search_plan)}"
    return result


def download_and_extract(bbbs_id: str) -> list[str]:
    """Download DOCX from NPC and extract text paragraphs. Returns list of non-empty lines."""
    if bbbs_id in _text_cache:
        return _text_cache[bbbs_id]

    # 取下载地址与取文件都走 tax_detail 的同一套节流 + 串行闸：
    # 这两步各自裸调 tax_http 就是闸外发请求，网页端连点几次能和检索撞车。
    try:
        content = download_bytes(bbbs_id, "docx")
    except Exception:
        # 本层的契约是"取不到就当没有正文"：拿不到地址、上游非 200、网络异常
        # 都归为取不到，由调用方按"未能获取全文"呈现，不把底层异常抛到路由外。
        return []

    paragraphs = _parse_docx_from_bytes(content)
    if paragraphs:
        _text_cache[bbbs_id] = paragraphs
    return paragraphs
