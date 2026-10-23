from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tax_engine_mcp import engine, server  # noqa: E402

TOOL_NAMES = {
    "engine_health",
    "classify_tax_question",
    "check_evidence_coverage",
    "plan_tax_analysis",
    "search_tax_policies",
    "search_gov_policies",
    "read_law",
    "find_official_interpretations",
    "find_related_web_pages",
    "gather_tax_evidence",
    "analyze_tax_question",
    "expand_calculation",
    "find_tax_cases",
    "audit_ledger",
    "simulate_inspection",
    "blank_intake_form",
    "intake_contracts",
    "reconcile_contracts",
    "generate_controls",
}

PROMPT_TEMPLATE_KEYS = {
    "analysis_six_sections": "analysis",
    "option_comparison": "option_compare",
    "fill_in_the_blank": "fill_blank",
    "rule_statement": "rule_statement",
    "entitlement_matrix": "entitlement_matrix",
    "calculation_writeup": "calculation",
    "insufficient_evidence": "insufficient_evidence",
    "risk_self_check": "risk_self_check",
    "ledger_review": "ledger",
    "intake_review": "intake",
    "inspection_interview": "inspection",
}


def _run(coro):
    return asyncio.run(coro)


def call_tool(name: str, arguments: dict | None = None):
    async def body():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments or {})

    return _run(body())


def read_resource(uri: str):
    async def body():
        async with Client(server.mcp) as client:
            return await client.read_resource(uri)

    return _run(body())


def get_prompt(name: str, arguments: dict | None = None):
    async def body():
        async with Client(server.mcp) as client:
            return await client.get_prompt(name, arguments or {})

    return _run(body())


# ── 目录与协议形态 ─────────────────────────────────────────────────────────

def test_tool_catalog_is_complete_and_read_only():
    async def body():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    tools = _run(body())
    assert {tool.name for tool in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.description


def test_tool_schema_marks_required_arguments():
    async def body():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in _run(body())}
    schema = tools["search_tax_policies"].input_schema
    assert "query" in schema["properties"]
    assert schema["required"] == ["query"]
    assert "sources" in schema["properties"]


def test_static_resources_expose_engine_catalogs():
    async def body():
        async with Client(server.mcp) as client:
            return (
                await client.list_resources(),
                await client.list_resource_templates(),
            )

    resources, templates = _run(body())
    uris = {str(resource.uri) for resource in resources}
    assert "tax://tax-types" in uris
    assert "tax://calc-skeletons" in uris
    assert "tax://output-templates" in uris
    template_uris = {template.uri_template for template in templates}
    assert "tax://templates/{key}" in template_uris

    types = json.loads(read_resource("tax://tax-types")[0].text)
    names = [row["name"] for row in types["items"]]
    assert types["count"] == len(types["items"]) == 33
    assert "烟叶税" in names

    skeletons = json.loads(read_resource("tax://calc-skeletons")[0].text)
    assert skeletons["count"] >= 1
    assert any(row["name"] == "从价计征" for row in skeletons["items"])


def test_template_resource_reads_full_text_and_reports_unknown_key():
    text = read_resource("tax://templates/analysis")[0].text
    assert text.startswith("## 分析六段式")

    ledger = read_resource("tax://templates/ledger")[0].text
    assert ledger.startswith("### 账套巡检条件块")

    output_templates = json.loads(read_resource("tax://output-templates")[0].text)
    assert output_templates["count"] == len(server.tax_service.TEMPLATE_KEYS)
    assert {row["key"] for row in output_templates["items"]} == set(
        server.tax_service.TEMPLATE_KEYS)

    with pytest.raises(MCPError, match="unknown_template"):
        read_resource("tax://templates/not-a-template")


def test_prompt_embeds_engine_template():
    result = get_prompt("analysis_six_sections", {"question": "研发费用加计扣除比例是多少"})
    text = result.messages[0].content.text
    assert "研发费用加计扣除比例是多少" in text
    assert "## 分析六段式" in text
    assert "不得补写未检索到的规定" in text


def test_intake_review_prompt_uses_nested_template():
    result = get_prompt("intake_review", {"question": "主合同与补充协议"})
    text = result.messages[0].content.text
    assert "主合同与补充协议" in text
    assert "### 多合同受理条件块" in text


