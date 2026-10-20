#!/usr/bin/env python3
"""多合同受理层：把多份合同收进固定模板，连成候选合同链，再按判断单元比「条款写的」与「做过的」。

它在主线的位置是 ② 的输入侧与 ⑥ 的前一道工序：合同看着是一份一份交来的，要判却是按
「主体 × 具体应税交易 × 业务所属期 × 资格条件」一笔一笔判，同一主体的两笔业务不能并成
一个单元（SKILL ② 末段那套名字一个不换）。适用政策不在这四格里，它是单元定下来之后回 ⑧
取的取值——拿它当拆分的刀，两份合同各引一份文件就把一笔业务切成两个单元，比出来的条数随
引用文件的多少而变，不随业务本身变。

三条硬约束，都在代码里判，不靠文档自觉：

1. **抽取的值必须带可定位的出处**（`_record`）。出处三格：哪份文件、哪一条、原文摘录。
   只给文件名等于说「那份文件里某处写着」，只给条号则抄错了也查不出来——缺一格就把这一条
   记『待核』、不参与比对；值给了而『原文出处』整格不给，受理时报错而不是当成没抽取，因为
   沉默降级会把猜的读成抽的。填表来的值不要求出处，但输出里标成来源『填表』，读者知道要
   向用户核对原件。
2. **合同链只做到候选**（`build_chain`）。补充还是替代判错一边，就把还生效的条款作废或把
   改掉的条款算回来，两种都错得很像。所以边连出来、依据那句带出来，状态一律『待用户确认』；
   只有按边代号显式 `--confirm` 才转『已确认』，未确认的『替代』边不参与判定哪份生效。
3. **比对做不成不写成通过**（`reconcile`）。结论名字取自 `tax_inspect` 的一致性核对四值
   （一致／矛盾／缺一边／未取数），本层不加第五个；做不成各有各的成因与动作，都不写成
   『通过』『无风险』，履行那一侧没记录时只能说『未提供记录，无法确认』（SKILL ⑦ 第 25 条）。

数值解析复用 `tax_calc._dec`，缺口类别与动作、两条红线词表复用 `tax_inspect`，覆盖状态复用
`tax_coverage.STATES`，② 的追问句复用 `tax_analyze.CONTEXT_AXES`：同一件事在本仓只有一处名字。

老合同里写的税名（营业税那一类）不是现行税种名。本层只登记『这个称谓是历史的』加上给 ③ 的
检索线索与动作，不登记这个税现在怎么缴；表里的年份与文号是沿革标识与线索，落进答案前必须
回 ⑧ 按观察时点重查，已废止的规定只能用于说明沿革（SKILL ⑦ 第 7 条）。

用法：
    python scripts/tax_intake.py --list
    python scripts/tax_intake.py --blank > intake.json
    python scripts/tax_intake.py --intake intake.json
    python scripts/tax_intake.py --reconcile intake.json --confirm E1 --dimension C4
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tax_analyze  # noqa: E402  ② 的追问句只有一处：CONTEXT_AXES
import tax_calc  # noqa: E402  数值解析复用 _dec，不在这里重写一遍
import tax_coverage  # noqa: E402  覆盖四态沿用覆盖率层那串名字
import tax_inspect  # noqa: E402  核对四值、缺口类别与动作、两条红线词表都取自这一处
import tax_ledger  # noqa: E402  禁数字扫描、『这一格算不算交了』、ISO 日期判据同巡检层
import tax_search  # noqa: E402  历史称谓不得撞上现行税种名，现行名取自 TAX_TYPE_KEYWORDS

REGISTRY_PATH = HERE.parent / "data" / "contract_intake_template.json"
FRAMEWORK_PATH = HERE.parent / "references" / "tax_risk_framework.md"

DIMENSION_KEYS = ("代号", "维度", "税务含义", "风险指标落点", "要素")
ELEMENT_KEYS = ("要素", "为什么", "抽取线索", "落点")
#: 一条要素要记的五格，顺序即输出顺序：前四是取值槽，第五格说明这四个槽里的数从哪来。
ELEMENT_SLOTS = ("金额", "比例", "期限", "触发时点", "原文出处")
#: 取值槽里要做数值比对的两格；其余两格比的是说法与日期。
NUMERIC_SLOTS = ("金额", "比例")
#: 一条要素记录认的键。槽名写错会静默变成『未提供』，所以未知键一律报错。
RECORD_KEYS = ELEMENT_SLOTS + ("来源", "口径")
#: 一条合同链边认的键：边本身也是「谁说的」这件事，所以带来源与出处。
EDGE_KEYS = ("代号", "在先合同", "在后合同", "类型", "来源", "原文出处")
SOURCE_CELLS = ("文件", "位置", "摘录")

FILL, EXTRACT, ABSENT = "填表", "抽取", "未提供"
SOURCES = (FILL, EXTRACT, ABSENT)

EDGE_KINDS = ("主从", "补充", "替代", "关联")
PENDING, CONFIRMED = "待用户确认", "已确认"
CHAIN_STATES = (PENDING, CONFIRMED)

#: 判断单元的四格，逐字取自 SKILL ② 末段那句定义，本层不重命名也不加第五格。
UNIT_KEYS = ("主体", "具体应税交易", "业务所属期", "资格条件")
#: 『主体』『业务所属期』缺时直接回吐 ② 现成的追问句，本层不另写一句。
AXIS_OF_UNIT_KEY = {"主体": "entity", "业务所属期": "time"}
UNIT_ASK = {
    "具体应税交易": "这笔业务具体是哪一项应税交易？同一主体名下不同交易要分开交——"
                    "免税项与应税项并成一个单元，会把不能叠加的算成可叠加。",
    "资格条件": "这一判断单元的资格条件是什么（纳税人身份、备案、资质那一格）？"
                "单元定不下来，比对结果就不知道归给谁。",
}

CHECK_AGREED, CHECK_CONFLICT = tax_inspect.CHECK_AGREED, tax_inspect.CHECK_CONFLICT
CHECK_ONE_SIDED, CHECK_NOT_TAKEN = tax_inspect.CHECK_ONE_SIDED, tax_inspect.CHECK_NOT_TAKEN
VERDICTS = (CHECK_AGREED, CHECK_CONFLICT, CHECK_ONE_SIDED, CHECK_NOT_TAKEN)

CAUSE_NO_CLAUSE = "条款未提供"
CAUSE_NO_PERFORMANCE = "履行记录缺失"
CAUSE_INCOMPARABLE = "口径不可比"
CAUSE_CONTRACTS_DISAGREE = "多份合同各写一个值"
CAUSE_UNLOCATED = "出处定不到位"
#: 五个成因都有代码路径能产出。「两侧都没交」不在这里：那一格没构成一对，报在受理覆盖的
#: 『缺』与『未填的要素』里（注册表『两侧都没给的要素报在哪』记着为什么不产条目）。
CAUSES = (CAUSE_NO_CLAUSE, CAUSE_NO_PERFORMANCE, CAUSE_INCOMPARABLE,
          CAUSE_CONTRACTS_DISAGREE, CAUSE_UNLOCATED)

#: 成因 → 结论／缺口类别。这两张表读起来像静默查表，所以 `validate()` 拿注册表
#: 『_说明.成因各自的动作』的每一句来比对：那句话里必须写出本表给的结论名与缺口类别名。
#: 表里改了说法而代码没跟着改，载入即报错，不会读出一个没人管的成因。
CAUSE_VERDICT = {
    CAUSE_NO_CLAUSE: CHECK_ONE_SIDED,
    CAUSE_NO_PERFORMANCE: CHECK_ONE_SIDED,
    CAUSE_INCOMPARABLE: CHECK_NOT_TAKEN,
    CAUSE_CONTRACTS_DISAGREE: CHECK_CONFLICT,
    CAUSE_UNLOCATED: CHECK_NOT_TAKEN,
}
CAUSE_GAP = {
    CAUSE_NO_CLAUSE: tax_inspect.GAP_MISSING,
    CAUSE_NO_PERFORMANCE: tax_inspect.GAP_MISSING,
    CAUSE_INCOMPARABLE: tax_inspect.GAP_CALIBER,
    CAUSE_CONTRACTS_DISAGREE: tax_inspect.GAP_CONFLICT,
    #: 出处缺格缺的是那一份能定位的原文，不是口径：归到『政策口径不清』会让人去查文件，
    #: 而该做的动作是把那一条原文连条款号取回来。
    CAUSE_UNLOCATED: tax_inspect.GAP_MISSING,
}
#: 『替代』边没确认时给的动作。择一采用等于把回避矛盾写成事实（SKILL ⑦ 第 23 条）。
CHAIN_ACTION = ("先按 `--confirm <边代号>` 确认这几份之间是补充还是替代：没确认之前两边的条款都进比对，"
                "不得择一采用，也不得据未确认的替代关系认定旧条款失效")
#: 出处不可定位这一格的动作是回 ③ 重取原文，不是向企业要一份新合同——合同在手，缺的是那句原文的条号。
PENDING_ACTION = "回 ③ 把那一条原文连条款号一起取回来，补进原文出处（文件／位置／摘录三格）"
CAUSE_ACTION = {c: tax_inspect.GAP_ACTION[CAUSE_GAP[c]] for c in CAUSES}
CAUSE_ACTION[CAUSE_CONTRACTS_DISAGREE] = CHAIN_ACTION
CAUSE_ACTION[CAUSE_UNLOCATED] = PENDING_ACTION

ENTRY_KEYS = ("判断单元", "维度代号", "要素", "槽", "条款侧", "履行侧",
              "核对结论", "缺口类别", "差异说明", "动作", "依据链")
#: 整条不参与比随时，两侧那两格写的不是『未提供』而是这一句：值确实给了，只是定不到位。
#: 把它标成『未提供』会让读者向企业要一份根本不缺的合同。『口径』留空：没有值就没有口径，
#: 这一格缺着会让 `_label` 在排版时抛 KeyError，把整份报告换成一行错误。
NOT_COMPARED = {"值": "整条未参与比对（见差异说明）", "口径": ""}
#: 要素『落点』的取值：判断单元四格，加上合同链这一层与交给 ② 的触发时点。
LANDINGS = UNIT_KEYS + ("合同链", "触发时点")
#: 历史称谓那一格的「这一笔按哪一时点的规定判」必须落在动作里。这一串说法是判据，
#: 不是政策数值：它只规定动作里要出现一个时间锚，不规定锚在哪一年。
RECHECK_NEEDLES = ("按业务发生时间", "所属期", "当年", "当时", "过渡期", "停征时点")
HISTORICAL_KEYS = ("称谓", "现行", "检索线索", "动作")

#: 注册表里不许出现政策数值的格子。年份与文号只出现在『历史税种称谓对照』那一张表里，
#: 那里写的是沿革标识与 ③ 的检索线索，不参与任何计算（`_说明.历史称谓这一列不算依据`）。
DIGIT_FREE_CELLS = ("维度", "税务含义", "风险指标落点", "要素", "为什么",
                    "抽取线索", "落点", "要素五件套", "落点值域")
#: 两条红线各扫哪几格。越红线词表里有『倒签』『补签』，而『事后补签』正是要从合同里检出的
#: 缺陷——把这两个词扫到线索与要素名上，本层就检不出这类合同了。所以越红线词表只扫本层自己
#: 写的动作与差异说明；要素名与线索里出现『倒签』是检出缺陷，不是开处方。
RED_SCAN_CELLS = ("动作", "差异说明")
PREDICT_SCAN_CELLS = ("税务含义", "为什么", "动作", "差异说明")

STATES = tax_coverage.STATES
SATISFIED, LACK, NA, TO_VERIFY = (tax_coverage.SATISFIED, tax_coverage.LACK,
                                  tax_coverage.NA, tax_coverage.TO_VERIFY)
#: 与巡检层同一口径：本层说的是『这一格没交』，不是『这件事没发生』。
NO_RECORD_NOTE = ("这一格没给记录：缺记录不等于未发生，本层只说无法确认，"
                  "不写『未付款』『未发生』『无需申报』『不涉及该税种』（SKILL ⑦ 第 25 条）。")
#: 一份材料都没给时的回话：交付的是拿到材料那一步，不是一份空报告。
NO_CONTRACT_NOTE = ("一份合同都没给，也没有判断单元——本层不产出比对结论。"
                    "先向企业要那一叠合同与履行凭证，按 `--blank` 出的那张表逐格填；"
                    "只有合同还没定单元时，先跑 `--intake` 看九维度罩住了没有。")

COVERAGE_NOTE = ("已满足＝这一维至少有一条要素的值带着可定位出处（或来源是填表）；"
                 "待核＝有值却没有一条出处定得到位置；缺＝整维没有任何值；"
                 "不适用＝调用方给了理由。没交不等于不涉及，『缺』与『不适用』各写各的。")


class NeedsContracts(ValueError):
    """受理门槛：连一份合同或一个判断单元都没给。"""


# ── 1 载入与自检 ──────────────────────────────────────────────

def _scan_no_digits(value, where, field):
    return tax_ledger._scan_no_digits(value, where, field)


def _nonempty(value, where, field):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{where} 的『{field}』是空的——空格子看着有内容其实判不动")
    return text


def _given(value):
    return tax_ledger._given(value)


def _flag(text, where, field, words, kind):
    hit = tax_inspect._bad_words(text, words)
    if hit:
        raise ValueError(f"{where} 的『{field}』写了{kind} {hit}：{text}")


#: 只用来把 `tax_risk_framework.md` 的三级节名切出来，不是政策数值。
H3 = "### "


def framework_sections() -> list:
    """`tax_risk_framework.md` 里的指标大类节名，用来验『风险指标落点』指得到人。"""
    return [line[len(H3):].strip()
            for line in FRAMEWORK_PATH.read_text(encoding="utf-8").splitlines()
            if line.startswith(H3)]


def validate(reg: dict) -> None:
    """模板的名字清单、结构、跨文件落点与两条红线都在载入时判，不留到输出里靠人眼抓。"""
    dims = reg.get("维度")
    if not isinstance(dims, list) or not dims:
        raise ValueError("模板里没有『维度』清单")
    hist = reg.get("历史税种称谓对照")
    if not isinstance(hist, list) or not hist:
        raise ValueError("模板里没有『历史税种称谓对照』")

    note = reg.get("_说明") or {}
    for field, want in (("九维度", tuple(d["维度"] for d in dims)),
                        ("维度字段", DIMENSION_KEYS), ("要素字段", ELEMENT_KEYS),
                        ("要素五件套", ELEMENT_SLOTS), ("记录字段", RECORD_KEYS),
                        ("合同链边字段", EDGE_KEYS), ("判断单元字段", UNIT_ROW_KEYS),
                        ("原文出处的三格", SOURCE_CELLS),
                        ("取值来源三态", SOURCES), ("合同链边类型", EDGE_KINDS),
                        ("合同链确认状态", CHAIN_STATES), ("判断单元四要素", UNIT_KEYS),
                        ("一致性核对四值", VERDICTS), ("比对结论的成因", CAUSES),
                        ("差异条目字段", ENTRY_KEYS), ("落点值域", LANDINGS),
                        ("历史税种称谓", tuple(r["称谓"] for r in hist)),
                        ("重查指针的说法", RECHECK_NEEDLES),
                        ("不许出现数字的格子", DIGIT_FREE_CELLS)):
        got = list(note.get(field) or [])
        if got != list(want):
            raise ValueError(f"_说明『{field}』与表里的实际形状不同源：清单是 {got}，"
                             f"逐格读到的是 {list(want)}")

    causes = note.get("成因各自的动作") or {}
    if sorted(causes) != sorted(CAUSES):
        raise ValueError(f"『成因各自的动作』的键与代码的成因不同源："
                         f"表里是 {sorted(causes)}，代码是 {sorted(CAUSES)}")
    for cause, sentence in causes.items():
        miss = [x for x in (CAUSE_VERDICT[cause], CAUSE_GAP[cause]) if x not in sentence]
        if miss:
            raise ValueError(f"『成因各自的动作．{cause}』没写出代码给的结论与缺口类别 {miss}："
                             f"表与代码各说一套时，读表的人会把这一格判成别的结论")

    redline = note.get("红线扫在哪几格") or {}
    for field, want in (("越红线词表只扫", RED_SCAN_CELLS), ("预测词表扫", PREDICT_SCAN_CELLS)):
        got = list(redline.get(field) or [])
        if got != list(want):
            raise ValueError(f"『红线扫在哪几格．{field}』与代码不同源：表里是 {got}，代码是 {list(want)}")

    codes = [d.get("代号") for d in dims]
    want_codes = [f"C{i}" for i in range(1, len(codes) + 1)]
    if codes != want_codes:
        raise ValueError(f"维度代号必须从 C1 连续排下来：读到 {codes}，应为 {want_codes}")
    dup = [x for x in set(d["维度"] for d in dims)
           if sum(1 for y in dims if y["维度"] == x) > 1]
    if dup:
        raise ValueError(f"维度名重复 {dup}，代号与名字必须一对一")

    _scan_no_digits(list(note["要素五件套"]), "注册表『_说明』", "要素五件套")
    _scan_no_digits(list(note["落点值域"]), "注册表『_说明』", "落点值域")
    _scan_no_digits(list(note["重查指针的说法"]), "注册表『_说明』", "重查指针的说法")

    sections = framework_sections()
    for d in dims:
        code = d.get("代号")
        where = f"维度 {code}"
        for field in DIMENSION_KEYS:
            if field not in d:
                raise ValueError(f"{where} 缺『{field}』这一格")
        for field in ("维度", "税务含义", "风险指标落点"):
            _scan_no_digits(d[field], f"注册表『{where}』", field)
        if not any(d["风险指标落点"] in s and "风险指标" in s for s in sections):
            raise ValueError(f"{where} 的『风险指标落点』「{d['风险指标落点']}」在 "
                             f"references/tax_risk_framework.md 里没有对应小节：落点指不到人，"
                             f"差异条目就喂不进那一层")
        _flag(d["税务含义"], where, "税务含义",
              tax_inspect.PREDICTION_PHRASES, "稽查结果预测")
        elems = d.get("要素")
        if not elems:
            raise ValueError(f"{where} 没有要素——空维度让覆盖数看着够，实际一格判不动")
        seen_names = set()
        for e in elems:
            ename = e.get("要素") or "（无名）"
            ewhere = f"{where}／要素 {ename}"
            for field in ELEMENT_KEYS:
                if field not in e:
                    raise ValueError(f"{ewhere} 缺『{field}』这一格")
            if ename in seen_names:
                raise ValueError(f"{where} 的要素名「{ename}」重复：同一维度里两个同名要素，"
                                 f"后填的那一份会顶掉前一份，读的人看不出少了哪一条")
            seen_names.add(ename)
            for field in ("要素", "为什么", "抽取线索", "落点"):
                _scan_no_digits(e[field], f"注册表『{ewhere}』", field)
            if e["落点"] not in LANDINGS:
                raise ValueError(f"{ewhere} 的落点「{e['落点']}」不在取值域：取 {list(LANDINGS)}")
            tax_inspect._nonempty_strs(e["抽取线索"], ewhere, "抽取线索")
            _flag(e["为什么"], ewhere, "为什么",
                  tax_inspect.PREDICTION_PHRASES, "稽查结果预测")

    live = set(tax_search.TAX_TYPE_KEYWORDS)
    for row in hist:
        name = row.get("称谓") or "（无称谓）"
        where = f"历史称谓 {name}"
        for field in HISTORICAL_KEYS:
            if field not in row:
                raise ValueError(f"{where} 缺『{field}』这一格")
        if name in live:
            raise ValueError(f"「{name}」在 ⑨ 的现行税种名里，不是历史称谓："
                             f"把它登记成历史称谓会让现行合同被误提醒")
        _nonempty(row["现行"], where, "现行")
        # 时间锚：一句『归到车船税』而不说按哪一时点的文件取，读者就会拿今天的税额表顶位。
        if not any(n in row["动作"] for n in RECHECK_NEEDLES):
            raise ValueError(f"{where} 的『动作』没写出这一笔按哪一时点的规定判："
                             f"要含 {list(RECHECK_NEEDLES)} 里的一个说法。历史称谓只能说明沿革，"
                             f"当期怎么缴要回 ⑧ 按观察时点重查（SKILL ⑦ 第 7 条）")
        tax_inspect._nonempty_strs(row["检索线索"], where, "检索线索")
        # 越红线词表只扫本层写的动作：这里扫的是『这个称谓现在怎么缴』的动作句，
        # 出现『倒签』『补签』就成了建议改动历史事实。
        _flag(row["动作"], where, "动作",
              tax_inspect.FORBIDDEN_REMEDIATION, "改动历史事实的说法")
        _flag(row["动作"], where, "动作",
              tax_inspect.PREDICTION_PHRASES, "稽查结果预测")


def load(path: Path = None) -> dict:
    reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    validate(reg)
    return reg


def index(reg: dict = None) -> dict:
    """{维度代号: {"维度": 名, "落点": 指标大类, "要素": {要素名: 要素}}}。"""
    reg = reg or load()
    return {d["代号"]: {"维度": d["维度"], "落点": d["风险指标落点"],
                        "要素": {e["要素"]: e for e in d["要素"]}}
            for d in reg["维度"]}


def elements(reg: dict = None) -> list:
    """摊平成 (维度代号, 维度名, 要素) 三元组，顺序即模板顺序。"""
    reg = reg or load()
    return [(d["代号"], d["维度"], e) for d in reg["维度"] for e in d["要素"]]


def historical(reg: dict = None) -> dict:
    """{历史称谓: 那一行的全文}，按模板顺序。"""
    reg = reg or load()
    return {r["称谓"]: r for r in reg["历史税种称谓对照"]}


# ── 2 空白填表：半结构化那一格先给出来 ────────────────────────

def _blank_record() -> dict:
    return {"来源": EXTRACT, **{k: "" for k in NUMERIC_SLOTS},
            "期限": "", "触发时点": "", "口径": "",
            "原文出处": {k: "" for k in SOURCE_CELLS}}


def _blank_side(reg: dict) -> dict:
    return {d["代号"]: {e["要素"]: _blank_record() for e in d["要素"]} for d in reg["维度"]}


def blank(reg: dict = None) -> dict:
    """出一张空白表：九维度 × 要素 × 五件套，取值全空、来源标『抽取』。

    要素格子整格留空读作『没交这一条』（`_record` 的『空』），所以这张表可以直接交给用户或
    Agent 填：漏填只在覆盖里报『缺』，不进比对、也不报错。合同代号与名称是标识，空着报错——
    一份叫不出名字的合同，依据链里写不出出处来自哪一份。

    『边』一栏留空：没有第二份合同就没有关系可连，写一行空边反而会让受理停在「在后合同是空的」
    上。要连边时按 `--list` 报出的边字段加一行；单元那一栏同理，一笔业务一个单元。
    """
    reg = reg or load()
    return {
        "合同": [{"代号": "CT-1", "名称": "", "签订方": "", "签订日期": "", "标的": "",
                  "条款": _blank_side(reg)}],
        "边": [],
        "不适用": {},
        "判断单元": [{"代号": "U1", **{k: "" for k in UNIT_KEYS}, "适用政策": "",
                      "合同": ["CT-1"], "履行": _blank_side(reg)}],
    }


# ── 3 记录归一：来源、值、出处三件一起判 ──────────────────────

def _cite(raw, where: str, owner: str = ""):
    """出处三格。整格没给回 None；给了则『文件』必须能定位，且与这条记录挂着的文件一致。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 的『原文出处』要写成 {{文件/位置/摘录}} 三格")
    got = {k: str(raw.get(k) or "").strip() for k in SOURCE_CELLS}
    if not any(got.values()):
        return None
    unknown = [k for k in raw if k not in SOURCE_CELLS]
    if unknown:
        raise ValueError(f"{where} 的『原文出处』有认不出的键 {unknown}：取 {list(SOURCE_CELLS)}")
    if not got["文件"]:
        raise ValueError(f"{where} 的『原文出处』没写『文件』：出处定不到哪一份材料上，"
                         f"就等于没给出处")
    if owner and got["文件"] != owner:
        raise ValueError(f"{where} 挂在 {owner} 上，出处却写 {got['文件']}："
                         f"两份文件的句子不能混成一条")
    return got


