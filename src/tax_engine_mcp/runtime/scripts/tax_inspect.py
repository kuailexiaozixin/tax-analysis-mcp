#!/usr/bin/env python3
"""稽查模拟：站在检查人员的视角出题，判"这一问我现在答不答得上"。

范式是"用对手方测试自己"：不是先想自己做了什么再去挑依据，而是按检查人员的问法
逐问要证据。每问固定六格（`SIX_FIELDS`）——检查目的／现有回答／支撑材料／证据缺口／
可能追问／整改动作，少任何一格就只是提示，不是能拿去执行的问询。

它不做的三件事，都写进了输出：
  · 不预测稽查结果（⑦ 第 11 条）：只说"这一问缺哪一份材料"，不说"一定会被查"。
    载入时按 `PREDICTION_PHRASES` 扫注册表的问询与追问文本，注册表自己先不许写这种话。
  · 不替企业作答：`现有回答` 只从调用方给的 `回答` 取，没给就写"未作答"，
    不编一句看起来像答案的话。
  · 不由缺项反推结论：本次没问到的要点全部列进「未询问」，覆盖判 `partial`。
    "没问"与"问了没查出问题"是两件事，写成后者就是把没做说成做过。

判据来自 `data/inspection_domains.json`（七个检查域、22 个检查要点，按事项切而不
按税种切，所以同一处工时划分会同时出现在加计扣除、高企认定与个税薪金三路）。缺口只有
三类（`GAP_TYPES`）：缺材料／材料矛盾／政策口径不清。第三类的处置不是由企业自选口径，
而是回 ⑧ 定级与 ⑨ 类案查现行口径，这一句随缺口一起输出。

`整改动作` 有红线：只许写"把真实发生过的事项证据补齐、把流程固化"，不许写补造、
倒签、补签、重做历史资料。`FORBIDDEN_REMEDIATION` 是这条红线的机器判据，载入时
逐条扫表里的整改动作；命中就报错，而不是等输出里冒出来再靠人眼抓。

用法：
    python scripts/tax_inspect.py --list
    python scripts/tax_inspect.py --domain D1 --domain D2 --answers ans.json
    python scripts/tax_inspect.py --scenario 增值税进项税额抵扣 --json
    python scripts/tax_inspect.py --signal 混岗 --tolerance 0.005

ans.json 形如 {"D1-1": {"回答": "…", "材料": ["工时记录"], "数值":
{"工时表研发工时合计": 1200, "费用分摊计算表的研发工时合计": 1150},
"口径年度": "2024", "口径依据": "…"}}——键用要点代号，见 `--list`。
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REGISTRY_PATH = HERE.parent / "data" / "inspection_domains.json"

#: 每一问必须带齐的六格，顺序即输出顺序（SKILL ⑥「稽查模拟问询式」同一顺序）。
SIX_FIELDS = ("检查目的", "现有回答", "支撑材料", "证据缺口", "可能追问", "整改动作")

#: 注册表里每一格检查要点的必填键。「触发信号」不在其内——域级不再另列一份词表，
#: 词表由要点的『风险指标』派生（见 `signals`）。
POINT_KEYS = ("代号", "主题", "问询", "检查目的", "支撑材料", "可能追问",
              "整改动作", "一致性核对", "政策口径依赖", "风险指标")

GAP_MISSING, GAP_CONFLICT, GAP_CALIBER = "缺材料", "材料矛盾", "政策口径不清"
GAP_TYPES = (GAP_MISSING, GAP_CONFLICT, GAP_CALIBER)

#: 三类缺口的动作各自不同，且都不含"替企业把这一格填上"。
GAP_ACTION = {
    GAP_MISSING: "向企业要这一份材料；没到手之前这一格空着，不代写",
    GAP_CONFLICT: "把两处数与差额写成一条说明交出去，说明差异从哪一笔记账产生",
    GAP_CALIBER: "回 ⑧ 定级与 ⑨ 类案核现行口径，不由企业自选一个口径填上",
}

#: 整改动作的禁用写法。判据取"改动历史事实"这一类动词，不取"补"这个字：
#: 「补充履约证明」「补问」是正当动作，把"补"整个禁掉会让注册表通不过自己的检查。
FORBIDDEN_REMEDIATION = ("补造", "伪造", "虚构", "倒签", "补签", "重签", "回填",
                         "涂改", "事后制作", "重新制作历史", "改历史", "换凭证",
                         "补开历史", "代做账")

#: 预测稽查结果的说法。红线在 ⑦ 第 11 条，这里管的是注册表与追问文本自己不许写；
#: 调用方给的『回答』原样转述，不在这里改写企业的话。
PREDICTION_PHRASES = ("一定会被查", "肯定被查", "必被查", "不会被罚", "不会稽查",
                      "不会被稽查", "绝对安全", "必定追缴")

#: 材料名匹配的判据：去掉括号里的限定语后比包含，短于这个长度的串不算命中——
#: 「表」这种单字包含会把三份不同的表认成同一份。
MATCH_MIN = 3

COVERAGE_COMPLETE, COVERAGE_PARTIAL = "complete", "partial"
#: 一份支撑材料的两种状态。"缺材料"这一类缺口的名字比它宽，不拿来当格子的取值，
#: 否则读 JSON 时分不清"这一份没给"和"这一问整体缺材料"是不是同一件事。
PROVIDED, LACKING = "已提供", "缺"
#: 一致性核对四值。"缺一边"与"未取数"都不许写成"一致"：单边有数说明这一对还没做成，
#: 把它归进任何一侧都是替检查结果说话。
CHECK_AGREED, CHECK_CONFLICT, CHECK_ONE_SIDED, CHECK_NOT_TAKEN = (
    "一致", "矛盾", "缺一边", "未取数")
#: 回答里"给了但没被用上"的两种成因，各配一句动作。键名由 `build` 建、`render` 说，
#: 只定义在这一处——两处各写一份就会漂成一个键没人报，等于静默丢材料。
UNUSED_UNKNOWN, UNUSED_OUT_OF_SCOPE = "代号不在注册表里", "本次没问这一格"
UNUSED_HINTS = {
    UNUSED_UNKNOWN: "注册表里没这个代号，那份材料没参与任何一问的判定——"
                    "要点代号照 `--list` 写",
    UNUSED_OUT_OF_SCOPE: "这一格在本次筛选范围外，回答留着但没被问——放宽范围再跑一次"
                         "才算问过它",
}
UNANSWERED = "未作答——检查人员问到这一句时没有可交出去的说法"


# ── 1 载入与自检 ──────────────────────────────────────────────

def _bad_words(text: str, words) -> list:
    return [w for w in words if w in (text or "")]


def _nonempty_strs(value, where: str, field: str) -> list:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} 的『{field}』必须是非空清单")
    out = []
    for i, v in enumerate(value):
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{where} 的『{field}』第 {i + 1} 项是空的——"
                             f"空条目会让这一格看起来有内容其实判不动")
        out.append(v.strip())
    return out


def validate(reg: dict) -> None:
    """注册表的结构与红线在载入时判，不留到输出里靠人眼抓。"""
    domains = reg.get("检查域")
    if not isinstance(domains, list) or not domains:
        raise ValueError("注册表里没有『检查域』清单")
    codes = [d.get("代号") for d in domains]
    want = [f"D{i}" for i in range(1, len(codes) + 1)]
    if codes != want:
        raise ValueError(f"域代号必须从 D1 连续排下来：读到 {codes}，应为 {want}")
    if any("触发信号" in d for d in domains):
        raise ValueError("域级『触发信号』已取消：词表只由要点的『风险指标』派生，"
                         "两处各写一份必然漂（注册表『风险指标的口径』记着这次的数字）")
    seen_points = set()
    for d in domains:
        name = d.get("代号")
        for field in ("域", "适用"):
            if not (d.get(field) or ""):
                raise ValueError(f"{name} 缺『{field}』")
        _nonempty_strs(d.get("适用"), f"域 {name}", "适用")
        points = d.get("检查要点")
        if not points:
            raise ValueError(f"域 {name} 没有检查要点——空域会让覆盖数看着够，实际一格判不动")
        for pt in points:
            code = pt.get("代号") or "（无代号）"
            where = f"{name}／要点 {code}"
            if not code.startswith(name + "-"):
                raise ValueError(f"要点代号 {code} 不在所属域 {name} 下")
            if code in seen_points:
                raise ValueError(f"要点代号 {code} 重复")
            seen_points.add(code)
            for field in POINT_KEYS:
                if field not in pt:
                    raise ValueError(f"{where} 缺『{field}』这一格")
            for field in ("主题", "问询", "检查目的", "政策口径依赖"):
                if not str(pt.get(field) or "").strip():
                    raise ValueError(f"{where} 的『{field}』是空的")
            for field in ("支撑材料", "可能追问", "整改动作"):
                _nonempty_strs(pt[field], where, field)
            inds = _nonempty_strs(pt["风险指标"], where, "风险指标")
            if len(set(inds)) != len(inds):
                raise ValueError(f"{where} 的风险指标有重复项")
            for pair in pt["一致性核对"]:
                for k in ("项A", "项B", "说明"):
                    if not str(pair.get(k) or "").strip():
                        raise ValueError(f"{where} 的一致性核对缺『{k}』")
            # 红线一：整改动作不许建议改动历史事实
            for act in pt["整改动作"]:
                bad = _bad_words(act, FORBIDDEN_REMEDIATION)
                if bad:
                    raise ValueError(f"{where} 的整改动作写了越红线的话 {bad}：{act}")
            # 红线二：本模块不预测稽查结果
            for field in ("问询", "检查目的", "可能追问"):
                value = pt[field] if isinstance(pt[field], str) else " ".join(pt[field])
                bad = _bad_words(value, PREDICTION_PHRASES)
                if bad:
                    raise ValueError(f"{where} 的『{field}』写了稽查结果预测 {bad}："
                                     f"只能说会触发风险指标")


def load(path: Path = None) -> dict:
    reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    validate(reg)
    return reg


# ── 2 词表：只从要点派生，不抄第二份 ──────────────────────────

def points(reg: dict = None) -> list:
    """摊平成 (域, 要点) 对，顺序即注册表顺序。"""
    reg = reg or load()
    out = []
    for d in reg["检查域"]:
        for pt in d["检查要点"]:
            out.append((d, pt))
    return out


def _ordered_union(values) -> list:
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def signals(reg: dict = None) -> list:
    """全部风险指标，按注册表出现顺序去重。`--signal` 的取值域就是这一串。"""
    return _ordered_union(r for _d, pt in points(reg) for r in pt["风险指标"])


def scenarios(reg: dict = None) -> list:
    """全部适用场景，按注册表出现顺序。`--scenario` 的取值域就是这一串。"""
    return _ordered_union(s for d in (reg or load())["检查域"] for s in d["适用"])


# ── 3 选材：按域／要点／场景／风险指标筛，筛空要说什么筛的 ──────

def select(reg: dict, domains=(), codes=(), scenario="", signal="") -> list:
    """返回 (域, 要点) 子集。取值认不全就报错列出现有取值，不静默筛成空清单。

    四个条件是"或"的关系以外的最松组合：给了 `domains`／`codes` 就先按它们收，
    `scenario`／`signal` 是在已收的范围内再筛一次，所以 `--domain D4 --scenario 增值税`
    读作「D4 里牵动增值税进项的那几问」。
    """
    pool = points(reg)
    known_domains = [d["代号"] for d in reg["检查域"]]
    known_codes = [pt["代号"] for _d, pt in pool]
    if domains:
        bad = [x for x in domains if x not in known_domains]
        if bad:
            raise ValueError(f"没有这些域：{bad}，可选 {known_domains}")
        pool = [(d, pt) for (d, pt) in pool if d["代号"] in domains]
    if codes:
        bad = [x for x in codes if x not in known_codes]
        if bad:
            raise ValueError(f"没有这些要点：{bad}，可选 {known_codes}")
        pool = [(d, pt) for (d, pt) in pool if pt["代号"] in codes]
    if scenario:
        known = scenarios(reg)
        if not any(scenario in s or s in scenario for s in known):
            raise ValueError(f"『{scenario}』不在适用场景里，现有取值：{known}")
        pool = [(d, pt) for (d, pt) in pool
                if any(scenario in s or s in scenario for s in d["适用"])]
    if signal:
        known = signals(reg)
        if not any(signal in s for s in known):
            raise ValueError(f"『{signal}』对不上任何风险指标，现有取值：{known}")
        pool = [(d, pt) for (d, pt) in pool
                if any(signal in r for r in pt["风险指标"])]
    return pool


# ── 4 三类缺口的判据 ─────────────────────────────────────────

def stem(material: str) -> str:
    """去掉括号里的限定语：「研发人员名册（含入职时间与岗位）」取「研发人员名册」。

    企业交出的材料名不会带限定语，逐字比必然判缺；限定语留在判据里给人看，
    不参与匹配。
    """
    return re.split(r"[（(]", material, maxsplit=1)[0].strip()


def covered_by(required: str, provided) -> str:
    """这一份必备材料被哪一句交出的资料对上；对不上返回空串。

    先比全等，再比双向包含：企业写「工时记录」对上表里的「逐人逐项目的工时记录」，
    写全名也照样对上。较短一侧短于 `MATCH_MIN` 时不认包含——「表」这种单字包含会把
    三张不同的表认成同一份，那是把没提供的材料记成已提供。
    """
    req = stem(required)
    for p in provided or []:
        have = stem(str(p))
        if not have:
            continue
        if have == req or (min(len(have), len(req)) >= MATCH_MIN
                           and (have in req or req in have)):
            return str(p).strip()
    return ""


def _num(value, item: str, code: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"『{code}』的核对项「{item}」给了非数值 {value!r}——"
                         f"这一项要么给数，要么不给（不给就判『缺一边』）") from None


def diff_text(a: float, b: float) -> str:
    """两处数的差：绝对值与相对值都给，只给一个的话读的人不知道差在哪一端。"""
    base = max(abs(a), abs(b))
    pct = abs(a - b) / base * 100 if base else 0.0
    return f"差 {abs(a - b):g}（{pct:.2f}%）"


def check_pairs(pt: dict, numbers: dict, tol: float) -> tuple:
    """逐对比一处一致性核对，返回 (核对记录, 材料矛盾缺口, 用不上的数)."""
    rows, gaps = [], []
    for pair in pt["一致性核对"]:
        a, b = numbers.get(pair["项A"]), numbers.get(pair["项B"])
        row = dict(pair)
        if a is None or b is None:
            row["状态"] = CHECK_ONE_SIDED if (a is not None or b is not None) \
                else CHECK_NOT_TAKEN
            row["取值"] = "" if a is None and b is None else \
                f"{pair['项A'] if a is not None else pair['项B']} 有数，另一处没数"
        else:
            a = _num(a, pair["项A"], pt["代号"])
            b = _num(b, pair["项B"], pt["代号"])
            row["取值"] = f"{pair['项A']} {a:g}｜{pair['项B']} {b:g}"
            base = max(abs(a), abs(b))
            ok = abs(a - b) <= tol * base if base else True
            row["状态"] = CHECK_AGREED if ok else CHECK_CONFLICT
            if not ok:
                row["差额"] = diff_text(a, b)
                gaps.append({"类": GAP_CONFLICT,
                             "对象": f"{pair['项A']} ↔ {pair['项B']}",
                             "说明": f"{row['取值']}，{row['差额']}。{pair['说明']}",
                             "动作": GAP_ACTION[GAP_CONFLICT]})
        rows.append(row)
    names = {k for pair in pt["一致性核对"] for k in (pair["项A"], pair["项B"])}
    unused = sorted(set(numbers) - names)
    return rows, gaps, unused


def caliber_gap(pt: dict, answer: dict) -> dict:
    """政策口径不清：适用年度或依据文件任一格没交代，就把这一问的口径标成不清。

    判据不看口径说得好不好，只看这两格在不在——本模块没有政策文本，判不了口径
    对不对，能判的是"这一问的答案有没有把口径绑到某一年、某一份文件上"。
    """
    lack = [k for k in ("口径年度", "口径依据") if not str(answer.get(k) or "").strip()]
    if not lack:
        return {}
    return {"类": GAP_CALIBER, "对象": pt["政策口径依赖"],
            "说明": f"没交代 {'、'.join(lack)}——口径落在哪一年、按哪份文件，"
                    f"这一问现在答不出去",
            "动作": GAP_ACTION[GAP_CALIBER]}


# ── 5 生成问询 ───────────────────────────────────────────────

def _answer_for(answers: dict, code: str) -> dict:
    got = (answers or {}).get(code) or {}
    if not isinstance(got, dict):
        raise ValueError(f"『{code}』那条要写成对象（材料／回答／数值／口径年度／口径依据）")
    for k in ("材料", "数值"):
        if k in got and not isinstance(got[k], dict if k == "数值" else list):
            raise ValueError(f"『{code}』的『{k}』类型不对：材料要清单、数值要 {{核对项名: 数}}")
    return got


def build(answers: dict = None, domains=(), codes=(), scenario="", signal="",
          tolerance: float = 0.0, reg: dict = None) -> dict:
    """按选出的检查要点逐问生成六字段问询单元，并判三类缺口。

    `answers` 缺省或某一条缺项都判得动：没作答是一格状态，不是跳过这一问的理由。
    `tolerance` 是相对容差（0.005 = 半个百分点），只在 0–1 之间有意义；超出报
    `ValueError`，因为容差 1.2 等于把所有差异都判成一致，那就不是核对而是放行。
    """
    reg = reg or load()
    if not 0.0 <= tolerance < 1.0:
        raise ValueError(f"容差要在 [0, 1) 之间（相对差），读到 {tolerance}")
    pool = select(reg, domains, codes, scenario, signal)
    asked = {pt["代号"] for _d, pt in pool}
    # 回答里的代号必须落得住：写错一个代号的那份材料会静默不参与判定，
    # 读起来却像"这一问答过了"。两种落不住分开列，成因不同、动作也不同。
    all_codes = {pt["代号"] for _d, pt in points(reg)}
    supplied = set(answers or {})
    unused_answers = {UNUSED_UNKNOWN: sorted(supplied - all_codes),
                      UNUSED_OUT_OF_SCOPE: sorted(supplied & all_codes - asked)}
    units = []
    for d, pt in pool:
        got = _answer_for(answers, pt["代号"])
        provided = got.get("材料") or []
        rows, conflicts, unused = check_pairs(pt, got.get("数值") or {}, tolerance)
        materials = []
        for m in pt["支撑材料"]:
            hit = covered_by(m, provided)
            materials.append({"材料": m,
                              "状态": PROVIDED if hit else LACKING,
                              "对上这句": hit})
        gaps = [{"类": GAP_MISSING, "对象": m["材料"],
                 "说明": "本次交出的资料里没有这一份",
                 "动作": GAP_ACTION[GAP_MISSING]}
                for m in materials if m["状态"] == LACKING]
        gaps += conflicts
        cal = caliber_gap(pt, got)
        if cal:
            gaps.append(cal)
        units.append({
            "代号": pt["代号"], "域代号": d["代号"], "域": d["域"],
            "主题": pt["主题"], "问询": pt["问询"],
            "风险指标": pt["风险指标"],
            "是否作答": bool(got.get("回答") or provided or got.get("数值")),
            "检查目的": pt["检查目的"],
            "现有回答": str(got.get("回答") or "").strip() or UNANSWERED,
            "支撑材料": materials,
            "证据缺口": gaps,
            "可能追问": pt["可能追问"],
            "整改动作": pt["整改动作"],
            "一致性核对": rows,
            "口径": {"适用年度": str(got.get("口径年度") or "").strip(),
                     "依据文件": str(got.get("口径依据") or "").strip()},
            "核对未用": unused,
        })
    gap_count = {g: sum(1 for u in units for x in u["证据缺口"] if x["类"] == g)
                 for g in GAP_TYPES}
    unasked = [{"代号": p["代号"], "域代号": d["代号"], "域": d["域"], "主题": p["主题"]}
               for d, p in points(reg) if p["代号"] not in asked]
    filtered = [x for x in ("、".join(domains), "、".join(codes), scenario, signal) if x]
    how = f"（本次筛过范围：{'；'.join(filtered)}）" if filtered else ""
    if not units:
        note = (f"本次筛选没命中任何要点{how}——这是范围筛空，不是全部问完，"
                f"未询问 {len(unasked)} 格照旧列在下面")
    elif unasked:
        tail = how if how else "——没问不等于没问题，这一份不能当成全项核过"
        note = f"另有 {len(unasked)} 个要点本次没问{tail}"
    else:
        note = "注册表内每个要点都问到了"
    return {
        "筛选": {"域": list(domains), "要点": list(codes), "场景": scenario,
                 "风险指标": signal, "条件": filtered},
        "本次范围": {
            "问询数": len(units),
            "覆盖域": _ordered_union(u["域代号"] for u in units),
            "注册表要点总数": len(asked) + len(unasked),
        },
        "覆盖": {"状态": COVERAGE_PARTIAL if unasked else COVERAGE_COMPLETE,
                 "已问": len(asked), "未询问": unasked, "说明": note},
        "未用的回答": unused_answers,
        "缺口统计": gap_count,
        "问询": units,
        "边界": ("本模块以检查人员视角出题，不预测稽查结果：只说这一问缺哪一份材料、"
                 "哪两处数对不上，不对稽查与处罚的结果下判断。具体政策口径、罚则与追缴年度"
                 "仍以 ⑧ 定级挑出的文件为准；涉及口径不清的这一问，处置是回 ⑧ 与 ⑨ 查现行口径。"),
    }


# ── 6 排版 ───────────────────────────────────────────────────

def render(out: dict) -> str:
    L = [f"稽查模拟问询：{out['本次范围']['问询数']} 问，"
         f"覆盖域 {'、'.join(out['本次范围']['覆盖域']) or '无'}",
         f"覆盖状态：{out['覆盖']['状态']}｜{out['覆盖']['说明']}"]
    if out["筛选"]["条件"]:
        L.append(f"筛选条件：{'；'.join(out['筛选']['条件'])}")
    L.append("缺口统计：" + "｜".join(f"{k} {v}" for k, v in out["缺口统计"].items()))
    for u in out["问询"]:
        L.append(f"\n【{u['代号']} {u['域']}／{u['主题']}】")
        if not u["是否作答"]:
            L.append("  本企业本次未提供这一问的任何材料或说法。")
        L.append(f"  问询：{u['问询']}")
        for name in SIX_FIELDS:
            if name == "支撑材料":
                L.append("  支撑材料：")
                for m in u["支撑材料"]:
                    mark = "已有" if m["状态"] == PROVIDED else "缺"
                    extra = f"（对上「{m['对上这句']}」）" if m["对上这句"] else ""
                    L.append(f"    [{mark}] {m['材料']}{extra}")
            elif name == "证据缺口":
                L.append(f"  证据缺口：{len(u['证据缺口'])} 条")
                for g in u["证据缺口"]:
                    L.append(f"    [{g['类']}] {g['对象']}——{g['说明']}")
                    L.append(f"           动作：{g['动作']}")
            elif name in ("可能追问", "整改动作"):
                L.append(f"  {name}：")
                for x in u[name]:
                    L.append(f"    · {x}")
            else:
                L.append(f"  {name}：{u[name]}")
        if u["一致性核对"]:
            L.append("  一致性核对：")
            for r in u["一致性核对"]:
                line = f"    [{r['状态']}] {r['项A']} ↔ {r['项B']}"
                if r.get("取值"):
                    line += f"｜{r['取值']}"
                if r.get("差额"):
                    line += f"｜{r['差额']}"
                L.append(line)
                L.append(f"           说明：{r['说明']}")
        if u["核对未用"]:
            L.append(f"  数值里给了本问没有的核对项：{'、'.join(u['核对未用'])}"
                     f"——名字对不上的数不会参与判定，照 `--list` 的核对项名写")
        L.append(f"  冲着这些风险指标来的：{'、'.join(u['风险指标'])}")
    for key, hint in UNUSED_HINTS.items():
        for bad in out["未用的回答"].get(key, []):
            L.append(f"未用的回答 {bad}：{hint}")
    if out["覆盖"]["未询问"]:
        L.append("\n未询问（本次没问，不代表没问题）：")
        for x in out["覆盖"]["未询问"]:
            L.append(f"  · {x['代号']} {x['域']}／{x['主题']}")
    L.append(f"\n{out['边界']}")
    return "\n".join(L)


def _read_answers(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--answers 要写成 {要点代号: {材料/回答/数值/口径年度/口径依据}}")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="税务稽查模拟问询（离线，不联网）")
    ap.add_argument("--list", action="store_true", help="列域、要点、适用场景与风险指标")
    ap.add_argument("--domain", action="append", default=[], metavar="D1",
                    help="按检查域取，可重复")
    ap.add_argument("--point", action="append", default=[], metavar="D1-1",
                    help="按检查要点代号取，可重复")
    ap.add_argument("--scenario", default="", metavar="场景",
                    help="按适用场景取（取值见 --list），在已选范围内再筛")
    ap.add_argument("--signal", default="", metavar="风险指标",
                    help="按风险指标字样取（取值见 --list），在已选范围内再筛")
    ap.add_argument("--answers", default="", metavar="JSON",
                    help="企业的回答：{要点代号: {材料/回答/数值/口径年度/口径依据}}")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="一致性核对的相对容差，[0,1)，默认 0（差异即报）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    reg = load()
    if args.list:
        for d in reg["检查域"]:
            print(f"{d['代号']} {d['域']}（适用：{'、'.join(d['适用'])}）")
            for pt in d["检查要点"]:
                pairs = "；".join(f"{p['项A']}↔{p['项B']}" for p in pt["一致性核对"])
                print(f"  {pt['代号']} {pt['主题']}｜材料 {len(pt['支撑材料'])} 份"
                      f"｜核对项 {pairs or '无'}｜指标 {'、'.join(pt['风险指标'])}")
        print(f"\n要点共 {len(points(reg))} 个；场景取值 {scenarios(reg)}；"
              f"风险指标取值 {signals(reg)}")
        return 0
    try:
        out = build(_read_answers(args.answers) if args.answers else {},
                    args.domain, args.point, args.scenario, args.signal,
                    args.tolerance, reg)
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
