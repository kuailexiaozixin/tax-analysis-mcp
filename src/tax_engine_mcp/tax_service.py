#!/usr/bin/env python3
"""服务契约层：把引擎的能力收成适配器能依赖的一份接口。

这一层不复制引擎判据，只做三件事：
  1. 参数边界校验：空值、取值域、日期格式、未知骨架、多余参数；
  2. 异常收口：把引擎的 KeyError/ValueError/Missing 翻译成带 code 的
     ServiceError，让三种适配器用同一套错误语义；
  3. 形状收口：搜索结果统一成 {query,total,page,page_size,has_next,items,meta}，
     不让 CLI、API、MCP 各自解释一遍聚合层的字段。

不放在这里的：⑥ 的成稿（由调用方写）、付费模型解读（三个适配器都不开放）、
逐源检索的参数细节（仍留在各源脚本，按需扩展）。

本文件是逐项目副本：CLI、API、MCP 各自带一份，三份内容要求逐字节相同，
由各自的测试用 SHA-256 校验。引擎仓库里没有这一层，适配器只读包内的
运行时快照，不往任何外部目录写文件。
"""
from __future__ import annotations

import time
from pathlib import Path

import tax_aggregator
import tax_analyze
import tax_answer
import tax_calc
import tax_cases
import tax_control
import tax_coverage
import tax_detail
import tax_fgk
import tax_http
import tax_intake
import tax_inspect
import tax_ledger
import tax_related
import tax_search
import tax_web_search

from . import tax_doc

#: 运行时根与模板文件路径由引导层注入：本副本住在适配器包里，
#: 不自己用 __file__ 的父目录去猜路径。
ENGINE_ROOT: Path | None = None
TEMPLATE_PATH: Path | None = None


def configure(engine_root) -> None:
    """引导层在导入运行时脚本后调用一次，告诉这一层快照根目录在哪。"""
    global ENGINE_ROOT, TEMPLATE_PATH
    ENGINE_ROOT = Path(engine_root).resolve()
    TEMPLATE_PATH = ENGINE_ROOT / "references" / "output_templates.md"


def _require_ready() -> tuple[Path, Path]:
    if ENGINE_ROOT is None or TEMPLATE_PATH is None:
        raise ServiceError("internal_error",
                           "引导层没有调用 configure()，本层不知道运行时根在哪", 500)
    return ENGINE_ROOT, TEMPLATE_PATH

TEMPLATE_KEYS = {
    "analysis": "分析六段式",
    "option_compare": "逐条比对式",
    "fill_blank": "填空式",
    "rule_statement": "规则陈述式",
    "entitlement_matrix": "资格判定条件矩阵式",
    "calculation": "计算式",
    "insufficient_evidence": "依据不足时",
    "risk_self_check": "风险自检专用输出",
    "ledger": "账套巡检条件块",
    "intake": "多合同受理条件块",
    "inspection": "稽查模拟问询式",
}

STATUS_VALUES = (1, 2, 3, 4)
SCOPE_VALUES = ("title", "fulltext")
SORT_VALUES = ("relevance", "date")
GOV_SOURCES = ("chinatax", "fgk")
CASES_MODES = ("exhaustive", "recent")
CASE_ORDER_VALUES = tuple(tax_cases.tws.ORDER_VALUES)


class ServiceError(Exception):
    """带 HTTP 语义的错误。status 由 API 直接采用，CLI 据此选退出码。"""

    def __init__(self, code: str, message: str, status: int = 400,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        out = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            out["error"]["details"] = self.details
        return out


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError("invalid_parameter", f"{name} 不能为空",
                           details={"field": name})
    return value.strip()


def _int_in(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError("invalid_parameter", f"{name} 必须是整数",
                           details={"field": name, "value": value})
    if not lo <= value <= hi:
        raise ServiceError("invalid_parameter", f"{name} 要在 {lo}–{hi} 之间",
                           details={"field": name, "value": value})
    return value


def _date(value, name: str) -> str:
    if value in (None, ""):
        return ""
    try:
        return tax_search.check_iso_date(value, name)
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e),
                           details={"field": name, "value": value}) from e


