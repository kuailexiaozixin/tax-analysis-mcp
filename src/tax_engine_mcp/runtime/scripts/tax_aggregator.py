#!/usr/bin/env python3
"""
Tax Policy Aggregator — concurrent multi-source search with dedup and ranking.

Data source priority (按权威性排序):
  1. NPC API (flk.npc.gov.cn)       — 法律、行政法规，权威性最高
  2. chinatax.gov.cn (search5)      — 税务总局公告、解读，覆盖 fgk 法规库
  3. 搜狗网页检索 (m.sogou.com)   — 省局子站、地方文件
  4. 税屋 (shui5.cn)                — 实务解读
  5. 微信公众号 (搜狗微信)          — 实务解读
  6. 中国税网 (ctaxnews.com.cn)     — 中国税务报社：政策转载、解读、税案通报

后四源默认开启：它们补的是前两个源（法规原文层）查不到的地方口径与实操层内容。
权威性低于法规原文，输出里以 _authority_rank 排在后面。

税屋的检索有两路（搜狗的 site:shui5.cn 与站内原生检索），正文要先过一次阿里云
WAF：默认这一路只取链接（不要凭据），要正文时由 tax_shui5 启本机浏览器过一次
挑战再连读。两路覆盖的是不同排序，一路 0 条不能读成另一路也 0 条。

中国税网是报社站，登的多是别人发的文件：这一路的条目进的是检索层，
被它登的那份文件才是依据，依据链里的 url 要写官方那一份（判据见
subskills/ctaxnews/ 与 SKILL ③ L4）。它服务端收发布日期区间，
所以和 NPC／税务总局一样走 DATE_SERVER_SOURCES，不靠本地补筛。

AnySearch 已移除：本机不存在其 CLI（~/.claude/skills/anysearch/scripts 下
没有 anysearch_cli.py），原实现永远静默返回空列表。

Usage:
  python tax_aggregator.py "增值税" --size 10
  python tax_aggregator.py "小微企业优惠" --size 10 --json
  python tax_aggregator.py "加计扣除" --sources npc,chinatax
  python tax_aggregator.py "资本化" --sources npc,chinatax,sogou,shui5,wechat,ctax
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Import sibling modules
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from tax_search import search_tax, check_iso_date, DATE_FLOOR, DATE_CEIL
from tax_web_search import search_chinatax, build_filters, label_scope_text
from tax_sogou import sogou_search
from tax_shui5 import search_shui5
from tax_wechat import search_wechat
from tax_ctax import search_ctax

# 权威性排名，数字越小越权威
SOURCE_RANK = {
    "npc": 0,
    "chinatax": 1,
    "sogou": 2,
    "shui5": 3,
    "wechat": 4,
    "ctax": 5,
}
DEFAULT_SOURCES = ["npc", "chinatax", "sogou", "shui5", "wechat", "ctax"]
SOURCE_LABELS = {
    "npc": "📜 NPC法规库",
    "chinatax": "🏛️ 国家税务总局",
    "sogou": "🔎 搜狗搜索",
    "shui5": "🏠 税屋(实务解读)",
    "wechat": "💬 微信公众号(实务解读)",
    "ctax": "📰 中国税网(报社转载与税案)",
}

# 每个源挂掉时"缺的到底是哪一层"，由程序说清楚：缺哪层、什么后果、以及不要用
# 其他源顶替。这件事只写在文档里就不算收口——读的人得自己把"某源 0 条"翻译成
# "哪一层没了、能不能拿别的源顶"。
SOURCE_IMPACT = {
    "npc": "全国性法律法规层（法律/行政法规/司法解释）缺失，没有别的源能覆盖它",
    "chinatax": "总局公告、部门规章与官方解读层缺失",
    "sogou": "地方口径层缺失，没有别的源能覆盖它",
    "shui5": "税屋的实务解读层缺失",
    "wechat": "微信公众号的实务解读层缺失",
    "ctax": "中国税网的报刊登载层缺失（税案通报与报刊视角的执行口径少这一路；"
            "这一路本来就不供法定依据，缺它不影响法规原文那三层）",
}

# 给说明文字用的平名（SOURCE_LABELS 带图标，抄进答案里会成噪声）。
SOURCE_PLAIN = {
    "npc": "NPC 法规库",
    "chinatax": "税务总局",
    "sogou": "搜狗搜索",
    "shui5": "税屋",
    "wechat": "微信公众号",
    "ctax": "中国税网",
}

# 日期区间这一维在六源里的作用方式不是一种，分界的依据是"该源有没有日期参数"：
#   DATE_SERVER_SOURCES — 接口自己收，三源各收哪一枚字段、字段叫什么，逐源登记在
#     下面的 DATE_SERVER_FIELDS，含义彼此不同。实测两路都真收窄：税务总局
#     2026-10-03「增值税」只给上界 2020-12-31 时 45→38 条、max 由 2025-12-25 收到
#     2020-07-30；中国税网 2026-10-19「增值税」全词 2,537 行起始、45,739 行截止、
#     成对取 2025 全年 1,405 行，起止两枚各自独立生效。
#   DATE_LOCAL_SOURCES — 三个网页源都没有日期参数，只能在取回的窗口内按条目
#     自带日期补筛。搜狗与税屋的搜狗通道都从结果引用行取日期，公众号取结果块
#     里的时间，三者都只有部分条目带日期，其余落进"无日期"那一档。
# 把这两路混成一句"已按日期收窄"，读者会把本地补筛读成源端收窄，进而把
# "该源这一轮 0 条"当成"该源在这个区间里没有文件"——这是要防的那一步。
DATE_SERVER_SOURCES = ("npc", "chinatax", "ctax")
DATE_LOCAL_SOURCES = ("sogou", "shui5", "wechat")
# 服务端那一档各自收的字段名。_date_scope_note 逐源点名靠这张表：加了源不登记
# 这里，回显就会出现"某源由接口自己收窄"却没说收哪一枚字段的那种半截话。
DATE_SERVER_FIELDS = {
    "npc": "公布日期 gbrq",
    "chinatax": "成文日期 cwrq",
    "ctax": "发布日期 startDate/endDate",
}
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _new_date_stat() -> dict:
    return {"got": 0, "in_range": 0, "out_of_range": 0, "no_date": 0}


def _date_scope_note(date_from: str, date_to: str, sources: list,
                     local_stats: dict, npc_note: str) -> str:
    """说清日期区间这一轮对各源分别是怎么生效的，逐句都只写这一轮真发生了的事。

    各源的补筛计数放 `_date_filter.local_window`（结构化，便于核对），这里只给
    判读规则：补筛是在窗口内做的，窗口外该源有没有区间内的文件并没有查。
    """
    lo = date_from or "不限"
    hi = date_to or "不限"
    server = [SOURCE_PLAIN[s] for s in DATE_SERVER_SOURCES if s in sources]
    local = [SOURCE_PLAIN[s] for s in DATE_LOCAL_SOURCES if s in sources]
    parts = []
    if server:
        fields = [f"{SOURCE_PLAIN[s]}收{DATE_SERVER_FIELDS[s]}"
                  for s in DATE_SERVER_SOURCES if s in sources]
        gap = ""
        if not (date_from and date_to):
            bits = []
            for s in DATE_SERVER_SOURCES:
                if s not in sources:
                    continue
                if s == "npc":
                    bits.append(f"NPC 那一路按 {DATE_FLOOR}／{DATE_CEIL} 补缺的那一端")
                else:
                    bits.append(f"{SOURCE_PLAIN[s]}那一路不发缺失的那一维")
            if bits:
                gap = "；" + "，".join(bits)
        parts.append(
            "、".join(server) + "由接口自己收窄（" + "，".join(fields) +
            (f"；{len(fields)} 个「日期」字段含义不同，同一区间在各侧圈到的条目"
             "本就不必相同" if len(fields) > 1 else "") + gap + "）")
    if local:
        parts.append(
            "、".join(local) + "这一路没有日期参数，是在本源本轮取回的条目窗口内"
            "按条目自带日期补筛（各源的取回／区间内／区间外剔除／无日期保留条数"
            "另列一行）。窗口外该源仍可能有区间内的文件，这几源"
            "补筛到 0 条不能读成「该源在这个区间里没有内容」；条目本身没带日期的"
            "按无法判定处理——已保留在结果里，但不算区间内条目，引用前要单独核对日期")
    if npc_note:
        parts.append("NPC 那一路另有情况：" + npc_note)
    rng = f"{lo} — {hi}"
    return f"日期区间 {rng}：" + "；".join(parts) + "。"


def _build_gaps(sources: list, errors: dict, source_summary: dict,
                 shui5_channels: dict | None = None) -> list:
    """把源级失败翻译成"缺了哪一层 + 怎么处理"，供上层照抄，不用自己推断。

    三条判定：
      1. 该源自己报了错（被拦、接口异常）→ 记一条缺口；
      2. 税屋有两头检索（搜狗的 site:shui5.cn 与站内原生检索，见
         `tax_shui5.search_shui5`）。只有搜狗那一路这一轮被拦或没走成时，
         "税屋 0 条"才可能是被搜狗连带的，标 blocked_by；
      3. 搜狗跑通了、站内也跑通了，各 0 条就不能再甩给搜狗——那是两条通道
         各自这一轮没命中，归到税屋自己身上。没有 `_channels` 回报时不做推断：
         那一轮到底走没走搜狗没有证据，按别的源（如聚合器自己的 sogou）的
         失败去猜，会把两次独立请求混成一件事。
    """
    gaps = []
    for src in sources:
        if src in errors:
            gaps.append({
                "source": src,
                "label": SOURCE_LABELS.get(src, src),
                "reason": errors[src],
                "impact": SOURCE_IMPACT.get(src, "该源本次没有取到内容"),
                "do_not_substitute": True,
            })

    ch = shui5_channels or {}
    sogou_status = ch.get("搜狗", "")
    native_status = ch.get("站内", "")
    # "命中/0 条" 才算这一路真跑过；"没走成/未走" 都还是没走到
    native_ran = native_status.startswith(("命中", "0 条"))
    sogou_failed = sogou_status.startswith(("被拦", "没走成"))
    sogou_word = "被拦" if sogou_status.startswith("被拦") else "没走成"
    if (sogou_failed and "shui5" in sources
            and not source_summary.get("shui5")
            and not any(g["source"] == "shui5" for g in gaps)):
        if native_ran:
            gaps.append({
                "source": "shui5",
                "label": SOURCE_LABELS.get("shui5", "shui5"),
                "reason": f"搜狗{sogou_word}，站内原生检索这一轮也没命中（{native_status}）",
                "impact": "两条通道各按自己的排序取数，这一轮各 0 条只说明没取到，"
                          "不代表税屋没有这个主题的解读",
                "do_not_substitute": True,
            })
        else:
            gaps.append({
                "source": "shui5",
                "label": SOURCE_LABELS.get("shui5", "shui5"),
                "reason": f"搜狗{sogou_word}，而这一轮税屋只走了搜狗的 site:shui5.cn 那一路"
                          f"（站内那一路：{native_status or '本轮未走'}）",
                "impact": "税屋这一层的空结果是被搜狗连带的，不代表税屋没有内容",
                "blocked_by": "sogou",
                "do_not_substitute": True,
            })
    return gaps


def _degraded_note(sources: list, gaps: list, source_summary: dict) -> str:
    """把缺口拼成一句可以直接抄进答案的话；没有缺口时给空串。

    这段话是给"答案里必须明说该层缺失"这条要求用的：程序给成句，照抄即可，
    不必读的人自己组织措辞、也不必自己判断哪层算缺。
    """
    if not gaps:
        return ""
    hit = sum(1 for s in sources if source_summary.get(s))
    missing = "、".join(g["label"] for g in gaps)
    return (f"本次多源检索 {hit}/{len(sources)} 个源有命中，缺失：{missing}。"
            "缺失只说明这次没取到，不代表该层没有对应内容；"
            "不要用其他源的条目顶替缺失层，答案里要明说缺的是哪一层。")


def _jaccard_similarity(s1: str, s2: str) -> float:
    """Simple Jaccard similarity on character trigrams for title dedup."""
    if not s1 or not s2:
        return 0.0

    def trigrams(s):
        s = s.lower().strip()
        return {s[i:i+3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}

    t1 = trigrams(s1)
    t2 = trigrams(s2)
    if not t1 or not t2:
        return 0.0

    intersection = len(t1 & t2)
    union = len(t1 | t2)
    return intersection / union if union > 0 else 0.0


def deduplicate(results: list, threshold: float = 0.7) -> list:
    """Remove near-duplicate results based on title similarity."""
    deduped = []
    for item in results:
        title = item.get("title", "")
        is_dup = False
        for existing in deduped:
            sim = _jaccard_similarity(title, existing.get("title", ""))
            if sim >= threshold:
                is_dup = True
                break
        if not is_dup:
            deduped.append(item)
    return deduped


def aggregate_search(keyword: str, *,
                     size: int = 10,
                     sources: list = None,
                     status: int = 3,
                     scope: str = "title",
                     exact: bool = False,
                     sort: str = "relevance",
                     date_from: str = None,
                     date_to: str = None,
                     file_only: bool = True) -> dict:
    """
    Concurrently search multiple data sources and return deduplicated, ranked results.

    Args:
        keyword: search term
        size: results per source
        sources: 见 DEFAULT_SOURCES；默认六源全开
        status: NPC status filter (default: 3 = effective)
        scope: NPC search scope (default: title；fulltext 已按相关度排序但可能偏题)
        exact: NPC 精确检索。检索词是本体法名时必须为 True，否则模糊检索
               按发布时间排，宪法会顶掉本该在首位的本体法。
        sort: "relevance"（默认）按权威度分层；"date" 时跨源按公布日期降序，
              不再按权威度分层——"只看最新"要的正是时间序，把 NPC 整源顶在
              前面会让总局 2026 年的公告排在 NPC 2024 年的法律之后。
        date_from / date_to: YYYY-MM-DD 日期区间，任一端可缺。生效方式分两档，
              见 _date_scope_note：npc、chinatax、ctax 由接口自己收（公布日期／
              成文日期／发布日期，三种含义都不同），sogou、shui5、wechat 没有
              日期参数，只能在本源取回的 size 条窗口内按条目日期补筛，计数写
              `_date_filter.local_window`，判读规则写 `_date_note`。没带日期的
              条目留在结果里并计入 no_date，不静默丢。
        file_only: 只落在税务总局那一路（search_chinatax 的同名参数），默认 True
              把这一路限定在十个文件类标签内。规则主体在境外辖区的专题（支柱二、
              全球最低税）文件类标签下实测 0 条，要传 False 连动态与境外立法编译
              一起取；其余五路（npc、sogou、shui5、wechat、ctax）没有这个维度，
              不受影响。

    Raises:
        ValueError: date_from/date_to 不是补零的 YYYY-MM-DD 真实日期。
    """
    if sources is None:
        sources = list(DEFAULT_SOURCES)

    date_from = check_iso_date(date_from, "date_from")
    date_to = check_iso_date(date_to, "date_to")
    use_date = bool(date_from or date_to)
    # 税务总局那一路收的收窄维度就是 build_filters 产出的日期区间（它没有 NPC
    # 那种 status/scope 参数）；这一层不往下传，界面上的日期控件在聚合这条路径
    # 上就是空转（2026-10-02 实测：带与不带日期的 12 条结果一模一样，仍含
    # 2019-11-27 那份）。build_filters 同时兼任校验。中国税网那一路不走这张表：起止两枚
    # 作为 startDate/endDate 直接下推，见下面 futures["ctax"]。
    date_filters = build_filters(cwrq_from=date_from, cwrq_to=date_to)

    results = {}
    errors = {}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}

        if "npc" in sources:
            # sort 要传到 NPC 这一路：不传的话取回的窗口是按接口自己那套序排的，
            # 后面只在窗口内按日期重排，"最新"的那批根本没能进窗口。
            futures["npc"] = pool.submit(
                search_tax, keyword, scope=scope, status=status, size=size,
                search_type=1 if exact else 2, sort=sort,
                date_from=date_from, date_to=date_to,
            )
        if "chinatax" in sources:
            futures["chinatax"] = pool.submit(
                search_chinatax, keyword, size=size, filters=date_filters,
                file_only=file_only
            )
        if "sogou" in sources:
            # 全网检索，再由调用方按需收窄站点；此处不带 site:
            futures["sogou"] = pool.submit(
                sogou_search, keyword, site="", size=size
            )
        if "shui5" in sources:
            futures["shui5"] = pool.submit(
                search_shui5, keyword, min(size, 5)
            )
        if "wechat" in sources:
            futures["wechat"] = pool.submit(
                search_wechat, keyword, min(size, 5)
            )
        if "ctax" in sources:
            # 服务端收 startDate/endDate（发布日期），所以日期直接下推，不靠本地补筛；
            # match 用 title 以外的默认全文，size 给的是合并后的篇数。
            futures["ctax"] = pool.submit(
                search_ctax, keyword, size=min(size, 5),
                date_from=date_from, date_to=date_to,
            )

        for source, future in futures.items():
            try:
                results[source] = future.result(timeout=30)
            except Exception as e:
                errors[source] = str(e)
                results[source] = None

    # Collect all results，按权威性排序
    all_items = []
    local_stats = {}
    for source, data in results.items():
        if not data:
            continue
        # 源自己回报"取不到"（被拦、接口异常）时要进 errors：
        # source_summary 里的 0 条只说明没拿到，不说明该源没有内容。
        if data.get("_error"):
            errors[source] = data["_error"]
        rank = SOURCE_RANK[source]
        source_rel = data.get("_reliability")
        source_note = data.get("_reliability_note", "")
        local_narrow = use_date and source in DATE_LOCAL_SOURCES
        for item in data.get("results", []):
            item["_source"] = source
            item["_authority_rank"] = rank
            # 各源日期字段不统一，统一到 publish_date 供排序用
            if item.get("date") and not item.get("publish_date"):
                item["publish_date"] = item["date"]
            # 响应级标记说的是"这一整窗条目共同的取回方式"（NPC 正文检索就是全文
            # 分词命中），逐条带上不算冤枉某一条。说明文字必须跟着一起带：只带档位
            # 的话，界面与定级层就退回到"凭档位猜原因"。
            # 本层六路源里没有会自带条目级标记的那一路（深页标记出自 tax_fgk，
            # 只在 tax_answer 的分轮取数里出现），所以这里不存在两种标记撞车的情况。
            if source_rel:
                item["_reliability"] = source_rel
                if source_note:
                    item["_reliability_note"] = source_note
            if local_narrow:
                st = local_stats.setdefault(source, _new_date_stat())
                st["got"] += 1
                d = (item.get("publish_date") or "")[:10]
                if not _ISO_DATE_RE.match(d):
                    # 日期缺失不能证明条目在区间外，保留并计入未标日期项。
                    st["no_date"] += 1
                elif (date_from and d < date_from) or (date_to and d > date_to):
                    st["out_of_range"] += 1
                    continue
                else:
                    st["in_range"] += 1
            all_items.append(item)

    # Deduplicate across sources
    all_items = deduplicate(all_items)

    # Sort: authority rank first, then by date
    if sort == "date":
        all_items.sort(key=lambda x: x.get("publish_date") or "", reverse=True)
    else:
        all_items.sort(key=lambda x: (
            x.get("_authority_rank", 99),
            # Put items with dates before those without
            0 if x.get("publish_date") else 1,
        ))

    source_summary = {
        s: len(results[s].get("results", [])) if results.get(s) else 0
        for s in sources
    }
    gaps = _build_gaps(sources, errors, source_summary,
                       (results.get("shui5") or {}).get("_channels"))
    out = {
        "keyword": keyword,
        "total_sources": len(sources),
        "total_items": len(all_items),
        "items": all_items[:size * 3],  # Cap total results
        # source_summary 是各源"取回"的条数，不含本源窗口内的日期补筛；补筛掉
        # 多少看 _date_filter.local_window，两个数不是一个口径，别互相核对。
        "source_summary": source_summary,
        "errors": errors,
        # 缺了哪一层、什么后果、要不要拿别的源顶 → 由程序说，不靠读者推断
        "gaps": gaps,
        "degraded_note": _degraded_note(sources, gaps, source_summary),
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if use_date:
        out["_date_filter"] = {
            "from": date_from,
            "to": date_to,
            "server_side": [s for s in DATE_SERVER_SOURCES if s in sources],
            "local_window": {s: local_stats.get(s, _new_date_stat())
                             for s in sources if s in DATE_LOCAL_SOURCES},
        }
        out["_date_note"] = _date_scope_note(
            date_from, date_to, sources, local_stats,
            (results.get("npc") or {}).get("_date_note", ""))
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Multi-source tax policy search aggregator",
        epilog="""
