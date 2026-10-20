#!/usr/bin/env python3
"""类案检索：把税务总局站内的查处通报与曝光典型收成一张候选台账。

这层不产依据，只产"这么理解被官方怎么对待过"。它与依据效力位阶
（`tax_evidence.LEGAL_RANK`）是两根正交的轴：位阶回答"谁能定"，本层回答
"同样的一件事，检查机关查过没有、按什么口径处理过"。所以台账里的
「来源等级 / 材料层级」都不填进 L1–L4，也不参与 `pick_primary` 的排队。

只走这一个通道是有意的边界：本脚本覆盖的是官方查处通报、违法案件公布与
曝光典型，**不含法院判决与复议决定**（裁判文书网与人民法院案例库要登录加
验证码，未实现，见 `references/source_defects.md`「仍然是边界的几件事」）。
强度上限因此是"官方处理口径"，不是"司法裁判口径"——这句话会随台账一起输出。

判据来自 2026-10-04 的现场实测、2026-10-20 复跑过一轮（台账同名那一节，复跑
`python tests/probe_case_channel.py`）：
  · 检索式必须发空 label——探针第 9–12 发的四枚标签名（`新闻`／`稽查`／`曝光台`
    与真实名 `新闻发布`）配「骗取出口退税」全回 total=0；真实名能收窄得出内容要靠
    主题在枚标签下有条目，所以这里不能拿"回 0"当成标签名不存在的证据；
  · `column=5741`（新闻发布）收得窄：「骗取出口退税」189→119；
  · 判"是文件还是案例"看主机名不看路径：案例条目也在 `www…/zcfgk/c102439/`
    下，`/zcfgk/` 这条路径判不出东西；
  · 同一正文 id 会在 `fgk` 与 `www` 各回一行，不去重同一起案件占两个位子；
  · 检索词里加"案例""公布"这类字样反而把案例筛掉（1 条 / 4 条），
    要的是处理结果词（依法查处、曝光、追缴、罚款）；
  · 发布方只在 `publisher` 栏与标题冒号前那一段可查，且这一栏 50 行里有 25 行是空的
    ——来源等级读不出机关就落 C，不默认成"各地税务机关通报"。

用法：
    python scripts/tax_cases.py "研发费用加计扣除" --element 混岗工时 --element 辅助账
    python scripts/tax_cases.py "骗取出口退税" --mode recent --limit 8 --json
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tax_web_search as tws  # noqa: E402
from tax_http import short_reason  # noqa: E402

#: 案例集中出现的栏目号（新闻发布）。台账第一条收窄判据用它。
CASE_COLUMN = "5741"

#: URL 路径里的 6 位栏目代号 → 子类型。三行都有实测行数（46 / 11 / 2），
#: 记在台账里。c102435 那两条是逐月汇总公布页，正文只有一句"链接：重大税
#: 法违法案件信息公布栏"，不是逐案清单，所以它召回得少是常态不是故障。
SUBTYPES = {
    "c102025": ("查处通报", 2),
    "c102439": ("曝光典型", 3),
    "c102435": ("违法公布", 3),
}
#: 有官方子栏目代号的三档名。`source_grade` 在两个发布方字段都读不出东西时用它：
#: 条目在总局自己的案例栏目里，公布方就是这个栏目所属的总局；落在「其他新闻」
#: 又没有署名，则不给它编一个机关背书。
CASE_SUBTYPE_NAMES = {name for name, _w in SUBTYPES.values()}
#: 认不出子栏目时的落点：留在台账里但排到最后，不删——删掉就等于把
#: "我没认出来"写成"官方没公布过"。
OTHER_SUBTYPE = ("其他新闻", 1)

#: 处理结果词。第三轮全部由它驱动；台账"检索词形态"那条说的就是这一组。
ADVERSE_WORDS = ("不予税前扣除", "追缴", "取消资格", "行政处罚", "曝光", "依法查处")

#: 标题里出现这些词样，说明这条是文件、解读或答复，不是案例。
FILE_TITLE_RE = re.compile(r"通知|办法|公告|指引|解读|答复|新闻发布会")

#: 文号形态（"国税发〔1998〕66号"、"国家税务总局公告2016年第24号"）。
DOC_NUM_RE = re.compile(r"[〔[]\s*\d{4}\s*[〕]].{0,8}?号|第\s*\d+\s*号")

#: 媒体转载的字形：标题以「[中国新闻社]」这类来源前缀开头。
REPRINT_RE = re.compile(r"^\[[^\]]{1,12}\]")

#: 正文 id：路径末段那 7 位（c5172440）。同一起案件在两台主机各回一遍时，
#: 这一位是相同的，所以拿它当去重键。
CONTENT_ID_RE = re.compile(r"(?<=/)c\d{7}(?=/content\.html)")

#: 目标时点的形状。`strptime("%Y-%m-%d")` 会放过 2024-1-1 这种非补零写法，而这一栏
#: 是拿来跟条目日期（接口给的永远是补零串）做字符串比较的：非补零的 "2024-1-1"
#: 比 "2024-01-05" 大，越界的案例反而全留下了——过滤静默不起，比报错坏。
AS_OF_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

#: 材料层级：与来源等级两根轴各管一件事——来源等级管"谁发的"，
#: 材料层级管"这条是个什么东西"。C1（法院判决/裁定）本通道不产。
LEVEL_BY_SUBTYPE = {
    "曝光典型": "C2 官方公布的典型案例",
    "违法公布": "C2 官方公布的典型案例",
    "查处通报": "C2 官方公布的典型案例",
    "其他新闻": "C4 媒体报道或转载",
}

#: 排序键要看得见它乘的是哪一档，所以权重从 SUBTYPES 摊出来单列一张表。
SUBTYPE_WEIGHT = {name: w for (name, w) in SUBTYPES.values()}
SUBTYPE_WEIGHT[OTHER_SUBTYPE[0]] = OTHER_SUBTYPE[1]
#: 税屋那一路是实务站转载，不在总局站内的栏目体系里，所以它没有子栏目代号，
#: 权重跟"其他新闻"同档（排在官方栏目件后面），但子类型单列一个名字，
#: 免得把它混进官方通报里读。
SHUI5_SUBTYPE = "税屋转载"
SUBTYPE_WEIGHT[SHUI5_SUBTYPE] = OTHER_SUBTYPE[1]

#: 机关字形的下限：署名里得出现"国家税务总局／总局／税务局／税务部"这一类。
#: 判据取正向而不是"带'税务'两字就算系统内"，因为「中国税务报」也带税务两字，
#: 它是报纸；带"税务"就认机关，等于让报纸的署名去给 A1/A2 担保。
ORG_NAME_RE = re.compile(r"国家税务总[局部]|税务总局|税务局|税务部")

#: 发布方是媒体而不是机关的字形。**只拿去比 publisher 栏**：标题是案情描述，
#: 以"通报""公告"结尾的标题一过"报"字就被误判成媒体。
MEDIA_PUBLISHER_RE = re.compile(
    r"日报|晚报|晨报|时报|商报|画报|报社|新闻社|通讯社|电视台|广播电台|杂志社|报$|社$|网$")

#: 地方税务机关的字形：机关名里带行政区划。带这一形的不是总局本级。
ADMIN_MARK_RE = re.compile(r"省|市|县|区|州|盟")

#: 标题里出现这些字样，说明转的是裁判文书或处理/处罚决定书原文。只按标题判
#: （这一层不取正文），判得出来才给 C1/C3，判不出来写"待核"。
DOC_FORM_TITLE_RE = re.compile(r"判决书?|裁定书?|复议决定|处罚决定|处理决定")

#: 旧案结论要按目标年度重验，这一句跟着每条候选走，不靠人记。
REMAP_NOTE = ("案例的处理结论按其发生时政策作出，用于本题前要先按目标年度的"
              "政策重验；能类比的是事实认定与证据评价，不是法律结论")

LIMITATIONS = (
    "覆盖范围：税务总局站内的查处通报（c102025）、典型案件曝光（c102439）、"
    "重大税收违法案件信息公布（c102435）。",
    "未覆盖：法院判决与复议决定（裁判文书网、人民法院案例库需登录与验证码，"
    "本仓库未实现）——所以本层的强度上限是「官方处理口径」，不是「司法裁判口径」。",
    "未覆盖：地方税务机关自己发布、未同步到总局站内的案件。",
    REMAP_NOTE,
)

#: 税屋那一路默认不发时的说明。它不是可选的质量增强，而是覆盖面的另一半：
#: 判决与复议文书的全文在这一层有转载，而这一层的入口是 360 的 site: 检索，
#: 360 被本机 IP 限的时候整路是空的（成因与表现见本文件所属台账的
#: `references/source_defects.md`「由代码保证的源缺陷」360 那一行）。
SHUI5_OFF_NOTE = ("本轮没发税屋那一路（默认不发；要判决／复议文书的转载全文时加 "
                  "--shui5）。它是 360 的 site:shui5.cn 检索，"
                  "360 被限时这一路直接空，不等于这一层没有材料。")


# ── 检索式构造（纯函数，不联网）────────────────────────────────

def check_as_of(value: str) -> str:
    """目标时点必须先是补零的 YYYY-MM-DD，再是个真实日期；空串表示不设时点。

    放在函数里而不是靠 `strptime`：后者把 2024-1-1 收下来，而这一栏接下来是跟
    条目日期做字符串比较的，非补零写法会让过滤静默失效（见 `AS_OF_RE` 那段）。
    """
    d = (value or "").strip()
    if not d:
        return ""
    if not AS_OF_RE.match(d):
        raise ValueError(f"目标时点要补零的 YYYY-MM-DD，收到 {value!r}")
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"目标时点不是真实日期，收到 {value!r}"
                         f"（要补零的 YYYY-MM-DD，如 2024-02-29）")
    return d


def build_queries(topic: str, elements=(), mode: str = "exhaustive") -> list:
    """三轮检索式：全要素 → 要素拆分 → 处理结果词。返回 [{round, word, why}]。

    三轮的取词规则只有一条来源：台账"检索词形态"那一行——案情词配处理结果词
    给 508 条且首屏全是案例，案情词配"案例"两字给 1 条。所以这一函数里
    不出现"案例""公布"这类字样，出现的是 `ADVERSE_WORDS`。

    mode 只改变发几条：`recent` 是要"最近有没有查过"，全要素一条加两个结果词
    就够了，逐要素拆分那一轮是给 exhaustive 用的；`exhaustive` 三轮全发。
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("检索主题不能为空——没有主题就拼不出任何一条检索式")
    if mode not in ("exhaustive", "recent"):
        raise ValueError(f"mode 只认 exhaustive / recent，收到 {mode!r}")
    els = [e.strip() for e in (elements or []) if e and e.strip()]

    out = []
    if els:
        out.append({"round": 1, "word": " ".join([topic] + els[:2]),
                    "why": "全要素：主题 + 前两个争议要素"})
    else:
        out.append({"round": 1, "word": topic, "why": "全要素：题面没给争议要素，只用主题"})
    if mode == "exhaustive":
        for e in els:
            out.append({"round": 2, "word": f"{topic} {e}",
                        "why": f"要素拆分：单要素「{e}」"})
    adverse = ADVERSE_WORDS[:2] if mode == "recent" else ADVERSE_WORDS
    for w in adverse:
        out.append({"round": 3, "word": f"{topic} {w}",
                    "why": f"处理结果词：{w}（案情词配结果词，不配「案例」字样）"})
    return out


