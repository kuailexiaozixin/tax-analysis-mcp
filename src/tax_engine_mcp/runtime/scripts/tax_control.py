#!/usr/bin/env python3
"""内控生成：把检查缺口换成分等级的制度文本（谁做、多久一次、留什么痕、留多久）。

它和稽查模拟是一上一下的两层：`tax_inspect.py` 判"这一问我现在答不答得出"，本模块判
"要让这一问以后总是答得出，制度里得写哪一条"。两层的输入输出相接——稽查模拟吐出的
缺口清单就是本模块的唯一入口。

三条硬约束，都在代码里判，不靠文档自觉：

1. **没有诊断就没有处方**（`NeedsDiagnosis`）。一次缺口都没给、也没给假设时直接拒
   生成，返回的是"怎么拿到诊断"的那条命令，不是一套通用制度。用户只要通用模板时可以
   生成，但必须显式给出所依据的假设（`build(assumptions=...)` / `--assume`），输出随即
   标成通用模板而不是本企业的制度——把假设藏起来就等于把猜测写成事实。
2. **每条控制活动必须有一个诊断入口**（`针对检查域`／`针对要点` × `处置缺口`）。入口为
   空的活动永远不会被任何缺口选中，留在表里只会让通用模板多出一节没人要求过的制度。
   范围可以点查到具体要点，也可以只挂域（`针对要点` 留空）；后者只允许用于
   『政策口径不清』这一类，因为它的处置是把现行口径落到那一年，与具体缺哪份材料无关。
3. **九要素逐格在场**（`ELEMENT_KEYS`）：目的／责任岗位／频率／输入资料／操作步骤／
   复核点／留痕／保存期限／例外处理。缺任何一格，这段文字就只是建议不是制度——
   没有责任岗位和频率的制度无法执行，没有复核点与留痕的制度无法验证自己执行过。
4. **反过来也要成立：有诊断就得有处方**（`validate()` 末尾的覆盖底线）。稽查层每个
   检查要点的三类缺口，逐格都要有对着它的控制活动；少一格载入即报错。这一条把两张表
   钉在一起：稽查层加了新要点，本表不跟着长出制度就加载不了，不会等到用户拿着缺口来
   才当场发现无方可开。

`validate()` 判这四条，`intake()` 判输入（类别、要点代号、域代号、两者是否同源），
`pick()` 判筛选值（认不出的环节／活动代号报错列取值，不静默筛成空）。

两条红线沿用稽查层的词表，不另立第二份（`tax_inspect.FORBIDDEN_REMEDIATION`、
`tax_inspect.PREDICTION_PHRASES`）：整改只能把真实发生过的事项的证据补齐、把流程固化，
绝不写伪造、把日期往前写、重做历史资料；本模块也不预测检查结果。
保存期限写的是管理下限，法定年限要按 ⑧ 定级挑出的那份文件核对后再定，这一句随输出走。

环节与活动来自 `data/control_activities.json`：环节下限九项（项目立项管理、人员工时与
薪酬归集、领料与耗用、设备与软件使用、委托研发与关联交易、辅助账与账证一致性、月度复核、
年度申报与优惠资格、档案管理），另设发票凭证与合同管理、政策变更与口径跟踪两环，
按检查域代号与 `data/inspection_domains.json` 对齐，所以覆盖面不局限研发与高企。

用法：
    python scripts/tax_control.py --list
    python scripts/tax_inspect.py --domain D1 --json > gaps.json
    python scripts/tax_control.py --diagnosis gaps.json
    python scripts/tax_control.py --assume "单一一般纳税人，查账征收，2025 年度" --link H6
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tax_inspect  # noqa: E402  缺口词表、红线词表与检查域代号都取自这一处，不抄第二份

REGISTRY_PATH = HERE.parent / "data" / "control_activities.json"

#: 制度文本的九要素，顺序即输出顺序（SKILL ⑥ 六段式的「制度与流程」条件块同一顺序）。
ELEMENT_KEYS = ("目的", "责任岗位", "频率", "输入资料", "操作步骤",
                "复核点", "留痕", "保存期限", "例外处理")
#: 九要素里以清单形态出现的四格——空条目会让这一格看起来有内容其实执行不了。
LIST_ELEMENTS = ("输入资料", "操作步骤", "复核点", "留痕")
#: 活动自身的管理字段（代号、名称与诊断入口），与九要素合起来才是完整的一格。
ACTIVITY_KEYS = ("代号", "活动", "针对检查域", "针对要点", "处置缺口")

#: 环节下限：顾问级工作流至少要覆盖这九环，名字逐字等于注册表里的『环节』。
#: 表里可以多设环节（发票与合同、口径跟踪），少一环就是制度有洞。
LINK_FLOOR = ("项目立项管理", "人员工时与薪酬归集", "领料与耗用", "设备与软件使用",
              "委托研发与关联交易", "辅助账与账证一致性", "月度复核",
              "年度申报与优惠资格", "档案管理")

#: 保存期限必须落到一个期限：带年数的，或长期／永久。只写"按相关规定"等于没写。
#: 判据只有这一条正则——把 `年` 从正则里去掉，本表 22 条活动的期限断言会全部转红。
RETENTION_RE = re.compile(r"(\d+\s*年|永久|长期)")

MODE_DIAGNOSED = "针对性制度（基于诊断）"
MODE_GENERAL = "通用模板（基于假设）"

#: 没有诊断也不给假设时的回话。它交付的是拿到诊断的那一步，不是一套通用制度。
NO_DIAGNOSIS_NOTE = (
    "没有检查缺口也没有假设——本模块拒绝凭空生成一套通用制度。"
    "先跑稽查模拟拿诊断：python scripts/tax_inspect.py --domain D1 --json > gaps.json，"
    "再 python scripts/tax_control.py --diagnosis gaps.json；"
    "确实只要通用模板的，用 --assume 写明所依据的假设（输出会标成通用模板）。")


#: 给了诊断文件、里面却一条缺口都没有时的回话。空诊断不是拒绝的理由，但也生不出处方。
NO_GAP_NOTE = (
    "读到了诊断，但里面 0 条证据缺口——本模块只按缺口出制度，没有缺口就没有对着它的处方。"
    "要么把稽查模拟的范围放宽重跑（--domain/--scenario/--signal），"
    "要么用 --assume 写明假设按通用模板生成。")


class NeedsDiagnosis(ValueError):
    """依赖门槛：没有诊断输入、也没有显式假设。"""


# ── 1 载入与自检 ──────────────────────────────────────────────

def _bad_words(text: str, words) -> list:
    return tax_inspect._bad_words(text, words)


def _str(value, where: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{where} 的『{field}』是空的——九要素缺这一格就不是制度")
    return text


def _strs(value, where: str, field: str) -> list:
    return tax_inspect._nonempty_strs(value, where, field)


def validate(reg: dict) -> None:
    """注册表的结构、诊断入口与两条红线在载入时判，不留到输出里靠人眼抓。"""
    note = reg.get("_说明") or {}
    for field, want in (("九要素", list(ELEMENT_KEYS)),
                        ("活动字段", list(ACTIVITY_KEYS)),
                        ("三类缺口", list(tax_inspect.GAP_TYPES)),
                        ("环节下限", list(LINK_FLOOR))):
        got = list(note.get(field) or [])
        if got != want:
            raise ValueError(f"_说明『{field}』与代码不同源：表里是 {got}，代码是 {want}")

    links = reg.get("控制环节")
    if not isinstance(links, list) or not links:
        raise ValueError("注册表里没有『控制环节』清单")
    codes = [d.get("代号") for d in links]
    want_codes = [f"H{i}" for i in range(1, len(codes) + 1)]
    if codes != want_codes:
        raise ValueError(f"环节代号必须从 H1 连续排下来：读到 {codes}，应为 {want_codes}")

    names = [d.get("环节") for d in links]
    missing = [x for x in LINK_FLOOR if x not in names]
    if missing:
        raise ValueError(f"环节下限缺 {missing}——这九环少一环，制度就有洞")
    dup = [x for x in set(names) if names.count(x) > 1]
    if dup:
        raise ValueError(f"环节名重复 {dup}，代号与名字必须一对一")

    insp = tax_inspect.load()
    domain_codes = [d["代号"] for d in insp["检查域"]]
    points_by_domain = {d["代号"]: [pt["代号"] for pt in d["检查要点"]] for d in insp["检查域"]}
    point_domain = {pt["代号"]: d["代号"] for d, pt in tax_inspect.points(insp)}

    seen = set()
    for link in links:
        name = link.get("代号")
        acts = link.get("控制活动")
        if not acts:
            raise ValueError(f"环节 {name}（{link.get('环节')}）没有控制活动——"
                             f"空环节看起来覆盖了，实际一条都生成不出来")
        for act in acts:
            code = act.get("代号") or "（无代号）"
            where = f"{name}／活动 {code}"
            if not code.startswith(name + "-"):
                raise ValueError(f"活动代号 {code} 不在所属环节 {name} 下")
            if code in seen:
                raise ValueError(f"活动代号 {code} 重复")
            seen.add(code)
            for field in ACTIVITY_KEYS + ELEMENT_KEYS:
                if field not in act:
                    raise ValueError(f"{where} 缺『{field}』这一格")
            _str(act["活动"], where, "活动")
            for field in ELEMENT_KEYS:
                if field in LIST_ELEMENTS:
                    _strs(act[field], where, field)
                elif field != "保存期限":
                    _str(act[field], where, field)

            retention = act["保存期限"]
            if not RETENTION_RE.search(retention):
                raise ValueError(f"{where} 的『保存期限』没写出期限（读到 {retention}）："
                                 f"要写成带年数的管理下限，或长期／永久；"
                                 f"『按相关规定』『视情况』这类不带期限的说法不算填了这一格")

            gaps = _strs(act["处置缺口"], where, "处置缺口")
            bad = [g for g in gaps if g not in tax_inspect.GAP_TYPES]
            if bad:
                raise ValueError(f"{where} 写了第四类缺口 {bad}："
                                 f"只有 {list(tax_inspect.GAP_TYPES)} 三类")
            doms = _strs(act["针对检查域"], where, "针对检查域")
            pts = act["针对要点"] or []
            if not isinstance(pts, list):
                raise ValueError(f"{where} 的『针对要点』必须是清单")
            unknown = [x for x in doms if x not in domain_codes]
            if unknown:
                raise ValueError(f"{where} 的『针对检查域』{unknown} 不在稽查层注册表里")
            cross = [x for x in pts if point_domain.get(x) not in doms]
            if cross:
                raise ValueError(f"{where} 针对的要点 {cross} 不属于它列出的检查域——"
                                 f"入口写串了会选出对不上的制度")
            if not pts and set(gaps) != {tax_inspect.GAP_CALIBER}:
                raise ValueError(f"{where} 没点名要点（按域挂），却处置 {gaps}："
                                 f"按域挂只允许用于『{tax_inspect.GAP_CALIBER}』。"
                                 f"缺材料与材料矛盾必须点名要点，"
                                 f"否则这一条会写成一句放到哪个环节都成立的大话")

            # 红线一：制度文本不许写改动历史事实的动作
            for field in ("活动", "输入资料", "操作步骤", "复核点", "留痕", "例外处理"):
                value = act[field]
                text = value if isinstance(value, str) else " ".join(value)
                hit = _bad_words(text, tax_inspect.FORBIDDEN_REMEDIATION)
                if hit:
                    raise ValueError(f"{where} 的『{field}』写了越红线的话 {hit}："
                                     f"只能把真实发生过的事项的证据补齐、把流程固化")
            # 红线二：本模块不预测检查结果
            for field in ("活动", "目的"):
                hit = _bad_words(act[field], tax_inspect.PREDICTION_PHRASES)
                if hit:
                    raise ValueError(f"{where} 的『{field}』写了结果预测 {hit}")

    # 覆盖底线：稽查层每个要点的三类缺口都要有对着它的活动——有诊断就必须有处方。
    covered = {p: set() for p in point_domain}
    for _link, act in activities(reg):
        for p in scope_of(act, points_by_domain):
            covered.setdefault(p, set()).update(act["处置缺口"])
    holes = [(p, g) for p in sorted(covered)
             for g in tax_inspect.GAP_TYPES if g not in covered[p]]
    if holes:
        raise ValueError(
            f"覆盖底线有 {len(holes)} 格空着（这些『检查要点 × 缺口类』没有对着它的控制活动）："
            + "；".join(f"{p}×{g}" for p, g in holes)
            + "——补活动，或把既有活动的『处置缺口』『针对要点』扩到这一格")


def load(path: Path = None) -> dict:
    reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    validate(reg)
    return reg


# ── 2 摊平与选材 ─────────────────────────────────────────────

def activities(reg: dict = None) -> list:
    """摊平成 (环节, 活动) 对，顺序即注册表顺序。"""
    reg = reg or load()
    return [(link, act) for link in reg["控制环节"] for act in link["控制活动"]]


def matches(act: dict, gap: dict) -> bool:
    """一条活动处不处置这一条缺口：类别相符，且缺口范围落在活动的挂点范围内。

    挂点分两段：`针对检查域` 必填，`针对要点` 可空——留空即按域覆盖该域全部要点，
    这种挂法只允许出现在『政策口径不清』上（`validate()` 判，注册表『范围怎么挂』记着理由）。
    缺口只给了域、没给要点时按域粗配，这一类缺口在 `诊断.缺范围` 里单独点名，
    不算精确命中：范围含糊的缺口配出来的制度，读者无法判断是不是对着自己那件事。
    """
    if gap["类"] not in act["处置缺口"]:
        return False
    dom = gap.get("域") or ""
    if not dom or dom not in act["针对检查域"]:
        return False
    return not act["针对要点"] or not gap.get("要点") or gap["要点"] in act["针对要点"]


def scope_of(act: dict, points_by_domain: dict) -> set:
    """一条活动罩住哪些检查要点：点名了就那几问，留空就是该域全部要点。"""
    if act["针对要点"]:
        return set(act["针对要点"])
    return {p for d in act["针对检查域"] for p in points_by_domain.get(d, ())}


def pick(reg: dict, gaps: list, links=(), codes=()) -> list:
    """按诊断选活动；给了环节／活动代号时在范围内再收一次。

    筛选值认不出就报错列出可用取值，不静默筛成空清单——空清单读起来像
    "这个范围没有该建的制度"，实际是代号写错了一个字符。
    """
    known_links = [link["代号"] for link in reg["控制环节"]]
    known_names = [link["环节"] for link in reg["控制环节"]]
    bad = [x for x in links if x not in known_links and x not in known_names]
    if bad:
        raise ValueError(f"环节代号或环节名 {bad} 不在注册表里："
                         f"取 {'、'.join(known_links)}（或环节名），见 --list")
    all_acts = [act["代号"] for _l, act in activities(reg)]
    bad_acts = [x for x in codes if x not in all_acts]
    if bad_acts:
        raise ValueError(f"活动代号 {bad_acts} 不在注册表里，可用取值见 --list")
    pool = []
    for link, act in activities(reg):
        if links and link["代号"] not in links and link["环节"] not in links:
            continue
        if codes and act["代号"] not in codes:
            continue
        hits = [g for g in gaps if matches(act, g)]
        if gaps and not hits:
            continue
        pool.append((link, act, hits))
    return pool


# ── 3 诊断输入：只认稽查层的缺口词汇 ─────────────────────────

def intake(diagnosis) -> list:
    """把诊断输入归一成 [{类, 对象, 要点, 域}]，词表与代号都取自稽查层注册表。

    收三种形态：`tax_inspect.build()` 的整个输出（直接喂，不必手工摘缺口）、
    `{"缺口": [...]}`、以及裸清单 `[{"类": ..., "对象": ..., "要点": ...}]`。
    三种形态过同一段判据：类别认不出、代号不在稽查层表里、要点与域不是同一个——
    都直接报错列出可用取值。静默丢掉的那一条，正是没人管的缺口。
    """
    if diagnosis is None or diagnosis == {}:
        return []
    if isinstance(diagnosis, dict) and "问询" in diagnosis:
        rows = [{"类": gap["类"], "对象": gap.get("对象"),
                 "要点": unit["代号"], "域": unit["域代号"]}
                for unit in diagnosis["问询"]
                for gap in (unit.get("证据缺口") or [])]
    elif isinstance(diagnosis, dict):
        rows = diagnosis.get("缺口")
        if rows is None:
            raise ValueError("诊断 JSON 认不出形态：给 `tax_inspect.py --json` 的整个输出、"
                             "{\"缺口\": [...]} 或裸清单")
    elif isinstance(diagnosis, list):
        rows = diagnosis
    else:
        raise ValueError("诊断只能是对象或清单")

    insp = tax_inspect.load()
    point_domain = {pt["代号"]: d["代号"] for d, pt in tax_inspect.points(insp)}
    domains = [d["代号"] for d in insp["检查域"]]
    out = []
    for i, row in enumerate(rows or []):
        where = f"诊断第 {i + 1} 条"
        if not isinstance(row, dict):
            raise ValueError(f"{where}不是对象（要 {{类/对象/要点/域}}）")
        kind = str(row.get("类") or "").strip()
        if kind not in tax_inspect.GAP_TYPES:
            raise ValueError(f"{where}的类别『{kind or '（空）'}』不在三类缺口里："
                             f"取 {'、'.join(tax_inspect.GAP_TYPES)}")
        if not str(row.get("对象") or "").strip():
            raise ValueError(f"{where}没写『对象』——哪一份材料／哪两处数说不清，"
                             f"选不出对着它的制度")
        point = str(row.get("要点") or "").strip()
        if point and point not in point_domain:
            raise ValueError(f"{where}的检查要点『{point}』不在稽查层注册表里，"
                             f"取值见 python scripts/tax_inspect.py --list")
        dom = str(row.get("域") or "").strip()
        if dom and dom not in domains:
            raise ValueError(f"{where}的检查域『{dom}』不在稽查层注册表里：取 {'、'.join(domains)}")
        if point:
            if dom and dom != point_domain[point]:
                raise ValueError(f"{where}的要点 {point} 属于 {point_domain[point]}，"
                                 f"却写成域 {dom}——范围串了会选出对不上的制度")
            dom = point_domain[point]
        out.append({"类": kind, "对象": str(row["对象"]).strip(),
                    "要点": point, "域": dom})
    return out


def _diagnosis_given(diagnosis) -> bool:
    """调用方是否真的递了诊断进来（与"诊断里有几条缺口"是两件事）。

    分开的理由：空诊断与没给诊断的回话不同——一个要放宽稽查范围重跑，
    一个要先去跑稽查模拟。混成一句会让人以为文件读错了。
    """
    return diagnosis is not None and diagnosis != {}


# ── 4 生成制度文本 ──────────────────────────────────────────

def build(diagnosis=None, assumptions=(), links=(), codes=(), reg: dict = None) -> dict:
    """按诊断选控制活动并输出九要素制度文本。

    `diagnosis` 空且 `assumptions` 也空时抛 `NeedsDiagnosis`（没有诊断就没有处方）。
    `assumptions` 非空即通用模板模式：照样生成，但形态标成通用模板，假设逐条列在输出里。
    """
    reg = reg or load()
    gaps = intake(diagnosis)
    assume = tax_inspect._nonempty_strs(list(assumptions), "假设", "假设") if assumptions else []
    if not gaps and not assume:
        raise NeedsDiagnosis(NO_GAP_NOTE if _diagnosis_given(diagnosis) else NO_DIAGNOSIS_NOTE)
    mode = MODE_DIAGNOSED if gaps else MODE_GENERAL

    pool = pick(reg, gaps, links, codes)
    full = activities(reg)
    asked = {act["代号"] for _l, act, _h in pool}
    all_codes = {act["代号"] for _l, act in full}
    unmatched, out_of_range = [], []
    for gap in gaps:
        anywhere = [act["代号"] for _l, act in full if matches(act, gap)]
        here = [act["代号"] for _l, act, _h in pool if matches(act, gap)]
        if not anywhere:
            unmatched.append(gap)
        elif not here:
            out_of_range.append(dict(gap, 应在=anywhere))

    items = []
    for link, act, hits in pool:
        items.append({
            "代号": act["代号"], "环节代号": link["代号"], "环节": link["环节"],
            "活动": act["活动"],
            "触发缺口": hits,
            "针对检查域": act["针对检查域"], "针对要点": act["针对要点"],
            "处置缺口": act["处置缺口"],
            **{k: act[k] for k in ELEMENT_KEYS},
        })
    floor_missing = [x for x in LINK_FLOOR
                     if not any(i["环节"] == x for i in items)]
    unasked = [{"代号": a["代号"], "环节代号": l["代号"], "环节": l["环节"], "活动": a["活动"]}
               for l, a in full if a["代号"] not in asked]
    if not items:
        note = (f"诊断里的缺口没命中本次范围内的任何控制活动"
                f"（范围筛过：{'、'.join(links or []) or '全部环节'}）"
                f"——这是缺口对不上这一份范围，不是没有风险，未生成的活动列在下面")
    elif unasked:
        why = "在本次筛选范围外" if (links or codes) else "没被诊断牵动"
        note = f"另有 {len(unasked)} 条控制活动{why}，未生成"
    else:
        note = "本表内每条控制活动都被牵动了"

    return {
        "形态": mode,
        "筛选": {"环节": list(links), "活动": list(codes),
                 "条件": [x for x in ("、".join(links), "、".join(codes)) if x]},
        "假设": assume,
        "诊断": {"缺口数": len(gaps),
                 "按类": {g: sum(1 for x in gaps if x["类"] == g)
                          for g in tax_inspect.GAP_TYPES},
                 "来源": ("tax_inspect 的输出" if isinstance(diagnosis, dict)
                          and "问询" in diagnosis else "调用方给的缺口清单" if gaps else "无"),
                 "缺范围": [g for g in gaps if not g["要点"] and not g["域"]]},
        "本次范围": {"活动数": len(items),
                     "覆盖环节": tax_inspect._ordered_union(i["环节代号"] for i in items),
                     "注册表活动总数": len(all_codes)},
        "未被处置的缺口": unmatched,
        "本次范围外的缺口": out_of_range,
        "下限环节未牵动": floor_missing,
        "未生成": {"活动数": len(unasked), "清单": unasked, "说明": note},
        "控制活动": items,
        "边界": ("本模块给的是制度文本（责任岗位、频率、复核点、留痕、保存期限），"
                 "不是整改清单：只要清单时不触发本模块。整改只把真实发生过事项的证据补齐、"
                 "把流程固化，不写任何改动历史事实的动作；本表也不预测检查结果。"
                 "保存期限是管理下限，法定年限以 ⑧ 定级挑出的文件为准；"
                 "政策口径不清的缺口，处置仍是回 ⑧ 与 ⑨ 查现行口径，不由本表定口径。"),
    }


# ── 5 排版 ───────────────────────────────────────────────────

def render(out: dict) -> str:
    L = [f"内控生成：{out['本次范围']['活动数']} 条控制活动，形态为{out['形态']}",
         f"诊断：{out['诊断']['来源']}，缺口 {out['诊断']['缺口数']} 条（"
         + "｜".join(f"{k} {v}" for k, v in out["诊断"]["按类"].items()) + "）"]
    if out["筛选"]["条件"]:
        L.append(f"筛选条件：{'；'.join(out['筛选']['条件'])}")
    if out["假设"]:
        L.append("所依据的假设（通用模板不是本企业的制度）：")
        for a in out["假设"]:
            L.append(f"  · {a}")
    if out["诊断"]["缺范围"]:
        L.append(f"缺口没写明检查要点或域的有 {len(out['诊断']['缺范围'])} 条——"
                 f"这类缺口只能按缺口类别粗配，指不到具体环节，补上代号才算定到位")
    if out["下限环节未牵动"]:
        L.append(f"九环节下限里本次没牵动到制度的：{'、'.join(out['下限环节未牵动'])}"
                 f"——没牵动只说明本次诊断没有指向这些环节，不代表这些环节没有洞")
    for i in out["控制活动"]:
        L.append(f"\n【{i['代号']} {i['环节']}】{i['活动']}")
        if i["触发缺口"]:
            L.append("  由这些缺口牵动：")
            for g in i["触发缺口"]:
                L.append(f"    [{g['类']}] {g['对象']}（{g['要点'] or g['域']}）")
        else:
            L.append("  由假设牵动（本次没有诊断输入对着它）")
        for k in ELEMENT_KEYS:
            v = i[k]
            if k in LIST_ELEMENTS:
                L.append(f"  {k}：")
                L.extend(f"    · {x}" for x in v)
            else:
                L.append(f"  {k}：{v}")
    if out["未被处置的缺口"]:
        L.append("\n本表没有对着它的制度、需要人工补或补本表的缺口：")
        for g in out["未被处置的缺口"]:
            L.append(f"  [{g['类']}] {g['对象']}（{g['要点'] or g['域'] or '范围未指明'}）")
    if out["本次范围外的缺口"]:
        L.append("\n本次筛选范围外被排除的缺口（本表有对着的制度，但环节／活动代号没取它）：")
        for g in out["本次范围外的缺口"]:
            L.append(f"  [{g['类']}] {g['对象']}（{g['要点'] or g['域'] or '范围未指明'}）"
                     f"→ 本表里对应 {'、'.join(g['应在'])}")
    L.append(f"\n未生成：{out['未生成']['说明']}")
    for x in out["未生成"]["清单"]:
        L.append(f"  · {x['代号']} {x['环节']}／{x['活动']}")
    L.append(f"\n{out['边界']}")
    return "\n".join(L)


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="内控生成：把检查缺口换成分等级制度文本（离线）")
    ap.add_argument("--list", action="store_true", help="列环节、活动、诊断入口与九要素格名")
    ap.add_argument("--diagnosis", default="", metavar="JSON",
                    help="诊断文件：tax_inspect.py --json 的整个输出，或 {\"缺口\":[...]}/裸清单")
    ap.add_argument("--link", action="append", default=[], metavar="H1",
                    help="按环节代号或环节名取，可重复")
    ap.add_argument("--activity", action="append", default=[], metavar="H1-1",
                    help="按活动代号取，可重复")
    ap.add_argument("--assume", action="append", default=[], metavar="假设",
                    help="只要通用模板时显式给出的假设，可重复；不填又没诊断则拒生成")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    reg = load()
    if args.list:
        print(f"九要素：{'、'.join(ELEMENT_KEYS)}")
        print(f"环节下限：{'、'.join(LINK_FLOOR)}")
        for link in reg["控制环节"]:
            print(f"\n{link['代号']} {link['环节']}"
                  f"{'（下限环节）' if link['环节'] in LINK_FLOOR else '（另设环节）'}")
            for act in link["控制活动"]:
                print(f"  {act['代号']} {act['活动']}")
                print(f"      处置 {'、'.join(act['处置缺口'])}"
                      f"｜针对 {'、'.join(act['针对要点']) or '、'.join(act['针对检查域'])}")
        print(f"\n环节共 {len(reg['控制环节'])} 个，活动共 {len(activities(reg))} 条")
        return 0
    try:
        out = build(_read_json(args.diagnosis) if args.diagnosis else None,
                    args.assume, args.link, args.activity, reg)
    except (ValueError, KeyError, json.JSONDecodeError, FileNotFoundError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=1))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else render(out))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
