#!/usr/bin/env python3
"""证据覆盖率：把"这类题手里必须有什么"从散文清单推导成一个数。

原先这份清单只住在两处散文里——SKILL.md ① 表格的「必需依据」一列，和 ② 那四根前提轴。
读完知道该查什么，但没人能逐条判"这一项有没有"，于是"依据是否充分"始终是作答人自己
给的一句话。这张表（`data/evidence_requirements.json`）把它拆成逐项可判的条目，每项
落四种状态之一（已满足 / 缺 / 不适用 / 待核），覆盖率 = 已满足 ÷（总数 − 不适用）。

三处消费同一张表，这是它值钱的地方：
  ② 前提补齐 —— 前提轴那几项判"缺"，追问句直接取 `tax_analyze.CONTEXT_AXES[轴]["probe"]`；
  ⑧ 依据定级 —— 依据要件那几项判"缺"，就是"这一层还没检到"的清单；
  计算取参 —— 算式那一项判"缺"，参数名直接来自 `tax_calc.inputs(骨架)`，不抄第二份。

命名一律引用代码里的符号集合，注册表不自立名字：`项` 逐字等于
`tax_analyze.QUESTION_TYPES[题型]["needs"]` 里的那串字，轴键等于 `CONTEXT_AXES`。
所以 `load()` 会把两边比对后报错，而不是读到一个对不上的名字就当没有这一项。

它不判的事：命中词只说明"手里有这一类材料"，不说明这条材料对不对、能不能援引——
那归 ⑧ 的三根轴与 ⑦ 第 6/17 条。覆盖率到 1.0 也不构成可以直接作答的许可。

用法：
    python scripts/tax_coverage.py --list
    python scripts/tax_coverage.py liability --question "小规模纳税人季度销售额30万缴多少"
    python scripts/tax_coverage.py liability --question "..." --evidence ev.json
    python scripts/tax_coverage.py liability --question "..." \\
        --evidence ev.json --skeleton 从价计征 --params params.json
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tax_analyze           # noqa: E402
import tax_calc              # noqa: E402
import tax_evidence          # noqa: E402

REGISTRY_PATH = HERE.parent / "data" / "evidence_requirements.json"

SATISFIED, LACK, NA, TO_VERIFY = "已满足", "缺", "不适用", "待核"
STATES = (SATISFIED, LACK, NA, TO_VERIFY)


def validate(reg: dict) -> None:
    """注册表与代码符号集合逐一对齐；漂了就报错，不静默少一项。"""
    types = tax_analyze.QUESTION_TYPES
    got = set(reg["题型"])
    if got != set(types):
        raise ValueError(
            f"注册表题型与 tax_analyze.QUESTION_TYPES 不一致：多 {sorted(got - set(types))} "
            f"少 {sorted(set(types) - got)}")
    axes = set(tax_analyze.CONTEXT_AXES)
    for name, spec in reg["题型"].items():
        want = list(types[name]["needs"])
        have = [it["项"] for it in spec["依据要件"]]
        if have != want:
            raise ValueError(f"题型「{name}」的依据要件与 needs 不一致：\n"
                             f"  needs   = {want}\n  注册表 = {have}")
        bad_axes = sorted(set(spec["前提轴"]) - axes)
        if bad_axes:
            raise ValueError(f"题型「{name}」写了未知前提轴 {bad_axes}，"
                             f"取值域是 {sorted(axes)}")
        for it in spec["依据要件"]:
            if not it["命中词"] or not all(k.strip() for k in it["命中词"]):
                raise ValueError(f"题型「{name}」要件「{it['项']}」的命中词为空——"
                                 f"空命中词会让这项永远判「缺」，覆盖率就成了假数")


def load(path: Path = None) -> dict:
    reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    validate(reg)
    return reg


def _texts(evidence: list) -> list:
    """把依据清单摊平成一段段文本，字段名沿用 ⑧ 那一份，不各定一套。"""
    out = []
    for item in evidence or []:
        if not isinstance(item, dict):
            out.append(str(item))
            continue
        out.extend(str(item.get(f) or "") for f in tax_evidence.TOPIC_FIELDS)
    return out


def assess(qtype: str, question: str = "", evidence: list = None,
           skeleton: str = "", params: dict = None, reg: dict = None) -> dict:
    """逐条判状态并算覆盖率。

    `evidence=None` 与 `evidence=[]` 是两件事：前者是"还没检"，判 `待核`；
    后者是"检了一圈没有这类材料"，判 `缺`。把两者混成一个，检索失败就会被
    读成"这类题不需要依据"，那正是本仓库反复修的那类静默降级。
    """
    reg = reg or load()
    if qtype not in reg["题型"]:
        raise KeyError(f"注册表里没有这个题型：{qtype}，可选 {sorted(reg['题型'])}")
    spec = reg["题型"][qtype]
    items = []

    blob = _texts(evidence)
    for it in spec["依据要件"]:
        if evidence is None:
            state, note = TO_VERIFY, "未传入依据清单，这一项脚本判不了"
        else:
            hit = [k for k in it["命中词"] if any(k in t for t in blob)]
            state, note = ((SATISFIED, "命中 " + "、".join(hit[:3])) if hit
                           else (LACK, "检回的依据里没有出现这类字样"))
        items.append({"项": it["项"], "类": "依据", "状态": state,
                      "判据": note, "缺时动作": it["缺时动作"],
                      "去哪层取": it["去哪层取"]})

    gaps = tax_analyze.detect_context_gaps(question)["missing"] if question else None
    for axis in tax_analyze.CONTEXT_AXES:
        if axis not in spec["前提轴"]:
            state, note = NA, f"「{spec['中文名']}」不按个案追问这根轴"
        elif gaps is None:
            state, note = TO_VERIFY, "未传入题面"
        elif axis in gaps:
            state, note = LACK, "题面没交代"
        else:
            state, note = SATISFIED, "题面已给出"
        items.append({"项": f"前提：{tax_analyze.CONTEXT_AXES[axis]['label']}",
                      "类": "前提", "轴": axis, "状态": state, "判据": note,
                      "缺时动作": "转 ② 追问" if state == LACK else ""})

    if not spec["算式"]:
        items.append({"项": "展开算式", "类": "计算", "状态": NA,
                      "判据": f"「{spec['中文名']}」不出数", "缺时动作": ""})
    elif not skeleton:
        items.append({"项": "展开算式", "类": "计算", "状态": TO_VERIFY,
                      "判据": f"未选定骨架，可选 {sorted(tax_calc.SKELETONS)}",
                      "缺时动作": "按税种从 tax_calc.SKELETONS 里选一个再重跑"})
    else:
        lack = [k for k in tax_calc.inputs(skeleton) if k not in (params or {})]
        items.append({"项": "展开算式", "类": "计算",
                      "状态": LACK if lack else SATISFIED,
                      "判据": (f"骨架「{skeleton}」缺参数 {'、'.join(lack)}" if lack
                               else f"骨架「{skeleton}」参数齐全"),
                      "缺时动作": "转 ② 补问或按 ③ 检回现行值，不猜数"})

    judged = [i for i in items if i["状态"] != NA]
    done = [i for i in judged if i["状态"] == SATISFIED]
    rate = round(len(done) / len(judged), 3) if judged else None
    return {
        "题型": qtype, "中文名": spec["中文名"],
        "覆盖率": rate,
        "分母说明": (f"必备 {len(items)} 项，不适用 {len(items) - len(judged)} 项，"
                    f"判得动的 {len(judged)} 项，其中已满足 {len(done)} 项"),
        "分项": items,
        "计数": {s: sum(1 for i in items if i["状态"] == s) for s in STATES},
        "补问": [tax_analyze.CONTEXT_AXES[i["轴"]]["probe"] for i in items
                if i.get("轴") and i["状态"] == LACK],
        "待办": [f"{i['项']}：{i['缺时动作']}" for i in items
                if i["状态"] in (LACK, TO_VERIFY) and i["缺时动作"]],
        "边界": "覆盖率只判\"手里有没有这一类材料\"，不判这条依据对不对、能不能援引；"
               "后者归 ⑧ 定级。到 1.0 也不构成可以直接作答的许可。",
    }


def _read_list(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # tax_aggregator 与检索脚本都把清单挂在 results 下
        return data.get("results") or data.get("rows") or data.get("list") or []
    return data


def _read_params(path: str) -> dict:
    params = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(params, dict):
        raise ValueError("--params 要写成 {参数名: 值}，参数名见 tax_calc --list")
    return params


def main(argv=None):
    ap = argparse.ArgumentParser(description="证据覆盖率（离线，不联网）")
    ap.add_argument("qtype", nargs="?", help="题型，见 --list")
    ap.add_argument("--list", action="store_true", help="列出题型与每型的必备项数")
    ap.add_argument("--question", default="", help="用户原话，用来判前提轴")
    ap.add_argument("--evidence", default="",
                    help="依据清单 JSON（列表或 {\"results\": [...]}）；不给就判待核")
    ap.add_argument("--skeleton", default="", help="已选的 tax_calc 骨架名")
    ap.add_argument("--params", default="", help="已备参数 JSON（{参数名: 值}）")
    args = ap.parse_args(argv)

    if args.list:
        reg = load()
        for k, spec in reg["题型"].items():
            print(f"{k}（{spec['中文名']}）：依据 {len(spec['依据要件'])} 项、"
                  f"前提 {len(spec['前提轴'])} 轴、算式 "
                  f"{'要' if spec['算式'] else '不要'}")
        return 0
    if not args.qtype:
        ap.error("要指定题型，或用 --list 看清单")

    try:
        out = assess(args.qtype, args.question,
                     _read_list(args.evidence) if args.evidence else None,
                     args.skeleton,
                     _read_params(args.params) if args.params else {})
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