def test_every_output_template_has_a_prompt():
    """11 份成稿模板都要有对应 prompt；引擎新增模板而这里漏挂会报红。"""
    async def body():
        async with Client(server.mcp) as client:
            return await client.list_prompts()

    prompts = {prompt.name for prompt in _run(body())}
    assert prompts == set(PROMPT_TEMPLATE_KEYS)
    assert set(PROMPT_TEMPLATE_KEYS.values()) == set(server.tax_service.TEMPLATE_KEYS)

    for name, key in PROMPT_TEMPLATE_KEYS.items():
        text = get_prompt(name, {"question": "测试问题"}).messages[0].content.text
        heading = server.tax_service.TEMPLATE_KEYS[key]
        assert heading in text, (name, key)
        assert "测试问题" in text


# ── 离线工具 ───────────────────────────────────────────────────────────────

def test_health_tool_reports_engine_root_and_counts():
    data = call_tool("engine_health").data
    assert data["status"] == "ok"
    assert data["tax_types"] == 33
    assert data["calc_skeletons"] >= 1
    assert data["template_count"] >= 1


def test_classify_tool_runs_offline():
    data = call_tool(
        "classify_tax_question",
        {"question": "研发费用加计扣除比例是多少", "max_probes": 2},
    ).data
    assert data["type"]["type"]
    assert data["type"]["label"]
    assert isinstance(data.get("probes"), list)


def test_coverage_rejects_unknown_question_type():
    with pytest.raises(ToolError, match="unknown_question_type"):
        call_tool("check_evidence_coverage", {"qtype": "不存在的题型"})


def test_calculation_expands_and_rejects_extra_params():
    data = call_tool(
        "expand_calculation",
        {"skeleton": "从价计征", "params": {"计税依据": 1000000, "税率": 0.012}},
    ).data
    assert data["结果"]["应纳税额"] == 12000.0

    with pytest.raises(ToolError, match="invalid_parameter"):
        call_tool(
            "expand_calculation",
            {"skeleton": "从价计征", "params": {"计税依据": 1, "税率": 0.1, "多余": 1}},
        )


def test_cases_reject_unsupported_order_before_network():
    with pytest.raises(ToolError, match="order 只认"):
        call_tool("find_tax_cases", {"topic": "虚开发票", "order": "date"})


# ── 联网工具的边界与注入 ───────────────────────────────────────────────────

def test_search_rejects_multi_source_pagination_before_network():
    with pytest.raises(ToolError, match="page_not_supported"):
        call_tool(
            "search_tax_policies",
            {"query": "增值税", "sources": ["npc", "chinatax"], "page": 2},
        )


def test_gov_search_forwards_every_filter(monkeypatch):
    captured = {}

    def fake_search_gov(query, **kwargs):
        captured["query"] = query
        captured.update(kwargs)
        return {
            "query": query,
            "source": kwargs["source"],
            "page": kwargs["page"],
            "page_size": kwargs["size"],
            "total": 1,
            "has_next": False,
            "items": [{"title": "测试文件"}],
            "meta": {"filters": {"xxgkAging": kwargs["aging"]}},
        }

    monkeypatch.setattr(server.tax_service, "search_gov", fake_search_gov)
    data = call_tool(
        "search_gov_policies",
        {
            "query": "研发费用",
            "source": "fgk",
            "size": 5,
            "page": 1,
            "scope": "fulltext",
            "exact": True,
            "sort": "date",
            "date_from": "2026-01-01",
            "date_to": "2026-09-30",
            "aging": "全文有效",
            "body": True,
            "file_only": False,
            "tax_type": "企业所得税",
            "doc_type": "财政部税务总局公告",
            "doc_year": "2026",
            "doc_no": "12",
            "effect_level": "财税文件",
        },
    ).data
    assert captured["query"] == "研发费用"
    assert captured["source"] == "fgk"
    assert captured["scope"] == "fulltext"
    assert captured["exact"] is True
    assert captured["body"] is True
    assert captured["file_only"] is False
    assert captured["tax_type"] == "企业所得税"
    assert captured["effect_level"] == "财税文件"
    assert data["items"][0]["title"] == "测试文件"
    assert data["meta"]["filters"]["xxgkAging"] == "全文有效"


