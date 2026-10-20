#!/usr/bin/env python3
"""算式展开器：把一部税的算法骨架展开成逐步算式，数值全部由调用方喂进来。

这一层刻意不含任何税率、扣除标准、档界——它只知道四则运算的顺序，以及累进税
"分段累加"与"速算扣除"这两条互验的路子。原因有两个：

1. 政策一动，写死在代码里的税率就变成错的答案。本技能的主线是 ③ 层级下挖检回
   现行文件，税率与扣除标准应当从那一步取，而不是从这份脚本记忆。
2. 一旦允许写死，界面就答不出"这个 13% 是从哪份文件取的"。参数带来源是答案
   能被引用的前提，见 `references/output_templates.md` 的计算式模板。

所以：缺参数就报"缺哪几个"，转 ② 前提补齐，不猜、不用训练数据里的旧值顶上。

用法：
    python scripts/tax_calc.py --list
    python scripts/tax_calc.py 增值税一般计税 \\
        --set 不含税销售额=1000000 --src 不含税销售额=http://www.chinatax.gov.cn/xxx \\
        --set 适用税率=0.13 --src 适用税率=http://... \\
        --set 进项税额=200000 --src 进项税额=http://...
    python scripts/tax_calc.py 超率累进 --params params.json
参数文件是 {"参数名": {"值": 0.13, "来源": "http://..."}}，也可以直接给数值。
"""

import argparse
import ast
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

DISCLAIMER = ("本引擎只做算式展开，不含任何现行税率与扣除标准；每个参数的值都来自"
              "调用方检回的文件，出处见「参数」一栏。要改口径，改参数来源，不要改这里。")

# 骨架里允许出现的数字常量：0 用来把"负数应纳税额"压成留抵，1 是占比换算的件。
# 出现别的数字就说明有人把政策数值写进了骨架，tests/test_calc.py 逐条扫这一点。
_ALLOWED_CONSTANTS = {0, 1}
_ALLOWED_FUNCS = {"max", "min", "abs", "分段累进", "超率累进"}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
                  ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)


def _dec(v):
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool) or v is None:
        raise ValueError(f"参数值不是数值：{v!r}")
    if isinstance(v, (int, float, str)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            # 千分位写法（"1,000,000"）会走到这里；报清是哪一层坏了，别让它冒成引擎故障
            raise ValueError(f"参数值不是可解析的数值：{v!r}") from None
    raise ValueError(f"参数值不是数值：{v!r}")


def _q(v):
    return _dec(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _show(v):
    """展示口径：金额到分，比率（绝对值小于 1）到小数点后六位。

    算式展开要给人对着文件核，核的就是这几位数字；比率压成分位就看不出去了
    （增值率 0.852352 与 0.85 在表上落在同一档，但读者无从判断是不是刚好压线）。
    """
    if isinstance(v, dict):
        return v
    if not isinstance(v, Decimal):
        return v
    if abs(v) < 1:
        # 比率补足到六位再削掉尾零：0.012 写成 0.012000 会让"代入"那行读不下去，
        # 而位数上限仍然锁住——0.852352 与 0.85 在核档界时是两回事。
        return v.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).normalize()
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _ascii(expr: str) -> str:
    """排印符号 → ASCII 运算符。只换运算符，中文参数名一律不动。

    骨架给人读，所以步骤串里写的是 `× ÷ −`；`ast.parse` 只认 ASCII，求值前换回去。
    这一处是唯一的转换点，`_parse` 与 `_refs` 都走它，免得两处各自演化。
    """
    return (expr.strip()
            .replace("×", "*").replace("÷", "/")
            .replace("−", "-").replace("＝", "=")
            .replace("（", "(").replace("）", ")"))


def _parse(expr: str):
    """解析一条算式，顺手挡掉不允许的节点：数字常量、任意函数调用、下标与属性。"""
    node = ast.parse(_ascii(expr), mode="eval")
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant):
            if sub.value not in _ALLOWED_CONSTANTS:
                raise ValueError(f"骨架里不得出现数字常量 {sub.value!r}（政策数值一律走参数）")
        elif isinstance(sub, ast.Call):
            if not isinstance(sub.func, ast.Name) or sub.func.id not in _ALLOWED_FUNCS:
                raise ValueError(f"算式只允许调用 {sorted(_ALLOWED_FUNCS)}：{expr}")
        elif not isinstance(sub, _ALLOWED_NODES):
            raise ValueError(f"算式含不允许的写法 {type(sub).__name__}：{expr}")
    return compile(node, "<calc>", "eval")