# ── 逐条判型与过滤（纯函数）────────────────────────────────────

def content_id(url: str) -> str:
    m = CONTENT_ID_RE.search(url or "")
    return m.group(0) if m else (url or "")


def column_codes(url: str) -> list:
    """URL 路径里的 6 位栏目代号。两头都用零宽断言：吃掉分隔斜杠的写法
    会把相邻段隔一个漏一个（/n810219/c102025/ 只匹到前一段）。"""
    return re.findall(r"(?<=/)c\d{6}(?=/)", url or "")


def classify_row(row: dict) -> dict:
    """给一条检索结果打子类型与去留，返回 {subtype, level, keep, reason, weight}。

    排除只有三条确定性理由，每条都写进台账的「排除理由」。留与不留分得开，
    是因为"我没认出这是哪个栏目"与"这明确不是案例"是两件事：认不出子栏目的
    落在「其他新闻」继续参与排序（权重 1，排在有代号的后面），只有明确是
    文件、解读、发布实录的才出局。
    """
    url = row.get("url") or ""
    title = row.get("title") or ""
    codes = column_codes(url)
    hit = next((SUBTYPES[c] for c in codes if c in SUBTYPES), None)
    # 文件与案例的判别只看两处：主机名与文号栏。路径里的 /zcfgk/ 判不出
    # 东西——案例条目也在 www 域的 /zcfgk/c102439/ 下（台账"案例与文件的
    # 判别只能用主机名"那一行）。
    if row.get("document_number") or DOC_NUM_RE.search(title):
        return {"subtype": "", "level": "", "keep": False, "weight": 0,
                "reason": "带文号，是文件不是案例"}
    if not hit and tws.FGK_MARKER in url:
        return {"subtype": "", "level": "", "keep": False, "weight": 0,
                "reason": "法规库条目（主机 fgk.chinatax.gov.cn）且不在案例子栏目"}
    if not hit and FILE_TITLE_RE.search(title):
        return {"subtype": "", "level": "", "keep": False, "weight": 0,
                "reason": "标题是通知／办法／公告／解读／发布实录一类，属文件与解读"}
    name, weight = hit or OTHER_SUBTYPE
    return {"subtype": name, "level": LEVEL_BY_SUBTYPE[name],
            "keep": True, "reason": "", "weight": weight}


