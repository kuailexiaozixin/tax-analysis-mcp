#!/usr/bin/env python3
"""
分析编排 — 把判型、检索、定级串成一次调用，产出结构化的分析底稿。

它不是自动答税题的工具，而是把"该按什么顺序查、查回来的依据哪条能当主
依据、哪些结论条件缺失"这件事固化成代码。上层模型拿着 analyze() 的返回
按 needs 清单组织文字答案，不需要重新判断该查什么。

为什么要编排层：单看各检索脚本，它们都只知道自己那一个源。问题在于
"一道题要几轮检索、每轮用什么源"这件事，取决于问题类型而不是源本身。
同是企业所得税法，lookup 类一轮标题检索就够，entitlement 类至少要三轮
（本体法 → 总局优惠文件 → 税屋实操口径），option_judge 类还得逐条比对
所以每一轮都要取全文。这个顺序住在编排层里，不该让每次调用重新想。

三段产物：
  plan    该查什么、查几轮、每轮用什么源（不联网，纯判定）
  gather  真去检索并定级（联网，按 plan 执行）
  answer  在 gather 基础上把依据分层、标出限制条件（不联网）

Usage:
  python tax_answer.py "研发费用加计扣除比例是多少" --plan
  python tax_answer.py "研发费用加计扣除比例是多少" --gather --at 2026-09-27
  python tax_answer.py "研发费用加计扣除比例是多少" --answer --json
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tax_analyze as A
import tax_cited as CITED
import tax_evidence as E
import tax_fgk as FGK
import tax_search as T
import tax_shui5 as S5
import tax_so360 as S360
import tax_terms as TT
import tax_web_search as W
import tax_wechat as WX

# 文号→官方链接缓存的路径覆盖（None 走默认 ~/.cache/.../cited_links.json）。
# 存在只是为了让离线用例把缓存指到临时文件，不把真实 home 写脏。
CITED_CACHE_PATH = None

# 每类问题要走哪几轮检索。轮次编号从 1 起，ordered 类必须按序。
# 每轮给 sources 是"该轮优先用的源"，不是"只能用这些源"。
PLAN_TEMPLATES = {
    "lookup": [
        {"round": 1, "goal": "取本体法现行条文", "sources": ["npc"],
         "call": "search_tax(关键词, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "确认有无更新或配套文件", "sources": ["fgk"],
         "call": "search_fgk(关键词, size=8)"},
    ],
    "option_judge": [
        {"round": 1, "goal": "取题干所指法律的完整条文", "sources": ["npc"],
         "call": "search_tax(关键词, scope='title', status=3, size=20) + fetch_detail"},
        {"round": 2, "goal": "逐条比对所需的配套规定", "sources": ["npc", "fgk"],
         "call": "search_fgk(关键词, size=8)"},
        {"round": 3, "goal": "对争议选项找实务口径", "sources": ["shui5"],
         "call": "search_shui5(关键词, size=3, read_body=True)"},
    ],
    "entitlement": [
        {"round": 1, "goal": "优惠本体法与享受条件", "sources": ["npc"],
         "call": "search_tax(优惠名, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "资格判定与备案留存要求", "sources": ["fgk"],
         "call": "search_fgk(优惠名, size=8)"},
        {"round": 3, "goal": "实务口径：卡在条件上怎么认定", "sources": ["shui5", "wechat"],
         "call": "search_shui5(优惠名+条件, size=3, read_body=True)"},
    ],
    "liability": [
        {"round": 1, "goal": "计税依据与税率", "sources": ["npc"],
         "call": "search_tax(税种, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "减免、加计、起征点等调整项", "sources": ["fgk"],
         "call": "search_fgk(税种+优惠, size=8)"},
        {"round": 3, "goal": "计算口径与常见调整", "sources": ["shui5"],
         "call": "search_shui5(税种+计算, size=3, read_body=True)"},
    ],
    "procedure": [
        {"round": 1, "goal": "征管法与办税依据", "sources": ["npc"],
         "call": "search_tax(税收征收管理法, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "办税指南与操作口径", "sources": ["fgk"],
         "call": "search_fgk(事项+办理, size=8)"},
        {"round": 3, "goal": "实操步骤与时限", "sources": ["shui5"],
         "call": "search_shui5(事项+怎么申报, size=3, read_body=True)"},
    ],
    "risk": [
        {"round": 1, "goal": "行为定性与罚则", "sources": ["npc"],
         "call": "search_tax(行为+税种, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "处罚依据与裁量口径", "sources": ["fgk"],
         "call": "search_fgk(行为+处罚, size=8)"},
        {"round": 3, "goal": "稽查口径与合规整改", "sources": ["shui5"],
         "call": "search_shui5(行为+风险, size=3, read_body=True)"},
    ],
    "treatment": [
        {"round": 1, "goal": "税目定义与列举", "sources": ["npc"],
         "call": "search_tax(事项, scope='title', status=3, size=20)"},
        {"round": 2, "goal": "视同销售、免税列举等配套规定", "sources": ["fgk"],
         "call": "search_fgk(事项, size=8)"},
        {"round": 3, "goal": "实操认定口径", "sources": ["shui5"],
         "call": "search_shui5(事项+怎么认定, size=3, read_body=True)"},
    ],
    "fill_blank": [
        {"round": 1, "goal": "取该法全文，按定义条款定位", "sources": ["npc"],
         "call": "search_tax(本体法, scope='title', search_type=1, status=3) + fetch_detail"},
        {"round": 2, "goal": "确认该术语的现行表述有无修订", "sources": ["fgk"],
         "call": "search_fgk(术语, size=8)"},
    ],
    "compare": [
        {"round": 1, "goal": "各方案各自的法律依据", "sources": ["npc"],
         "call": "search_tax(方案A, ...) 与 search_tax(方案B, ...) 各一轮"},
        {"round": 2, "goal": "适用条件与临界值", "sources": ["fgk"],
         "call": "search_fgk(方案, size=8)"},
        {"round": 3, "goal": "实务选择建议", "sources": ["shui5"],
         "call": "search_shui5(方案+怎么选, size=3, read_body=True)"},
    ],
}

# 每轮每源取多少条。option_judge 要逐条比对所以取多，其余按够用即可。
ROUND_FETCH = {
    "option_judge": {"npc": 20, "fgk": 8, "shui5": 3, "wechat": 3},
    "default": {"npc": 20, "fgk": 8, "shui5": 3, "wechat": 3},
}


def build_plan(question: str) -> dict:
    """只做判定与编排，不联网。

    Returns:
        tax_analyze.analyze 的返回 + {"rounds":[...]} + {"probes":[...]}。
    """
    a = A.analyze(question)
    tname = a["type"]["type"]
    tpl = [dict(r) for r in PLAN_TEMPLATES.get(tname, PLAN_TEMPLATES["lookup"])]
    _reroute_first_round(tpl, question)
    if a.get("legislative_stage"):
        _retarget_for_legislation(tpl)
    a["rounds"] = tpl
    # overseas 专题那句限制住在专题项上，plan 带出去给命令行、服务端与模型共用
    a["overseas_note"] = (T.resolve_tax_type(question) or {}).get("note", "")
    return a


def _retarget_for_legislation(rounds: list) -> None:
    """题面问的是草案或征求意见稿时，把轮次目标改成"取修订基线"，再补一轮立法取证。

    这一条是实测逼出来的：同一份《税收征管法》修订草案的提问，轮次若写成
    "取本体法现行条文""确认有无更新或配套文件"，跑完取回的是 2015 年修正的现行
    文本，看起来答完了，实际一句草案内容都没碰——搜索策略按"规定是什么"排，
    而题面问的是"正在审的那份文本改了什么"。立法过程件不在六个税收源里
    （见 tax_analyze.legislative_note），所以取证的落点放在人大网站内检索。
    """
    for rnd in rounds:
        rnd["goal"] = rnd["goal"] + "（作修订基线对照，不是草案内容）"
    rounds.append({
        "round": len(rounds) + 1,
        "goal": "取立法过程件与草案解读：人大网草案/审议公告，及税屋、公众号的逐条解读",
        # 草案正文不在六个税收源里，但草案解读文章在税屋与公众号：实测
        # search_shui5("税收征管法 修订草案") 首条即国务院法制办征求意见稿通知，
        # search_wechat 同名次全是"修订草案解读"。只查人大网等于漏掉这一层。
        "sources": ["legis", "shui5", "wechat"],
        "term_key": "legis",
        "call": "so360_search(草案词, site='npc.gov.cn') + "
                "search_shui5 / search_wechat(同一草案词)",
    })


def _overseas_rounds(rounds: list, info: dict) -> None:
    """把 overseas 专题（支柱二与全球最低税）的第 1 轮改到全站层，再补一轮境内相邻专题。

    两条都不沿用现有模板，各对应一处实测的空转：
      · 沿用 sta：第 1 轮走法规库的文件类标签，「支柱二」「全球最低税」「GloBE」
        各 0 条，整轮取回空清单。
      · 沿用 npc：按原话去 NPC 标题检索，"支柱二对跨国企业有什么影响"取回的是
        《企业破产法》《合伙企业法》《中小企业促进法》——命中靠的是"企业"两个字，
        这些无关法律会占住【主依据】那一栏。
    全站层取回的是境外辖区的立法进展，境内这一侧的规定不在那里，所以按专题项上
    的 `adjacent` 补一轮法规库，用的是那个相邻专题自己实测过的检索词。
    """
    first = rounds[0]
    first["sources"] = ["whole_site", "shui5"]
    first["call"] = ("search_chinatax(专题检索词, size=8, file_only=False) + "
                     "search_shui5(同一词)")
    first["goal"] = (f"{first['goal']}（这一类在中国官方法规库里没有对应文件，"
                     "本轮取的是税务总局网站全站层的境外辖区动态与实务解读）")
    adjacent = info.get("adjacent") or ""
    term = T.adjacent_search_term(info)
    if len(rounds) > 1 and rounds[1].get("sources") == ["fgk"]:
        # 模板把这一轮写成"确认有无更新或配套文件"，对这一类专题它实际承担的是
        # 另一件事：境内法规库到底有没有就这件事发的文件。这一轮用专题检索词
        # （`search_terms` 的 fgk 一路），实测 0 条命中——那个 0 是答案的一部分，
        # 所以把轮次目标写明白，免得界面只留一句"取数失败"让人以为再补取能有东西。
        rounds[1]["goal"] = (f"查境内法规库有没有就「{info.get('search_term') or ''}」"
                             f"这一件事发的文件")
    if term:
        rounds.append({
            "round": len(rounds) + 1,
            "goal": f"境内这一侧的相邻专题「{adjacent}」现行怎么规定",
            "sources": ["fgk"],
            "call": f"search_fgk({term}, size=8)",
            "term_key": "adjacent_fgk",
        })


def _reroute_first_round(rounds: list, question: str) -> None:
    """第 1 轮按专题的 authority 换源。

    模板把第 1 轮都写成 npc，但 sta 专题（转让定价、税收优惠等）在 NPC 库里
    检索无效——搜"转让定价"命中 10 条全是土地和矿产资源转让条例。这类题第 1 轮
    就该查总局法规库，否则取回的全是无关法规，还要多花一轮才发现。
    overseas 专题（支柱二与全球最低税）走的是另一条，见 `_overseas_rounds`。
    """
    info = T.resolve_tax_type(question) or {}
    if info.get("authority") == "overseas":
        _overseas_rounds(rounds, info)
        return
    if info.get("authority") != "sta":
        return
    first = rounds[0]
    if "npc" not in first["sources"]:
        return
    # 总局源已在轮内就只调顺序，不重复排
    first["sources"] = (["fgk"] + [s for s in first["sources"] if s != "fgk"]
                        if "fgk" in first["sources"]
                        else ["fgk", "npc"])
    first["call"] = first["call"].replace("search_tax(", "search_fgk(")
    first["goal"] = f"{first['goal']}（该专题在 NPC 库检索无效，改查总局）"


# ── 执行 ───────────────────────────────────────────────────────────────────
def _npc_round(term: str, size: int) -> tuple[list, str]:
    # 本体法名已知时走精确检索。模糊检索按相关度排，宪法类文本会因为
    # "法"字出现在"企业所得税法"里而被顶到首位（实测"中华人民共和国企业
    # 所得税法"模糊检索首条是宪法），主依据会被选错。
    st = 1 if term.startswith("中华人民共和国") else 2
    rows, err = _rows_and_error(
        T.search_tax(term, scope="title", search_type=st, status=3, size=size))
    return rows, err


def _fgk_round(term: str, size: int) -> tuple[list, str]:
    rows, err = _rows_and_error(FGK.search_fgk(term, size=size))
    return rows, err


def _whole_site_round(term: str, size: int) -> tuple[list, str]:
    """税务总局站的全站层：关掉文件类标签，取动态、新闻与解读编译。

    只有 authority="overseas" 的专题走这里（判据见 `_overseas_rounds`）。取回的
    每一条都不是文件类条目，所以逐条带一句具体提醒：它记录的是境外辖区立了什么法、
    哪个年度生效，拿去答"在中国要怎么缴"就是错位。这一句和"来源提示可能偏题"那句
    兜底提醒说的不是同一件事，所以写成条目自己的 `_reliability_note`。
    """
    rows, err = _rows_and_error(W.search_chinatax(term, size=size,
                                                  file_only=False))
    for row in rows:
        row["_reliability"] = "medium"
        row["_reliability_note"] = (
            "税务总局网站全站层（文件类标签之外）的动态或解读页：它说明的是境外辖区"
            "的立法进展，不是中国境内的征税依据——要落到具体辖区名与生效年度才能用")
    return rows, err


def _shui5_round(term: str, size: int) -> tuple[list, str]:
    rows, err = _rows_and_error(S5.search_shui5(term, size=size, read_body=True))
    return rows, err


def _wechat_round(term: str, size: int) -> tuple[list, str]:
    rows, err = _rows_and_error(WX.search_wechat(term, size=size))
    return rows, err


def _rows_and_error(r: dict) -> tuple[list, str]:
    """把源返回拆成 (条目, 失败说明)，两种"没有"必须分开带出去。

    轮次函数若写成 `rows if not r.get("_error") else []`，源挂了和被
    判空就都变成长度 0 的列表，错误文本就地丢掉。后果是主依据会静默降级——实测
    点名《税收征管法》那一趟，NPC 没回来时法律那一档的现行有效依据不见了，
    顶上【主依据】的是部门规章《个体工商户建账管理暂行办法》，输出里一个字都
    看不
    出这是取数失败而不是库里没有。
    """
    err = r.get("_error") or ""
    return (r.get("results", []) if not err else []), err


# 法律草案、审议结果报告、征求意见公告的法定公布平台。六个税收源都不收这一层。
LEGISLATION_SITE = "npc.gov.cn"


def _legis_round(term: str, size: int) -> tuple[list, str]:
    """立法过程实位：在中国人大网站内找草案与审议公告。

    问的是草案时，只取回现行有效文本等于答错题——那一份是修订基线，不是草案
    内容（判据见 `tax_analyze.legislative_stage`）。这里取回的每一条都打上
    `_reliability: medium`（这一条只是定位文本的线索，可能偏题）与
    `legislative_process`（它不是已公布的条文）：前者进 `caveats` 变成一句
    提醒，后者让 `tax_evidence.pick_primary` 不把它挑成主依据。
    """
    rows, err = _rows_and_error(S360.so360_search(term, site=LEGISLATION_SITE,
                                                 size=size))
    for row in rows:
        row["_reliability"] = "medium"
        row["_reliability_note"] = ("立法过程件（中国人大网站内检索所得），"
                                    "不在六个税收源的收录范围内，只用于定位文本")
        # 单独一个标记，不靠 medium 顶替：medium 说的是"检索结果可能偏题"，
        # 这一层的件子可能恰恰就是题面问的那份草案，它不能当主依据的原因是
        # 文本还没有效力，不是排序不可信。compose 据此把它排除在主依据候选之外。
        row["legislative_process"] = True
    return rows, err


# 这一张表按回合（round）逐源取正文，和 ④ 的并发名单是两件事，键集合本来就不必
# 相等：中国税网没有单独的"回合"——④ 每答必走、它那一路的题录已经带回来了，
# 再在这里发一次是重取而不是补齐。要正文时走 tax_ctax.py --read，见 SKILL ③ L4。
_FETCHERS = {"npc": _npc_round, "fgk": _fgk_round,
             "whole_site": _whole_site_round,
             "shui5": _shui5_round, "wechat": _wechat_round,
             "legis": _legis_round}


def search_terms(question: str) -> dict:
    """把用户原话拆成各源该用的检索词。

    直接拿整句去检索不行，NPC 标题检索按整段字面匹配，"研发费用加计扣除比例
    是多少"取回来的全是含"费用"二字的无关法规（实测首条是《国家赔偿费用
    管理条例》）。所以每个源用自己的词：法规库认规范名，法规库清单认专题
    检索词，解读源认口语化的短词。

    Returns:
        {"npc": 本体法名或原话, "fgk": 专题检索词, "shui5": 短词,
        "wechat": 短词, "legis": 立法过程检索词（题面没点立法阶段则为空）,
        "whole_site": overseas 专题在全站层用的检索词,
        "adjacent_fgk": overseas 专题补查那一轮用的检索词,
        "topic": 识别到的专题名, "parent_law": 本体法}
    """
    info = T.resolve_tax_type(question) or {}
    topic = info.get("type", "")
    parent = info.get("parent_law", "")
    # 总局专题的检索词不是分类名，"税收争议救济"这类名号搜不到东西
    sta_term = info.get("search_term") or topic
    # 解读源用匹配上的别名当短词：把整句事实丢进 360 搜是搜不出实务文章的，
    # 命中的别名（加计扣除、小规模）才是税屋文章标题里真会用到的词。
    alias = info.get("matched_alias") or ""
    short = _strip_question_tail(question)
    if alias and len(short) > len(alias) + 6:
        short = alias
    # 用户点名了某份文件时，法规库那一轮要找的是这一份文件本身，
    # 不是它所属税种的本体法——本体法留给 npc 那一轮当上位法依据。
    # overseas 专题的法规库那一轮也用专题检索词：它问的是"境内文件类标签下到底
    # 有没有这一件事的文件"，用原话整句去查只会因字面匹配不到而 0 条，那个 0
    # 说明不了任何事（实测"支柱二对跨国企业有什么影响"在法规库 0 条命中）。
    cited = TT.cited_documents(question)
    fgk_term = TT.core_of_title(cited[0]["title"]) if cited else (
        sta_term if info.get("authority") in ("sta", "overseas")
        else parent or short)
    if cited and (not alias or len(short) > len(cited[0]["core"])):
        short = cited[0]["core"][:6]

    # 立法过程实位用的是法名简称 + 阶段词："中华人民共和国税收征收管理法 草案"
    # 在人大网站内搜不到，去掉国号前缀才命中草案与审议公告的标题写法。
    stage_words = A.legislative_terms(question)
    law_short = (parent.replace("中华人民共和国", "").strip() if parent else short)
    legis_term = f"{law_short} {stage_words[0]}".strip() if stage_words else ""

    return {
        "npc": parent or short,
        "fgk": fgk_term,
        "shui5": short,
        "wechat": short,
        "legis": legis_term,
        # overseas 专题的两格：全站层用专题自己的检索词，补查那轮用相邻专题的
        "whole_site": sta_term,
        "adjacent_fgk": T.adjacent_search_term(info),
        "topic": topic,
        "parent_law": parent,
        "authority": info.get("authority", "npc"),
        "cited": cited,
    }


def _topic_terms(terms: dict) -> list:
    """从检索词表里挑出"题面在问哪件事"，交给 ⑧ 判对应关系。

    只取专题名与口语短词（`topic`、`shui5`）：本体法名不在其中。理由见
    `gather` 里那次调用——把《企业所得税法》这类法名当主题词，法会因为标题里
    含自己的名字被判成"本题的直接规定"，抢走真正规定这题的那份公告的位置。
    """
    out = []
    for key in ("topic", "shui5"):
        v = (terms.get(key) or "").strip()
        if len(v) >= 2 and v not in out:
            out.append(v)
    return out


def locate_cited_document(title: str, size: int = 6) -> tuple:
    """按用户点名的文件标题在总局法规库里定位这一份文件。

    检索词由长到短依次试，全部试完再把各次结果合在一起按标题命中率排。
    长词精确但要求库里标题字面含着整串（用户给的标题常缺发文机关前缀或词序
    不同），短词召回广但会把整个主题域都带回来，所以两头都要试，见
    tax_terms.title_candidates。

    原文排在解读之前：解读件标题与原文只差"的解读"三个字，字面命中率几乎
    并列，而解读按 ⑦ 不能当主依据。点名的是文件本身时，只要检索到了原文就
    不该让解读顶上去；只有原文一条没取到，才把解读放首位并另作标记。

    排在最前的还得"就是被点名的那一份"：字符命中率量不出这个区别（见
    tax_terms.title_identity 的说明），所以每条都标上 _cited_identity，
    判过关系的排在没判过的之前，调用方只认 rows[0] 带标记的那一条。

    Returns:
        (按标题命中率降序的条目列表, 实际试过的检索词)
    """
    # 缓存优先：这份点名的文件以前核实过原文地址（按文号存），直接复用、不打网络。
    # 复用的前提仍是"标题确实是被点名的那一份"——缓存只记文号→链接，认不认还得靠
    # title_identity 现判；判不过就丢弃缓存、照常检索，绝不让旧命中冒充点名目标。
    dn = TT.doc_number_of(title)
    if dn:
        hit = CITED.get_cited_link(dn, CITED_CACHE_PATH)
        if hit:
            row = {"title": title, "url": hit, "document_number": dn,
                   "source": "文号链接缓存"}
            if TT.title_identity(title, title) == "same":
                row.update({"_title_match": 1.0, "_is_interpretation": False,
                            "_cited_identity": "same", "_from_cache": True})
                return [row], [dn]

    rows, tried = {}, []
    for cand in TT.title_candidates(title):
        tried.append(cand)
        try:
            r = FGK.search_fgk(cand, size=size)
        except Exception:
            continue
        for row in r.get("results", []):
            url = row.get("url") or ""
            if url and url not in rows:
                rows[url] = row
    sim = {u: TT.title_similarity(title, row.get("title", ""))
           for u, row in rows.items()}
    ident = {u: TT.title_identity(title, row.get("title", ""))
             for u, row in rows.items()}

    def _is_jiedu(row):
        return row.get("title", "").endswith("的解读")

    out = sorted(rows.values(),
                 key=lambda x: (not ident[x.get("url", "")], _is_jiedu(x),
                                -sim[x.get("url", "")]))
    for row in out:
        row["_title_match"] = round(sim[row.get("url", "")], 3)
        row["_is_interpretation"] = _is_jiedu(row)
        row["_cited_identity"] = ident[row.get("url", "")]
    # 定位成功且能归出文号、来源又是总局官方域——把这枚文号→原文链接落进缓存，
    # 下次同名文件直接命中、零网络。判不过点名同一性的不落（免得把误命中的链接
    # 供养成"下次一定对"），非官方域由 put_cited_link 自身拒收。
    if out and out[0].get("_cited_identity"):
        top = out[0]
        top_dn = top.get("document_number") or TT.doc_number_of(top.get("title", ""))
        if top_dn and CITED.is_official(top.get("url", "")):
            CITED.put_cited_link(top_dn, top["url"], CITED_CACHE_PATH)
    return out[:size], tried


def corroborate_validity_from_target(evidence: list, at: str = "") -> list:
    """用点名文件正文首段的"根据……规定"，为被援引的上位规则文件补时效证据。

    法规库的「财税文件」栏不录入时效：实测财税〔2009〕59 号、财税〔2014〕109 号
    的录入项为空，财税〔2003〕16 号那个位置上是字符串 "null"；详情页也只有成文
    日期，没有时效栏。所以这类文件按状态字段只能判"时效未标明"，会被压在依据线
    以下——而企业重组这道题里，59 号正是点名公告的上位规则。一份现行有效的公告
    把它列为制定依据，是这个来源里能拿到的在效证据。只补状态判不出来的条目，
    已标明废止或未生效的一律不动。

    Args:
        evidence: gather 取回并已带 body 字段的条目列表（原地标注）。
        at: 观察时点，用来确认援引方自己在该时点在效。

    Returns:
        被标注 corroborated_by 的条目标题列表。
    """
    target = next((e for e in evidence
                   if e.get("_cited_target") and (e.get("body") or "").strip()),
                  None)
    if not target or target.get("_is_interpretation"):
        return []
    if E.judge_validity(target, at)["validity"] != "effective":
        return []
    body = target["body"]
    head = body[:600]
    colon = head.find("：")
    para = head[:colon + 1] if colon > 0 else head
    titles = re.findall(r"《([^》]{4,80})》", para)
    tagged = []
    for e in evidence:
        if e is target or e.get("corroborated_by"):
            continue
        if E.judge_validity(e, at)["validity"] != "unknown":
            continue
        num = (e.get("document_number") or "").strip()
        title = (e.get("title") or "").strip()
        hit = bool(num and num in para) or \
            any(t and (t in title or title in t) for t in titles)
        if hit:
            e["corroborated_by"] = target.get("title", "")
            tagged.append(title)
    return tagged


# ── 实务层援引的文号回官方库核对 ───────────────────────────────────────────
OFFICIAL_WHERE = "税务总局法规库"
# 一次答案最多核对几个文号。实务文章里文号出现得密集（一篇点出五六个是常态），
# 不设上限时一道题能打出去几十次检索，NPC 那一路还有限流。
OFFICIAL_LOOKUP_LIMIT = 4


def _is_practice_row(row: dict) -> bool:
    """认这一条是不是实务材料那一路，判据直接用定级层的同一个常量。

    条目上带的不是 "shui5" 这种源键，而是给人看的标签——`tax_shui5` 写
    "税屋 (shui5.cn)"、`tax_wechat` 写"微信公众号"，360 回填的那批又是纯域名。
    自己在这里列一组源键，列错时这个函数会一条都不认，核对静默变成空转，
    输出上看不出任何异常；共用 `tax_evidence.PRACTICE_SOURCES` 就不会出现
    "核对层认为不是实务、定级层认为是"这种两说的情形。
    """
    src = " ".join(str(row.get(k) or "") for k in ("source", "site", "url"))
    return any(d in src for d in E.PRACTICE_SOURCES)


_ISSUER_TOKENS = ("国家税务总局", "税务总局", "财政部", "海关总署",
                  "人力资源社会保障部", "国务院办公厅", "国务院",
                  "国税发", "国税函", "财税", "税总", "地税发")
# 长写法在前：先试"暂行条例"再试"条例"，否则会截出错一半的前缀
_CITATION_TYPES = ("暂行条例", "公告", "通知", "办法", "批复", "意见",
                   "规定", "决定", "细则", "通告", "函")


def _citation_phrase(text: str, dn: str) -> str:
    """把裸文号补上它左侧原文里的机关与文件类型，凑成库里检索得到的写法。

    实测两件事决定了这一层：`search_fgk("2023年第19号")` 一条都不给
    （total_hits=0），而"国家税务总局公告2021年第5号"这种带前缀的写法能精确命中
    一份；"财税〔2016〕36号"本身就带机关名，原样即可。`tax_terms` 抽出来的是
    短形，直接拿去检索会一律落空，把现行有效的文件误报成"查不到"。

    前缀只从文号左侧的原文取，并且只认 `_ISSUER_TOKENS` 里的机关名当起点：
    正文与标题拼在一起后进来，纯按字符宽度截会把"减免购置税的执行口径 依据"
    这类散文一起当成检索词——那种词一个也检索不出，还让"库里没有"变成假结论。
    认不到机关名就照用裸文号，宁可检索得宽，不猜一个前缀。
    """
    m = re.search(r"\s*".join(re.escape(c) for c in dn), text or "")
    if not m:
        return dn
    run = re.search(r"[\u4e00-\u9fa5 ]{1,24}$", text[:m.start()])
    if not run:
        return dn
    head = run.group(0).rstrip()
    ctype = next((t for t in _CITATION_TYPES if head.endswith(t)), "")
    if not ctype:
        return dn
    segs = head[:len(head) - len(ctype)].split()
    for i, seg in enumerate(segs):
        at = [seg.find(t) for t in _ISSUER_TOKENS if t in seg]
        if at:
            phrase = segs[i][min(at):]
            phrase += "".join(" " + s for s in segs[i + 1:])
            return phrase + ctype + dn
    return dn


def _doc_number_in_library(term: str, want: str = "", lookup=None) -> tuple:
    """按 term 检索，用 want 对文号，返回 (条目或 None, 取数失败说明, 接口命中数)。

    检索词与比对词分开是因为它们各管一件事：`term` 要能在总局的检索里出结果
    （带机关前缀，见 `_citation_phrase`），`want` 要尽量宽（就是文章里那枚文号，
    库里多写几个字的机关名也算同一份）。合成一个词就会两头都失：短形检索不出，
    长形又比不中。

    三种"没有"必须分开，读者的下一步动作完全不同：
      接口挂了（补一轮）；检索有命中但取回的清单里没一份文号对上（这个文号
      可能不在收录范围或写法不同）；按这个词一个命中都没有（词没对上，
      不等于库里没有）。后两种都走 not_in_library，句子里带上命中数说明。
    失败与空结果的区分不用 `_error` 文本，用 `tax_fgk` 自己打的标记：
    `_fetch_failed`（请求/解析失败）与 `_empty_reason`（接口给了命中数却没给清单）。
    零命中时 `search_fgk` 也会写一句 `_error`，那是说明不是故障。
    """
    fn = lookup or (lambda t: FGK.search_fgk(t, size=5))
    try:
        r = fn(term)
    except Exception as e:
        return None, str(e).splitlines()[0][:110], None
    if r.get("_fetch_failed") or r.get("_empty_reason"):
        return None, r.get("_error") or "取数失败", None
    rows = r.get("results") or []
    hits = r.get("total_hits")
    want = re.sub(r"\s+", "", want or term)
    for row in rows:
        got = re.sub(r"\s+", "", row.get("document_number") or "")
        # 只认 want 是 got 的子串：文号抽取器给的是短形（"2021年第5号"），库里
        # 带发文机关前缀（"国家税务总局公告2021年第5号"）。反方向会把空号或
        # 半截号匹配成任何一份文件。
        if got and (got == want or want in got):
            return row, "", hits
    return None, "", hits


def check_practice_citations(rows: list, at: str = "", lookup=None,
                            limit: int = OFFICIAL_LOOKUP_LIMIT) -> dict:
    """把实务材料援引的文号回官方库核对存在与时效，结果原地写进 `official_status`。

    为什么要这一层：税屋与公众号那一路是法条落到实践的地方，它的价值恰恰在于
    告诉纳税人"口径怎么执行"；价值对应的是责任——这条口径得追到一份现行有效的
    法定文件上。给这一层配一个低分、标成"只能参考"是用降权代替
    核对，读者既看不出哪条有文件托着、也看不出哪条是悬空的。这里逐条核对，
    `outcome` 有九种取值，各配一句下一步动作（句子在 `tax_evidence.OFFICIAL_CAVEAT`）：
    在库且现行有效（effective）、在库但已废止（repealed）、在库但尚未生效
    （pending）、在库但时效判不出来（unknown）、库里查不到同一份
    （not_in_library），再加本层自己的四种缺口：正文没写文号（no_citation）、
    这一轮没取正文所以没读过文号（no_body）、这一轮没连上库（lookup_failed）、
    核对轮次用满（not_checked）。

    Args:
        rows: gather 取回的条目列表，原地标注；只动 `_is_practice_row` 认出的那些。
        at: 观察时点，用来判那份文件在题面时点是否还在效。
        lookup: 文号→库的取数函数，返回与 `FGK.search_fgk` 同形的 dict；离线用例打桩用。
        limit: 本次最多发几轮真实核对。同一个文号只查一次，命中缓存的不计数。

    Returns:
        {"checked": 发过核对请求的文号数（取数失败也算发过）,
         "skipped": 因上限没发请求的条数,
         "by_outcome": {outcome: 条数}}——前两个数说明核对到了什么程度，
        第三个数说明核对的结果各自是什么，别让读者从提醒句数去猜。
    """
    checked, skipped, by_outcome = {}, 0, {}
    for row in rows:
        if not _is_practice_row(row):
            continue
        # 正文在两个解读源里都叫 `content`（`tax_shui5`/`tax_wechat` 的 read_body 分支），
        # `body` 是点名文件那一路取回来的键；标题里的文号也要读——被解读的那份
        # 文件的文号常常只出现在标题上，它就是这一篇的口径出处。
        body = row.get("content") or row.get("body") or ""
        text = " ".join(x for x in (row.get("title", ""), body,
                                    row.get("summary") or row.get("snippet") or "") if x)
        own = re.sub(r"\s+", "", row.get("document_number") or "")
        dns = [d for d in TT.doc_numbers(text) if re.sub(r"\s+", "", d) != own]
        if not dns:
            # 没抽到文号有两种完全不同的原因：这一篇确实没写，与这一轮压根没取正文
            # （`gather(read_body=False)` 是常用形态）。写成同一句会把"没读"报成
            # "文章没标出处"，读者的下一步动作完全不同。
            st = {"outcome": "no_citation" if (body or row.get("summary")) else "no_body",
                  "doc_number": "", "title": "", "where": OFFICIAL_WHERE}
        else:
            dn = dns[0]
            if dn in checked:
                st = dict(checked[dn])
            elif len(checked) >= limit:
                st = {"outcome": "not_checked", "doc_number": dn, "title": "",
                      "where": OFFICIAL_WHERE, "limit": limit}
                skipped += 1
            else:
                # 检索词用补过前缀的写法（见 `_citation_phrase`），记下来的还是
                # 文章里那枚文号：读者要核对的是文章写了什么，不是我们拿什么去查。
                term = _citation_phrase(text, dn)
                hit, err, hits = _doc_number_in_library(term, dn, lookup)
                if err:
                    st = {"outcome": "lookup_failed", "doc_number": dn,
                          "title": "", "where": OFFICIAL_WHERE, "error": err}
                elif not hit:
                    st = {"outcome": "not_in_library", "doc_number": dn,
                          "title": "", "where": OFFICIAL_WHERE,
                          "searched_as": term, "hits": hits,
                          "also_cited": dns[1:3]}
                else:
                    # 库里查到了这一份：它的时效直接就是本层的结果名。
                    # `judge_validity` 的四个取值（effective/pending/repealed/
                    # unknown）与 `OFFICIAL_CAVEAT` 的四个同名条目是一一对应的，
                    # 这里不再另写一张映射表——写表就会多一处要同步的地方，而那位
                    # 真加了新时效档时，`official_caveat` 会把认不出的结果原样报出来
                    # 而不是静默给空串，比映射表的默认分支更显眼。
                    v = E.judge_validity(hit, at)["validity"]
                    st = {"outcome": v, "doc_number": dn,
                          "title": hit.get("title", ""),
                          "where": OFFICIAL_WHERE,
                          "validity_label": E.VALIDITY[v],
                          "url": hit.get("url", "")}
                checked[dn] = st
                st = dict(st)
        row["official_status"] = st
        by_outcome[st["outcome"]] = by_outcome.get(st["outcome"], 0) + 1
    return {"checked": len(checked), "skipped": skipped, "by_outcome": by_outcome}


def citation_note(cc: dict | None) -> str:
    """把文号核对进行到什么程度说一句人话。

    逐条的提醒已经挂在各条自己的 caveats 里，这一句管的是整体：读者看到
    "3 个文号里 1 个已废止"与看到"没核对过"，下一步动作完全不同。上限用满、
    库没连上这两种"没查成"必须显式说出来，否则空白的 by_outcome 会被读成
    "都查过了、都没问题"。
    """
    if not cc:
        return ""
    by = cc.get("by_outcome") or {}
    if not by:
        # 说清楚是"哪一类条目没取回"：上面【执行口径与实务认定】那一栏可能明明
        # 有条目（官方解读也算实务），这一句却要说没核对，读者就会以为两句打架。
        # 核对只管税屋与公众号，"实务材料"四个字撑不住这个区分。
        return "本轮没有取回税屋或公众号的文章，没有文号需要回官方库核对"
    detail = "、".join(f"{E.OFFICIAL_OUTCOME_LABEL.get(k, k)} {v} 条"
                       for k, v in sorted(by.items(), key=lambda kv: kv[1],
                                          reverse=True))
    head = f"实务口径援引的文号已回{OFFICIAL_WHERE}核对 {cc.get('checked', 0)} 个"
    if cc.get("skipped"):
        head += f"，另有 {cc['skipped']} 条因单次上限 {OFFICIAL_LOOKUP_LIMIT} 个没查"
    return f"{head}；结果分布：{detail}。单条的下一步动作见该条提醒行"


_TAIL_RE = None


def _strip_question_tail(question: str) -> str:
    """把问句压成检索用的短词。

    保留主题，去掉"是多少/可以享受吗/怎么办/比例多少"这类询问措辞。
    只做尾部截断，不改中间内容——中间的"加计扣除""小规模"都是检索关键词，
    改掉会丢信息。
    """
    global _TAIL_RE
    if _TAIL_RE is None:
        _TAIL_RE = re.compile(
            r"(是多少|是多少？|是多少?)|(可以享受吗)|(能不能享受)|"
            r"(能否享受)|(怎么办理|怎么办|怎么算|怎么申报|怎么交)|"
            r"(有什么风险|有风险吗)|(符合条件吗|符合条件|是否适用)|"
            r"[？?。，,、\s]+$")
    out = _TAIL_RE.sub("", question.strip())
    return out.strip() or question.strip()


def gather(question: str, at: str = "", read_body: bool = True,
           max_per_round: int | None = None,
           check_citations: bool = True) -> dict:
    """按 plan 逐轮检索，合并去重后定级。

    Args:
        question: 用户原话。
        at: 观察时点 YYYY-MM-DD，传空则不判生效区间。
        read_body: 是否取税屋正文。取正文要启浏览器，慢，不取时仍能拿到
            标题与地址用于分层展示。
        max_per_round: 每轮每源最多取几条，调试时用来压时间。
        check_citations: 是否把实务材料援引的文号回官方库核对。这一项要发
            真实检索（每次答案至多 `OFFICIAL_LOOKUP_LIMIT` 个文号），
            离线用例与只想要底稿时传 False。

    Returns:
        plan 字段 + {"evidence":[定级后的依据], "rounds_done":[...],
        "errors":[...], "citation_check":{核对进度}}。
    """
    plan = build_plan(question)
    tname = plan["type"]["type"]
    sizes = ROUND_FETCH.get(tname, ROUND_FETCH["default"])
    terms = search_terms(question)

    seen, evidence, errors, rounds_done = set(), [], [], []
    cited = terms.get("cited") or []
    cited_located = False
    if cited:
        # 用户点名了文件，先按标题把这一份文件本身捞出来排在证据最前，
        # 后面各轮取回的上位法与配套规定当其余法定依据，不抢它的位置。
        rows, tried = locate_cited_document(cited[0]["title"])
        terms["cited_candidates"] = tried
        # 只有标题确实就是被点名那一份才认目标。实测点名「税收征管法」时
        # 字面命中率 1.0 的那条是国税发〔2001〕110号——它在标题里引用了这部法，
        # 一旦当上主依据，现行有效的法律就被挤到后面那一栏。
        cited_located = bool(rows) and bool(rows[0].get("_cited_identity"))
        for i, row in enumerate(rows):
            url = row.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            if i == 0 and cited_located:
                row["_cited_target"] = True
                if read_body:
                    body = FGK.fetch_fgk_body(url)
                    if body.get("content"):
                        row["body"] = body["content"]
                        row["body_date"] = body.get("pub_date", "")
                    elif body.get("_error"):
                        errors.append("点名文件取正文失败：" + body["_error"])
            evidence.append(row)
    for rnd in plan["rounds"]:
        got_this_round, failed_this_round = 0, []
        # 一轮内各源默认用自己的词；立法过程轮用 term_key 指定草案词，让
        # 税屋/公众号这一趟按"法名+草案"搜，而不是按主题短词搜（后者取回现行规定）。
        for src in rnd["sources"]:
            fn = _FETCHERS[src]
            kw = terms.get(rnd.get("term_key") or src, question)
            try:
                if src == "shui5" and not read_body:
                    r = S5.search_shui5(kw, size=1, read_body=False)
                    rows, err = _rows_and_error(r)
                else:
                    rows, err = fn(kw, sizes.get(src, 8))
            except Exception as e:                      # 单源失败不中断整轮
                errors.append(f"第{rnd['round']}轮 {src} 失败："
                              f"{str(e).splitlines()[0][:110]}")
                failed_this_round.append(src)
                continue
            if err:
                # 取数失败和被判空是两件事，这里必须留名：下游 rounds_done 的
                # found=0 只有配上这一条才不读成"库里没有"（见 ⑤）。本轮目标要
                # 一起带出去：答案是按轮次分段渲染的，读者看不到轮次表，只看这
                # 一句时分不清坏掉的是哪一趟、这一趟本来要去取什么。
                errors.append(f"第{rnd['round']}轮 {src} 取数失败：{err}"
                              f"（本轮目标：{rnd['goal']}）")
                failed_this_round.append(src)

            for row in rows:
                url = row.get("url") or row.get("id") or row.get("title", "")
                if url in seen:
                    continue
                seen.add(url)
                evidence.append(row)
                got_this_round += 1
        rounds_done.append({"round": rnd["round"], "goal": rnd["goal"],
                            "found": got_this_round,
                            "failed": failed_this_round})

    # 实务材料援引的文号回官方库核对存在与时效。必须排在定级之前：`grade` 要把
    # `official_status` 翻成提醒句，定级完再标就晚了。核对本身要发检索，
    # 每份答案用满 OFFICIAL_LOOKUP_LIMIT 次为止，同一文号只查一次；
    # 单个文号的取数失败由 `_doc_number_in_library` 收成 lookup_failed，
    # 不会把这一层抛成整条答案的失败。
    citation_check = check_practice_citations(evidence, at) if check_citations else None

    # 点名文件正文把上位规则列为制定依据，据此给这些文件补时效证据，再定级。
    corroborated = corroborate_validity_from_target(evidence, at)
    # 主题词只给"题面在问哪件事"，不给本体法名：《企业所得税法》《增值税暂行
    # 条例》这类法名一旦进了词表，法自己就因为标题里含法名被判成"本题的直接
    # 规定"，把真正规定这道题的那份公告压到下面当上位依据——这正是 ⑧ 要避免的
    # 那种"位阶高就抢头条"的错。
    topic_terms = _topic_terms(terms)
    graded = E.grade_all(evidence, at=at, topic=topic_terms)
    plan["evidence"] = graded
    plan["topic_terms"] = topic_terms
    plan["rounds_done"] = rounds_done
    plan["errors"] = errors
    plan["corroborated"] = corroborated
    plan["citation_check"] = citation_check
    plan["at"] = at
    plan["terms"] = terms
    # 点名了文件但库里没捞到那一份：compose 要把这件事说明白，不能拿同域文件顶位
    if cited:
        plan["cited_located"] = cited_located
    plan["gathered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return plan


# ── 组织 ───────────────────────────────────────────────────────────────────
# 点名文件定位成功后写进主依据那句的措辞：判据来自 tax_terms.title_identity，
# 这里只把它翻译给人看，不再复述"命中率"——那个数是排序用的，撑不起"就是这一份"。
_CITED_IDENTITY_LABEL = {"same": "标题字面同一",
                         "abbrev": "缩略同一（题面是漏字写法）"}


def compose(plan: dict) -> dict:
    """在 gather 底稿上做依据分层与结论骨架，不联网、不编造结论。

    它不替代模型写答案，而是按角色把底稿分成「主依据 / 其余法定依据 /
    执行口径与实务认定 / 待核对线索」四栏，再把时效问题、未问的前提和取数
    缺口列出来，模型照着组织文字即可。分层是分工不是高下：实务解读那一栏
    照常进答案，只是它给的是口径怎么执行，不给"依据"这两个字。
    """
    ev = plan.get("evidence", [])
    cited = [e for e in ev if e.get("_cited_target")]
    target = cited[0] if cited else None
    cited_note = ""
    if target and not target.get("_is_interpretation"):
        # 层级最高的那条不是答案的主依据：用户问的是这一份文件，
        # 拿《企业所得税法》顶上去等于没读那份公告。
        primary = dict(target)
        doc_num = primary.get("document_number") or "文号未标"
        ident = _CITED_IDENTITY_LABEL.get(target.get("_cited_identity", ""),
                                          "标题字面同一")
        primary["_why"] = (
            f"用户点名的文件本身（{doc_num}），与点名标题{ident}；"
            f"{primary.get('rank_label', '未定性')}、"
            f"{primary.get('validity_label', '时效未标明')}。它的上位法在"
            f"【其余法定依据】里并列给出，结论要与上位法同读")
    elif target:
        # 只捞到官方解读，没捞到文件原文：解读讲的是口径怎么执行，可以进答案，
        # 但它不是被点名的那一份，所以不占主依据那一行；"原文没取到"要说明白。
        primary = E.pick_primary(ev)
        cited_note = (
            f"点名的《{target.get('title', '')[:40]}》只取到官方解读、没取到原文："
            f"解读交代执行口径，照常可用，但不能顶替被点名的那份文件当依据；"
            f"答案要写明原文待核")
    else:
        primary = E.pick_primary(ev)
        if plan.get("cited_located") is False:
            # 点名了文件、但召回的没有一份是它：不能装作没听见这个点名，
            # 也不能把同域文件当它用——只说清"没定位到"和"现在给的是什么"。
            want = ((plan.get("terms") or {}).get("cited") or [{}])[0].get("title", "")
            cited_note = (
                f"点名的《{want}》没有定位到同一份文件：召回的标题都只是在字面上"
                f"含住这几个字（包括在书名号里引用它的），按 ⑦ 不能顶替被点名的那份。"
                f"下面的主依据按主题分层给出，答案要写明点名文件待核")

    # 主依据那一行不再进后面的栏，否则同一份文件在输出里出现两次，
    # 一次标"主依据"一次标"其余依据"。NPC 那条链路不带 url，所以
    # 有 url 按 url 认，没 url 按标题认。
    def _is_primary(e):
        purl, ptitle = primary.get("url", ""), primary.get("title", "")
        if purl:
            return e.get("url", "") == purl
        return e.get("title", "") == ptitle

    rest = [e for e in ev if not _is_primary(e)]
    # 按角色分栏，不按分数分栏。用 `score >= 50` 一刀切成"依据/参考"两堆，
    # 结果是税屋与公众号的文章（层级 10 或 25）永远进不了前一种：一道题的口径
    # 往往就写在那一层里，把整栏压进"只能参考"等于把最有用的东西挡在答案外。
    statutory = [e for e in rest if e.get("role") in ("direct", "superior")]
    practice = [e for e in rest if e.get("role") == "practice"]
    to_verify = [e for e in rest if e.get("role") == "unmatched"]
    repealed = [e for e in ev if e.get("validity") == "repealed"]
    pending = [e for e in ev if e.get("validity") == "pending"]
    # 收口闸：能不能给具体结论，看的是"有没有一条法定文件在规定这件事"，
    # 不是各条分数够不够。立法过程件（草案）不算，废止的也不算。
    citable = [e for e in ev
               if e.get("role") in ("direct", "superior")
               and not e.get("legislative_process")
               and e.get("validity") != "repealed"]

    def _rows(rows):
        return [{"title": e.get("title", ""),
                 "rank_label": e.get("rank_label", ""),
                 "validity_label": e.get("validity_label", ""),
                 "role_label": e.get("role_label", ""),
                 "on_topic": e.get("on_topic"),
                 "reliability": e.get("reliability", ""),
                 "citation_hint": e.get("citation_hint", ""),
                 "caveats": e.get("caveats", []),
                 "url": e.get("url", ""),
                 "basis": e.get("corroborated_by", "")} for e in rows]

    spec = A.QUESTION_TYPES[plan["type"]["type"]]
    parts = plan.get("unanswered", [])

    return {
        "question": plan["question"],
        "type": plan["type"],
        "must_answer": plan["must_answer"],
        # 用户点名文件时的说明（没点名或已取到原文则为空）
        "cited_note": cited_note,
        # 主依据：按角色与时效挑出来的那一条，理由与提醒都在 why 里
        "primary": {
            "title": primary.get("title", ""),
            "rank_label": primary.get("rank_label", ""),
            "validity_label": primary.get("validity_label", ""),
            "role_label": primary.get("role_label", ""),
            "document_number": primary.get("document_number", ""),
            "caveats": primary.get("caveats", []),
            "url": primary.get("url", ""),
            "why": primary.get("_why", ""),
        },
        # 其余法定文件：上位授权与并列的直接规定，用来交叉验证与交代授权来源
        "supporting": _rows(statutory),
        # 执行口径与实务认定：税屋、公众号、总局官方解读、办税指南都在这一栏。
        # 它是答案里"怎么落地"那一段的取材处，不是禁止引用的隔离区。
        "practice": _rows(practice),
        # 这一栏的口径出处核到什么程度（查了几个文号、结果各是什么）。
        # 没跑核对那一层时是空串，读者据此知道"没核"而不是"核过没问题"。
        "citation_note": citation_note(plan.get("citation_check")),
        # 层级没判出、主题也没对上的线索：要先核对才能用，但照常列出
        "to_verify": _rows(to_verify),
        "repealed": [e.get("title", "") for e in repealed],
        "pending": [e.get("title", "") for e in pending],
        # 答案里必须写出来的限制条件
        "conditions": parts,
        # 规则陈述类题型：个案轴缺失不追问，但要在答案里留一句适用边界
        "rule_note": plan.get("rule_note", ""),
        # 题面借用了会计确认与计量的地方：税法条文引的是会计结果，准则那一层
        # 六个税收源都不收，取证入口是子技能 chenyiwei-bbs（见 SKILL.md ③）
        "accounting_gap": plan.get("accounting_gap", []),
        # 题面点名的是草案/征求意见稿时，本轮取回的同名文本是现行有效版本，
        # 不是草案内容。这句话命令行与服务端共用，不在两处各说各话。
        "legislative_note": A.legislative_note(plan.get("legislative_stage", [])),
        # 题面归到 overseas 专题（支柱二与全球最低税）时，这一句划清境内依据的
        # 边界：本轮取回的是境外辖区的立法动态，不是中国的征收依据。
        "overseas_note": plan.get("overseas_note", ""),
        "probes": plan.get("probes", []),
        # 取数失败的轮次：这一栏非空时，上面各层列出的条数不能读成"库里只有这些"
        "fetch_errors": plan.get("errors", []),
        "fetch_note": (
            "本轮有源取数失败，上面的分层是在缺源的底稿上做的："
            "没列出的文件不等于库里没有，先补取这一轮再下结论"
            if plan.get("errors") else ""),
        "evidence_gap": (
            "" if citable else
            "本轮没有取到规定这道题的法定文件——各栏里只有执行口径、立法过程线索"
            "或已废止件。结论只能给方向，不能给具体数额；先按【待核对线索】与"
            "【执行口径与实务认定】里那份文件的文号补一轮检索，取不到再转人工确认"),
        "counts": {"total": len(ev), "statutory": len(statutory),
                   "practice": len(practice), "to_verify": len(to_verify),
                   "repealed": len(repealed), "pending": len(pending)},
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print_plan(plan: dict):
    t = plan["type"]
    print(f"问题：{plan['question']}")
    print(f"类型：{t['label']}（{t['type']}，把握 {t['confidence']}）")
    print(f"  判型依据：{t['reason']}")
    print(f"\n这类题要答到位，需要：")
    for s in plan["sources"]:
        print(f"  - {s}")
    print(f"结论应包含：{plan['must_answer']}")
    print(f"\n检索轮次：")
    for r in plan["rounds"]:
        print(f"  第{r['round']}轮  {r['goal']}")
        print(f"         源 {'/'.join(r['sources'])}")
        print(f"         {r['call']}")
    if plan["unanswered"]:
        print(f"\n题面没交代的前提：{', '.join(plan['unanswered'])}")
    for pr in plan["probes"]:
        print(f"  建议先问：{pr['probe']}")
    _print_accounting_gap(plan.get("accounting_gap", []))
    _print_legislative_note(A.legislative_note(plan.get("legislative_stage", [])))
    _print_overseas_note(plan.get("overseas_note", ""))


def _print_legislative_note(note: str):
    """问的是草案时，这一句必须原样进答案：它划清了库的覆盖边界。"""
    if not note:
        return
    print(f"\n立法阶段限制：{note}")


def _print_overseas_note(note: str):
    """问的是支柱二那一类境外辖区规则时，这一句同样原样进答案。"""
    if not note:
        return
    print(f"\n境外辖区规则限制：{note}")


def _print_accounting_gap(gap: list):
    """会计口径缺口：六个税收源都不收会计准则，这一层要交给子技能去取。"""
    if not gap:
        return
    print("\n会计口径缺口（六个税收源都不收准则条文，取证的入口是子技能）：")
    for g in gap:
        print(f"  · 要先问清会计上的哪一件事：{g}")
    print("  · 读 subskills/chenyiwei-bbs/SKILL.md，准则原文与实务答疑都在那里")
    print("  · 准则与答疑不是税收法定依据：前提写进【适用边界】，"
          "口径进【执行口径与实务认定】那一栏")


def _print_gather(plan: dict):
    _print_plan(plan)
    terms = "、".join(plan.get("topic_terms") or []) or "未传"
    print(f"\n实际取回 {len(plan.get('evidence', []))} 条材料"
          f"（观察时点 {plan.get('at') or '未指定'}，主题词 {terms}）：")
    for e in plan["evidence"][:15]:
        print(f"  [{e.get('role_label', '')}] {e.get('rank_label', '')}/"
              f"{e.get('validity_label', '')}  {e.get('title', '')[:44]}")
    if len(plan["evidence"]) > 15:
        print(f"  … 另有 {len(plan['evidence']) - 15} 条")
    for err in plan.get("errors", []):
        print(f"  ⚠ {err}")


def _print_rows(rows: list, title: str, limit: int, blurb: str = ""):
    """按角色打印一栏材料，每条带上它自己的提醒。

    提醒逐条打印而不是只印一个角标，是因为"这一条存疑"必须说清疑在哪一处、
    要核对什么，读者才能动手核对；界面上一个"仅参考"小圆点做不到这点。
    """
    if not rows:
        return
    print(f"\n【{title}】{len(rows)} 条" + (f"　{blurb}" if blurb else ""))
    for s in rows[:limit]:
        print(f"  {s['rank_label']}/{s['validity_label']}  {s['title'][:44]}")
        if s.get("basis"):
            print(f"        时效判定依据：被现行有效的《{s['basis']}》"
                  f"列为制定依据（本条无时效录入）")
        for c in (s.get("caveats") or [])[:2]:
            print(f"        提醒：{c}")
    if len(rows) > limit:
        print(f"  …另有 {len(rows) - limit} 条未列出")


def _print_answer(a: dict):
    p = a["primary"]
    print(f"问题：{a['question']}")
    print(f"类型：{a['type']['label']}")
    print(f"结论要答到位必须有：{a['must_answer']}\n")
    print("【主依据】")
    if p["title"]:
        num = f"（{p['document_number']}）" if p.get("document_number") else ""
        print(f"  {p['title'][:60]}{num}")
        print(f"  {p.get('role_label', '')} / {p['rank_label']} / "
              f"{p['validity_label']} — {p['why']}")
    else:
        print("  未取到可作主依据的条文")
    if a.get("cited_note"):
        print(f"  ⚠ {a['cited_note']}")
    _print_rows(a["supporting"], "其余法定依据", 6,
                "上位授权与并列的直接规定，用来交叉验证与交代授权来源")
    _print_rows(a["practice"], "执行口径与实务认定", 6,
                "答案里「怎么落地」那一段从这里取材；口径要落到它引用的文号上")
    if a.get("citation_note"):
        print(f"  {a['citation_note']}")
    _print_rows(a["to_verify"], "待核对线索", 5,
                "层级与主题都还没对上，先核对再决定用不用")
    if a["repealed"]:
        print(f"\n【已废止，仅可用于说明沿革】")
        for s in a["repealed"][:5]:
            print(f"  {s[:52]}")
    if a["pending"]:
        print(f"\n【尚未生效，不能用于当期回答】")
        for s in a["pending"][:5]:
            print(f"  {s[:52]}")
    if a["conditions"]:
        print(f"\n【答案必须写明的限制条件】")
        for c in a["conditions"]:
            print(f"  · 题面没交代「{c}」，结论要标注这一项未定")
    if a.get("rule_note"):
        print(f"\n【适用边界】\n  · {a['rule_note']}")
    _print_accounting_gap(a.get("accounting_gap", []))
    _print_legislative_note(a.get("legislative_note", ""))
    _print_overseas_note(a.get("overseas_note", ""))
    if a["probes"]:
        print(f"\n【建议先向用户确认】")
        for pr in a["probes"]:
            print(f"  · {pr['probe']}")
    if a["evidence_gap"]:
        print(f"\n⚠ {a['evidence_gap']}")
    if a.get("fetch_errors"):
        print(f"\n【取数失败】")
        for e in a["fetch_errors"][:5]:
            print(f"  ⚠ {e[:130]}")
        if len(a["fetch_errors"]) > 5:
            print(f"  …另有 {len(a['fetch_errors']) - 5} 条")
        print(f"  {a['fetch_note']}")
    print(f"\n统计：{a['counts']}")


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="税务问题分析编排")
    p.add_argument("question", help="用户原话")
    p.add_argument("--plan", action="store_true", help="只出检索计划，不联网")
    p.add_argument("--gather", action="store_true", help="执行检索并定级")
    p.add_argument("--answer", action="store_true", help="检索并组织依据分层")
    p.add_argument("--at", default="", help="观察时点 YYYY-MM-DD")
    p.add_argument("--no-body", action="store_true", help="不取税屋正文（更快）")
    p.add_argument("--no-citation-check", action="store_true",
                   help="不把实务材料援引的文号回官方库核对（每次答案省掉至多 "
                        f"{OFFICIAL_LOOKUP_LIMIT} 次检索）")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.answer or args.gather:
        plan = gather(args.question, at=args.at, read_body=not args.no_body,
                      check_citations=not args.no_citation_check)
        if args.answer and not args.json:
            _print_answer(compose(plan))
            return
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
            return
        _print_gather(plan)
        return

    plan = build_plan(args.question)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    _print_plan(plan)


if __name__ == "__main__":
    main()
