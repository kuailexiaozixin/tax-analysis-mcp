#!/usr/bin/env python3
"""账套巡检：把账上的数与按现行文件算出的限额逐条对比，缺参数就整条停。

这一层与算式展开器（`tax_calc`）守同一条纪律：**代码与注册表里都不存政策数值**。
比例、限额、期限、税率档都是会动的东西，写死一处，答案就再也指不回文件。所以规则只写
「比哪两个数」，这两个数各自要的参数（含适用税率表、申报期限表这类整张表）由 ③ 检回后
连同来源 URL 一起喂进来；`--pending` 打的就是这份取数工作清单。参数没回填（或回填了
但说不出来源）的规则整条不执行，状态记「待核」并回吐该去检回什么——宁可停住，不拿
旧比例顶上。

四态引用 `tax_coverage.STATES`，缺口类型与动作引用 `tax_inspect.GAP_MISSING`／
`GAP_CALIBER` 与 `GAP_ACTION`，两条红线词表也是 `tax_inspect` 的那两份：本层不自立
第二套词汇，自立的那套迟早会漂。

三条判不动的成因分开列，因为动作不同：参数未回填（回 ③ 检回）、账套缺那一格（向企业
要数据）、检回的表里没有那一档（换词重取或补一档）。三条都不写成「通过」；账套里找不
到那一格也不写成「没有发生」（SKILL ⑦ 第 25 条）。

用法：
    python scripts/tax_ledger.py --list
    python scripts/tax_ledger.py --pending
    python scripts/tax_ledger.py --ledger ledger.json --params params.json
    python scripts/tax_ledger.py --ledger ledger.json --params params.json \\
        --rule VAT03 --tolerance 0.005 --json

账套文件形如 {"行": [{"行号": 12, "科目": "管理费用—业务招待费", "金额": 380000,
"税额": 42000, "业务类型": "销售货物", "计税方法": "一般计税", "所属期": "2024-06",
"申报日期": "2024-07-20", "税前扣除额": 380000, "进项税额": 5000,
"加计抵减额": 0, "允许加计研发费用": 1200000, "加计扣除额": 1200000}, …]}；
参数文件形如 {"参数名": {"值": 0.6, "来源": "http://…"}}，值也可以是一整张表
（适用税率表给成 [{"业务类型": "销售货物", "税率": 0.13}, …]）。
"""

import argparse
import ast
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tax_calc        # noqa: E402
import tax_coverage    # noqa: E402
import tax_inspect     # noqa: E402

REGISTRY_PATH = HERE.parent / "data" / "ledger_rules.json"

#: 四层检查。分界只看一件事：被比的数在行上、在整表合计上、还是在子集合计上。
CHECK_LAYERS = ("row", "calc", "cross_row", "global")
#: 风险条目固定八格，少一格就只是提示不是条目（与稽查层那六字段同一纪律）。
ENTRY_KEYS = ("行号", "科目", "规则代号", "规则名称", "等级", "明细", "建议", "依据链")
RULE_KEYS = ("代号", "名称", "层", "税种", "场景", "查什么", "子集", "取数",
             "算式", "判据", "参数", "账套字段", "等级", "建议", "依据")
PARAM_KEYS = ("名", "检索词", "law_hint")
JUDGE_KEYS = ("左", "算", "右")
COMPARE_OPS = ("大于", "不等于", "晚于")
SELECTOR_OPS = ("有数", "属于", "不属于", "包含任一")
LEVELS = ("低", "中", "高")

#: 四态与缺口类型都不重新定义，直接引用上游那两份（见文件头）。
STATES = tax_coverage.STATES
VERIFIED, LACK, NA, TO_VERIFY = (tax_coverage.SATISFIED, tax_coverage.LACK,
                                 tax_coverage.NA, tax_coverage.TO_VERIFY)
GAP_MISSING, GAP_CALIBER = tax_inspect.GAP_MISSING, tax_inspect.GAP_CALIBER
GAP_ACTION = tax_inspect.GAP_ACTION
#: 覆盖状态也接上游那份：筛了范围就是 partial，与「这次没查出问题」是两件事。
COVERAGE_COMPLETE, COVERAGE_PARTIAL = (tax_inspect.COVERAGE_COMPLETE,
                                       tax_inspect.COVERAGE_PARTIAL)

#: 判据要比「有没有数」时比的是零。零是本层唯一自带的数，不是政策数值；注册表里
#: 连这一个也不许出现，`validate` 逐格扫。
RESERVED = {"零": Decimal(0)}
ALLOWED_FUNCS = ("min", "max", "abs", "查表")
_MIN_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Call,
              ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOTAL_MARK, SUBSET_MARK = "合计:", "|"
#: 这些格是判据本身，出现数字就等于把政策数值抄进了注册表；代号与依据里的文号不在这几格里。
NUMERIC_FREE = ("算式", "判据", "取数", "子集", "账套字段")
#: 禁数字的格子一共这几处：规则那五格，加上参数名与检索词，再加上两张算子词表
#: （词表里冒出「大于3万」这样的名字，门槛就写进骨架了）。注册表必须逐字列出同一份。
DIGIT_FREE_CELLS = NUMERIC_FREE + ("参数.名", "参数.检索词", "筛选算子", "比较算子")
NO_RECORD_NOTE = ("账套里没有这一格的记录：缺记录不等于未发生，本层只说无法确认，"
                  "不写『未计提』『未享受』『不涉及该税种』")


class NeedsLedger(Exception):
    """没给账套就没有巡检：这一层不拿题面推断账上的数。"""


class Unresolved(Exception):
    """算式或判据要用一个取不到的名字；`cause` 区分参数没回填还是账套缺那一格。"""

    def __init__(self, name, cause, detail=""):
        super().__init__(name)
        self.name, self.cause, self.detail = name, cause, detail


