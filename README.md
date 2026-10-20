# tax-analysis-mcp

税务问题分析引擎的 MCP 服务端。项目自带引擎的运行时快照（取数脚本、判据
注册表与输出模板），不读取外部引擎目录；整个目录拷到哪里都能单独运行。

- Python ≥ 3.10；已验证 Python 3.13.14、FastMCP 4.0.8。
- 默认传输是 stdio；`TAX_MCP_TRANSPORT=http` 可起 Streamable HTTP。
- 运行时依赖 `requests`、`urllib3`、`playwright`（`playwright` 只在税屋
  WAF 挑战那条路用，浏览器本体取本机已装的 Edge/Chrome/Firefox）。

## 启动

```bash
cd tax-analysis-mcp
PYTHONPATH=src python -m tax_engine_mcp.server
```

HTTP 模式：

```bash
TAX_MCP_TRANSPORT=http TAX_MCP_HOST=127.0.0.1 TAX_MCP_PORT=8765 \
  PYTHONPATH=src python -m tax_engine_mcp.server
```

HTTP 模式下 MCP 端点是 `http://127.0.0.1:8765/mcp`（FastMCP 默认路径，
本项目未改）；客户端配 `/mcp/` 会先收到 307 跳到 `/mcp`。8765 被其它
程序占用时改 `TAX_MCP_PORT`；端口被占时报的是
`[winerror 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试`，
不是权限问题。

stdio 与 HTTP 两种传输都已实测：15 个工具、3 个静态资源 +
`tax://templates/{key}`、6 个 prompt，`engine_health` 回到的 `engine_root`
是本包内的 `runtime/`。

安装成命令（可选）：

```bash
python -m pip install -e .
tax-analysis-mcp
```

stdio 客户端配置示例（路径按部署位置替换）：

```json
{
  "mcpServers": {
    "tax-analysis": {
      "command": "python",
      "args": ["-m", "tax_engine_mcp.server"],
      "env": {
        "PYTHONPATH": "/path/to/tax-analysis-mcp/src"
      }
    }
  }
}
```

## 运行时快照与独立性

- 本包 `src/tax_engine_mcp/runtime/` 是引擎的只读快照：24 个取数与判据脚本、
  7 个判据注册表、2 份输出模板与风险框架。引导层只把这一个目录放进
  `sys.path`，不 import 任何外部引擎包。
- `runtime/MANIFEST.json` 记录快照对应的引擎版本与每个文件的 SHA-256；
  `tests/test_server.py::test_runtime_snapshot_matches_manifest` 逐文件核对，
  缺文件或改一个字节都会报红。
- 本项目另带两份实现：`tax_service.py`（服务契约层：参数边界、错误码、
  结果形状）与 `tax_doc.py`（正文下载解析、官方解读检索）。CLI、API、MCP
  各带一份，三份要求逐字节相同，由
  `tests/test_server.py::test_vendored_copies_match_fingerprint` 用 SHA-256 钉住。
- 与引擎仓库没有运行时依赖：引擎升级后，本项目的行为不变，直到快照被重新
  同步并更新清单。`test_no_external_engine_lookup` 禁止代码回退到环境变量
  或同级目录查找。

## 工具面

工具按任务组织，不按引擎脚本切；一次调用完成检索、读法规、展开算式或
生成检查件。

| 工具 | 联网 | 说明 |
|---|---|---|
| `engine_health` | 否 | 引擎根、税种数、骨架数、模板数 |
| `classify_tax_question` | 否 | ① 判型、前提缺口、追问探针 |
| `check_evidence_coverage` | 否 | ② 依据覆盖率；`evidence=null` 是「没检」，`[]` 是「检了没有」 |
| `plan_tax_analysis` | 否 | ⑥ 之前的检索计划 |
| `search_tax_policies` | 是 | ③④ 六源聚合；只有 `sources=["npc"]` 支持 `page>1` |
| `read_law` | 是 | ⑧ 法规元数据 + 可选正文段落 |
| `find_official_interpretations` | 是 | 只查 `.gov.cn` 的官方解读 |
| `analyze_tax_question` | 是 | ⑥ 检索 + 定级的结构化底稿，不写最终答案 |
| `expand_calculation` | 否 | 参数化算式展开，不含现行税率 |
| `find_tax_cases` | 是 | 类案台账；`order` 只认 `relevance/date_desc/category/date_asc` |
| `audit_ledger` | 否 | 账套巡检 |
| `simulate_inspection` | 否 | 稽查模拟问询 |
| `intake_contracts` | 否 | 多合同受理（九维度覆盖） |
| `reconcile_contracts` | 否 | 条款与履行记录对账 |
| `generate_controls` | 否 | 九要素内控制度文本 |

写类工具全部标了 `readOnlyHint=true`：它们只读引擎与外部站点，不修改任何
账套或远端状态。联网工具另标 `openWorldHint=true`。

## 资源与 Prompt

资源放只读的小词表与模板，避免把大段文本塞进工具参数：

| URI | 内容 |
|---|---|
| `tax://tax-types` | 33 个税种/专题：别名、上位法、权限 |
| `tax://calc-skeletons` | 算式骨架、公式、输入与结果 |
| `tax://output-templates` | 模板键与标题 |
| `tax://templates/{key}` | 单份模板原文 |

Prompt 把引擎的输出模板与取证纪律封在一起：

`analysis_six_sections`、`calculation_writeup`、`entitlement_matrix`、
`risk_self_check`、`inspection_interview`、`intake_review`。

每个 prompt 接受可选的 `question`，返回「先取证、按模板成稿、缺依据写明」
的完整指令。

## 错误语义

引擎错误统一转成 `ToolError` / `ResourceError`，文本保留引擎的 `code`，
例如：

```text
[page_not_supported] 多源聚合没有页码维度：...；details={"sources": [...]}
[unknown_question_type] ...；details={"allowed": [...]}
```

资源模板不存在时，客户端收到的是 MCP 协议层错误，消息以
`unknown_template` 开头；这是 FastMCP 对资源读取错误的包装方式。

## 测试

```bash
PYTHONPATH=src python -m pytest -q
```

用例走 FastMCP 内存传输，覆盖工具目录、JSON Schema、静态资源与模板资源、
Prompt 嵌入、离线工具、错误映射；联网工具只验参数边界或 monkeypatch 换掉
服务函数，不联网、不调用付费模型。

## 边界

- 不提供付费模型解读；`analyze_tax_question` 只跑引擎的检索与分层。
- `analyze_tax_question` 是同步长任务，可能耗时数十秒；客户端应设置足够超时。
- 多源聚合没有页码维度；`page>1` 在发请求前直接报错。
- 部分源取数失败写在 `meta.errors`，不是「该源没有内容」。
- 重试策略仍归引擎取数层，服务端不另加一套。