def _record(raw, where: str, owner: str) -> dict:
    """一条要素记录归一成 {来源, 值, 口径, 出处, 出处可用, 空}。"""
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 不是一条要素记录（要 {list(RECORD_KEYS)} 那一组键）")
    unknown = [k for k in raw if k not in RECORD_KEYS]
    if unknown:
        raise ValueError(f"{where} 有认不出的键 {unknown}：槽名写错会静默变成『未提供』，"
                         f"可用的键是 {list(RECORD_KEYS)}")
    src = str(raw.get("来源") or "").strip()
    if src not in SOURCES:
        raise ValueError(f"{where} 的来源『{src or '（空）'}』不在三态里：取 {list(SOURCES)}")
    slots = {k: raw.get(k) for k in ELEMENT_SLOTS if k != "原文出处"}
    has_value = any(_given(v) for v in slots.values())
    cite = _cite(raw.get("原文出处"), where, owner)
    if src == ABSENT and (has_value or cite):
        raise ValueError(f"{where} 的来源写成『未提供』却给了值或出处：两者说的相反——"
                         f"要么把来源改成 {FILL}／{EXTRACT}，要么把这一条清空")
    out = {"来源": src, "值": slots, "口径": str(raw.get("口径") or "").strip(),
           "出处": cite, "出处可用": src == FILL or (cite is not None
                                                  and all(cite[k] for k in SOURCE_CELLS)),
           #: 只给了出处没给值算没交这一条：那一句摘录留着给『线索命中却没交』用，
           #: 值没抽出来就不能进比对，也不能被读成一条有值的记录。
           "空": not has_value}
    if has_value and src == EXTRACT and cite is None:
        raise ValueError(f"{where} 的值标成来源『{EXTRACT}』却没有『原文出处』这一格："
                         f"抽取的值必须可定位，否则本层分不清这是抽出来的还是猜出来的——"
                         f"补齐 {{文件/位置/摘录}}，或把来源改成 {FILL}")
    return out