class NotInTable(Exception):
    """检回的那张表里没有这一档（或有这一档却缺要比的那一栏）：换词重取或补一档，不借近档顶位。"""

    def __init__(self, field, value, detail=""):
        super().__init__(f"{field}={value}")
        self.field, self.value, self.detail = field, value, detail


# ── 1 载入与自检 ──────────────────────────────────────────────

def _scan_no_digits(value, where: str, field: str) -> None:
    blob = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    hit = sorted({ch for ch in blob if ch.isdigit()})
    if hit:
        raise ValueError(f"{where} 的『{field}』里出现了数字 {''.join(hit)}：比例、限额、"
                         f"期限一律走『参数』，值由 ③ 检回并带来源，注册表与代码都不存")


def _nonempty(value, where: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{where} 的『{field}』是空的——空格子看起来有内容其实判不动")
    return text


def _names(rule: dict) -> dict:
    """一条规则能拿到哪些名字，四类分列：参数、取数、中间量、账套字段。"""
    return {
        "参数": [p["名"] for p in rule["参数"]],
        "取数": list(rule["取数"]),
        "中间量": [s.split("=", 1)[0].strip() for s in rule["算式"]],
        "行字段": list(rule["账套字段"]),
    }


def _pool(rule: dict, names: dict) -> list:
    """这一层的算式与判据允许引用的名字集合：逐行才有行字段，不逐行只有取数。"""
    base = names["参数"] + names["中间量"] + names["取数"] + list(RESERVED)
    return base + names["行字段"] if rule["层"] == "row" else base


def _refs(step: str):
    left, right = [x.strip() for x in step.split("=", 1)]
    expr = tax_calc._ascii(right)
    seen, out = set(), []
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCS \
                and node.id not in seen:
            seen.add(node.id)
            out.append((expr.find(node.id), node.id))
    return left, [n for _i, n in sorted(out)]


def validate(reg: dict) -> None:
    """字段名清单、结构与两条红线都在载入时判，不留到输出里靠人眼抓。"""
    note = reg.get("_说明") or {}
    for field, want in (("检查层", CHECK_LAYERS), ("风险条目八要素", ENTRY_KEYS),
                        ("规则字段", RULE_KEYS), ("参数字段", PARAM_KEYS),
                        ("判据字段", JUDGE_KEYS), ("比较算子", COMPARE_OPS),
                        ("筛选算子", SELECTOR_OPS), ("等级值域", LEVELS),
                        ("状态值域", STATES), ("不许出现数字的格子", DIGIT_FREE_CELLS)):
        if note.get(field) != list(want):
            raise ValueError(f"注册表『_说明.{field}』与代码常量不一致：\n"
                             f"  代码   = {list(want)}\n  注册表 = {note.get(field)}")
    for field in ("筛选算子", "比较算子"):
        for op in note[field]:
            _scan_no_digits(op, f"注册表『_说明.{field}』", field)
    rules = reg.get("规则")
    if not isinstance(rules, list) or not rules:
        raise ValueError("注册表里没有『规则』清单")
    codes = [r.get("代号") for r in rules]
    if len(set(codes)) != len(codes):
        raise ValueError(f"规则代号重复：{codes}")
    where_param = {}
    for rule in rules:
        code = str(rule.get("代号") or "（无代号）")
        where = f"规则 {code}"
        for field in RULE_KEYS:
            if field not in rule:
                raise ValueError(f"{where} 缺『{field}』这一格")
        if not re.fullmatch(r"[A-Z]{2,4}\d+", code):
            raise ValueError(f"规则代号 {code} 不是「税种字母＋序号」的形态（如 VAT03）")
        if rule["层"] not in CHECK_LAYERS:
            raise ValueError(f"{where} 的层「{rule['层']}」不在四层里，可选 {list(CHECK_LAYERS)}")
        if rule["等级"] not in LEVELS:
            raise ValueError(f"{where} 的等级「{rule['等级']}」不在值域里，可选 {list(LEVELS)}")
        for field in ("名称", "税种", "场景", "查什么"):
            _nonempty(rule[field], where, field)
        for p in rule["参数"]:
            for key in PARAM_KEYS:
                if key not in p:
                    raise ValueError(f"{where} 的参数缺『{key}』这一格")
        # 禁数字的格子逐格扫，参数名也在内：名字写成「加计比例2」就是把一个数藏进了
        # 标识符，回填表按名字取值，日后换一个数就得改代码。
        for field in NUMERIC_FREE:
            _scan_no_digits(rule[field], where, field)
        for p in rule["参数"]:
            _scan_no_digits(p["名"], where, "参数.名")
            _scan_no_digits(p["检索词"], where, "参数.检索词")
        for key in JUDGE_KEYS:
            if key not in rule["判据"]:
                raise ValueError(f"{where} 的判据缺『{key}』")
        if rule["判据"]["算"] not in COMPARE_OPS:
            raise ValueError(f"{where} 的判据算了「{rule['判据']['算']}」，可选 {list(COMPARE_OPS)}")
        if rule["层"] == "row" and rule["取数"]:
            raise ValueError(f"{where} 是 row 层却写了取数——逐行判用的数就在行上，"
                             f"聚合取数是 calc/cross_row/global 的写法")
        if rule["层"] == "cross_row" and not rule["子集"]:
            raise ValueError(f"{where} 是 cross_row 层却没带子集——这一层的分界就在子集："
                             f"跨行比的是两个子集各自的合计，没有子集它与 calc 无别")
        for p in rule["参数"]:
            _nonempty(p["law_hint"], where, f"参数 {p['名']} 的 law_hint")
            if p["名"] in where_param:
                raise ValueError(f"参数名「{p['名']}」在 {where} 与 {where_param[p['名']]} "
                                 f"两处都用——回填的是一张扁平参数表，同名会共用一个值")
            where_param[p["名"]] = where
        if not rule["建议"]:
            raise ValueError(f"{where} 没有『建议』——判出风险却不给下一步动作，这一条落不了地")
        for line in rule["建议"]:
            bad = tax_inspect._bad_words(line, tax_inspect.FORBIDDEN_REMEDIATION)
            if bad:
                raise ValueError(f"{where} 的『建议』写了改动历史事实的动作 {bad}：{line}")
        for field in ("查什么", "建议"):
            text = rule[field] if isinstance(rule[field], str) else " ".join(rule[field])
            bad = tax_inspect._bad_words(text, tax_inspect.PREDICTION_PHRASES)
            if bad:
                raise ValueError(f"{where} 的『{field}』写了检查结果预测 {bad}："
                                 f"只能说这两个数对不上，不能说会不会被查")
        for text in rule["依据"]:
            _nonempty(text, where, "依据")

        names = _names(rule)
        pool = _pool(rule, names)
        # 五类名字要在一条规则里各占一格：同名会被顶掉——算式的中间量会盖掉回填的参数值，
        # 行上的字段会盖掉同名参数，被顶掉的那一格既不报错也不参与判定。
        pools = {"参数": names["参数"], "取数": names["取数"],
                 "中间量": names["中间量"], "账套字段": names["行字段"],
                 "保留名": list(RESERVED)}
        for (a, pa), (b, pb) in combinations(pools.items(), 2):
            both = sorted(set(pa) & set(pb))
            if both:
                raise ValueError(f"{where} 的 {'、'.join(both)} 既当『{a}』又当『{b}』——"
                                 f"求值时后写入的一格会顶掉先写入的那格，判的不再是意图里的数")
        if len(set(names["中间量"])) != len(names["中间量"]):
            raise ValueError(f"{where} 的算式里有两条产出同一个中间量名")
        for step in rule["算式"]:
            if "=" not in step:
                raise ValueError(f"{where} 的算式要写成「名字 = 表达式」：{step}")
            left, refs = _refs(step)
            earlier = names["中间量"][:names["中间量"].index(left)]
            for ref in refs:
                if ref not in earlier + names["参数"] + names["取数"] + \
                        (names["行字段"] if rule["层"] == "row" else []):
                    raise ValueError(f"{where} 的算式用了「{ref}」，它不在参数、取数"
                                     f"{'、账套字段' if rule['层'] == 'row' else ''}"
                                     f"或前面算出的中间量里；不逐行的层取数要写"
                                     f"「{TOTAL_MARK}字段」")
            _parse(step.split("=", 1)[1])
        for side in (rule["判据"]["左"], rule["判据"]["右"]):
            if side not in pool:
                raise ValueError(f"{where} 的判据引用了「{side}」，它既不是参数、取数、"
                                 f"中间量也不是账套字段，跑起来只会判不动")

        for name, how in rule["取数"].items():
            field, _, subset = str(how).partition(SUBSET_MARK)
            if not field.startswith(TOTAL_MARK):
                raise ValueError(f"{where} 的取数「{name}」要写成「{TOTAL_MARK}字段[|子集]」：{how}")
            column = field[len(TOTAL_MARK):]
            if column not in rule["账套字段"]:
                raise ValueError(f"{where} 的取数要「{column}」这一列，但账套字段没声明它")
            if subset and subset not in rule["子集"]:
                raise ValueError(f"{where} 的取数「{name}」指向没有的子集「{subset}」")
            if rule["层"] == "calc" and subset:
                raise ValueError(f"{where} 是 calc 层，取数「{name}」却带了子集——带子集就是"
                                 f"跨行聚合，层应写 cross_row")
            if rule["层"] == "cross_row" and not subset:
                raise ValueError(f"{where} 是 cross_row 层，取数「{name}」没带子集——"
                                 f"整表比整表是 calc，这一层的分界就在子集")
        if rule["层"] == "cross_row" and not any("|" in str(v) for v in rule["取数"].values()):
            raise ValueError(f"{where} 是 cross_row 层却没有任何带子集的取数，那它与 calc 无别")

        for name, sel in rule["子集"].items():
            for key in ("字段", "算"):
                if key not in sel:
                    raise ValueError(f"{where} 的子集「{name}」缺『{key}』")
            if sel["算"] not in SELECTOR_OPS:
                raise ValueError(f"{where} 的子集「{name}」算了「{sel['算']}」，"
                                 f"可选 {list(SELECTOR_OPS)}")
            if sel["字段"] not in rule["账套字段"]:
                raise ValueError(f"{where} 的子集「{name}」按「{sel['字段']}」筛行，"
                                 f"但账套字段没声明它")
            if sel["算"] == "有数":
                if "对参数" in sel:
                    raise ValueError(f"{where} 的子集「{name}」算『有数』却给了对参数——"
                                     f"这一档不看值只看那一格交没交，参数不会被读到")
            else:
                if "对参数" not in sel:
                    raise ValueError(f"{where} 的子集「{name}」缺『对参数』")
                if sel["对参数"] not in names["参数"]:
                    raise ValueError(f"{where} 的子集「{name}」要对照参数「{sel['对参数']}」，"
                                     f"但这条规则没声明它")
        if rule["层"] == "row" and len(rule["子集"]) > 1:
            raise ValueError(f"{where} 是 row 层，子集有 {len(rule['子集'])} 份——"
                             f"逐行判只有一条作用行的口径，多份要说清算哪一份")


def load(path: Path = None) -> dict:
    reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    validate(reg)
    return reg


# ── 2 账套与参数：什么叫做「交了」 ────────────────────────────

def _rows(data) -> list:
    if isinstance(data, dict):
        data = data.get("行")
    if not isinstance(data, list):
        raise ValueError('账套要写成 {"行": [{"行号": 1, "科目": "…", "金额": 0}, …]}')
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"账套第 {i + 1} 行不是对象，读不出字段")
    return data


