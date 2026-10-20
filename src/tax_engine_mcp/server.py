"""tax-analysis-skill 的 MCP 服务端。

工具按任务组织，不按引擎脚本切：一次调用完成检索、读法规、展开算式或
生成检查件。引擎的静态词表与模板走资源，成稿模板走 prompt，避免把
大段只读文本塞进工具参数。

错误语义与 CLI/API 共用 tax_service.ServiceError：工具内转成 ToolError，
资源内转成 ResourceError，调用方按 code 判断下一步。
"""
from __future__ import annotations

import json
import os

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated

from .engine import tax_service

INSTRUCTIONS = """\
税收政策分析引擎（tax-analysis-skill）的 MCP 入口。

主线：① 判型 → ② 依据覆盖率 → ③④ 检索 → ⑧ 读法规与定级 → ⑥ 按模板成稿。
离线工具（classify / coverage / calc / ledger / intake / control）不联网；
search / read_law / interpretations / analyze / cases 会访问外部站点。

使用纪律：
- 先用 classify_tax_question 与 check_evidence_coverage 看清题型和缺口，
  再检索；不要凭标题猜依据。
- 检索结果的 meta.errors 是取数失败，不是「该源没有内容」；写答案时要把
  缺口说清楚，不得补写未检索到的规定。
- analyze_tax_question 返回分层底稿，不写最终答案；成稿用 prompts 里的模板。
- 本服务不调用付费模型，也不修改任何外部系统。
"""