class Missing(Exception):
    """骨架要用、调用方却没给的参数。"""

    def __init__(self, names):
        super().__init__("、".join(names))
        self.names = list(names)


def _table(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("税率表必须是至少一档的列表")
    return rows


def _progressive(基数, rows, ded_key, factor):
    """分段累加与速算扣除各算一遍，两式相等才返回。

    `factor` 把档界与速算扣除换算到基数同一量纲：绝对额累进取 1；超率累进取
    扣除项目金额，此时档界是"占扣除项目金额的倍数"、速算扣除是倍数（系数）。
    两式互验查的是抄表这一件事：档界漏一档、速算扣除抄错一行，逐档加与速算加
    就会分叉，这里直接报错，不静默给出其中一个。最后一档的上限给 null。
    """
    base = _dec(基数)
    bounds = []
    for i, row in enumerate(_table(rows)):
        raw = row.get("上限")
        last = i == len(rows) - 1
        if last and raw is not None:
            raise ValueError("最后一档必须不封顶（上限为 null）")
        if not last and raw is None:
            raise ValueError(f"第 {i + 1} 档上限为 null，但它不是最后一档")
        if raw is None:
            bounds.append(None)
            continue
        bound = _dec(raw) * factor
        if bounds and bounds[-1] is not None and bound <= bounds[-1]:
            raise ValueError(f"档界不连续：第 {i + 1} 档上限 {bound} 不大于前一档 {bounds[-1]}")
        bounds.append(bound)

    total, acc, hit, detail = Decimal(0), Decimal(0), None, []
    for i, row in enumerate(rows):
        rate, top = _dec(row["税率"]), bounds[i]
        seg_top = base if top is None else min(base, top)
        seg = max(Decimal(0), seg_top - acc)
        acc = seg_top
        tax = seg * rate
        detail.append({"计入基数": _jsonable(_q(seg)), "税率": float(rate),
                       "税额": _jsonable(_q(tax)),
                       "算式": f"{_q(seg)} × {float(rate)}"})
        total += tax
        if top is None or base <= seg_top:
            hit = i
            break
    row = rows[hit]
    rate, ded = _dec(row["税率"]), _dec(row.get(ded_key, 0))
    quick = base * rate - ded * factor
    if quick != total:
        raise ValueError(
            f"税率表不自洽：第 {hit + 1} 档逐档累加得 {_q(total)}，按"
            f"「基数×{float(rate)} − {ded_key}{float(ded)}」复算得 {_q(quick)}，"
            f"两者不等，多半是档界或{ded_key}抄错了一行")
    return {"适用档": hit + 1, "逐档": detail, "结果": _q(total),
            "速算式": f"{_q(base)} × {float(rate)} − {_q(ded * factor)}"}


def 分段累进(基数, 税率表):
    """超额累进：档界是基数自身的绝对额，速算扣除数是元。"""
    return _progressive(基数, 税率表, "速算扣除数", Decimal(1))


def 超率累进(扣除项目金额, 增值额, 税率表):
    """超率累进（土地增值税那一类）：档界与速算扣除都按扣除项目金额的倍数计。"""
    return _progressive(增值额, 税率表, "速算扣除系数", _dec(扣除项目金额))


_FUNCS = {"分段累进": 分段累进, "超率累进": 超率累进,
          "max": lambda *a: max(_dec(x) for x in a),
          "min": lambda *a: min(_dec(x) for x in a), "abs": abs}

# 税率表这一枚参数要按哪几列填。列名不是随便起的：前两枚是 _progressive 直接按键
# 取的（row.get("上限")、row["税率"]），第三枚就是它收到的 ded_key 实参——分段累进
# 传 "速算扣除数"、超率累进 传 "速算扣除系数"。这一处与那两个函数之间的对应由
# tests/test_server_routes.py 的 TestCalcRoutes 钉住：拿一张不自洽的表触发报错，
# 取回的原文里必须出现这里写的第三枚列名，改了 ded_key 而没改这张表就会报红。
# 下游用途只有一个：/api/calc 把形态与列名一起给界面，让税率表这一枚参数排成
# 一行一档的编辑表，而不是让前端照猜。
RATE_TABLE_COLUMNS = {"分段累进": ["上限", "税率", "速算扣除数"],
                      "超率累进": ["上限", "税率", "速算扣除系数"]}


# ── 骨架：步骤写成「名字 = 算式」，逐条按序求值 ───────────────────────────────
SKELETONS = {
    "增值税一般计税": {
        "公式": "应纳税额 = max(0, 销售额×税率 − 进项 + 进项转出 − 上期留抵)",
        "步骤": [
            "销项税额 = 不含税销售额 × 适用税率",
            "可抵扣进项税额 = 进项税额 − 进项税额转出",
            "一般计税应纳税额 = 销项税额 − 可抵扣进项税额 − 上期留抵税额",
            "应纳税额 = max(0, 一般计税应纳税额)",
            "期末留抵税额 = max(0, 可抵扣进项税额 + 上期留抵税额 − 销项税额)",
        ],
        "结果": ["应纳税额", "期末留抵税额"],
    },
    "城建税及附加": {
        "公式": "应纳税费 = 实缴增值税与消费税之和 ×（城建税率 + 两附加费率）",
        "步骤": [
            "计税依据 = 实缴增值税 + 实缴消费税",
            "城市维护建设税 = 计税依据 × 城建税税率",
            "教育费附加 = 计税依据 × 教育费附加费率",
            "地方教育附加 = 计税依据 × 地方教育附加费率",
            "应纳税费合计 = 城市维护建设税 + 教育费附加 + 地方教育附加",
        ],
        "结果": ["城市维护建设税", "教育费附加", "地方教育附加", "应纳税费合计"],
    },
    "超额累进": {
        "公式": "应纳税额 = 分段累进(计税基数, 税率表)",
        "步骤": ["应纳税额 = 分段累进(计税基数, 税率表)"],
        "结果": ["应纳税额"],
    },
    "综合所得年度汇算": {
        # 这一步存在的理由：③ 检回的是"基本减除费用每年 X 元"这类条文，而算式要的
        # 是数进率表之前的那个所得额。把减法写成显式步骤，中间量才留在输出里可核。
        "公式": "应纳税所得额 = 收入总额 − 费用减除 − 专项扣除 − 专项附加扣除；"
                "应纳税额 = 分段累进(应纳税所得额, 税率表)",
        "步骤": [
            "应纳税所得额 = 收入总额 − 费用减除 − 专项扣除 − 专项附加扣除",
            "应纳税额 = 分段累进(应纳税所得额, 税率表)",
            "应补退税额 = 应纳税额 − 已预缴税额",
        ],
        "结果": ["应纳税所得额", "应纳税额", "应补退税额"],
    },
    "超率累进": {
        "公式": "应纳税额 = 超率累进(扣除项目金额, 增值额, 税率表)",
        "步骤": [
            "增值额 = 转让收入 − 扣除项目金额",
            "增值率 = 增值额 / 扣除项目金额",
            "应纳税额 = 超率累进(扣除项目金额, 增值额, 税率表)",
        ],
        "结果": ["增值额", "增值率", "应纳税额"],
    },
    "从价计征": {
        "公式": "应纳税额 = 计税依据 × 税率",
        "步骤": ["应纳税额 = 计税依据 × 税率"],
        "结果": ["应纳税额"],
    },
}


def _refs(step):
    """一条步骤用到的符号，按它们在算式里首次出现的先后排。

    这个顺序有下游用途：② 的补问清单、缺参数报错都照它念，所以必须是"读起来
    的顺序"，不能是 `ast.walk` 的广度优先顺序——那会把最右的操作数排到最前。
    """
    left, right = [x.strip() for x in step.split("=", 1)]
    expr = _ascii(right)
    names = [n.id for n in ast.walk(ast.parse(expr, mode="eval"))
             if isinstance(n, ast.Name)]
    uniq = dict.fromkeys(n for n in names if n not in _ALLOWED_FUNCS)
    return left, sorted(uniq, key=expr.find)


def inputs(name: str) -> list:
    """一个骨架必须由调用方给值的参数名，按算式里首次用到的先后排。"""
    sk = _skeleton(name)
    defined, out = set(), []
    for step in sk["步骤"]:
        for ref in _refs(step)[1]:
            if ref not in defined and ref not in out:
                out.append(ref)
        defined.add(_refs(step)[0])
    return out


def inputs_spec(name: str) -> list:
    """待填参数各收什么形态的值：数值，还是一张税率表（表按哪几列填）。

    判据取自步骤串本身，不另立一份参数清单：只有 RATE_TABLE_COLUMNS 里那两个
    函数的入参吃表，表固定在它们的最后一个实参上（两个函数的签名都把 税率表
    放在末位）。名字与先后一律照 inputs()，所以这条规格不会跟缺参数报错脱节——
    界面照它排表单，报错照 inputs() 念，两处同源。
    """
    tables = {}
    for step in _skeleton(name)["步骤"]:
        tree = ast.parse(_ascii(step.split("=", 1)[1]), mode="eval")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in RATE_TABLE_COLUMNS and node.args):
                continue
            last = node.args[-1]
            if isinstance(last, ast.Name):
                tables[last.id] = RATE_TABLE_COLUMNS[node.func.id]
    return [{"名称": k, "形态": "税率表" if k in tables else "数值",
             "列": tables.get(k, [])} for k in inputs(name)]