def _contracts(doc, reg) -> dict:
    """把每份合同的条款摊成 {合同代号: {维度代号: {要素名: 归一记录}}}。"""
    idx = index(reg)
    raw = doc.get("合同")
    if not isinstance(raw, list) or not raw:
        return {}
    seen, out = set(), {}
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError("『合同』里的每一项都要是对象（代号、名称、条款那一组）")
        code = _nonempty(c.get("代号"), "合同", "代号")
        if code in seen:
            raise ValueError(f"合同代号 {code} 重复：两条合同用同一个代号，"
                             f"依据链就分不清出处来自哪一份")
        seen.add(code)
        name = _nonempty(c.get("名称"), f"合同 {code}", "名称")
        body = {}
        for dim, elems in (c.get("条款") or {}).items():
            if dim not in idx:
                raise ValueError(f"合同 {code} 的条款里出现了模板没有的维度代号 {dim}："
                                 f"取 {list(idx)}，见 --list")
            if not isinstance(elems, dict):
                raise ValueError(f"合同 {code}／{dim} 要是一张 {{要素名: 记录}} 的表")
            row = {}
            for ename, rec in elems.items():
                if ename not in idx[dim]["要素"]:
                    raise ValueError(f"合同 {code}／{dim} 里的要素名「{ename}」不在模板里："
                                     f"取 {list(idx[dim]['要素'])}")
                row[ename] = _record(rec, f"合同 {code}／{dim}／{ename}", code)
            body[dim] = row
        out[code] = {"名称": name, "条款": body,
                     "签订方": str(c.get("签订方") or "").strip(),
                     "签订日期": str(c.get("签订日期") or "").strip(),
                     "标的": str(c.get("标的") or "").strip()}
    return out