def element_hits(row: dict, elements) -> list:
    """要素命中：标题或摘要里出现该要素的字面。可复核，不让模型打分。"""
    hay = f"{row.get('title') or ''} {row.get('snippet') or ''}"
    return [e for e in (elements or []) if e and e in hay]


def issuer_of(row: dict) -> str:
    """这一条的发布方字样：接口只在两处写它——`publisher` 栏，以及标题冒号前那一段。

    两处都没有就返回空串。空串是有含义的判据而不是缺省值：`source_grade` 靠它
    把"读出来不是机关"和"根本读不出来"分开填，后者只在官方案例栏目里才给 A1。
    """
    pub = (row.get("publisher") or "").strip()
    if pub:
        return pub
    return re.split(r"[：:]", row.get("title") or "")[0].strip()


def is_media(row: dict) -> bool:
    """媒体字形：标题带「[央视新闻]」这类站内转载前缀，或发布方是报社／通讯社／台／刊。

    判断只用 `publisher` 栏和标题前缀两处。拿 `MEDIA_PUBLISHER_RE` 去比标题会误伤
    以"通报""公告"结尾的案情标题——那一串里有"报"字。
    """
    return bool(REPRINT_RE.match(row.get("title") or "")
                or MEDIA_PUBLISHER_RE.search((row.get("publisher") or "").strip()))