def _choice(value, name: str, allowed) -> str:
    if value not in allowed:
        raise ServiceError("invalid_parameter",
                           f"{name} 只认 {'／'.join(map(str, allowed))}，收到 {value!r}",
                           details={"field": name, "allowed": list(allowed)})
    return value


def _opt_text(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ServiceError("invalid_parameter", f"{name} 必须是字符串",
                           details={"field": name, "value": value})
    return value.strip()


def health() -> dict:
    """不联网的健康检查：只报本进程能读到的注册表规模与引擎根。"""
    root, _ = _require_ready()
    available = set(template_index())
    return {
        "status": "ok",
        "engine_root": str(root),
        "tax_types": len(tax_search.TAX_TYPE_KEYWORDS),
        "calc_skeletons": len(tax_calc.SKELETONS),
        "template_count": sum(1 for h in TEMPLATE_KEYS.values() if h in available),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def classify(question: str, max_probes: int = 3) -> dict:
    """① 判型 + 前提缺口 + 探针；纯离线。"""
    q = _text(question, "question")
    max_probes = _int_in(max_probes, "max_probes", 1, 10)
    return tax_analyze.analyze(q, max_probes=max_probes)


def coverage(qtype: str, question: str = "", evidence: list | None = None,
             skeleton: str = "", params: dict | None = None) -> dict:
    """② 依据覆盖率；evidence=None 是"还没检"，[] 是"检了没有"。"""
    qtype = _text(qtype, "qtype")
    if evidence is not None and not isinstance(evidence, list):
        raise ServiceError("invalid_parameter", "evidence 必须是数组或 null",
                           details={"field": "evidence"})
    if params is not None and not isinstance(params, dict):
        raise ServiceError("invalid_parameter", "params 必须是对象或 null",
                           details={"field": "params"})
    try:
        return tax_coverage.assess(qtype, question=question or "",
                                   evidence=evidence,
                                   skeleton=skeleton or "",
                                   params=params)
    except KeyError as e:
        allowed = sorted(tax_coverage.load()["题型"])
        raise ServiceError("unknown_question_type", str(e),
                           details={"allowed": allowed}) from e
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e)) from e


def search(query: str, *, sources: list[str] | None = None, size: int = 10,
           page: int = 1, status: int = 3, scope: str = "title",
           exact: bool = False, sort: str = "relevance",
           date_from: str = "", date_to: str = "") -> dict:
    """③④ 检索。多源走聚合；只有单源 NPC 有真实的页码维度。

    聚合层是六源各取 size 条再合并截断，没有 pageNum；只有 NPC 的
    search_tax 有 total/page/page_size。多源传 page>1 一律 400，不静默停在第一页。
    """
    query = _text(query, "query")
    size = _int_in(size, "size", 1, 50)
    page = _int_in(page, "page", 1, 1000)
    _choice(status, "status", STATUS_VALUES)
    _choice(scope, "scope", SCOPE_VALUES)
    _choice(sort, "sort", SORT_VALUES)
    date_from = _date(date_from, "date_from")
    date_to = _date(date_to, "date_to")

    allowed_sources = list(tax_aggregator.DEFAULT_SOURCES)
    if sources is None:
        use_sources = allowed_sources
    else:
        if not isinstance(sources, list) or not sources:
            raise ServiceError("invalid_parameter", "sources 必须是非空数组")
        unknown = [s for s in sources if s not in allowed_sources]
        if unknown:
            raise ServiceError("invalid_parameter",
                               f"未知数据源：{'、'.join(unknown)}",
                               details={"allowed": allowed_sources})
        use_sources = list(dict.fromkeys(sources))

    if use_sources == ["npc"]:
        try:
            raw = tax_search.search_tax(
                query, scope=scope,
                search_type=1 if exact else 2, status=status,
                date_from=date_from or None, date_to=date_to or None,
                page=page, size=size, sort=sort)
        except Exception as e:
            raise ServiceError("upstream_error",
                               f"NPC 检索失败：{tax_http.short_reason(e)}",
                               502) from e
        items = raw.get("results", [])
        total = int(raw.get("total") or 0)
        meta = {
            "total": total,
            "source_summary": {"npc": len(items)},
            "errors": {},
            "gaps": [],
            "searched_at": raw.get("searched_at", ""),
            "from_cache": bool(raw.get("_from_cache")),
            "reliability": raw.get("_reliability", ""),
            "reliability_note": raw.get("_reliability_note", ""),
            "date_note": raw.get("_date_note", ""),
        }
        return {
            "query": query, "page": page, "page_size": size,
            "total": total, "has_next": page * size < total,
            "items": items, "meta": meta,
        }

    if page != 1:
        raise ServiceError(
            "page_not_supported",
            "多源聚合没有页码维度：聚合是每源各取 size 条后合并截断；"
            "要翻页请把 sources 限定为 [\"npc\"]",
            details={"sources": use_sources})
    try:
        raw = tax_aggregator.aggregate_search(
            query, size=size, sources=use_sources, status=status,
            scope=scope, exact=exact, sort=sort,
            date_from=date_from or None, date_to=date_to or None)
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"多源检索失败：{tax_http.short_reason(e)}",
                           502) from e
    items = raw.get("items", [])
    meta = {
        "total": int(raw.get("total_items") or 0),
        "source_summary": raw.get("source_summary", {}),
        "errors": raw.get("errors", {}),
        "gaps": raw.get("gaps", []),
        "degraded_note": raw.get("degraded_note", ""),
        "searched_at": raw.get("searched_at", ""),
        "date_filter": raw.get("_date_filter"),
        "date_note": raw.get("_date_note", ""),
    }
    return {
        "query": query, "page": 1, "page_size": size,
        "total": meta["total"], "has_next": False,
        "items": items, "meta": meta,
    }