def _absent(doc, idx) -> dict:
    """显式标『不适用』的维度必须带理由，代号要在模板里。

    理由原样转述企业的话，不在这里改写也不扫红线词表：一句『这笔已经倒签了』正是要检出的
    事实，扫成越红线就把材料本身拒掉了（同 `tax_inspect`：调用方给的回答原样带走）。
    """
    raw = doc.get("不适用") or {}
    if not isinstance(raw, dict):
        raise ValueError("『不适用』要写成 {维度代号: 理由}")
    unknown = [k for k in raw if k not in idx]
    if unknown:
        raise ValueError(f"『不适用』里的 {unknown} 不是模板的维度代号：取 {list(idx)}")
    return {code: _nonempty(reason, f"维度 {code} 标『不适用』", "理由")
            for code, reason in raw.items()}


#: 一条判断单元认的键。四格写错一个字（「主体」打成「甲方」）会静默变成『缺格』，
#: 报出来的是「向用户追问主体」，而真正错的是这张表的键名——所以认不出的键直接报错。
UNIT_ROW_KEYS = ("代号",) + UNIT_KEYS + ("适用政策", "合同", "履行")


def _units(doc, contracts, idx) -> list:
    out, seen = [], set()
    for u in doc.get("判断单元") or []:
        if not isinstance(u, dict):
            raise ValueError("『判断单元』里的每一项都要是对象")
        code = _nonempty(u.get("代号"), "判断单元", "代号")
        if code in seen:
            raise ValueError(f"判断单元代号 {code} 重复：比对结果归不到一笔业务")
        seen.add(code)
        unknown = [k for k in u if k not in UNIT_ROW_KEYS]
        if unknown:
            raise ValueError(f"判断单元 {code} 有认不出的键 {unknown}：取 {list(UNIT_ROW_KEYS)}")
        codes = u.get("合同")
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"判断单元 {code} 没写『合同』这一格：单元不知道由哪几份合同的条款来说，"
                             f"比对就只能凭空")
        unknown = [c for c in codes if c not in contracts]
        if unknown:
            raise ValueError(f"判断单元 {code} 引用了『合同』清单里没有的代号 {unknown}："
                             f"可用代号 {list(contracts)}")
        row = {"代号": code, "四要素": {}, "缺格": [],
               "适用政策": str(u.get("适用政策") or "").strip(), "合同": codes,
               "履行": _performance(u, idx, code)}
        for k in UNIT_KEYS:
            v = str(u.get(k) or "").strip()
            if v:
                row["四要素"][k] = v
            else:
                row["缺格"].append(k)
        out.append(row)
    return out


def _performance(unit, idx, code) -> dict:
    raw = unit.get("履行") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"单元 {code} 的『履行』要是一张 {{维度代号: {{要素名: 记录}}}} 的表")
    out = {}
    for dim, elems in raw.items():
        if dim not in idx:
            raise ValueError(f"单元 {code} 的履行里出现了模板没有的维度代号 {dim}：取 {list(idx)}")
        if not isinstance(elems, dict):
            raise ValueError(f"单元 {code}／履行／{dim} 要是一张 {{要素名: 记录}} 的表")
        row = {}
        for ename, rec in elems.items():
            if ename not in idx[dim]["要素"]:
                raise ValueError(f"单元 {code}／履行／{dim} 里的要素名「{ename}」不在模板里："
                                 f"取 {list(idx[dim]['要素'])}")
            row[ename] = _record(rec, f"单元 {code}／履行／{dim}／{ename}", "")
        out[dim] = row
    return out


# ── 4 合同链：只连候选 ───────────────────────────────────────