def media_name(row: dict) -> str:
    """这一条转自谁：优先取标题的「[中国政府网]」前缀，前缀没有才退到 publisher 栏。

    两处顺序不能反：带前缀的行 publisher 栏往往是站内转载它的账号
    （实测里有一条「税务总局新媒体」转「[中国政府网]」），拿它当原发源就把
    转载方写成了原作者。
    """
    m = REPRINT_RE.match(row.get("title") or "")
    if m:
        return m.group(0)[1:-1]
    return (row.get("publisher") or "").strip()


def source_grade(row: dict, subtype: str = "") -> str:
    """来源等级：谁以什么身份发布这一条。A1 税务总局本级 / A2 各地税务机关 / C 不是税务机关。

    这一根轴量"谁发布"，与 `tax_evidence.LEGAL_RANK`（谁能定）和材料层级
    （它是什么：判决／典型案例／通报／报道）都无关，三根轴各自独立填。

    读不出税务机关就落 C，不默认成 A2：2026-10-04 实跑「研发费用加计扣除 混岗工时」时
    「人民日报：研发费用加计扣除申报方式优化」拿到的是 A2，而 A2 的话是"各地税务
    机关通报"——这一栏说的是谁发的，猜不出来就不替它担保有机关背书。
    """
    if is_media(row):
        return "C"
    who = issuer_of(row)
    if ORG_NAME_RE.search(who):
        return "A2" if ADMIN_MARK_RE.search(who) else "A1"
    # 署名读不出：官方案例子栏目里的条目按栏目名义公布，公布方就是总局；
    # 认不出子栏目又没有署名的只能落 C，这一栏不给猜测留位置。
    return "A1" if subtype in CASE_SUBTYPE_NAMES else "C"