def _given(value) -> bool:
    """这一格算不算「交了」：空串与 null 都没交，数值零交了——零是交了零。"""
    return not (value is None or (isinstance(value, str) and not value.strip()))


def _has(row: dict, field: str) -> bool:
    return _given(row.get(field))


def _dec(value, where: str, item: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{where} 的「{item}」给了 true/false，不是数值")
    try:
        return tax_calc._dec(value)
    except ValueError:
        raise ValueError(f"{where} 的「{item}」给了非数值 {value!r}——要么给数，"
                         f"要么整格不给（整格不给判『账套缺那一格』）") from None


def _norm_params(data: dict) -> dict:
    """直接给数值也认这个形态，但补出来的『来源』是空串——没来源的规则照样整条待核。

    这一层认形状不认账：形状对了只说明能读，出处没有才是不能判的理由。
    """
    out = {}
    for k, v in (data or {}).items():
        out[k] = v if isinstance(v, dict) and "值" in v else {"值": v, "来源": ""}
    return out


def _pending_params(rule: dict, params: dict) -> list:
    """这条规则还缺哪些参数——回填了值但没给来源的也算缺，一个说不出处的数不能用来判定。

    回填成 true/false 或 null 的也算没回填：那不是数，落到 Decimal 会静默变成 1 或 0，
    比停住更糟——答案带着一个布尔值算出来的限额，看着像按文件算的。
    """
    out = []
    for p in rule["参数"]:
        got = params.get(p["名"])
        if not isinstance(got, dict) or "值" not in got \
                or not str(got.get("来源") or "").strip():
            out.append(p)
        elif isinstance(got["值"], bool) or got["值"] is None:
            out.append(p)
    return out


def _value(raw):
    """参数标量统一换 Decimal（Decimal 与 JSON 的 float 相乘会 TypeError）；表与清单原样留。"""
    if isinstance(raw, (list, dict)) or not _numeric(raw):
        return raw
    return Decimal(str(raw))


def _subset(rule: dict, name: str, rows: list, values: dict) -> list:
    sel = rule["子集"][name]
    if sel["算"] == "有数":
        return [r for r in rows if _has(r, sel["字段"])]
    want = values[sel["对参数"]]
    want = [str(x) for x in (want if isinstance(want, list) else [want])]
    if sel["算"] == "属于":
        return [r for r in rows if str(r.get(sel["字段"], "")) in want]
    if sel["算"] == "不属于":
        return [r for r in rows if str(r.get(sel["字段"], "")) not in want]
    return [r for r in rows if any(k in str(r.get(sel["字段"], "")) for k in want)]


def _total(rows: list, column: str, where: str) -> Decimal:
    got = [_dec(r[column], where, column) for r in rows if _has(r, column)]
    if not got:
        raise Unresolved(column, "账套缺那一格")
    out = Decimal(0)
    for v in got:
        out += v
    return out


# ── 3 算式：与 tax_calc 同一套「不许有数字」的判据 ─────────────

def _parse(expr: str):
    node = ast.parse(tax_calc._ascii(expr), mode="eval")
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant):
            if isinstance(sub.value, (int, float)) and sub.value not in (0, 1):
                raise ValueError(f"算式里不得出现数字常量 {sub.value!r}"
                                 f"（政策数值一律走参数）：{expr}")
            if sub.value is None:
                raise ValueError(f"算式里不许出现 null：{expr}")
        elif isinstance(sub, ast.Call):
            if not isinstance(sub.func, ast.Name) or sub.func.id not in ALLOWED_FUNCS:
                raise ValueError(f"算式只允许调用 {list(ALLOWED_FUNCS)}：{expr}")
        elif not isinstance(sub, _MIN_NODES):
            raise ValueError(f"算式含不允许的写法 {type(sub).__name__}：{expr}")
    return compile(node, "<ledger>", "eval")