def search_gov(query: str, *, source: str = "chinatax", size: int = 10,
               page: int = 1, scope: str = "title", exact: bool = False,
               sort: str = "relevance", date_from: str = "", date_to: str = "",
               aging: str = "", body: bool = False, file_only: bool = True,
               tax_type: str = "", doc_type: str = "", doc_year: str = "",
               doc_no: str = "", effect_level: str = "") -> dict:
    """税务总局站内（search5）与税务法规库的单源检索。

    与 ``search`` 的区别是数据层而不是检索词：``search`` 的 ``chinatax`` 是聚合层
    里的一路，只做关键词检索；这一路直接调 search5，能吃下范围、精确、成文日期、
    时效、税种、文种、年份、文号、效力等级九组收窄维度。法规库（fgk）在这一层
    再筛一遍清单并可按需取正文；它没有页码，翻页会报 page_not_supported。

    两条路的取值域都由引擎校验：时效、效力等级、日期格式在 build_filters 里，
    非法值发出去接口只会回 total=0，看起来像"筛空了"，所以在这里挡成 400。
    """
    query = _text(query, "query")
    source = _choice(source, "source", GOV_SOURCES)
    size = _int_in(size, "size", 1, 50)
    page = _int_in(page, "page", 1, 1000)
    scope = _choice(scope, "scope", SCOPE_VALUES)
    sort = _choice(sort, "sort", SORT_VALUES)
    date_from = _date(date_from, "date_from")
    date_to = _date(date_to, "date_to")
    aging = _opt_text(aging, "aging")
    tax_type = _opt_text(tax_type, "tax_type")
    doc_type = _opt_text(doc_type, "doc_type")
    doc_year = _opt_text(doc_year, "doc_year")
    doc_no = _opt_text(doc_no, "doc_no")
    effect_level = _opt_text(effect_level, "effect_level")
    if not isinstance(body, bool):
        raise ServiceError("invalid_parameter", "body 必须是布尔值",
                           details={"field": "body"})
    if not isinstance(file_only, bool):
        raise ServiceError("invalid_parameter", "file_only 必须是布尔值",
                           details={"field": "file_only"})
    if body and source != "fgk":
        raise ServiceError("invalid_parameter",
                           "body 只在 source=fgk 时有效：search5 那一路不回正文",
                           details={"field": "body", "source": source})
    if page != 1 and source == "fgk":
        raise ServiceError(
            "page_not_supported",
            "税务法规库没有页码：它一次连扫若干页再按 size 截断；"
            "要翻页请改用 source=\"chinatax\"",
            details={"source": source})
    try:
        filters = tax_web_search.build_filters(
            in_title=(scope == "title"), precise=exact,
            tax_type=tax_type, doc_type=doc_type, doc_year=doc_year,
            doc_no=doc_no, cwrq_from=date_from, cwrq_to=date_to,
            aging=aging, effect_level=effect_level)
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e),
                           details={"field": "filters"}) from e
    order = "date_desc" if sort == "date" else "relevance"
    try:
        if source == "fgk":
            raw = tax_fgk.search_fgk(
                query, size=size, with_body=body, filters=filters,
                file_only=file_only, order=order)
        else:
            raw = tax_web_search.search_chinatax(
                query, page=page, size=size, filters=filters,
                file_only=file_only, order=order)
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e)) from e
    except Exception as e:
        where = "税务法规库" if source == "fgk" else "税务总局"
        raise ServiceError("upstream_error",
                           f"{where}检索失败：{tax_http.short_reason(e)}",
                           502) from e

    items = raw.get("results")
    if not isinstance(items, list):
        items = raw.get("items")
    if not isinstance(items, list):
        items = []
    if source == "fgk":
        total = int(raw.get("total_hits") or raw.get("total") or 0)
        has_next = False
    else:
        total = int(raw.get("total") or 0)
        has_next = page * size < total
    error = str(raw.get("_error") or "")
    meta = {
        "source": source,
        "page": page,
        "page_size": size,
        "total": total,
        "has_next": has_next,
        "source_summary": {source: len(items)},
        "errors": {source: error} if error else {},
        "gaps": [],
        "searched_at": raw.get("searched_at", ""),
        "from_cache": bool(raw.get("_from_cache")),
        "cache_age_s": raw.get("_cache_age_s"),
        "filters": raw.get("filters", filters),
        "filter_note": raw.get("_filter_note", ""),
        "total_hits": raw.get("total_hits"),
        "pages_scanned": raw.get("pages_scanned"),
        "stopped_early": raw.get("stopped_early"),
        "file_only": file_only,
        "order": order,
    }
    return {
        "query": query, "source": source, "page": page, "page_size": size,
        "total": total, "has_next": has_next, "items": items, "meta": meta,
    }