def test_gov_search_rejects_fgk_page_and_chinatax_body_before_network():
    with pytest.raises(ToolError, match="page_not_supported"):
        call_tool(
            "search_gov_policies",
            {"query": "增值税", "source": "fgk", "page": 2},
        )
    with pytest.raises(ToolError, match="body 只在 source=fgk"):
        call_tool(
            "search_gov_policies",
            {"query": "增值税", "source": "chinatax", "body": True},
        )


def test_read_law_combines_metadata_and_text(monkeypatch):
    monkeypatch.setattr(
        server.tax_service,
        "law_info",
        lambda law_id: {"id": law_id, "title": "测试法"},
    )
    monkeypatch.setattr(
        server.tax_service,
        "law_text",
        lambda law_id: {
            "law_id": law_id,
            "available": True,
            "count": 1,
            "paragraphs": ["第一条"],
        },
    )
    data = call_tool("read_law", {"bbbs_id": "abc", "include_text": True}).data
    assert data["law"]["title"] == "测试法"
    assert data["text"]["count"] == 1
    assert data["text"]["paragraphs"] == ["第一条"]


def test_read_law_maps_not_found_to_tool_error(monkeypatch):
    def missing(law_id):
        raise server.tax_service.ServiceError("not_found", f"没有这份法规：{law_id}", 404)

    monkeypatch.setattr(server.tax_service, "law_info", missing)
    with pytest.raises(ToolError, match="not_found"):
        call_tool("read_law", {"bbbs_id": "nope"})


def test_find_related_web_pages_forwards_keyword(monkeypatch):
    captured = {}

    def fake_related(law_id, keyword=""):
        captured.update(law_id=law_id, keyword=keyword)
        return {"law_id": law_id, "law_title": "增值税法", "keyword": keyword,
                "total": 1, "sources": [{"title": "解读A", "source_label": "税屋"}]}

    monkeypatch.setattr(server.tax_service, "web_related", fake_related)
    data = call_tool(
        "find_related_web_pages", {"bbbs_id": "abc", "keyword": "税率"}).data
    assert captured == {"law_id": "abc", "keyword": "税率"}
    assert data["sources"][0]["source_label"] == "税屋"


def test_find_related_web_pages_maps_upstream_error(monkeypatch):
    def broken(law_id, keyword=""):
        raise server.tax_service.ServiceError("upstream_error", "找相关网页失败", 502)

    monkeypatch.setattr(server.tax_service, "web_related", broken)
    with pytest.raises(ToolError, match="upstream_error"):
        call_tool("find_related_web_pages", {"bbbs_id": "abc"})


def test_blank_intake_form_round_trips_through_intake():
    doc = call_tool("blank_intake_form").data["document"]
    assert [c["代号"] for c in doc["合同"]] == ["CT-1"]
    assert [u["代号"] for u in doc["判断单元"]] == ["U1"]

    doc["合同"][0]["名称"] = "采购合同"
    checked = call_tool("intake_contracts", {"document": doc}).data
    assert checked["缺维度数"] == 9


def test_analyze_tool_returns_structured_draft(monkeypatch):
    def fake_answer(question, at="", read_body=True, check_citations=True,
                    max_per_round=None):
        return {
            "question": question,
            "at": at,
            "read_body": read_body,
            "check_citations": check_citations,
            "max_per_round": max_per_round,
            "primary": {"title": "测试依据"},
            "supporting": [],
        }

    monkeypatch.setattr(server.tax_service, "analyze_answer", fake_answer)
    data = call_tool(
        "analyze_tax_question",
        {"question": "测试问题", "at": "2026-10-20", "read_body": False,
         "max_per_round": 20},
    ).data
    assert data["question"] == "测试问题"
    assert data["at"] == "2026-10-20"
    assert data["read_body"] is False
    assert data["max_per_round"] == 20
    assert data["primary"]["title"] == "测试依据"