def _lookup(row, where: str):
    def 查表(table, key_field, value_field):
        """查表(检回的整张表, '键字段', '值字段')：拿本行在键字段上的值去表里对同一字段。"""
        if not isinstance(table, list) or not table:
            raise Unresolved(str(key_field), "参数未回填",
                             f"{where} 的查表要的是 ③ 检回的那张表（非空清单），"
                             f"回填成标量就没法按字段对档——回 ③ 换词重取")
        if not isinstance(key_field, str) or not isinstance(value_field, str):
            raise ValueError(f"{where} 的查表两个字段名要写成文字（'业务类型', '税率'）")
        if row is None or not _has(row, key_field):
            raise Unresolved(key_field, "账套缺那一格")
        want = str(row[key_field])
        for entry in table:
            if isinstance(entry, dict) and str(entry.get(key_field, "")) == want:
                if not _has(entry, value_field):
                    raise NotInTable(key_field, want,
                                     f"检回的表里有 {key_field}={want} 这一档，"
                                     f"但那档的『{value_field}』是空的")
                return entry[value_field]
        raise NotInTable(key_field, want)
    return 查表


def _sub(expr: str, env: dict) -> str:
    """把算式里的名字换成算出的数，给『明细』那一格看。表与文字一律不换成计数。"""
    out = expr
    for k in sorted(env, key=len, reverse=True):
        if k in ALLOWED_FUNCS or not _numeric(env[k]):
            continue
        out = out.replace(k, _flat(env[k]))
    return out


def _flat(value):
    """明细与依据链里的展示口径：表按档计、清单按项计、数值走 tax_calc 的展示精度。"""
    if isinstance(value, list):
        return f"{len(value)} 档" if value and isinstance(value[0], dict) \
            else f"{len(value)} 项"
    if isinstance(value, dict):
        return f"{len(value)} 项"
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        return str(tax_calc._show(Decimal(str(value))))
    return str(value)