def law_info(bbbs_id: str) -> dict:
    """法规元数据。404 与上游故障分开报。"""
    bbbs_id = _text(bbbs_id, "bbbs_id")
    try:
        detail = tax_detail.fetch_detail(bbbs_id)
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"取法规详情失败：{tax_http.short_reason(e)}",
                           502) from e
    if not isinstance(detail, dict) or not detail.get("title"):
        raise ServiceError("not_found", f"没有这份法规：{bbbs_id}", 404)
    return detail


def law_text(bbbs_id: str) -> dict:
    """法规全文段落。取不到时 available=False，不把空清单伪装成"没有正文"。"""
    bbbs_id = _text(bbbs_id, "bbbs_id")
    paragraphs = tax_doc.download_and_extract(bbbs_id)
    return {
        "law_id": bbbs_id,
        "available": bool(paragraphs),
        "count": len(paragraphs),
        "paragraphs": paragraphs,
    }


def law_interpretations(title: str, keyword: str = "",
                        province: str = "", sources: list[str] | None = None) -> dict:
    """按法规标题找官方解读；只回 .gov.cn 来源。"""
    title = _text(title, "title")
    if sources is not None and (not isinstance(sources, list) or not sources):
        raise ServiceError("invalid_parameter", "sources 必须是非空数组或 null")
    try:
        return tax_doc.search_interpretations(
            title, keyword=keyword or "", province=province or "",
            sources=sources)
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"找官方解读失败：{tax_http.short_reason(e)}",
                           502) from e


def web_related(bbbs_id: str, keyword: str = "") -> dict:
    """法规的实务相关网页：税屋、公众号与搜狗全网。

    标题从 law_info 取，组合与过滤在 tax_related：Flask 的
    /api/web-related/<bbbs_id> 用的是同一份实现。
    """
    bbbs_id = _text(bbbs_id, "bbbs_id")
    keyword = _opt_text(keyword, "keyword")
    info = law_info(bbbs_id)
    try:
        payload = tax_related.collect_related(info["title"], keyword=keyword)
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"找相关网页失败：{tax_http.short_reason(e)}",
                           502) from e
    payload["law_id"] = bbbs_id
    return payload