def build_chain(doc: dict, contracts: dict, confirmed=()) -> dict:
    """把边收成候选链，只有 `confirmed` 里的边代号才转成『已确认』。

    链上出现互相替代（这一份替掉那一份、那一份又替掉这一份）时报错：照单执行就没有一份生效，
    读者看到的会是一份没有条款的报告。
    """
    edges, seen = [], set()
    for e in doc.get("边") or []:
        if not isinstance(e, dict):
            raise ValueError("『边』里的每一项都要是对象")
        code = _nonempty(e.get("代号"), "边", "代号")
        if code in seen:
            raise ValueError(f"边代号 {code} 重复：`--confirm` 认代号，重复就没法指名这一条")
        seen.add(code)
        for field in EDGE_KEYS:
            if field not in e:
                raise ValueError(f"边 {code} 缺『{field}』这一格")
        unknown = [k for k in e if k not in EDGE_KEYS]
        if unknown:
            raise ValueError(f"边 {code} 有认不出的键 {unknown}：取 {list(EDGE_KEYS)}")
        a = _nonempty(e["在先合同"], f"边 {code}", "在先合同")
        b = _nonempty(e["在后合同"], f"边 {code}", "在后合同")
        if a == b:
            raise ValueError(f"边 {code} 的两端是同一份合同 {a}：自己对自己没有主从、补充或替代关系")
        for x in (a, b):
            if x not in contracts:
                raise ValueError(f"边 {code} 指向合同 {x}，『合同』清单里没有这一份："
                                 f"可用代号 {list(contracts)}")
        kind = e["类型"]
        if kind not in EDGE_KINDS:
            raise ValueError(f"边 {code} 的类型「{kind}」不在取值域：取 {list(EDGE_KINDS)}")
        src = str(e["来源"] or "").strip()
        if src not in SOURCES:
            raise ValueError(f"边 {code} 的来源『{src or '（空）'}』不在三态里：取 {list(SOURCES)}")
        cite = _cite(e.get("原文出处"), f"边 {code}")
        if src == EXTRACT and cite is None:
            raise ValueError(f"边 {code} 的类型是抽出来的（来源『{EXTRACT}』）却没带出处："
                             f"『未尽事宜按原合同执行』那一句在哪一条，不写出来这条边就只是猜")
        state = CONFIRMED if code in set(confirmed) else PENDING
        edges.append({"代号": code, "在先": a, "在后": b, "类型": kind, "来源": src,
                      "状态": state, "依据": cite,
                      "出处可用": src == FILL or (cite is not None
                                               and all(cite[k] for k in SOURCE_CELLS))})
    replaced = [e for e in edges if e["类型"] == "替代" and e["状态"] == CONFIRMED]
    pairs = {(e["在先"], e["在后"]) for e in replaced}
    mutual = [ab for ab in sorted(pairs) if (ab[1], ab[0]) in pairs]
    if mutual:
        a, b = mutual[0]
        raise ValueError(f"合同链把 {a} 替成 {b}，又确认了 {b} 替回 {a}：这两条边同时执行时，"
                         f"两份合同里同维同名的条款互相顶掉，比对的条款侧就整片空掉，报出来的全是"
                         f"『条款未提供』。把其中一条的确认收回去（`--confirm` 只留一条边代号）")
    pending = []
    for e in edges:
        if e["状态"] == PENDING:
            e["动作"] = CHAIN_ACTION if e["类型"] == "替代" else \
                "确认这一条关系；没确认时两边的条款都进比对，不据此认定任何一份失效"
            if not e["出处可用"]:
                e["动作"] += "；这条边的出处还定不到位（文件／位置／摘录缺一格），确认之前先补齐"
            pending.append(e)
    return {"边": edges, "待用户确认": pending, "已确认替代": replaced}


def superseded_cells(chain: dict, contracts: dict) -> dict:
    """已确认的『替代』边落在哪些 (合同, 维度, 要素) 上——替代按要素落位，不整份作废。

    补充协议普遍只改那一条款（实测形态：『第六条约定的付款期限变更为…原条款不再执行』），
    整份作废会把没被改动过的对价、开票条款一起摘掉，报出来的就是「合同条款里没找到这一格」，
    而那一条明明在合同里写着。所以只有在先那份的这条要素在后一份里也有记录时才算被替代；
    后一份没写这一条，旧条款没有被取代，仍然采信。
    """
    out = {}
    for edge in chain["已确认替代"]:
        a, b = edge["在先"], edge["在后"]
        for dim, row in contracts[a]["条款"].items():
            for ename, rec in row.items():
                if rec["空"]:
                    continue
                other = contracts[b]["条款"].get(dim, {}).get(ename)
                if other and not other["空"]:
                    out[(a, dim, ename)] = {"由": b, "边": edge["代号"]}
    return out


# ── 5 受理覆盖：九维度罩住了没有 ─────────────────────────────

def intake(doc: dict, reg: dict = None) -> dict:
    """受理报告：合同清单、九维度覆盖、出处待核的抽取、没交的要素、历史税种称谓提醒。"""
    reg = reg or load()
    idx = index(reg)
    contracts = _contracts(doc, reg)
    if not contracts:
        raise NeedsContracts(NO_CONTRACT_NOTE)
    na = _absent(doc, idx)

    rows, pending, clues = [], [], []
    for code, dname in [(d["代号"], d["维度"]) for d in reg["维度"]]:
        usable, cited, untouched = [], [], []
        for ename in idx[code]["要素"]:
            recs = [(c, contracts[c]["条款"].get(code, {}).get(ename)) for c in contracts]
            got = [(c, r) for c, r in recs if r and not r["空"]]
            if [c for c, r in got if r["出处可用"]]:
                usable.append(ename)
            elif got:
                cited.append(ename)
                pending += [{"合同": c, "要素": f"{code}／{ename}"} for c, _r in got]
            else:
                untouched.append(ename)
                hit = _clue_hit(idx[code]["要素"][ename]["抽取线索"], contracts, code, ename)
                if hit:
                    clues.append({"要素": f"{code}／{ename}", "命中": hit})
        if code in na:
            state = NA
        elif usable:
            state = SATISFIED
        elif cited:
            state = TO_VERIFY
        else:
            state = LACK
        rows.append({"维度代号": code, "维度": dname, "状态": state,
                     "可用要素": usable, "待核要素": cited, "未填要素": untouched,
                     "不适用理由": na.get(code, "")})

    hits = historical_hits(doc, reg)
    return {
        "合同": [{"代号": c, "名称": v["名称"], "签订方": v["签订方"],
                  "签订日期": v["签订日期"], "标的": v["标的"]} for c, v in contracts.items()],
        "合同数": len(contracts),
        "合同链": build_chain(doc, contracts),
        "覆盖": {"按维度": rows, "判据": COVERAGE_NOTE,
                 "状态": (tax_inspect.COVERAGE_COMPLETE
                          if all(r["状态"] in (SATISFIED, NA) for r in rows)
                          else tax_inspect.COVERAGE_PARTIAL)},
        "已满足维度数": sum(1 for r in rows if r["状态"] == SATISFIED),
        "待核维度数": sum(1 for r in rows if r["状态"] == TO_VERIFY),
        "缺维度数": sum(1 for r in rows if r["状态"] == LACK),
        "不适用维度数": sum(1 for r in rows if r["状态"] == NA),
        "待核清单": pending,
        "未填的要素": [f"{r['维度代号']}／{n}" for r in rows for n in r["未填要素"]],
        "线索命中却没交": clues,
        "触发时点清单": time_points(contracts),
        "历史称谓提醒": hits,
        "边界": ("本层只说这些条款收齐了没有、哪一条的出处定不到位，不判这笔业务该怎么缴税；"
                 "条款与履行的比对走 --reconcile，税目定性与现行口径回 ⑧ 与 ⑨。"
                 + NO_RECORD_NOTE),
    }


def _clue_hit(terms, contracts, dim, ename) -> list:
    """抽取线索在别处摘录里出现过、这一条却整格没交——最容易漏的就长这样。

    跳过这一条自己的摘录：只给了出处没给值的记录也算没交，把它的摘录拿来跟自己那条线索
    相对，报出来的就是「别处有」这句假话。命中只报「哪个词在哪一条的摘录里」，不重述摘录
    本身：把那一句抄进报告再截断，读者看到的是本层挑过一半的话，位置对了也以为读全了。
    """
    hits = []
    for code, contract in contracts.items():
        for d, row in contract["条款"].items():
            for e, rec in row.items():
                if d == dim and e == ename:
                    continue
                cite = rec["出处"]
                if not cite or not cite["摘录"]:
                    continue
                for term in terms:
                    if term in cite["摘录"]:
                        hits.append(f"{term}（出现在 {code}／{d}／{e} 的摘录）")
    return hits