def _split(params: dict) -> tuple:
    """把 {"值":…, "来源":…} 拆成 值表 与 来源表，直接给数值的也认。"""
    values, sources = {}, {}
    for k, v in (params or {}).items():
        if isinstance(v, dict) and ("值" in v or "value" in v):
            raw = v.get("值", v.get("value"))
            sources[k] = v.get("来源", v.get("source", ""))
        else:
            raw, sources[k] = v, ""
        values[k] = _dec(raw) if not isinstance(raw, (list, dict)) else raw
    return values, sources


def _sub(expr: str, values: dict) -> str:
    """把算式里的名字换成实际数字，给人照着核。档表这类列表按档数说明。"""
    out = expr
    for k in sorted(values, key=len, reverse=True):
        v = values[k]
        if k in _FUNCS:
            continue
        if isinstance(v, list):
            shown = f"{len(v)} 档"
        elif isinstance(v, (Decimal, int, float)) and not isinstance(v, bool):
            shown = str(_show(v))
        else:
            shown = str(v)
        out = out.replace(k, shown)
    return out


def _skeleton(name: str) -> dict:
    sk = SKELETONS.get(name)
    if sk is None:
        raise KeyError(f"没有这个骨架：{name}，可选 {sorted(SKELETONS)}")
    return sk