def _steps(rule: dict, env: dict, row, where: str) -> list:
    """按序求值算式，返回 [(名, 代入串, 结果)]。名字取不到就抛 Unresolved。"""
    local = {"min": lambda *a: min(_dec(x, where, "min") for x in a),
             "max": lambda *a: max(_dec(x, where, "max") for x in a),
             "abs": abs, "查表": _lookup(row, where)}
    local.update(env)
    local.update(RESERVED)
    pnames = {p["名"] for p in rule["参数"]}
    if row is not None:
        for k, v in row.items():
            if _has(row, k):
                local[k] = _dec(v, where, k) if _numeric(v) else v
    trace = []
    for step in rule["算式"]:
        left, refs = _refs(step)
        for ref in refs:
            if ref not in local:
                raise Unresolved(ref, "参数未回填" if ref in pnames else "账套缺那一格")
        expr = step.split("=", 1)[1].strip()
        try:
            val = eval(_parse(expr), {"__builtins__": {}}, local)
        except ZeroDivisionError:
            raise Unresolved(left, "账套缺那一格",
                             f"{where} 的算式「{left}」除数是零：比率算不出，"
                             f"这一格按未取数处理而不是当零") from None
        except (InvalidOperation, TypeError):
            # 说得出是哪一格不是数，才谈得上下一步动作：参数的回去 ③，账上的向企业要。
            hard = [r for r in refs if isinstance(local.get(r), str)]
            raise Unresolved(left,
                             "参数未回填" if any(r in pnames for r in hard) else "账套缺那一格",
                             f"{where} 的算式「{left}」里"
                             f"{'、'.join(hard) or '有一格'}不是可算的数值") from None
        local[left] = _dec(val, where, left) if _numeric(val) else val
        trace.append((left, _sub(expr, local), local[left]))
    return trace