# ── 6 比对：逐单元、逐要素、逐槽 ─────────────────────────────

def _num(value, where, slot):
    """数值槽取值：认 JSON 数与去掉千分位、空白后的写法，『比例』一槽容得下尾格的百分号。

    百分号只当写法，不当量级：按号前那个数取值，不折成小数——合同里没写过那一个小数，折出来的
    是本层自己换算的。两侧一侧写百分数、一侧写同值的小数时判成矛盾，两侧原值都显示出来，
    让读的人回原文认那一个口径。
    """
    text = str(value).strip().replace(",", "")
    if slot == "比例":
        text = text.rstrip("%").strip()
    try:
        return tax_calc._dec(text)
    except (ValueError, TypeError):
        raise ValueError(f"{where} 的「{slot}」给了 {value!r}，不是可算的数值——"
                         f"要么给数（千分位与尾格百分号认），要么整格不给"
                         f"（整格不给判『条款未提供』或『履行记录缺失』）；"
                         f"币种、含税与否这类说法写进『口径』那一格，不写在数值里") from None


def _same(slot, left, right) -> tuple:
    """两格值相等吗：数值槽按 Decimal 比，其余按去掉首尾空白的说法比，日期两端都给补一句先后。"""
    where = f"{left['在哪']}｜{right['在哪']}／{slot}"
    if slot in NUMERIC_SLOTS:
        a, b = _num(left["值"], where, slot), _num(right["值"], where, slot)
        return (True, "") if a == b else (False, f"差 {a - b}")
    a, b = str(left["值"]).strip(), str(right["值"]).strip()
    if a == b:
        return True, ""
    if tax_ledger.ISO_DATE.fullmatch(a) and tax_ledger.ISO_DATE.fullmatch(b):
        return False, (f"{a} 早于 {b}" if a < b else f"{a} 晚于 {b}")
    return False, f"条款写「{a}」，履行记「{b}」"


def _label(side) -> str:
    if not side:
        return "未提供"
    if side.get("多份"):
        return "｜".join(f"{c}：{v}" + (f"（{k}）" if k else "")
                         for c, v, k in side["多份"])
    return f"{side['值']}" + (f"（{side['口径']}）" if side["口径"] else "")


def _clause_side(clause, slot):
    """条款侧这一槽的值。多份合同各写一个值时不成对，交回一个『多份』标记。"""
    vals = [(c, r) for c, r in clause if _given(r["值"].get(slot))]
    if not vals:
        return None
    distinct = {}
    for c, r in vals:
        distinct.setdefault(str(r["值"][slot]).strip(), None)
    if len(distinct) > 1:
        return {"多份": [(c, r["值"][slot], r["口径"]) for c, r in vals]}
    c, r = vals[0]
    return {"值": r["值"][slot], "在哪": f"合同 {c}", "口径": r["口径"],
            "来源": r["来源"], "出处": r["出处"]}


def _perf_side(unit, dim, ename, slot):
    rec = unit["履行"].get(dim, {}).get(ename)
    if not rec or not _given(rec["值"].get(slot)):
        return None
    return {"值": rec["值"][slot], "在哪": f"单元 {unit['代号']}／履行",
            "口径": rec["口径"], "来源": rec["来源"], "出处": rec["出处"]}


#: 填表来的值不要求出处，但每一处引用它的输出都要带上这一句：读者看得见这一格还没跟原件核对过。
SOURCE_TAG = "［填表，未经抽取核对］"


def _chain(left, right, elem) -> list:
    out = []
    for side in (left, right):
        if not side or side.get("多份") or "在哪" not in side:
            continue
        cite = side.get("出处")
        out.append(side["在哪"] + (f"：{cite['位置']}「{cite['摘录']}」"
                                   if cite and cite["位置"] else "")
                   + (SOURCE_TAG if side["来源"] == FILL else ""))
    out.append(f"落点：{elem['落点']}")
    return out


def _entry(unit, dim, ename, slot, left, right, verdict, gap, why, action, elem):
    return {"判断单元": unit["代号"], "维度代号": dim, "要素": ename, "槽": slot,
            "条款侧": _label(left), "履行侧": _label(right), "核对结论": verdict,
            "缺口类别": gap, "差异说明": why, "动作": action,
            "依据链": _chain(left, right, elem)}


def _compare_element(unit, dim, ename, clause, idx) -> list:
    """一条要素的四个取值槽逐槽比对；条款侧可以是多份合同，履行侧一条记录。

    同一条要素里只要有一份记录出处定不到位，整条就不比对而不是只比出处齐的那几份：
    只信任何一半都会把「两份合同各写一个值」这一格在报告里抹掉，抹掉的那一份恰恰是差异。
    """
    elem = idx[dim]["要素"][ename]
    perf = unit["履行"].get(dim, {}).get(ename)
    untrusted = [(c, r) for c, r in clause if not r["出处可用"]]
    perf_untrusted = bool(perf and not perf["空"] and not perf["出处可用"])
    if untrusted or perf_untrusted:
        whose = sorted({c for c, _r in untrusted})
        if perf_untrusted:
            whose.append(f"单元 {unit['代号']}／履行")
        return [_entry(unit, dim, ename, "", NOT_COMPARED, NOT_COMPARED,
                       CAUSE_VERDICT[CAUSE_UNLOCATED], CAUSE_GAP[CAUSE_UNLOCATED],
                       f"这一条的值来自 {'、'.join(whose)}，出处定不到位"
                       f"（文件／位置／摘录缺一格），整条不参与比对（成因：{CAUSE_UNLOCATED}）",
                       CAUSE_ACTION[CAUSE_UNLOCATED], elem)]

    rows = []
    for slot in ELEMENT_SLOTS:
        if slot == "原文出处":
            continue
        left = _clause_side(clause, slot)
        right = _perf_side(unit, dim, ename, slot)
        if left is None and right is None:
            continue
        rows.append(_verdict(unit, dim, ename, slot, left, right, elem))
    return rows


def _verdict(unit, dim, ename, slot, left, right, elem) -> dict:
    where = f"单元 {unit['代号']}／{dim}／{ename}／{slot}"
    if left and left.get("多份"):
        return _entry(unit, dim, ename, slot, left, right,
                      CAUSE_VERDICT[CAUSE_CONTRACTS_DISAGREE],
                      CAUSE_GAP[CAUSE_CONTRACTS_DISAGREE],
                      f"{where}：{len(left['多份'])} 份合同在这一格各写一个值："
                      + "；".join(f"{c} 写 {v}" + (f"（{k}）" if k else "")
                                  for c, v, k in left["多份"])
                      + f"（成因：{CAUSE_CONTRACTS_DISAGREE}）",
                      CAUSE_ACTION[CAUSE_CONTRACTS_DISAGREE], elem)
    if left is None or right is None:
        cause = CAUSE_NO_CLAUSE if left is None else CAUSE_NO_PERFORMANCE
        side = "合同条款里没找到这一格，只有履行侧的记录" if left is None \
            else "合同条款写了这一格，履行侧没给记录"
        action = CAUSE_ACTION[cause]
        if cause == CAUSE_NO_PERFORMANCE:
            action += "；" + NO_RECORD_NOTE
        return _entry(unit, dim, ename, slot, left, right, CAUSE_VERDICT[cause],
                      CAUSE_GAP[cause], f"{where}：{side}（成因：{cause}）", action, elem)
    if left["口径"] and right["口径"] and left["口径"] != right["口径"]:
        return _entry(unit, dim, ename, slot, left, right,
                      CAUSE_VERDICT[CAUSE_INCOMPARABLE], CAUSE_GAP[CAUSE_INCOMPARABLE],
                      f"{where} 两侧都有值却不是同一口径（条款 {left['口径']}／履行 {right['口径']}），"
                      f"这一对没做成（成因：{CAUSE_INCOMPARABLE}）——不折算成一个数再比",
                      CAUSE_ACTION[CAUSE_INCOMPARABLE], elem)
    ok, note = _same(slot, left, right)
    if ok:
        return _entry(unit, dim, ename, slot, left, right, CHECK_AGREED, "", "", "", elem)
    return _entry(unit, dim, ename, slot, left, right, CHECK_CONFLICT,
                  tax_inspect.GAP_CONFLICT,
                  f"{where} 两侧都有值而不等：{note}",
                  tax_inspect.GAP_ACTION[tax_inspect.GAP_CONFLICT], elem)