# ── 取数与台账组装 ────────────────────────────────────────────

def make_entry(row: dict, q: dict, elements, subtype_info: dict,
               grade: str, verify: str) -> dict:
    """把一条检索结果收成台账一行。字段名用中文，是因为这张表要直接进答案。

    「案号/文号」没有的一律写"无"而不是留空：留空读起来像"还没查这一栏"，
    写"无"才是"看过并且没有"。税务查处通报绝大多数不给案号，这一栏本来就是
    以"无"为主，所以它是个真信号而不是占位。
    """
    return {
        "标题": row.get("title", ""),
        "链接": row.get("url", ""),
        "日期": row.get("date", ""),
        "机关": row.get("publisher", "") or "未标明",
        "子类型": subtype_info["subtype"],
        "来源等级": grade,
        "材料层级": subtype_info["level"],
        "案号/文号": row.get("document_number") or "无",
        "事实摘要": (row.get("snippet") or "")[:120],
        "要素命中": element_hits(row, elements),
        "检索轮次": q["round"],
        "检索式": q["word"],
        "核验状态": verify,
    }


def verify_note(row: dict, subtype: str) -> str:
    """核验状态一句话：官方原文／媒体稿／读不出发布方／子栏目未识别，四种说法不同。

    四种说法不能合并成一句"待核"：读者要的是下一步动作。媒体稿要回到作出处理的
    机关，未署名要按报道读，栏目未识别的别当官方通报引。
    """
    grade = source_grade(row, subtype)
    if grade == "C":
        if is_media(row):
            return (f"发布方是媒体（{media_name(row)}），不是税务机关；"
                    "要引处理口径得回到作出处理的税务机关")
        return "待核：发布方两处字段都读不出机关，按报道读，别当官方通报引"
    if subtype == OTHER_SUBTYPE[0]:
        return "待核：子栏目未识别，按报道读，别当官方通报引"
    return f"官方站内原文（{subtype}栏）"