Examples:
  python tax_aggregator.py "增值税" --size 10
  python tax_aggregator.py "加计扣除" --sources npc,chinatax --json
        """
    )
    p.add_argument("keyword", help="Search keyword")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                   help=f"Comma-separated source names (default: {','.join(DEFAULT_SOURCES)})")
    p.add_argument("--status", type=int, default=3,
                   help="NPC status filter (3=effective)")
    p.add_argument("--scope", choices=["title", "fulltext"], default="title",
                   help="NPC search scope (default: title)")
    p.add_argument("--sort", choices=["relevance", "date"], default="relevance",
                   help="relevance=按权威度分层；date=跨源按公布日期降序（不再分层）")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                   help="日期区间下界。npc 按公布日期、chinatax 按成文日期由接口收窄；"
                        "sogou/shui5/wechat 没有日期参数，只在本源取回的窗口内补筛")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                   help="日期区间上界，生效方式同 --from")
    p.add_argument("--all-labels", action="store_true",
                   help="税务总局那一路关掉文件类标签，连新闻、视频、各地动态一起搜。"
                        "规则主体在境外辖区的专题（支柱二）要用它，否则这一路 0 条")
    p.add_argument("--json", action="store_true")

    args = p.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    try:
        result = aggregate_search(
            args.keyword,
            size=args.size,
            sources=sources,
            status=args.status,
            scope=args.scope,
            sort=args.sort,
            date_from=args.date_from,
            date_to=args.date_to,
            file_only=not args.all_labels,
        )
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"🔍 多源搜索 \"{args.keyword}\" | {result['searched_at']}")
    print(f"   数据源: {', '.join(sources)}")
    if "chinatax" in sources:
        # 0 条有两种来处：该源这一层真没有，和搜错了层。不印出层名，这两种在
        # 屏幕上长得一模一样。
        print(f"   税务总局检索范围: {label_scope_text(not args.all_labels)}")
    for src in sources:
        print(f"   {SOURCE_LABELS.get(src, src)}: {result['source_summary'].get(src, 0)} 条")
    if result.get("_date_note"):
        df = result["_date_filter"]
        # 各源取回多少上面那行已经印过，这里只补上面没有的那个口径：窗口内补筛
        # 之后区间内/外/无日期各多少。0 条的源不补这行，免得和上面的"0 条"重复。
        for src, st in df["local_window"].items():
            if st["got"]:
                print(f"     日期补筛 {SOURCE_PLAIN.get(src, src)}：取回 {st['got']} 条，"
                      f"区间内 {st['in_range']} 条，区间外剔除 {st['out_of_range']} 条，"
                      f"无日期保留 {st['no_date']} 条")
        print(f"   ⚠️ {result['_date_note']}")
    if result.get("errors"):
        for src, err in result["errors"].items():
            print(f"   ⚠️ {src}: {err}")
    flagged = [i for i in result.get("items", []) if i.get("_reliability")]
    if flagged:
        # 按每条自带的提醒原文分组，不按档位分组：同一个 medium 在 NPC 正文检索
        # 和总局法规库深页说的是两件不同的事，按档位配一句固定话就有一句是假的。
        # 按档位写死一句"不得作为权威依据引用"是用禁令代替核对——读者既
        # 不知道存疑在哪一处，也不知道要核对什么。
        by_note = {}
        for i in flagged:
            key = (i.get("_reliability_note")
                   or f"来源带 {i['_reliability']} 档标记，但未写明存疑在哪一处——"
                      f"引用前先核对这条到底有没有规定本题这件事")
            by_note[key] = by_note.get(key, 0) + 1
        for note, n in by_note.items():
            print(f"   ⚠️ {n} 条结果带来源存疑标记：{note}")
    if result.get("gaps"):
        print(f"   ⚠️ 缺失的层 {len(result['gaps'])} 处（答案里要明说，不要用其他源顶替）：")
        for g in result["gaps"]:
            by = f"［被 {g['blocked_by']} 连带］" if g.get("blocked_by") else ""
            print(f"      - {g['label']}{by}：{g['impact']}")
        print(f"      {result['degraded_note']}")
    print()

    for item in result.get("items", [])[:20]:
        label = SOURCE_LABELS.get(item.get("_source", ""), "")
        flag = f"  [_reliability: {item['_reliability']}]" if item.get("_reliability") else ""
        print(f"  {label} {item.get('title', '')[:80]}{flag}")
        if item.get("publish_date"):
            print(f"     日期: {item['publish_date']}")
        if item.get("document_number"):
            print(f"     文号: {item['document_number']}")
        if item.get("id"):
            print(f"     NPC ID: {item['id']}")
        if item.get("url"):
            print(f"     {item['url']}")
        print()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    main()