def test_gather_tool_returns_evidence_and_forwards_limit(monkeypatch):
    def fake_gather(question, at="", read_body=True, check_citations=True,
                    max_per_round=None):
        return {
            "question": question,
            "at": at,
            "max_per_round": max_per_round,
            "rounds": [{"round": 1, "hits": 2}],
            "evidence": [{"title": "测试证据"}],
            "errors": [],
        }

    monkeypatch.setattr(server.tax_service, "analyze_gather", fake_gather)
    data = call_tool(
        "gather_tax_evidence",
        {"question": "测试问题", "at": "2026-10-20", "max_per_round": 8},
    ).data
    assert data["question"] == "测试问题"
    assert data["max_per_round"] == 8
    assert data["rounds"][0]["hits"] == 2
    assert data["evidence"][0]["title"] == "测试证据"


def test_service_layer_lives_here_not_in_engine():
    """服务契约层、文档读取层与运行时快照都住在本项目里。

    引擎目录里出现这两个模块就说明适配器与引擎又缠回去了：适配器会跟着
    引擎的提交一起变，四个项目的独立性就没有了。
    """
    project_root = Path(__file__).resolve().parents[1]
    assert Path(server.tax_service.__file__).is_relative_to(project_root / "src")
    assert Path(server.tax_service.tax_doc.__file__).is_relative_to(project_root / "src")
    assert engine.ENGINE_ROOT.is_relative_to(project_root / "src")
    assert (engine.ENGINE_ROOT / "MANIFEST.json").is_file()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_service.py").exists()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_doc.py").exists()