def analyze_plan(question: str) -> dict:
    """⑥ 之前的检索计划；不联网。"""
    q = _text(question, "question")
    return tax_answer.build_plan(q)


def analyze_gather(question: str, at: str = "", read_body: bool = True,
                   check_citations: bool = True,
                   max_per_round: int | None = None) -> dict:
    """按计划联网检索并定级。部分源失败会留在结果的 errors 里，不抛。"""
    q = _text(question, "question")
    at = _date(at, "at")
    if max_per_round is not None:
        max_per_round = _int_in(max_per_round, "max_per_round", 1, 200)
    try:
        return tax_answer.gather(q, at=at, read_body=bool(read_body),
                                 max_per_round=max_per_round,
                                 check_citations=bool(check_citations))
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"分析检索失败：{tax_http.short_reason(e)}",
                           502) from e


def analyze_answer(question: str, at: str = "", read_body: bool = True,
                   check_citations: bool = True,
                   max_per_round: int | None = None) -> dict:
    """一次跑完检索与分层，回结构化底稿；不写最终答案。"""
    plan = analyze_gather(question, at=at, read_body=read_body,
                          check_citations=check_citations,
                          max_per_round=max_per_round)
    return tax_answer.compose(plan)


def calc_list() -> list[dict]:
    """列出算式骨架与所需参数。"""
    return [{
        "name": name,
        "formula": sk.get("公式", ""),
        "inputs": tax_calc.inputs(name),
        "results": list(sk.get("结果", {})),
    } for name, sk in tax_calc.SKELETONS.items()]


def calc_run(skeleton: str, params: dict) -> dict:
    """展开一条算式。多余参数在这里挡住：引擎的 run 会把它并进求值命名空间。"""
    skeleton = _text(skeleton, "skeleton")
    if skeleton not in tax_calc.SKELETONS:
        raise ServiceError("unknown_skeleton", f"没有这个骨架：{skeleton}",
                           details={"allowed": sorted(tax_calc.SKELETONS)})
    if not isinstance(params, dict):
        raise ServiceError("invalid_parameter", "params 必须是对象")
    need = tax_calc.inputs(skeleton)
    extra = [k for k in params if k not in need]
    if extra:
        raise ServiceError("invalid_parameter",
                           f"「{skeleton}」没有这些参数：{'、'.join(extra)}",
                           details={"allowed": need})
    try:
        return tax_calc.run(skeleton, params)
    except tax_calc.Missing as e:
        raise ServiceError("missing_params",
                           f"缺少参数：{'、'.join(e.names)}",
                           details={"missing": e.names, "allowed": need}) from e
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        why = (f"税率表少给一列：{e.args[0]}"
               if isinstance(e, KeyError) and e.args else str(e))
        raise ServiceError("invalid_parameter", why) from e


def cases(topic: str, elements: list[str] | None = None,
          mode: str = "exhaustive", pages: int = 1,
          order: str = "relevance", limit: int = 10,
          as_of: str = "", shui5: bool = False) -> dict:
    """类案台账。"""
    topic = _text(topic, "topic")
    mode = _choice(mode, "mode", CASES_MODES)
    order = _choice(order, "order", CASE_ORDER_VALUES)
    pages = _int_in(pages, "pages", 1, 20)
    limit = _int_in(limit, "limit", 1, 100)
    if as_of:
        try:
            as_of = tax_cases.check_as_of(as_of)
        except ValueError as e:
            raise ServiceError("invalid_parameter", str(e),
                               details={"field": "as_of"}) from e
    shui5_fetch = None
    if shui5:
        from tax_shui5 import search_shui5
        shui5_fetch = lambda w: search_shui5(w, size=5)  # noqa: E731
    try:
        return tax_cases.collect(topic, elements or (), mode, pages,
                                 order, limit, as_of,
                                 shui5_fetch=shui5_fetch)
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e)) from e
    except Exception as e:
        raise ServiceError("upstream_error",
                           f"类案检索失败：{tax_http.short_reason(e)}",
                           502) from e