def collect(topic: str, elements=(), mode: str = "exhaustive",
            pages: int = 1, order: str = "relevance", limit: int = 10,
            as_of: str = "", fetch=None, shui5_fetch=None) -> dict:
    """发检索式、过滤、去重、排序，回一张候选台账。

    pages 只作用在最后一轮（处理结果词那一轮命中最多，翻两页取到的是同一批
    案子往前排）；order 传 date_desc 时按成文日期倒序取，这是"最近有没有查过"
    那种问法要的形状。

    两个取数出口都可注入（默认走真接口）：`fetch` 是总局站内那一路，
    `shui5_fetch` 给值才加第二路（税屋）。离线用例靠这两个出口把快照喂进
    整条台账链路——过滤、去重、排序、零结果这几段只要不被执行，写在源码里
    就等于没写。
    """
    if order not in tws.ORDER_VALUES:
        raise ValueError(f"order 只认 {tuple(tws.ORDER_VALUES)}，收到 {order!r}")
    as_of = check_as_of(as_of)
    fetch = fetch or tws.search_chinatax
    queries = build_queries(topic, elements, mode)
    wanted = {3: max(1, pages)} if pages > 1 else {}
    rounds, dropped, failures = [], [], []
    seen = {}

    def absorb(row, q, subtype_info, grade, verify):
        key = content_id(row.get("url") or "")
        if key in seen:
            # 同一正文 id 在 fgk 与 www 各回一遍（台账"同一条正文会在两台主机
            # 各回一遍"那一行）：留先取到的那条，另一条只加重复计数，
            # 不让同一起案件占两个候选位。
            seen[key]["重复次数"] += 1
            return
        entry = make_entry(row, q, elements, subtype_info, grade, verify)
        entry["重复次数"] = 1
        seen[key] = entry

    for q in queries:
        page_n = 1
        while True:
            res = fetch(q["word"], page=page_n, file_only=False,
                        order=order, column=CASE_COLUMN)
            if res.get("_error"):
                failures.append({"word": q["word"], "page": page_n,
                                 "error": res["_error"]})
                break
            items = res.get("results") or []
            for row in items:
                call = classify_row(row)
                if not call["keep"]:
                    dropped.append({"标题": row.get("title", ""),
                                    "理由": call["reason"]})
                    continue
                entry_date = row.get("date", "")
                if as_of and entry_date and entry_date > as_of:
                    dropped.append({"标题": row.get("title", ""),
                                    "理由": f"晚于目标时点 {as_of}"})
                    continue
                absorb(row, q, call,
                       source_grade(row, call["subtype"]),
                       verify_note(row, call["subtype"]))
            rounds.append({"round": q["round"], "word": q["word"], "why": q["why"],
                           "page": page_n, "reported_total": res.get("total"),
                           "fetched": len(items), "source": "chinatax"})
            if page_n >= wanted.get(q["round"], 1) or not items:
                break
            page_n += 1

    if shui5_fetch:
        # 第二路：税屋。它是实务站，转载件居多，所以这一路的每一条都固定按
        # 「C 级转载」进台账，核验状态写明"原文机关待回溯"——这一路能给的是
        # 线索与判决书全文的所在，不能替代官方通报那一层。
        for q in queries:
            res = shui5_fetch(q["word"])
            if res.get("_error"):
                failures.append({"word": q["word"], "page": 1,
                                 "error": f"税屋：{res['_error']}"})
                continue
            items = res.get("results") or []
            for row in items:
                title = row.get("title", "")
                level = ("C1/C3 判决或决定文书（按标题判断，正文待核）"
                         if DOC_FORM_TITLE_RE.search(title) else "C4 实务站转载")
                info = {"subtype": SHUI5_SUBTYPE, "level": level}
                absorb(row, q, info, "C",
                       "二手转载（税屋），原文机关与文号待回溯后再引")
            rounds.append({"round": q["round"], "word": q["word"], "why": q["why"],
                           "page": 1, "reported_total": res.get("total"),
                           "fetched": len(items), "source": "shui5"})

    kept = list(seen.values())
    # 排序只有两根键，且同向降序：先 要素命中数 × 子类型权重，同分再按日期倒序
    # （日期是 YYYY-MM-DD，字符串降序就是新的在前）。没有模型打分，任何人拿
    # 同一份候选重算一遍都对得上——这是这一层敢用确定性排序的全部理由。
    kept.sort(key=lambda e: (len(e["要素命中"]) * SUBTYPE_WEIGHT[e["子类型"]],
                             e["日期"]), reverse=True)
    return {
        "topic": topic,
        "mode": mode,
        "order": order,
        "as_of": as_of,
        "columns": CASE_COLUMN,
        "round_queries": queries,
        "rounds": rounds,
        "candidates": kept[:limit],
        "candidate_count": len(kept),
        "excluded": dropped,
        "sources": (["chinatax.gov.cn search5（column=5741，label 空）"]
                    + (["shui5.cn 经 360 的 site: 检索取得"] if shui5_fetch else [])),
        "case_support": "有" if kept else "无",
        "limitations": (list(LIMITATIONS)
                        + ([] if shui5_fetch else [SHUI5_OFF_NOTE])),
        "fetch_failures": failures,
        "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def render(out: dict) -> str:
    """台账的人读形态。零结果也要有一段，不能只回"没查到"。"""
    L = [f"类案检索：{out['topic']}（模式 {out['mode']}，栏目 {out['columns']}）",
         f"检索时间：{out['searched_at']}    类案支持：{out['case_support']}"]
    L.append(f"检索式 {len(out['round_queries'])} 条：")
    for q in out["round_queries"]:
        L.append(f"  第 {q['round']} 轮  {q['word']}   —— {q['why']}")
    if out["candidates"]:
        L.append(f"\n候选 {out['candidate_count']} 条（列前 {len(out['candidates'])} 条）：")
        for i, e in enumerate(out["candidates"], 1):
            dup = f"（同一条目出现 {e['重复次数']} 次）" if e["重复次数"] > 1 else ""
            L.append(f"  {i}. [{e['来源等级']}/{e['子类型']}] {e['标题']}{dup}")
            L.append(f"     机关 {e['机关']}｜日期 {e['日期']}｜案号/文号 {e['案号/文号']}"
                     f"｜{e['材料层级']}｜核验 {e['核验状态']}")
            hits = "、".join(e["要素命中"]) or "无（按子类型权重排位）"
            L.append(f"     要素命中：{hits}")
            L.append(f"     {e['链接']}")
    else:
        L.append("\n本轮没有留下候选案例。")
    if out["excluded"]:
        L.append(f"\n排除 {len(out['excluded'])} 条，理由逐条列（前 10 条）：")
        for d in out["excluded"][:10]:
            L.append(f"  · {d['理由']} ｜ {d['标题'][:40]}")
    if out["fetch_failures"]:
        L.append(f"\n取数失败 {len(out['fetch_failures'])} 轮（不是"
                 f"「库里没有」，失败与判空两件事）：")
        for f in out["fetch_failures"]:
            L.append(f"  · 第 {f['word']} 第 {f['page']} 页：{f['error']}")
    L.append("\n局限：")
    for s in out["limitations"]:
        L.append(f"  · {s}")
    L.append("\n注：类案是支持层，不占 L1–L4 位次；法律依据仍以文件为准。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="类案检索（税务总局站内查处通报与曝光典型）")
    ap.add_argument("topic", nargs="?", help="主题词，如「研发费用加计扣除」")
    ap.add_argument("--element", action="append", default=[],
                    metavar="要素", help="争议要素，可重复；决定第一、二轮的拼法")
    ap.add_argument("--mode", choices=("exhaustive", "recent"), default="exhaustive",
                    help="exhaustive 三轮全发；recent 只发全要素 + 两个结果词")
    ap.add_argument("--pages", type=int, default=1, help="结果词那一轮翻几页（默认 1）")
    ap.add_argument("--order", default="relevance",
                    help=f"排序，取值限 {tuple(tws.ORDER_VALUES)}")
    ap.add_argument("--limit", type=int, default=10, help="台账列几条（默认 10）")
    ap.add_argument("--as-of", default="", metavar="YYYY-MM-DD",
                    help="只要这一日及之前公布的案例（要补零）；晚于该时点处理的案件不计")
    ap.add_argument("--shui5", action="store_true",
                    help="加发第二路（税屋 site: 检索，取判决／复议文书的转载全文）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 台账")
    args = ap.parse_args(argv)

    if not args.topic:
        ap.print_help()
        return 2
    if args.as_of:
        try:
            args.as_of = check_as_of(args.as_of)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    shui5_fetch = None
    if args.shui5:
        # 用到才导：税屋那一路要拉 requests 与 so360，默认不发时不必为它付导入成本
        from tax_shui5 import search_shui5
        shui5_fetch = lambda w: search_shui5(w, size=5)
    try:
        out = collect(args.topic, args.element, args.mode, args.pages,
                      args.order, args.limit, args.as_of, shui5_fetch=shui5_fetch)
    except Exception as e:  # noqa: BLE001 命令行出口要把原因说清不吐栈
        print(f"检索失败：{short_reason(e)}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else render(out))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
