from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from tax_engine_mcp import engine, server

TOOL_NAMES = {
    "engine_health",
    "classify_tax_question",
    "check_evidence_coverage",
    "plan_tax_analysis",
    "search_tax_policies",
    "read_law",
    "find_official_interpretations",
    "analyze_tax_question",
    "expand_calculation",
    "find_tax_cases",
    "audit_ledger",
    "simulate_inspection",
    "intake_contracts",
    "reconcile_contracts",
    "generate_controls",
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


def test_analyze_tool_returns_structured_draft(monkeypatch):
    def fake_answer(question, at="", read_body=True, check_citations=True):
        return {
            "question": question,
            "at": at,
            "read_body": read_body,
            "check_citations": check_citations,
            "primary": {"title": "测试依据"},
            "supporting": [],
        }

    monkeypatch.setattr(server.tax_service, "analyze_answer", fake_answer)
    data = call_tool(
        "analyze_tax_question",
        {"question": "测试问题", "at": "2026-10-20", "read_body": False},
    ).data
    assert data["question"] == "测试问题"
    assert data["at"] == "2026-10-20"
    assert data["read_body"] is False
    assert data["primary"]["title"] == "测试依据"


def test_service_layer_lives_here_not_in_engine():
    """服务契约层和文档读取层是本项目的副本。

    引擎目录里出现这两个模块就说明适配器与引擎又缠回去了：适配器会跟着
    引擎的提交一起变，四个项目的独立性就没有了。
    """
    project_root = Path(__file__).resolve().parents[1]
    assert Path(server.tax_service.__file__).is_relative_to(project_root / "src")
    assert Path(server.tax_service.tax_doc.__file__).is_relative_to(project_root / "src")
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_service.py").exists()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_doc.py").exists()
    assert engine.ENGINE_ROOT != project_root


def test_vendored_copies_match_fingerprint():
    """三个项目各带一份副本，指纹相同才算同一份。

    改动任一处都要同步另外两个项目；先在这里报红，而不是等到某个适配器
    的行为跟别人不一样。
    """
    import hashlib

    pins = {
        "tax_service": "b8235637b8937cc3c948d7ef4a8e16e887453d9d0f1ca07bbed15e54199f1841",
        "tax_doc": "ef3f8cb2b5c59181a367f368e18cb92a77ed3e9bd9441eec40cca670dd2913de",
    }
    got = {
        "tax_service": hashlib.sha256(
            Path(server.tax_service.__file__).read_bytes()).hexdigest(),
        "tax_doc": hashlib.sha256(
            Path(server.tax_service.tax_doc.__file__).read_bytes()).hexdigest(),
    }
    assert got == pins, got