def ledger_check(ledger: dict, params: dict | None = None,
                 codes: list[str] | None = None, tolerance: float = 0.0) -> dict:
    """账套巡检。"""
    if not isinstance(ledger, dict):
        raise ServiceError("invalid_parameter", "ledger 必须是对象")
    try:
        return tax_ledger.scan(ledger, params=params, codes=codes or (),
                               tolerance=float(tolerance))
    except (tax_ledger.NeedsLedger, tax_ledger.Unresolved,
            tax_ledger.NotInTable, ValueError) as e:
        raise ServiceError("invalid_request", str(e)) from e


def inspection(answers: dict | None = None, domains: list[str] | None = None,
               codes: list[str] | None = None, scenario: str = "",
               signal: str = "", tolerance: float = 0.0) -> dict:
    """稽查模拟问询。"""
    if answers is not None and not isinstance(answers, dict):
        raise ServiceError("invalid_parameter", "answers 必须是对象或 null")
    try:
        return tax_inspect.build(answers, domains or (), codes or (),
                                 scenario, signal, float(tolerance))
    except ValueError as e:
        raise ServiceError("invalid_parameter", str(e)) from e


def intake_blank() -> dict:
    """空白受理表；改完可直接传给 intake()。"""
    try:
        return tax_intake.blank()
    except Exception as e:
        raise ServiceError("internal_error", f"空白受理表生成失败：{e}",
                           500) from e


def intake(document: dict) -> dict:
    """多合同受理（九维度覆盖）。"""
    if not isinstance(document, dict):
        raise ServiceError("invalid_parameter", "document 必须是对象")
    try:
        return tax_intake.intake(document)
    except (tax_intake.NeedsContracts, ValueError) as e:
        raise ServiceError("invalid_request", str(e)) from e


def reconcile(document: dict, confirmed: list[str] | None = None,
              dims: list[str] | None = None) -> dict:
    """合同条款与履行记录对账。"""
    if not isinstance(document, dict):
        raise ServiceError("invalid_parameter", "document 必须是对象")
    try:
        return tax_intake.reconcile(document, confirmed or (), dims or ())
    except (tax_intake.NeedsContracts, ValueError) as e:
        raise ServiceError("invalid_request", str(e)) from e


def controls(diagnosis: dict | None = None, assumptions: list[str] | None = None,
             links: list[str] | None = None, codes: list[str] | None = None) -> dict:
    """把已确认缺口转成九要素制度文本。"""
    try:
        return tax_control.build(diagnosis, assumptions or (), links or (),
                                 codes or ())
    except (tax_control.NeedsDiagnosis, ValueError) as e:
        raise ServiceError("invalid_request", str(e)) from e


def tax_types() -> list[dict]:
    """税种与专题词表；键序就是界面与归类的优先序。"""
    out = []
    for name, info in tax_search.TAX_TYPE_KEYWORDS.items():
        out.append({
            "name": name,
            "aliases": list(info.get("aliases", [])),
            "parent_law": info.get("parent_law", ""),
            "authority": info.get("authority", ""),
            "note": info.get("note", ""),
        })
    return out


def template_index() -> list[str]:
    return list(_template_sections())


def template_text(key: str) -> str:
    sections = _template_sections()
    heading = TEMPLATE_KEYS.get(key, key)
    if heading not in sections:
        raise ServiceError("unknown_template", f"没有这个模板：{key}", 404,
                           details={"allowed": list(TEMPLATE_KEYS)})
    return sections[heading]


_SECTION_CACHE: dict[str, str] | None = None


def _template_sections() -> dict[str, str]:
    """按 TEMPLATE_KEYS 注册的标题切段。

    只收注册过的标题，且跳过代码围栏里的示例标题：模板文件用 ``` 展示成稿形态，
    围栏内也有 `## [问题] …` 这类行；把它们当段落会让索引多出一批取不到的键。
    标题可能出现在二级或三级（账套巡检、多合同受理两块是三级）。
    """
    global _SECTION_CACHE
    if _SECTION_CACHE is not None:
        return _SECTION_CACHE
    _, template_path = _require_ready()
    wanted = set(TEMPLATE_KEYS.values())
    text = template_path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith(("## ", "### ")):
            heading = line[3:].strip()
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = heading if heading in wanted else None
            buf = [line] if current is not None else []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    _SECTION_CACHE = sections
    return sections