def reconcile(doc: dict, confirmed=(), dims=(), reg: dict = None) -> dict:
    """按判断单元逐条比条款与履行，出差异条目、无法确认条目与落成证据缺口的那一份。"""
    reg = reg or load()
    idx = index(reg)
    contracts = _contracts(doc, reg)
    if not contracts:
        raise NeedsContracts(NO_CONTRACT_NOTE)
    units = _units(doc, contracts, idx)
    if not units:
        raise NeedsContracts(NO_CONTRACT_NOTE)
    na = _absent(doc, idx)
    chain = build_chain(doc, contracts, confirmed)
    dropped = superseded_cells(chain, contracts)
    want = list(dims) or list(idx)
    for d in want:
        if d not in idx:
            raise ValueError(f"维度代号 {d} 不在模板里：取 {list(idx)}，见 --list")

    counts = {v: 0 for v in VERDICTS}
    diff, unjudgeable, agreed, asks, gaps = [], [], [], [], []
    for unit in units:
        for k in unit["缺格"]:
            probe = (tax_analyze.CONTEXT_AXES[AXIS_OF_UNIT_KEY[k]]["probe"]
                     if k in AXIS_OF_UNIT_KEY else UNIT_ASK[k])
            asks.append({"单元": unit["代号"], "缺": k, "追问": probe})
        for dim in want:
            if dim in na:
                continue
            for ename in idx[dim]["要素"]:
                clause = []
                for c in unit["合同"]:
                    rec = contracts[c]["条款"].get(dim, {}).get(ename)
                    if rec and not rec["空"] and (c, dim, ename) not in dropped:
                        clause.append((c, rec))
                for r in _compare_element(unit, dim, ename, clause, idx):
                    counts[r["核对结论"]] += 1
                    if r["核对结论"] == CHECK_CONFLICT:
                        diff.append(r)
                    elif r["核对结论"] == CHECK_AGREED:
                        agreed.append(r)
                    else:
                        unjudgeable.append(r)
                    if r["核对结论"] != CHECK_AGREED:
                        gaps.append({"类": r["缺口类别"],
                                     "对象": f"单元 {unit['代号']}／{dim} {idx[dim]['维度']}／"
                                             f"{r['要素']}／{r['槽'] or '整条'}"})
        unit["被替代而未采信的条款"] = [
            f"{c}／{dim} {idx[dim]['维度']}／{ename}"
            f"（由 {dropped[(c, dim, ename)]['由']} 依边 {dropped[(c, dim, ename)]['边']} 替代）"
            for c in unit["合同"] for dim in idx for ename in idx[dim]["要素"]
            if (c, dim, ename) in dropped]

    superseded_rows = sorted({x for u in units for x in u["被替代而未采信的条款"]})
    return {
        "判断单元数": len(units),
        "单元": [{"代号": u["代号"], "主体": u["四要素"].get("主体", ""),
                  "具体应税交易": u["四要素"].get("具体应税交易", ""),
                  "业务所属期": u["四要素"].get("业务所属期", ""),
                  "资格条件": u["四要素"].get("资格条件", ""),
                  "适用政策": u["适用政策"], "合同": u["合同"],
                  "被替代而未采信的条款": u["被替代而未采信的条款"]} for u in units],
        "②要补问": asks,
        "合同链": chain,
        "历史称谓提醒": historical_hits(doc, reg),
        "核对": {"总成对数": sum(counts.values()), **{v: counts[v] for v in VERDICTS},
                 "按维度": {d: sum(1 for r in diff + unjudgeable + agreed if r["维度代号"] == d)
                          for d in want}},
        "差异条目": diff,
        "无法确认条目": unjudgeable,
        "一致明细": agreed,
        "证据缺口": gaps,
        "证据缺口按类": {g: sum(1 for x in gaps if x["类"] == g) for g in tax_inspect.GAP_TYPES},
        "范围": {"比对维度": want, "筛选条件": "、".join(dims),
                 "不适用维度": na, "被替代而未采信的条款": superseded_rows},
        "触发时点清单": time_points(contracts, dropped),
        "边界": ("本层判的是合同写的那一格与做过的那一格对不对得上，不判这笔业务该缴多少、"
                 "也不预测检查结果；差异条目交给 references/tax_risk_framework.md 与 "
                 "scripts/tax_cases.py，无法确认落成 scripts/tax_inspect.py 的证据缺口那一类"
                 "（可直接喂 `--answers`）。替代按要素落位：一条已确认的替代边只作废它在后一份"
                 "合同里也有记录的那几条要素，其余条款仍从原合同采信，没有『整份失效』这种判定。"
                 "等级不在本层：风险等级落在「风险自检专用输出」那一式里判。"
                 + NO_RECORD_NOTE),
    }


def time_points(contracts, dropped=None) -> list:
    """把『触发时点』收成一张清单交给 ②：本层只收齐与带出处，不判这个时点上政策是什么。

    `dropped` 是 `superseded_cells` 那张 (合同, 维度, 要素) 表：被确认替代掉的那一条要素不再
    进清单，它写的付款节点已不是要问 ② 的那一个。整份合同不会被作废——没被替代的那几条照常收。
    """
    dropped = dropped or {}
    out = []
    for code, contract in contracts.items():
        for dim, row in contract["条款"].items():
            for ename, rec in row.items():
                if (code, dim, ename) in dropped:
                    continue
                value = rec["值"].get("触发时点")
                if _given(value):
                    cite = rec["出处"] or {}
                    out.append({"合同": code, "要素": f"{dim}／{ename}",
                                "触发时点": str(value).strip(), "来源": rec["来源"],
                                "出处": cite.get("位置", ""),
                                "出处可用": rec["出处可用"]})
    return out


def historical_hits(doc, reg=None) -> list:
    """老合同里写的历史税种称谓：只报『这个名不是现行税种』与给 ③ 的线索，不报现在怎么缴。"""
    reg = reg or load()
    table = historical(reg)
    found, out = set(), []
    for path, text in _texts(doc):
        for name, row in table.items():
            if name in text and name not in found:
                found.add(name)
                out.append({"称谓": name, "出现在": path, "现行": row["现行"],
                            "检索线索": row["检索线索"], "动作": row["动作"]})
    return out