def run(name: str, params: dict) -> dict:
    sk = _skeleton(name)
    given, sources = _split(params)
    # 缺口整份报，不按步骤挤牙膏：② 补问要一次把缺的都问出去，逐条报会让用户
    # 跑六趟才凑齐参数，也让人以为"跑到第三步就齐了"。
    lack = [k for k in inputs(name) if k not in given]
    if lack:
        raise Missing(lack)
    values = dict(_FUNCS)
    values.update(given)      # 调用方给的值先进命名空间，"代入"才换得出数字
    steps = []
    for step in sk["步骤"]:
        left, _ = _refs(step)
        expr = step.split("=", 1)[1].strip()
        val = eval(_parse(expr), {"__builtins__": {}}, values)
        record = {"名称": left, "算式": expr}
        if isinstance(val, dict):
            # 累进函数返回的是 {适用档, 逐档, 结果, 速算式}。进入下一步算式的
            # 只有那个数额，分档明细挂在这条步骤上给人对着税率表核。
            record["分档"] = _jsonable(val)
            val = val["结果"]
        values[left] = val if isinstance(val, Decimal) else _dec(val)
        record["代入"] = _sub(expr, values)
        record["结果"] = _jsonable(_show(values[left]))
        steps.append(record)
    used = inputs(name)
    return {
        "骨架": name, "公式": sk["公式"], "步骤": steps,
        "结果": {k: _jsonable(_show(values[k])) for k in sk["结果"]},
        "参数": [{"名称": k, "值": _jsonable(given[k]), "来源": sources.get(k, "")}
                 for k in used],
        "无来源参数": [k for k in used if not sources.get(k)],
        "限制": DISCLAIMER,
    }


