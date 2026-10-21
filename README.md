# tax-analysis-mcp

给支持 MCP 的 AI 客户端使用的税务分析服务端。模型可以调用 19 个工具完成
判型、依据覆盖检查、六源检索、法规正文与官方解读、算式展开、类案查询与
账套/合同检查；3 个静态资源与 1 个模板资源提供税种、算式和模板词表，
11 个 prompt 把成稿模板与取证纪律封成可直接使用的指令。

适合 Claude Desktop、Qoder 等 MCP 客户端。默认 stdio，也可起 Streamable
HTTP；所有工具只读，服务端不修改账套或远端状态。

## 快速开始

```bash
cd tax-analysis-mcp
python -m pip install -e .
```

在客户端配置里加入：

```json
{
  "mcpServers": {
    "tax-analysis": {
      "command": "tax-analysis-mcp"
    }
  }
}
```

客户端启动的进程不继承终端 `PATH` 时，把 `command` 换成安装生成的绝对
路径：Windows 虚拟环境是 `<venv>\Scripts\tax-analysis-mcp.exe`，
macOS/Linux 是 `<venv>/bin/tax-analysis-mcp`。在装它的虚拟环境里激活后，
用 `where tax-analysis-mcp`（PowerShell 同）或 `which tax-analysis-mcp`
可以看到实际位置。Windows 路径写进 JSON 时，反斜杠要写成 `\\`，或者直接
改用 `/`。

连上后调用 `engine_health`：`status` 为 `ok`，`engine_root` 指向已安装
包内的 `runtime/`，税种、算式骨架、模板计数均非零。随后可以问「研发费用
加计扣除比例是多少」。例如调用 `classify_tax_question` 会得到（节选）：

```json
{
  "type": {"type": "lookup", "label": "政策查询", "confidence": 0.85},
  "context": {"missing": ["time", "entity", "place", "scale"]},
  "must_answer": "结论 + 效力位阶 + 文号 + 生效时点"
}
```

HTTP 模式：

```bash
TAX_MCP_TRANSPORT=http TAX_MCP_HOST=127.0.0.1 TAX_MCP_PORT=8765 tax-analysis-mcp
```

PowerShell 用 `$env:TAX_MCP_TRANSPORT='http'; tax-analysis-mcp`。端点为
`http://127.0.0.1:8765/mcp`；端口被占用时改 `TAX_MCP_PORT`。

从源码开发时不必安装，在仓库根目录运行：

```bash
PYTHONPATH=src python -m tax_engine_mcp.server
```

不带环境变量时走 stdio，进程会等待客户端握手，终端没有提示属正常。

## 环境

- Python ≥ 3.10；依赖见 `pyproject.toml`。
- `playwright` 只在税屋 WAF 挑战那条路用，浏览器取系统已安装的
  Edge/Chrome/Firefox。

## 工具

| 任务 | 工具 | 联网 | 说明 |
|---|---|---|---|
| 健康检查 | `engine_health` | 否 | 引擎根、税种数、骨架数、模板数 |
| 判型 | `classify_tax_question` | 否 | 题型、前提缺口、追问探针 |
| 依据覆盖 | `check_evidence_coverage` | 否 | 覆盖率；`evidence=null` 是「没检」，`[]` 是「检了没有」 |
| 分析计划 | `plan_tax_analysis` | 否 | 题型、缺口、每轮目标与取数清单 |
| 检索 | `search_tax_policies` | 是 | 六源聚合；只有 `sources=["npc"]` 支持 `page>1` |
| 单源检索 | `search_gov_policies` | 是 | 税务总局／法规库，可叠加九组收窄维度 |
| 读法规 | `read_law` | 是 | 元数据与可选正文段落 |
| 官方解读 | `find_official_interpretations` | 是 | 只查 `.gov.cn` |
| 实务线索 | `find_related_web_pages` | 是 | 税屋／公众号／全网；`engine_error` 非空表示全网那一路没取成，不代表没有相关内容 |
| 取数定级 | `gather_tax_evidence` | 是 | 逐轮检索与定级，返回证据、轮次与缺口 |
| 整题底稿 | `analyze_tax_question` | 是 | 检索 + 定级的结构化底稿，不写最终答复 |
| 算式 | `expand_calculation` | 否 | 参数化展开，不含现行税率 |
| 类案 | `find_tax_cases` | 是 | 台账；`order` 只认 `relevance/date_desc/category/date_asc` |
| 账套巡检 | `audit_ledger` | 否 | 账套规则检查 |
| 稽查模拟 | `simulate_inspection` | 否 | 生成问询单元 |
| 空白受理表 | `blank_intake_form` | 否 | 填好合同名称后交给 `intake_contracts` |
| 合同受理 | `intake_contracts` | 否 | 多合同九维度覆盖 |
| 条款对账 | `reconcile_contracts` | 否 | 条款与履行记录对账 |
| 内控生成 | `generate_controls` | 否 | 九要素内控制度文本 |

所有工具都标了 `readOnlyHint=true`（只读）；联网工具另标
`openWorldHint=true`。

## 资源与 Prompt

| URI | 内容 |
|---|---|
| `tax://tax-types` | 33 个税种/专题：别名、上位法、权限 |
| `tax://calc-skeletons` | 算式骨架、公式、输入与结果 |
| `tax://output-templates` | 模板键与标题 |
| `tax://templates/{key}` | 单份模板原文 |

11 个 prompt 把输出模板与取证纪律封在一起：`analysis_six_sections`、
`option_comparison`、`fill_in_the_blank`、`rule_statement`、
`entitlement_matrix`、`calculation_writeup`、`insufficient_evidence`、
`risk_self_check`、`ledger_review`、`intake_review`、`inspection_interview`。
每个 prompt 接受可选的 `question`，返回「先取证、按模板成稿、缺依据写明」
的完整指令。

## 错误语义

引擎错误保留原始 `code`，例如：

```text
[page_not_supported] 多源聚合没有页码维度：...；details={"sources": [...]}
[unknown_question_type] ...；details={"allowed": [...]}
```

模板不存在时，客户端收到形如 `[unknown_template] ...` 的协议层错误。

## 限制

- 不提供付费模型解读；`analyze_tax_question` 只跑检索与分层。
- `analyze_tax_question` 与 `gather_tax_evidence` 是同步长任务，可能耗时
  数十秒；客户端要设置足够超时。`max_per_round` 控制每轮取数上限（1–200）。
- 多源聚合没有页码维度，`page>1` 在发请求前直接报错。
- `search_gov_policies` 的 `fgk` 一路没有页码；`body` 只对 `fgk` 有效。
- 部分源取数失败写在 `meta.errors`，不是「该源没有内容」。

## 独立运行与测试

包内 `src/tax_engine_mcp/runtime/` 是取数脚本、判据注册表与输出模板的只读
快照，`MANIFEST.json` 记录每个文件的 SHA-256；运行时不查环境变量，也不找
同级目录。

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

用例走 FastMCP 内存传输，不联网、不调用付费模型。