def _numeric(value) -> bool:
    """true/false 不算数值：它落到 Decimal 会变成 1/0，把布尔当金额是最坏的一种误读。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (Decimal, int, float)):
        return True
    return isinstance(value, str) and _is_number(value)


def _is_number(text) -> bool:
    try:
        tax_calc._dec(text)
        return True
    except ValueError:
        return False


# ── 4 判据 ───────────────────────────────────────────────────

def _compare(left, op, right, tol: float, where: str, names=("左", "右")):
    """返回 (是否命中, 一句话)。容差只作用于「不等于」，另外两条是硬判。

    `names` 是判据两侧在注册表里写的名字，只用来把报错说到人能改的那一格上。
    """
    if op == "晚于":
        a, b = _iso(left, where, names[0]), _iso(right, where, names[1])
        return a > b, f"{a} 对 {b}"
    a, b = _dec(left, where, names[0]), _dec(right, where, names[1])
    if op == "不等于":
        base = max(abs(a), abs(b))
        # 容差转成 Decimal 再乘：Decimal 与 float 相乘会 TypeError，
        # 而这一路是逐行判的，抛出去就成了整条规则崩，不是判不动。
        same = abs(a - b) <= base * Decimal(str(tol)) if base else True
        return (not same), f"{_flat(a)} 对 {_flat(b)}"
    return a > b, f"{_flat(a)} 对 {_flat(b)}"


def _iso(value, where: str, name: str) -> str:
    text = str(value or "").strip()
    if not ISO_DATE.match(text):
        raise Unresolved(name, "账套缺那一格",
                         f"{where} 要比的「{name}」不是 YYYY-MM-DD 形态"
                         f"（读到 {text or '空'}）——期限与日期都按原文给，本层不推算")
    return text


def _pick(name, pools):
    """在几个取值池里按序找这个名字；找到了还得『交了』才算——空串与 null 回到判不动。"""
    for pool in pools:
        if name in pool and _given(pool[name]):
            return pool[name]
    raise Unresolved(name, "账套缺那一格")


def _detail(rule: dict, trace: list, note: str, verdict: str) -> str:
    parts = [f"{name} = {shown} = {_flat(val)}" for name, shown, val in trace]
    j = rule["判据"]
    parts.append(f"判据：{j['左']} {j['算']} {j['右']} → {verdict}（{note}）")
    return "；".join(parts)


def _chain(rule: dict, values: dict, sources: dict) -> list:
    """依据链：用到的参数各带值与来源 URL，再接规则自己声明的那几条依据。"""
    out = [{"项": p["名"], "值": _flat(values[p["名"]]),
            "来源": sources.get(p["名"], ""), "检索词": p["检索词"],
            "law_hint": p["law_hint"]}
           for p in rule["参数"] if p["名"] in values]
    out += [{"项": "规则依据", "值": text, "来源": ""} for text in rule["依据"]]
    return out


# ── 5 逐条执行 ───────────────────────────────────────────────

def _gap(cause: str, obj: str, detail: str) -> dict:
    """两类成因两类动作：账上没那一格是缺材料，检回的表没覆盖到这一档是口径没取全。"""
    kind = GAP_MISSING if cause == "账套缺那一格" else GAP_CALIBER
    return {"类": kind, "对象": obj, "说明": detail or NO_RECORD_NOTE,
            "动作": GAP_ACTION[kind]}


def _state_for(cause: str) -> str:
    """成因定状态：账上没那一格记『缺』（向企业要数据），参数那条线的问题记『待核』（回 ③）。"""
    return TO_VERIFY if cause == "参数未回填" else LACK


def _row_label(lineno, pos=None) -> str:
    """账套行没给行号时也要指得清是哪一行：写成『第 None 行』等于没指，
    退而按提交顺序给第几条，读者回到那份文件里数得出来。"""
    if lineno not in (None, ""):
        return f"第 {lineno} 行"
    return f"第 {pos} 条（该行未给行号）" if pos else "该行未给行号"


def _marks(rows: list) -> dict:
    """整张账套的『这一条是第几个』：只在行没给行号时用来顶上，不改行上的值。"""
    return {id(r): i + 1 for i, r in enumerate(rows)}


def _row_mark(row: dict, marks: dict):
    got = row.get("行号")
    return got if got not in (None, "") else f"第 {marks[id(row)]} 条（该行未给行号）"


def _result(rule: dict, rows: list, state: str, cause="", reason="", scope=0,
            entries=None, unjudged=None, passed=0, note="") -> dict:
    # 逐行判不动已经各有一条缺口了，整条规则那句就不再补一条：同一件事在两处
    # 计数，缺口统计就会比实际该要的材料多，读的人会以为还要再补一份别的东西。
    gaps = [_gap(u["成因"], _row_label(u["行号"], u.get("序")), u["原因"])
            for u in unjudged or []]
    if state != VERIFIED and reason and not gaps:
        gaps = [_gap(cause, rule["代号"], reason)]
    return {
        "代号": rule["代号"], "名称": rule["名称"], "层": rule["层"],
        "税种": rule["税种"], "场景": rule["场景"], "等级": rule["等级"],
        "状态": state, "判不动成因": cause, "原因": reason,
        "作用行数": scope, "未作用行数": len(rows) - scope,
        "通过次数": passed, "命中数": len(entries or []),
        "判不动行": unjudged or [], "参与判定说明": note,
        "风险条目": entries or [], "缺口": gaps,
    }


def _entry(rule: dict, subjects, rownos, detail: str, chain: list) -> dict:
    return {"行号": rownos, "科目": subjects, "规则代号": rule["代号"],
            "规则名称": rule["名称"], "等级": rule["等级"], "明细": detail,
            "建议": list(rule["建议"]), "依据链": chain}


def run_rule(rule: dict, rows: list, params: dict, tolerance: float = 0.0) -> dict:
    where = f"规则 {rule['代号']}"
    lack = _pending_params(rule, params)
    if lack:
        p = lack[0]
        detail = (f"参数「{p['名']}」没检回、检回了没给来源，或回填的是 true/false、null "
                  f"而不是数，整条规则没执行。检索词：{p['检索词']}｜提示：{p['law_hint']}")
        head = detail if len(lack) == 1 else \
            f"{len(lack)} 个参数没检回（{p['名']} 只是第一个）——{detail}"
        return _result(rule, rows, TO_VERIFY, "参数未回填", head)
    values = {p["名"]: _value(params[p["名"]]["值"]) for p in rule["参数"]}
    sources = {p["名"]: str(params[p["名"]].get("来源") or "") for p in rule["参数"]}
    chain = _chain(rule, values, sources)
    subsets = {name: _subset(rule, name, rows, values) for name in rule["子集"]}
    if rule["层"] == "row":
        scope = subsets[next(iter(rule["子集"]))] if rule["子集"] else rows
        return _run_row(rule, rows, scope, values, chain, tolerance, where)
    return _run_set(rule, rows, values, subsets, chain, tolerance, where)


def _aggregate(rule: dict, rows: list, subsets: dict, where: str) -> tuple:
    agg, subjects, rownos, used = {}, [], [], []
    marks = _marks(rows)
    for name, how in rule["取数"].items():
        field, _, subset = str(how).partition(SUBSET_MARK)
        column = field[len(TOTAL_MARK):]
        pool = subsets.get(subset, rows) if subset else rows
        try:
            agg[name] = _total(pool, column, where)
        except Unresolved:
            raise Unresolved(name, "账套缺那一格",
                             f"取数「{name}」要的那一列（{column}）在"
                             f"{subset or '整张账套'}里没有一行给了值——{NO_RECORD_NOTE}") from None
        for r in pool:
            if _has(r, column):
                subjects.append(str(r.get("科目", "")))
                rownos.append(_row_mark(r, marks))
                if not any(r is u for u in used):
                    used.append(r)
    return agg, _ordered(subjects), _ordered(rownos), used


def _run_set(rule: dict, rows: list, values: dict, subsets: dict,
             chain: list, tolerance: float, where: str) -> dict:
    try:
        agg, subjects, rownos, used = _aggregate(rule, rows, subsets, where)
    except Unresolved as e:
        return _result(rule, rows, _state_for(e.cause), e.cause, e.detail)
    env = dict(agg)
    env.update(values)
    try:
        trace = _steps(rule, env, None, where)
        got = {n: v for n, _s, v in trace}
        j = rule["判据"]
        left = _pick(j["左"], [got, agg, RESERVED, values])
        right = _pick(j["右"], [got, agg, RESERVED, values])
        hit, note = _compare(left, j["算"], right, tolerance, where,
                             (j["左"], j["右"]))
    except Unresolved as e:
        return _result(rule, rows, _state_for(e.cause), e.cause,
                       e.detail or f"判据要用「{e.name}」而它取不到：{NO_RECORD_NOTE}")
    except NotInTable as e:
        return _result(rule, rows, LACK, "表里没有那一档",
                       f"{e.detail or f'检回的表里没有 {e.field}={e.value} 这一档'}："
                       f"换词重取或补一档，不借近档顶位")
    except ValueError as e:
        return _result(rule, rows, LACK, "账套缺那一格", str(e))
    entries = []
    if hit:
        entries.append(_entry(rule, subjects, rownos,
                              _detail(rule, trace, note, "命中"), chain))
    # 作用行数只计真正参与聚合的行：整表 10 行里只有 2 行进了取数，
    # 报「作用 10 行」就是把没参与的行说成查过了。global 层没有取数，判的是整张账套。
    return _result(rule, rows, VERIFIED, scope=len(used) if rule["取数"] else len(rows),
                   entries=entries, passed=0 if hit else 1, note=note)


def _run_row(rule: dict, rows: list, scope: list, values: dict, chain: list,
             tolerance: float, where: str) -> dict:
    if not scope:
        return _result(rule, rows, LACK, "账套缺那一格",
                       f"没有一行落进子集「{'、'.join(rule['子集']) or '未声明'}」——"
                       f"{NO_RECORD_NOTE}")
    entries, unjudged, passed = [], [], 0
    marks = _marks(rows)
    for row in scope:
        try:
            trace = _steps(rule, dict(values), row, where)
            got = {n: v for n, _s, v in trace}
            j = rule["判据"]
            left = _pick(j["左"], [got, RESERVED, row, values])
            right = _pick(j["右"], [got, RESERVED, row, values])
            hit, note = _compare(left, j["算"], right, tolerance, where,
                             (j["左"], j["右"]))
        except Unresolved as e:
            unjudged.append({"行号": row.get("行号"), "序": marks[id(row)],
                             "成因": e.cause,
                             "原因": e.detail or
                             f"这一行取不到「{e.name}」"
                             + (f"——{NO_RECORD_NOTE}" if e.cause == "账套缺那一格" else "")})
            continue
        except NotInTable as e:
            unjudged.append({"行号": row.get("行号"), "序": marks[id(row)],
                             "成因": "表里没有那一档",
                             "原因": f"{e.detail or f'检回的表里没有 {e.field}={e.value} 这一档'}"
                                     f"——换词重取或补一档，不借近档顶位"})
            continue
        except ValueError as e:
            unjudged.append({"行号": row.get("行号"), "序": marks[id(row)],
                             "成因": "账套缺那一格", "原因": str(e)})
            continue
        if hit:
            entries.append(_entry(rule, _ordered([row.get("科目")]),
                                  _ordered([_row_mark(row, marks)]),
                                  _detail(rule, trace, note, "命中"), chain))
        else:
            passed += 1
    # 整条规则判不动时的成因跟着逐行走：几种行有几种成因就不合并成一句，
    # 把「表里没那一档」写成「账套缺那一格」会把动作指错（一个回 ③，一个向企业要数据）。
    causes = {u["成因"] for u in unjudged}
    cause = causes.pop() if len(causes) == 1 else "账套缺那一格"
    state = VERIFIED if entries or passed else _state_for(cause)
    return _result(rule, rows, state, "" if state == VERIFIED else cause,
                   "" if state == VERIFIED else
                   f"{len(unjudged)} 行全部判不动（成因：{cause}），"
                   f"逐行的原因列在下面",
                   scope=len(scope), entries=entries, unjudged=unjudged, passed=passed)


# ── 6 总跑 ───────────────────────────────────────────────────

def scan(ledger=None, params: dict = None, codes=(), reg: dict = None,
         tolerance: float = 0.0) -> dict:
    """按选中的规则巡检账套，产出八要素风险条目、判不动清单与两类缺口。"""
    reg = reg or load()
    if not 0.0 <= tolerance < 1.0:
        raise ValueError(f"容差要在 [0, 1) 之间（相对差），读到 {tolerance}")
    if ledger is None:
        raise NeedsLedger("没给账套就不巡检：这一层不拿题面推断账上的数")
    rows = _rows(ledger)
    table = {r["代号"]: r for r in reg["规则"]}
    if codes:
        bad = [c for c in codes if c not in table]
        if bad:
            raise ValueError(f"没有这些规则：{bad}，可选 {sorted(table)}")
    picked = [r for r in reg["规则"] if not codes or r["代号"] in codes]
    skipped = [{"代号": r["代号"], "名称": r["名称"], "状态": NA}
               for r in reg["规则"] if r["代号"] not in {p["代号"] for p in picked}]
    results = [run_rule(r, rows, params or {}, tolerance) for r in picked]
    used = {f for r in picked for f in r["账套字段"]}
    unused = sorted({k for row in rows for k in row
                     if k not in used and k != "行号" and _has(row, k)})
    gap_count = {g: sum(1 for r in results for x in r["缺口"] if x["类"] == g)
                 for g in (GAP_MISSING, GAP_CALIBER)}
    if not picked:
        note = "本次筛选没命中任何规则——这是范围筛空，不是全查过"
    elif skipped:
        note = f"另有 {len(skipped)} 条规则本次没跑（范围外，不等于查过）"
    else:
        # 「都跑了」要说得清跑的含义：选中不等于判得动，参数没回填的那几条整条没执行。
        stalled = sum(1 for r in results if r["状态"] == TO_VERIFY)
        note = (f"注册表内 {len(picked)} 条规则都在本次范围内："
                f"{len(picked) - stalled} 条判得动，{stalled} 条整条没执行（参数未回填），"
                f"命中 {sum(len(r['风险条目']) for r in results)} 条")
    return {
        "筛选": {"规则": list(codes), "条件": "、".join(codes)},
        "本次范围": {"规则数": len(picked), "账套行数": len(rows),
                     "层分布": {k: sum(1 for r in reg["规则"] if r["层"] == k)
                                for k in CHECK_LAYERS}},
        "覆盖": {"状态": COVERAGE_PARTIAL if (not picked or skipped)
                            else COVERAGE_COMPLETE,
                 "未巡检规则": skipped, "说明": note},
        "缺口统计": gap_count,
        "规则": results,
        "风险条目": [e for r in results for e in r["风险条目"]],
        "未用的账套字段": unused,
        "边界": ("本层按注册表的判据比账上的数与按现行文件算出的限额，不预测检查与处罚"
                 "结果；限额、比例、期限的取值一律来自 ③ 检回并带来源的那一份文件，参数"
                 "没回填的规则整条没执行，列在状态『待核』里。条目自带的等级是规则的基准"
                 "等级，不是按差额算出来的——要不要升级、怎么落进答案，归「风险自检专用"
                 "输出」那一式的『风险等级』格。"),
    }


# ── 7 取数工作清单（交给 ③ 的那一份）───────────────────────────

def pending(reg: dict = None, params: dict = None) -> dict:
    """列出还没回填的参数：每条带检索词与 law_hint，就是 ③ 的一组取数任务。"""
    reg = reg or load()
    params = params or {}
    items = []
    for rule in reg["规则"]:
        for p in _pending_params(rule, params):
            items.append({"规则代号": rule["代号"], "规则名称": rule["名称"],
                          "税种": rule["税种"], "参数": p["名"],
                          "检索词": p["检索词"], "law_hint": p["law_hint"],
                          "回填形态": json.dumps({p["名"]: {"值": "…", "来源": "http://…"}},
                                                 ensure_ascii=False)})
    return {"待检回参数数": len(items), "参数": items,
            "怎么用": ("逐条走 ③ 层级下挖取现行值与官方链接，写成参数 JSON 再连同 "
                       "--ledger 重跑；取不到就让它留在『待核』，不填旧值"),
            "不猜的理由": "注册表与代码都不存政策数值，写进去就答不出这个数出自哪一号文"}


# ── 8 排版 ───────────────────────────────────────────────────

def render(out: dict) -> str:
    L = [f"账套巡检：{out['本次范围']['规则数']} 条规则，账套 "
         f"{out['本次范围']['账套行数']} 行",
         f"覆盖状态：{out['覆盖']['状态']}｜{out['覆盖']['说明']}",
         "层分布：" + "｜".join(f"{k} {v}" for k, v in out["本次范围"]["层分布"].items()),
         "缺口统计：" + "｜".join(f"{k} {v}" for k, v in out["缺口统计"].items())]
    for r in out["规则"]:
        # 状态与命中数并排打：单看『已满足』会被读成『这条没问题』，
        # 而它说的是判据跑完了；跑完的结果就在后面的命中数上。
        L.append(f"\n【{r['代号']} {r['名称']}】{r['层']}｜{r['税种']}／{r['场景']}"
                 f"｜状态 {r['状态']}｜命中 {r['命中数']} 条｜作用 {r['作用行数']} 行"
                 f"｜未作用 {r['未作用行数']} 行")
        if r["原因"]:
            L.append(f"  判不动（{r['判不动成因']}）：{r['原因']}")
        if r["通过次数"]:
            L.append(f"  这条判下来 {r['通过次数']} 次没命中——不等于没问题，"
                     f"它只覆盖『{r['名称']}』这一件事")
        for e in r["风险条目"]:
            for key in ENTRY_KEYS:
                value = e[key]
                if key in ("行号", "科目"):
                    value = "、".join(str(v) for v in value) or \
                            (f"该行未给『{key}』这一格" if r["层"] == "row" else "整表")
                elif key == "依据链":
                    value = "；".join(f"{x['项']}＝{x['值']}"
                                      f"（来源：{x['来源'] or '规则自带'}）"
                                      for x in value)
                elif key == "建议":
                    value = "／".join(value)
                L.append(f"    {key}：{value}")
        for u in r["判不动行"]:
            L.append(f"    [判不动行] {_row_label(u['行号'], u.get('序'))}"
                     f"（{u['成因']}）——{u['原因']}")
    for x in out["覆盖"]["未巡检规则"]:
        L.append(f"未巡检 {x['代号']} {x['名称']}（{x['状态']}）：本次范围外，"
                 f"放宽 --rule 再跑才算问过它")
    if out["未用的账套字段"]:
        L.append(f"账套里给了但本次没有规则用到这些字段：{'、'.join(out['未用的账套字段'])}"
                 f"——没用到不等于用不上，是注册表里还没这条判据")
    L.append(f"\n{out['边界']}")
    return "\n".join(L)


def _ordered(values) -> list:
    seen, out = set(), []
    for v in values:
        if v not in (None, "") and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ── 9 命令行 ─────────────────────────────────────────────────

def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="账套巡检（离线，不联网，不含政策数值）")
    ap.add_argument("--list", action="store_true", help="列规则、层、判据与待检回参数")
    ap.add_argument("--pending", action="store_true",
                    help="列还没检回的参数与检索词，交给 ③ 当取数清单")
    ap.add_argument("--ledger", default="", metavar="JSON", help='账套文件：{"行": […]}')
    ap.add_argument("--params", default="", metavar="JSON",
                    help='参数文件：{"参数名": {"值": …, "来源": "…"}}')
    ap.add_argument("--rule", action="append", default=[], metavar="VAT03",
                    help="只跑这几条规则，可重复")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="「不等于」判据的相对容差，[0,1)，默认 0（差一点就算不一致）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    reg = load()
    if args.list:
        for r in reg["规则"]:
            j = r["判据"]
            print(f"{r['代号']} {r['名称']}（{r['层']}｜{r['税种']}／{r['场景']}"
                  f"｜等级 {r['等级']}）")
            print(f"  比的是：{j['左']} {j['算']} {j['右']}｜查什么：{r['查什么']}")
            print(f"  待检回参数：{'、'.join(p['名'] for p in r['参数'])}"
                  f"｜账套字段：{'、'.join(r['账套字段'])}")
        layers = {k: sum(1 for r in reg["规则"] if r["层"] == k) for k in CHECK_LAYERS}
        print(f"\n规则共 {len(reg['规则'])} 条；层分布 "
              + "｜".join(f"{k} {v}" for k, v in layers.items())
              + "（calc 与 global 两层本表暂无落点，引擎已支持，加规则即生效）")
        return 0

    params = _norm_params(_read_json(args.params)) if args.params else {}
    if args.pending:
        print(json.dumps(pending(reg, params), ensure_ascii=False, indent=1))
        return 0
    if not args.ledger:
        print(json.dumps({
            "error": "没给账套就不巡检：这一层不拿题面推断账上的数",
            "先跑": "python scripts/tax_ledger.py --pending"
                    "     # 拿取数工作清单，交给 ③ 逐条检回现行值与来源",
            "再跑": "python scripts/tax_ledger.py --ledger ledger.json --params params.json",
            "账套形态": '{"行": [{"行号": 1, "科目": "…", "金额": 0, …}]}',
        }, ensure_ascii=False, indent=1))
        return 2
    try:
        out = scan(_read_json(args.ledger), params, args.rule, reg, args.tolerance)
    except (ValueError, KeyError, NeedsLedger, json.JSONDecodeError,
            FileNotFoundError) as e:
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