def _cli_value(raw: str):
    """--set 的右值：税率表这类复合参数按 JSON 给（以 [ 或 { 开头），其余按数值。"""
    text = raw.strip()
    if text.startswith(("[", "{")):
        return json.loads(text)
    return _dec(text)


def _kv(item: str, what: str) -> tuple:
    if "=" not in item:
        raise ValueError(f"{what} 要写成 名=值 的形式：{item!r}")
    return tuple(x.strip() for x in item.split("=", 1))


def _cli_params(args) -> dict:
    """把 --params / --set / --src 并成一张 {参数名: {"值", "来源"}}。

    --set 先于 --src 处理，所以可以先给值再补来源；两者都覆盖参数文件里的同名项。
    """
    params = {}
    if args.params:
        params.update(json.loads(Path(args.params).read_text(encoding="utf-8")))
    for item in args.set:
        k, v = _kv(item, "--set")
        cur = params.get(k)
        params[k] = {"值": _cli_value(v),
                     "来源": cur.get("来源", "") if isinstance(cur, dict) else ""}
    for item in args.src:
        k, v = _kv(item, "--src")
        cur = params.get(k)
        params[k] = {"值": cur.get("值") if isinstance(cur, dict) else cur, "来源": v}
    return params


def main(argv=None):
    ap = argparse.ArgumentParser(description="算式展开器（不联网、不含税率）")
    ap.add_argument("skeleton", nargs="?", help="骨架名，见 --list")
    ap.add_argument("--list", action="store_true", help="列出骨架与所需参数")
    ap.add_argument("--set", action="append", default=[], metavar="名=值",
                    help="给一个参数的值，可重复；税率表这类复合参数给 JSON")
    ap.add_argument("--src", action="append", default=[], metavar="名=URL",
                    help="给一个参数的来源链接，可重复")
    ap.add_argument("--params", default="", help="参数 JSON 文件路径")
    args = ap.parse_args(argv)

    if args.list:
        for k, sk in SKELETONS.items():
            print(f"{k}\n  公式：{sk['公式']}\n  待给参数：{'、'.join(inputs(k))}")
        return 0
    if not args.skeleton:
        ap.error("要指定骨架名，或用 --list 看清单")

    try:
        out = run(args.skeleton, _cli_params(args))
    except Missing as e:
        print(json.dumps({"缺参数": e.names, "骨架": args.skeleton,
                          "处置": "转 ② 前提补齐，或按 ③ 层级下挖检回现行值后重跑；"
                                 "本引擎不猜数值"}, ensure_ascii=False, indent=1))
        return 2
    except (ValueError, KeyError) as e:
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
