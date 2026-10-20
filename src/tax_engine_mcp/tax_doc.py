#!/usr/bin/env python3
"""全文下载、段落解析与官方解读检索。

引擎里这两件事写在 tax_server 的 Flask 路由层内，适配器要用就得连带把
Flask 拉进来。本副本只 import 随包携带的运行时取数脚本（tax_detail／
tax_so360／tax_shui5／tax_wechat），不 import tax_server，所以 CLI、API、
MCP 都拿得到全文与官方解读，又都不依赖 Flask。

本文件是逐项目副本：CLI、API、MCP 各自带一份，三份内容要求逐字节相同，
由各自的测试用 SHA-256 校验。缓存是本进程内的字典，三个进程各存各的。

本模块只做取数与解析，不生成解读、不调用外部模型。
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from tax_detail import download_bytes, _parse_docx_from_bytes
from tax_so360 import so360_search
from tax_shui5 import search_shui5
from tax_wechat import search_wechat


_text_cache = {}
_interp_cache = {}

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


TAX_PRACTICE_SOURCES = [
    {"name": "小颖言税", "query_hint": "小颖言税"},
    {"name": "税海涛声", "query_hint": "税海涛声"},
    {"name": "会计网",   "query_hint": "会计网"},
    {"name": "税小课",   "query_hint": "税小课服务"},
    {"name": "朴税",     "query_hint": "朴税"},
    # 这两个是 /api/web-related 实际会直连的源，不列进来的话，
    # 税屋和公众号的条目会被打成"实务解读"以外的泛标签。
    {"name": "税屋",     "query_hint": "shui5.cn"},
    {"name": "微信公众号", "query_hint": "mp.weixin.qq.com"},
]
# 官方站点：这些是政策原文来源，不能标成"实务解读"
OFFICIAL_DOMAINS = [
    "chinatax.gov.cn", "mof.gov.cn", "gov.cn", "npc.gov.cn",
    "chinatax.cn", "chinatax.gov",
]
# Sources that should be tagged as "实务解读" even if not from .gov.cn
# 不要用 "tax" 这种裸子串：chinatax.gov.cn 含 tax，会把官方站点误判成实务解读。
PRACTICE_DOMAIN_KEYWORDS = [
    "小颖言税", "税海涛声", "会计网", "税小课", "朴税",
    "mp.weixin.qq.com", "zhuanlan.zhihu.com", "toutiao.com",
    "shui5.cn", "shuiwu", "kuaiji", "chinaacc", "shuilishi", "shuikuai",
]


# ── Interpretation Search Engine ─────────────────────────────────────────

def _date_from_url(url: str) -> str:
    """从 URL 里的日期目录里取发布日期，取不到就返回空串。

    常见形态是 /201904/t20220313_xxx.html 或 /201904/816904caec....shtml。
    后者那串 32 位哈希紧跟在年份后面，直接正则抓 8 位数字会拼出 "2019-04-81"
    这种不存在的日期，所以抓到之后要按真实日历校验一遍。
    """
    m = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", url)
    if not m:
        return ""
    y, mo, d = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    try:
        date(y, mo, d)
    except ValueError:
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _search_one_source(site: str, query: str, n: int = 5) -> tuple[list, str]:
    """Search a specific site for policy interpretations, via 360 site: search.

    返回 (结果, 拦截说明)。说明非空表示这一路根本没取到数据，调用方不能把
    0 条当成"该站点没有解读"。

    Bing 在此处已被移除：www.bing.com 对 site: 查询返回 0 个结果块，
    cn/m.bing.com 虽偶发返回 10 个 <li class="b_algo">，但内容与查询无关
    （查 chinatax.gov.cn 企业所得税法 返回"元气壁纸"），属于不可信降级。
    """
    found = so360_search(query, site=site, size=n)
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
            "date": _date_from_url(item.get("url", "")),
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


def _is_practice_source(label_or_domain: str) -> bool:
    """Check if a source label matches known tax practice outlets."""
    s = (label_or_domain or "").lower()
    # 官方站点优先排除：chinatax.gov.cn 里含 "tax"，不先排除会被误判
    for dom in OFFICIAL_DOMAINS:
        if dom in s:
            return False
    for kw in PRACTICE_DOMAIN_KEYWORDS:
        if kw.lower() in s:
            return True
    return False


def _practice_source_name(domain: str, title: str) -> str:
    """Return the practice source name if the domain/title matches, else ''."""
    combined = f"{domain} {title}".lower()
    for src in TAX_PRACTICE_SOURCES:
        if src["query_hint"] in combined:
            return src["name"]
    return ""


def _search_practice_sources(query: str, n: int = 3) -> list[dict]:
    """Practical-interpretation sources: www.shui5.cn + WeChat public accounts.

    Both were previously routed through Bing site: search, which no longer
    returns usable results. They are now queried through their own modules.
    """
    results = []
    seen = set()

    for name, data in [("税屋", search_shui5(query, size=n)),
                       ("微信公众号", search_wechat(query, size=n))]:
        for item in (data or {}).get("results", []):
            href = item.get("url", "")
            if not href or href in seen or _is_garbage_result(href, item.get("title", "")):
                continue
            seen.add(href)
            results.append({
                "title": item.get("title", ""),
                "url": href,
                "date": item.get("date", ""),
                "source": name,
                "source_label": name,
                "snippet": (item.get("snippet") or item.get("content", ""))[:180],
            })
    return results


def search_interpretations(law_title: str, keyword: str = "",
                           sources: list[str] = None,
                           province: str = "") -> dict:
    """Search **official** policy interpretations ONLY from .gov.cn sources.
    Practice/public-account/web results belong in /api/web-related, NOT here."""
    cache_key = f"{law_title}|{keyword}|{'-'.join(sources or [])}|{province}"
    if cache_key in _interp_cache:
        return _interp_cache[cache_key]

    if sources is None:
        if province:
            sources = [f"{province}.chinatax.gov.cn", "chinatax.gov.cn", "www.gov.cn"]
        else:
            sources = ["chinatax.gov.cn", "fgk.chinatax.gov.cn", "mof.gov.cn", "www.gov.cn"]

    title_short = law_title.replace("中华人民共和国", "").strip()

    # 只搜官方站点的解读：每站两条问法，避免 4 站 × 4 问 = 16 次请求
    official_queries = [
        f"{title_short} 政策解读",
        f"{keyword} 官方解读" if keyword and keyword != law_title else f"{title_short} 解读",
    ]

    all_results = []
    seen_urls = set()
    blocked = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for site in sources:
            for q_text in official_queries[:3]:
                futures[(site, q_text)] = pool.submit(
                    _search_one_source, site, q_text, 4
                )

        for (site, q_text), future in futures.items():
            try:
                items, why = future.result(timeout=15)
                if why:
                    blocked.append(why)
                for item in items:
                    if isinstance(item, dict) and item.get("url"):
                        if item["url"] not in seen_urls:
                            seen_urls.add(item["url"])
                            all_results.append(item)
            except Exception:
                pass

    result = {
        "law_title": law_title,
        "keyword": keyword,
        "total": len(all_results),
        "sources": all_results[:15],
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 一条站点都没取到时必须说清是"被拦"还是"确实没有"：前端空态文案
    # "该法规可能暂无公开的政策解读文件"只有在搜索引擎正常工作时才成立。
    # 被拦的这种结果不进缓存，否则限流恢复后仍会一直回空。
    if not all_results and blocked:
        result["engine_error"] = blocked[0]
        result["queries_blocked"] = f"{len(blocked)}/{len(futures)}"
        return result

    _interp_cache[cache_key] = result
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