mcp = FastMCP(
    name="tax-analysis",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_READ_OFFLINE = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_READ_WEB = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tax_service.ServiceError as exc:
        text = f"[{exc.code}] {exc.message}"
        if exc.details:
            text += "；details=" + json.dumps(exc.details, ensure_ascii=False)
        raise ToolError(text) from exc


def _resource(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tax_service.ServiceError as exc:
        text = f"[{exc.code}] {exc.message}"
        if exc.details:
            text += "；details=" + json.dumps(exc.details, ensure_ascii=False)
        raise ResourceError(text) from exc


# ── 工具：引擎与判型 ───────────────────────────────────────────────────────

@mcp.tool(tags={"offline", "engine"}, annotations=_READ_OFFLINE)
def engine_health() -> dict:
    """引擎健康检查（离线）：引擎根、税种/专题数、算式骨架数、输出模板数。"""
    return _call(tax_service.health)


@mcp.tool(tags={"offline", "analysis"}, annotations=_READ_OFFLINE)
def classify_tax_question(question: str, max_probes: int = 3) -> dict:
    """① 判型（离线）：题型、前提缺口、追问探针、应覆盖的来源与结论要件。

    question 传用户原话，不要先改写。max_probes 控制追问条数（1–10）。
    """
    return _call(tax_service.classify, question, max_probes=max_probes)


@mcp.tool(tags={"offline", "analysis"}, annotations=_READ_OFFLINE)
def check_evidence_coverage(
    qtype: str,
    question: str = "",
    evidence: list | None = None,
    skeleton: str = "",
    params: dict | None = None,
) -> dict:
    """② 依据覆盖率：evidence=null 是「还没检」，[] 是「检了没有」。

    qtype 取 classify_tax_question 返回的 type.type；未知题型会报
    unknown_question_type 并列出允许值。skeleton/params 只在计算题需要。
    """
    return _call(
        tax_service.coverage,
        qtype,
        question=question,
        evidence=evidence,
        skeleton=skeleton,
        params=params,
    )


@mcp.tool(tags={"offline", "analysis"}, annotations=_READ_OFFLINE)
def plan_tax_analysis(question: str) -> dict:
    """⑥ 之前的检索计划（离线）：题型、缺口、每轮目标/源/调用。"""
    return _call(tax_service.analyze_plan, question)


# ── 工具：检索与法规 ───────────────────────────────────────────────────────

@mcp.tool(tags={"network", "search"}, annotations=_READ_WEB)
def search_tax_policies(
    query: str,
    sources: Annotated[
        list[str] | None,
        Field(description="数据源；默认六源，可选 npc/chinatax/so360/shui5/wechat/ctax"),
    ] = None,
    size: Annotated[int, Field(description="每源条数 1–50")] = 10,
    page: Annotated[int, Field(description="页码；只有 npc 单源支持大于 1")] = 1,
    status: Annotated[int, Field(description="1/2/3/4；3=现行有效")] = 3,
    scope: Annotated[str, Field(description="title 或 fulltext")] = "title",
    exact: Annotated[bool, Field(description="精确检索；法名本体检索时开")] = False,
    sort: Annotated[str, Field(description="relevance 或 date")] = "relevance",
    date_from: Annotated[str, Field(description="YYYY-MM-DD")] = "",
    date_to: Annotated[str, Field(description="YYYY-MM-DD")] = "",
) -> dict:
    """③④ 多源检索法规与实务线索。

    返回 items 与 meta.source_summary/errors/gaps。meta.errors 里的源是
    取数失败，不等于该源没有内容；写答案时要写明这个缺口。
    多源聚合是每源各取 size 条后合并，没有页码维度；要翻页请把 sources
    限定为 ["npc"]，否则 page>1 会报 page_not_supported。
    """
    return _call(
        tax_service.search,
        query,
        sources=sources,
        size=size,
        page=page,
        status=status,
        scope=scope,
        exact=exact,
        sort=sort,
        date_from=date_from,
        date_to=date_to,
    )


@mcp.tool(tags={"network", "law"}, annotations=_READ_WEB)
def read_law(bbbs_id: str, include_text: bool = True) -> dict:
    """⑧ 读取一部法规：元数据，以及可选的正文段落。

    bbbs_id 取 search_tax_policies 结果里的 id（NPC bbbs 编号）。include_text
    为 true 时下载并解析正文；text.available=false 只表示这次下载/解析失败，
    不是「该法规没有正文」。法规不存在会报 not_found。
    """
    info = _call(tax_service.law_info, bbbs_id)
    out = {"law": info, "text": None}
    if include_text:
        out["text"] = _call(tax_service.law_text, bbbs_id)
    return out


@mcp.tool(tags={"network", "law"}, annotations=_READ_WEB)
def find_official_interpretations(
    title: str,
    keyword: str = "",
    province: str = "",
    sources: list[str] | None = None,
) -> dict:
    """按法规标题找 .gov.cn 官方解读。

    keyword 补充检索词；province 传省份子域名（如 shanghai）；sources 限定
    站点域名。只回官方来源，实务站转载不算官方解读。
    """
    return _call(
        tax_service.law_interpretations,
        title,
        keyword=keyword,
        province=province,
        sources=sources,
    )


@mcp.tool(tags={"network", "analysis"}, annotations=_READ_WEB)
def analyze_tax_question(
    question: str,
    at: str = "",
    read_body: bool = True,
    check_citations: bool = True,
) -> dict:
    """⑥ 一次跑完检索与分层，返回结构化底稿（不写最终答案）。

    联网且可能耗时。at 是观察时点（YYYY-MM-DD）；read_body 控制是否取税屋
    正文；check_citations 控制是否回官方库核对文号。结果含 primary/
    supporting/practice/to_verify 等分层与缺口；成稿请用 prompts 里的模板。
    本工具不调用付费模型。
    """
    return _call(
        tax_service.analyze_answer,
        question,
        at=at,
        read_body=read_body,
        check_citations=check_citations,
    )


# ── 工具：算式与类案 ───────────────────────────────────────────────────────

@mcp.tool(tags={"offline", "calc"}, annotations=_READ_OFFLINE)
def expand_calculation(skeleton: str, params: dict) -> dict:
    """展开一条算式骨架；骨架与所需参数见 tax://calc-skeletons 资源。

    只做算式展开，不含任何现行税率与扣除标准。缺参数会报 missing_params，
    多余参数会报 invalid_parameter——参数值应由调用方从检回的文件给出。
    """
    return _call(tax_service.calc_run, skeleton, params)


@mcp.tool(tags={"network", "cases"}, annotations=_READ_WEB)
def find_tax_cases(
    topic: str,
    elements: list[str] | None = None,
    mode: str = "exhaustive",
    pages: int = 1,
    order: str = "relevance",
    limit: int = 10,
    as_of: str = "",
    shui5: bool = False,
) -> dict:
    """类案台账：税务总局查处通报/曝光台的候选、过滤与去重结果。

    mode 只认 exhaustive/recent；order 只认 relevance/date_desc/category/
    date_asc；pages 只作用在最后一轮（1–20）；as_of 是 YYYY-MM-DD 截止时点。
    shui5=true 追加税屋转载线索，这一路固定按 C 级转载登记，不能替代官方通报。
    """
    return _call(
        tax_service.cases,
        topic,
        elements=elements,
        mode=mode,
        pages=pages,
        order=order,
        limit=limit,
        as_of=as_of,
        shui5=shui5,
    )


# ── 工具：检查类 ───────────────────────────────────────────────────────────

@mcp.tool(tags={"offline", "audit"}, annotations=_READ_OFFLINE)
def audit_ledger(
    ledger: dict,
    params: dict | None = None,
    codes: list[str] | None = None,
    tolerance: float = 0.0,
) -> dict:
    """账套巡检：产出八要素风险条目、判不动清单与两类缺口。

    ledger 是引擎账套 JSON；codes 只跑指定规则，params 补规则参数，
    tolerance 是相对容差。缺关键字段会报 invalid_request，不会猜数。
    """
    return _call(
        tax_service.ledger_check,
        ledger,
        params=params,
        codes=codes,
        tolerance=tolerance,
    )


@mcp.tool(tags={"offline", "audit"}, annotations=_READ_OFFLINE)
def simulate_inspection(
    answers: dict | None = None,
    domains: list[str] | None = None,
    codes: list[str] | None = None,
    scenario: str = "",
    signal: str = "",
    tolerance: float = 0.0,
) -> dict:
    """稽查模拟问询：逐问生成六字段问询单元，并判三类缺口。

    answers 缺省或某条缺项都判得动——没作答是一格状态，不是跳过这一问的
    理由。tolerance 是 0–1 的相对容差，超出会报 invalid_parameter。
    """
    return _call(
        tax_service.inspection,
        answers=answers,
        domains=domains,
        codes=codes,
        scenario=scenario,
        signal=signal,
        tolerance=tolerance,
    )


@mcp.tool(tags={"offline", "contract"}, annotations=_READ_OFFLINE)
def intake_contracts(document: dict) -> dict:
    """多合同受理：合同清单、九维度覆盖、待核抽取与没交的要素。

    document 至少要能构成合同清单，否则报 invalid_request。历史税种称谓
    会在结果里单独提醒，不静默改写成现行称谓。
    """
    return _call(tax_service.intake, document)


@mcp.tool(tags={"offline", "contract"}, annotations=_READ_OFFLINE)
def reconcile_contracts(
    document: dict,
    confirmed: list[str] | None = None,
    dims: list[str] | None = None,
) -> dict:
    """合同条款与履行记录对账：差异条目、无法确认条目与证据缺口。

    confirmed 是已确认的替代边，dims 限定只比对的维度；不传则全量比对。
    """
    return _call(
        tax_service.reconcile,
        document,
        confirmed=confirmed,
        dims=dims,
    )


@mcp.tool(tags={"offline", "control"}, annotations=_READ_OFFLINE)
def generate_controls(
    diagnosis: dict | None = None,
    assumptions: list[str] | None = None,
    links: list[str] | None = None,
    codes: list[str] | None = None,
) -> dict:
    """把已确认的稽查缺口转成九要素内控制度文本。

    diagnosis 为空时必须给 assumptions——没有诊断就按通用模板生成，并在
    输出里逐条列出假设，形态会标成通用模板而不是诊断处方。
    """
    return _call(
        tax_service.controls,
        diagnosis=diagnosis,
        assumptions=assumptions,
        links=links,
        codes=codes,
    )


# ── 资源：静态词表与模板 ───────────────────────────────────────────────────

@mcp.resource(
    "tax://tax-types",
    name="tax-types",
    description="税种与专题词表：名称、别名、上位法、权限与备注",
    mime_type="application/json",
    tags={"offline", "catalog"},
)
def tax_types_resource() -> dict:
    items = tax_service.tax_types()
    return {"count": len(items), "items": items}


@mcp.resource(
    "tax://calc-skeletons",
    name="calc-skeletons",
    description="算式骨架：公式、输入参数与结果字段",
    mime_type="application/json",
    tags={"offline", "catalog"},
)
def calc_skeletons_resource() -> dict:
    items = tax_service.calc_list()
    return {"count": len(items), "items": items}


@mcp.resource(
    "tax://output-templates",
    name="output-templates",
    description="输出模板键与标题；原文用 tax://templates/{key} 读取",
    mime_type="application/json",
    tags={"offline", "template"},
)
def output_templates_resource() -> dict:
    available = set(tax_service.template_index())
    items = [
        {"key": key, "title": heading}
        for key, heading in tax_service.TEMPLATE_KEYS.items()
        if heading in available
    ]
    return {"count": len(items), "items": items}


@mcp.resource(
    "tax://templates/{key}",
    name="output-template",
    description="单份输出模板原文（key 见 tax://output-templates）",
    mime_type="text/markdown",
    tags={"offline", "template"},
)
def output_template_resource(key: str) -> str:
    return _resource(tax_service.template_text, key)


# ── Prompt：成稿模板 ───────────────────────────────────────────────────────

def _template_prompt(template_key: str, question: str = "") -> str:
    template = tax_service.template_text(template_key)
    lead = f"问题：{question.strip()}\n\n" if question.strip() else ""
    return (
        f"{lead}先调用 classify_tax_question、check_evidence_coverage、"
        "search_tax_policies、read_law 等工具取证，再按下面的引擎模板成稿。"
        "只写工具返回的文件与段落；取数失败或依据不足要写明，"
        "不得补写未检索到的规定。\n\n"
        f"{template}"
    )


@mcp.prompt(
    name="analysis_six_sections",
    description="按「分析六段式」成稿（需先取回依据）",
    tags={"output-template"},
)
def analysis_six_sections(question: str = "") -> str:
    return _template_prompt("analysis", question)


@mcp.prompt(
    name="calculation_writeup",
    description="按「计算式」成稿：参数来源、步骤与结果",
    tags={"output-template"},
)
def calculation_writeup(question: str = "") -> str:
    return _template_prompt("calculation", question)


@mcp.prompt(
    name="entitlement_matrix",
    description="按「资格判定条件矩阵式」成稿",
    tags={"output-template"},
)
def entitlement_matrix(question: str = "") -> str:
    return _template_prompt("entitlement_matrix", question)


@mcp.prompt(
    name="risk_self_check",
    description="按「风险自检专用输出」成稿",
    tags={"output-template"},
)
def risk_self_check(question: str = "") -> str:
    return _template_prompt("risk_self_check", question)


@mcp.prompt(
    name="inspection_interview",
    description="按「稽查模拟问询式」成稿",
    tags={"output-template"},
)
def inspection_interview(question: str = "") -> str:
    return _template_prompt("inspection", question)


@mcp.prompt(
    name="intake_review",
    description="按「多合同受理条件块」成稿",
    tags={"output-template"},
)
def intake_review(question: str = "") -> str:
    return _template_prompt("intake", question)


def main() -> None:
    transport = (os.environ.get("TAX_MCP_TRANSPORT") or "stdio").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport not in ("http", "streamable-http"):
        raise SystemExit("TAX_MCP_TRANSPORT 只认 stdio/http/streamable-http")
    mcp.run(
        transport=transport,
        host=os.environ.get("TAX_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("TAX_MCP_PORT", "8765")),
    )


if __name__ == "__main__":
    main()