def _texts(node, path=""):
    """摊平出 (位置, 文本) 对，供历史称谓定位。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _texts(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _texts(v, f"{path}[{i + 1}]")
    elif isinstance(node, str) and node.strip():
        yield path, node


# ── 7 排版 ───────────────────────────────────────────────────

def _tp_line(t) -> str:
    tag = SOURCE_TAG if t["来源"] == FILL else ""
    if not t["出处可用"]:
        tag += "［出处待核］"
    return (f"  · {t['合同']}／{t['要素']}：{t['触发时点']}"
            + (f"（{t['出处']}）" if t["出处"] else "") + tag)


def render_intake(out: dict) -> str:
    L = [f"合同受理：{out['合同数']} 份合同，九维度覆盖 {out['覆盖']['状态']}",
         f"（已满足 {out['已满足维度数']}｜待核 {out['待核维度数']}｜缺 {out['缺维度数']}"
         f"｜不适用 {out['不适用维度数']}）", f"覆盖判据：{out['覆盖']['判据']}"]
    for r in out["覆盖"]["按维度"]:
        L.append(f"  {r['维度代号']} {r['维度']}：{r['状态']}"
                 + (f"（理由：{r['不适用理由']}）" if r["不适用理由"] else ""))
        if r["待核要素"]:
            L.append(f"      待核（值有了但出处定不到位）：{'、'.join(r['待核要素'])}")
    if out["待核清单"]:
        L.append(f"\n出处缺格的抽取 {len(out['待核清单'])} 条——补齐文件／位置／摘录三格才算抽到：")
        for x in out["待核清单"]:
            L.append(f"  · {x['合同']}／{x['要素']}")
    for x in out["线索命中却没交"]:
        L.append(f"  ! {x['要素']} 没交，但别处的摘录里出现过它的线索：{'；'.join(x['命中'])}")
    if out["未填的要素"]:
        L.append(f"\n没交的要素 {len(out['未填的要素'])} 条：没交不等于不涉及——"
                 f"要么向企业要那一格，要么写明不适用理由")
    if out["触发时点清单"]:
        L.append("\n触发时点清单（交 ② 的时点轴，本层不判这个时点上政策是什么）：")
        for t in out["触发时点清单"]:
            L.append(_tp_line(t))
    for h in out["历史称谓提醒"]:
        L.append(f"\n[历史税种称谓] {h['称谓']}（出现在 {h['出现在']}）")
        L.append(f"  现行：{h['现行']}")
        L.append(f"  检索线索：{'、'.join(h['检索线索'])}")
        L.append(f"  动作：{h['动作']}")
    L.append(f"\n{out['边界']}")
    return "\n".join(L)


def render(out: dict) -> str:
    c = out["核对"]
    L = [f"多合同受理与比对：{out['判断单元数']} 个判断单元，比对做成 {c[CHECK_AGREED]} 对，"
         f"差异 {len(out['差异条目'])} 条，无法确认 {len(out['无法确认条目'])} 条",
         f"核对结论分布：{'｜'.join(f'{v} {c[v]}' for v in VERDICTS)}"]
    if out["②要补问"]:
        L.append("判断单元还缺格，回 ② 问——缺格不是不比对的理由，也不是通过：")
        for a in out["②要补问"]:
            L.append(f"  · 单元 {a['单元']} 缺『{a['缺']}』：{a['追问']}")
    ch = out["合同链"]
    L.append(f"合同链：边 {len(ch['边'])} 条（已确认替代 {len(ch['已确认替代'])}、"
             f"待用户确认 {len(ch['待用户确认'])}）")
    for e in ch["边"]:
        L.append(f"  {e['代号']} {e['在先']} —{e['类型']}→ {e['在后']}［{e['状态']}］")
        if e["状态"] == PENDING:
            L.append(f"      {e['动作']}")
    for u in out["单元"]:
        L.append(f"\n【单元 {u['代号']}】{u['主体'] or '（主体未给）'}／"
                 f"{u['具体应税交易'] or '（交易未给）'}／{u['业务所属期'] or '（所属期未给）'}／"
                 f"{u['资格条件'] or '（资格未给）'}")
        L.append(f"  适用政策：{u['适用政策'] or '（未回 ⑧ 取）'}｜条款来自："
                 f"{'、'.join(u['合同'])}")
        for x in u["被替代而未采信的条款"]:
            L.append(f"    被替代而未采信：{x}")
    for title, rows in (("差异条目（条款与履行说的不是一件事）", out["差异条目"]),
                        ("无法确认条目（缺一边、口径不可比或出处待核）", out["无法确认条目"])):
        if not rows:
            continue
        L.append(f"\n{title}：")
        for r in rows:
            L.append(f"  [{r['核对结论']}] 单元 {r['判断单元']}／{r['维度代号']}／"
                     f"{r['要素']}／{r['槽'] or '整条'}")
            L.append(f"      条款侧 {r['条款侧']}｜履行侧 {r['履行侧']}")
            L.append(f"      {r['差异说明']}")
            L.append(f"      动作：{r['动作']}")
            if r["依据链"]:
                L.append(f"      依据链：{'；'.join(r['依据链'])}")
    if out["证据缺口"]:
        L.append("\n落成稽查层那一类的证据缺口（可直接喂 `scripts/tax_inspect.py --answers`）：")
        for g in out["证据缺口"]:
            L.append(f"  [{g['类']}] {g['对象']}")
        L.append("  按类：" + "｜".join(f"{k} {v}" for k, v in out["证据缺口按类"].items()))
    if out["触发时点清单"]:
        L.append("\n触发时点清单（交 ② 的时点轴，本层不判这个时点上政策是什么）：")
        for t in out["触发时点清单"]:
            L.append(_tp_line(t))
    if out["范围"]["筛选条件"]:
        L.append(f"\n本次只比了这些维度：{out['范围']['筛选条件']}，"
                 f"不适用维度 {len(out['范围']['不适用维度'])} 个"
                 f"——范围外的维度没比，没比不等于没问题")
    L.append(f"\n{out['边界']}")
    return "\n".join(L)


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多合同受理层：条款收进模板、连候选合同链、按判断单元比履行（离线）")
    ap.add_argument("--list", action="store_true",
                    help="列九维度、要素、五件套、抽取线索、落点与历史税种称谓")
    ap.add_argument("--blank", action="store_true", help="出一份空白填表（JSON）")
    ap.add_argument("--intake", default="", metavar="JSON", help="只看受理与九维度覆盖")
    ap.add_argument("--reconcile", default="", metavar="JSON",
                    help="受理之外再按判断单元比条款与履行")
    ap.add_argument("--confirm", action="append", default=[], metavar="E1",
                    help="按边代号确认合同链上的这一条关系，可重复；不确认一律记待用户确认")
    ap.add_argument("--dimension", action="append", default=[], metavar="C4",
                    help="只比这些维度，可重复")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    reg = load()
    if args.list:
        print(f"九维度：{'、'.join(d['维度'] for d in reg['维度'])}")
        print(f"要素五件套：{'、'.join(ELEMENT_SLOTS)}（取值槽 {'、'.join(NUMERIC_SLOTS)}"
              f"＋期限＋触发时点）")
        print(f"来源三态：{'、'.join(SOURCES)}｜出处三格：{'、'.join(SOURCE_CELLS)}")
        print(f"一条记录认的键：{'、'.join(RECORD_KEYS)}")
        print(f"一条边认的键：{'、'.join(EDGE_KEYS)}｜边类型：{'、'.join(EDGE_KINDS)}"
              f"｜确认状态：{'、'.join(CHAIN_STATES)}")
        print(f"一个判断单元认的键：{'、'.join(UNIT_ROW_KEYS)}")
        print(f"判断单元四要素：{'、'.join(UNIT_KEYS)}｜比对结论：{'、'.join(VERDICTS)}")
        print(f"成因：{'、'.join(CAUSES)}")
        for d in reg["维度"]:
            print(f"\n{d['代号']} {d['维度']}（风险指标落点：{d['风险指标落点']}）")
            print(f"    税务含义：{d['税务含义']}")
            for e in d["要素"]:
                print(f"  · {e['要素']}｜落点 {e['落点']}")
                print(f"      为什么：{e['为什么']}")
                print(f"      抽取线索：{'、'.join(e['抽取线索'])}")
        print("\n历史税种称谓（只作沿革线索，不是现行税种名）：")
        for r in reg["历史税种称谓对照"]:
            print(f"  {r['称谓']} → {r['现行']}")
        return 0
    if args.blank:
        print(json.dumps(blank(reg), ensure_ascii=False, indent=1))
        return 0
    path = args.intake or args.reconcile
    if not path:
        print(NO_CONTRACT_NOTE)
        return 1
    try:
        doc = _read_json(path)
        out = (intake(doc, reg) if args.intake else
               reconcile(doc, args.confirm, args.dimension, reg))
        # 排版也放在同一道闸门里：渲染抛出的键缺失一样要带类型名，而不是把 Traceback
        # 交给不懂代码的读者——那份回显看着像政策结论，实际是这一层自己坏了。
        text = (json.dumps(out, ensure_ascii=False, indent=1) if args.json else
                (render_intake(out) if args.intake else render(out)))
    except (ValueError, KeyError, json.JSONDecodeError, FileNotFoundError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False, indent=1))
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