def test_runtime_snapshot_matches_manifest():
    """运行时快照逐文件与清单里的 SHA-256 对齐。

    少一个脚本、改一个字节都在这里报红，而不是等某个取数分支到运行时才炸。
    """
    import re

    runtime = engine.ENGINE_ROOT
    project = Path(__file__).resolve().parents[1]
    version = re.search(r'^version = "([^"]+)"',
                        (project / "pyproject.toml").read_text(encoding="utf-8"), re.M)[1]
    assert sys.modules[engine.__package__].__version__ == version
    assert server.mcp.version == version
    manifest = json.loads((runtime / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["engine_version"] == "3.60.1"
    listed = manifest["files"]
    actual = {
        path.relative_to(runtime).as_posix()
        for folder in ("scripts", "data", "references")
        for path in (runtime / folder).glob("*")
        if path.is_file()
    }
    assert set(listed) == actual
    bad = [rel for rel, digest in listed.items()
           if hashlib.sha256((runtime / rel).read_bytes()).hexdigest() != digest]
    assert not bad, bad


def test_no_external_engine_lookup():
    """分发出去的项目不能再从环境变量或同级目录找引擎。"""
    project_root = Path(__file__).resolve().parents[1]
    package = project_root / "src" / "tax_engine_mcp"
    needles = ("TAX_ENGINE_ROOT", "find_engine_root")
    offenders = []
    for path in package.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders.extend(f"{path.relative_to(project_root).as_posix()}: {needle}"
                         for needle in needles if needle in text)
    assert offenders == []


def test_official_interpretations_query_plan_offline(monkeypatch):
    tax_doc = engine.tax_service.tax_doc
    calls = []

    def fake_source(site, query, n):
        calls.append((site, query, n))
        return [{"url": f"https://{site}/interpretation"}], ""

    monkeypatch.setattr(tax_doc, "_search_one_source", fake_source)
    result = tax_doc.search_interpretations("中华人民共和国增值税法", "增值税")
    expected = [
        ("chinatax.gov.cn", "增值税 官方解读", 100),
        ("mof.gov.cn", "增值税法 政策解读", 100),
        ("www.gov.cn", "增值税法 政策解读", 100),
    ]
    assert sorted(calls) == sorted(expected)
    assert [item["url"] for item in result["sources"]] == [
        f"https://{site}/interpretation" for site, _, _ in expected
    ]

    calls.clear()
    result = tax_doc.search_interpretations("增值税法", sources=[
        "chinatax.gov.cn", "chinatax.gov.cn", "fgk.chinatax.gov.cn",
        "shanghai.chinatax.gov.cn", "fgk.chinatax.gov.cn", "www.gov.cn",
    ])
    expected = [
        ("chinatax.gov.cn", "增值税法 解读", 100),
        ("fgk.chinatax.gov.cn", "增值税法 政策解读", 100),
        ("shanghai.chinatax.gov.cn", "增值税法 政策解读", 100),
        ("www.gov.cn", "增值税法 政策解读", 100),
    ]
    assert sorted(calls) == sorted(expected)
    assert [item["url"] for item in result["sources"]] == [
        f"https://{site}/interpretation" for site, _, _ in expected
    ]

    answer = sys.modules["tax_answer"]
    sogou = sys.modules[tax_doc.sogou_search.__module__]
    draft_calls = []

    def draft_search(query, *, site, size):
        draft_calls.append((query, site, size))
        return {"results": []}

    monkeypatch.setattr(sogou, "sogou_search", draft_search)
    assert answer._legis_round("离线草案", 5) == ([], "")
    assert draft_calls == [("离线草案", "npc.gov.cn", 5)]
    monkeypatch.setattr(answer, "build_plan", lambda question: {
        "type": {"type": "default"},
        "rounds": [{"round": 1, "goal": "草案", "sources": ["legis"],
                    "term_key": "legis"}],
    })
    monkeypatch.setattr(answer, "search_terms", lambda question: {"legis": "离线草案"})
    gathered = answer.gather("离线草案", read_body=False, max_per_round=2,
                             check_citations=False)
    assert gathered["errors"] == []
    assert draft_calls == [("离线草案", "npc.gov.cn", 5),
                           ("离线草案", "npc.gov.cn", 2)]


def test_sogou_mobile_full_page_cache_offline(monkeypatch, tmp_path):
    from urllib.parse import quote

    tax_doc = engine.tax_service.tax_doc
    tax_sogou = sys.modules[tax_doc.sogou_search.__module__]
    tax_cache = sys.modules["tax_cache"]
    for module in (tax_sogou, tax_cache):
        assert Path(module.__file__).is_relative_to(engine.ENGINE_ROOT / "scripts")

    cache = tax_cache.CacheManager(enabled=True, namespace="sogou")
    cache.dir = tmp_path
    monkeypatch.setattr(tax_sogou, "_cache", cache)
    snippet = "增值税政策解读摘要完整保留。" * 30
    assert len(snippet) > 200
    urls = [f"https://chinatax.gov.cn/interpretation-{i}.html" for i in range(12)]
    html = "<html><body>" + "".join(
        f'<div class="vrResult"><h3><a class="resultLink" '
        f'href="https://m.sogou.com/web?url={quote(url, safe="")}">'
        f'增值税政策<em>解读</em>{i}</a></h3>'
        '<div class="citeurl"><span>chinatax.gov.cn</span> '
        '<span class="date">1995-01-27</span></div>'
        f'<div class="txt-summary">{snippet}</div></div>'
        for i, url in enumerate(urls)
    ) + "</body></html>"
    calls = []

    def fake_fetch(query):
        calls.append(query)
        return html, "https://m.sogou.com/web", "", False

    monkeypatch.setattr(tax_sogou, "_fetch", fake_fetch)
    for size, count, from_cache in ((1, 1, False), (5, 5, True), (100, 12, True)):
        result = tax_doc.sogou_search("增值税政策解读", site="chinatax.gov.cn", size=size)
        assert not result.get("_error"), result
        assert len(result["results"]) == count
        assert result["_from_cache"] is from_cache
        assert [item["url"] for item in result["results"]] == urls[:count]
        for i, item in enumerate(result["results"]):
            assert item["title"] == f"增值税政策解读{i}"
            assert item["snippet"] == snippet
            assert item["date"] == "1995-01-27"
    assert calls == ["site:chinatax.gov.cn 增值税政策解读"]
    assert cache.stats()["entries"] == 1


def test_interpretations_cache_expiry_and_retry_boundaries_offline(monkeypatch, tmp_path):
    import threading
    import time
    from unittest.mock import patch

    tax_doc = engine.tax_service.tax_doc
    tax_sogou = sys.modules[tax_doc.sogou_search.__module__]
    cache = sys.modules["tax_cache"].CacheManager(enabled=True, namespace="sogou")
    cache.dir = tmp_path
    monkeypatch.setattr(tax_sogou, "_cache", cache)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    calls = []
    caller = threading.get_ident()

    def fetch(query):
        assert threading.get_ident() == caller
        calls.append(query)
        html = ('<div class=vrResult><h3><a class=resultLink '
                'href="/tc?url=https%3A%2F%2Fchinatax.gov.cn%2Farticle.html">'
                f'增值税法官方解读第{len(calls)}版</a></h3></div>')
        return html, "", "", False

    with patch.object(tax_sogou, "_fetch", fetch):
        first = tax_doc.search_interpretations("过期验证", sources=["chinatax.gov.cn"])
        clock[0] += tax_sogou.CACHE_TTL - 1
        cached = tax_doc.search_interpretations("过期验证", sources=["chinatax.gov.cn"])
        assert len(calls) == 1 and first["sources"] == cached["sources"]
        clock[0] += 2
        refreshed = tax_doc.search_interpretations("过期验证", sources=["chinatax.gov.cn"])
        assert len(calls) == 2
        assert refreshed["sources"][0]["title"] == "增值税法官方解读第2版"

    for status, count in ((400, 1), (404, 1), (503, 2)):
        response = type("R", (), {"status_code": status, "url": "",
                                 "headers": {}, "content": b""})()
        with patch.object(tax_sogou, "_get", return_value=response) as hit, \
                patch.object(time, "sleep") as wait:
            _, _, error, blocked = tax_sogou._fetch("离线验证")
        assert hit.call_count == count and wait.call_count == count - 1
        assert f"HTTP {status}" in error and not blocked


def test_interpretations_partial_failure_and_missing_date_offline(monkeypatch):
    tax_doc = engine.tax_service.tax_doc
    calls = []
    success = {"title": "增值税政策官方解读", "url": "https://chinatax.gov.cn/interpretation"}
    returned_error = "合成反爬拦截"
    raised_reason = "合成上游异常"

    def fake_source(site, query, n):
        calls.append((site, query, n))
        if site == "chinatax.gov.cn":
            return [success], ""
        if site == "mof.gov.cn":
            return [], returned_error
        assert site == "www.gov.cn"
        raise RuntimeError(raised_reason)

    with monkeypatch.context() as patch:
        patch.setattr(tax_doc, "_search_one_source", fake_source)
        for _ in range(2):
            result = tax_doc.search_interpretations("增值税法")
            assert result["queries_blocked"] == "2/3"
            assert returned_error in result["engine_error"]
            assert raised_reason in result["engine_error"]
            assert "RuntimeError" in result["engine_error"]
            assert result["sources"] == [success]
            assert result["total"] == 1
    expected = [
        ("chinatax.gov.cn", "增值税法 解读", 100),
        ("mof.gov.cn", "增值税法 政策解读", 100),
        ("www.gov.cn", "增值税法 政策解读", 100),
    ]
    assert sorted(calls) == sorted(expected * 2)

    url = "https://chinatax.gov.cn/20240101/interpretation.html"

    def fake_sogou(query, *, site, size):
        assert (query, site, size) == ("增值税法 解读", "chinatax.gov.cn", 100)
        return {"results": [{"title": "增值税政策官方解读", "url": url,
                             "date": "", "snippet": "合成摘要"}]}

    monkeypatch.setattr(tax_doc, "sogou_search", fake_sogou)
    items, error = tax_doc._search_one_source("chinatax.gov.cn", "增值税法 解读", 100)
    assert error == ""
    assert len(items) == 1
    assert items[0]["url"] == url
    assert items[0]["date"] == ""


def test_vendored_copies_match_fingerprint():
    """三个项目各带一份副本，指纹相同才算同一份。

    改动任一处都要同步另外两个项目；先在这里报红，而不是等到某个适配器
    的行为跟别人不一样。
    """
    import hashlib

    pins = {
        "tax_service": "adf63b15c32df4637d6fe730835e1eb4524e28afe8484c95111557d33a531b42",
        "tax_doc": "01f66bf24739285cf42cc72a2d7ec0cdff9f89e29e896db38365ae9e3176738f",
    }
    got = {
        "tax_service": hashlib.sha256(
            Path(server.tax_service.__file__).read_bytes()).hexdigest(),
        "tax_doc": hashlib.sha256(
            Path(server.tax_service.tax_doc.__file__).read_bytes()).hexdigest(),
    }
    assert got == pins, got
