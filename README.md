# tax-analysis-mcp

给支持 MCP 的 AI 客户端装上一套税务分析工具。装好后，模型在回答税务问题时
可以实际调用工具去检索法规、读回原文、核对文号与时效、展开算式、查类案，
并把材料整理成分层底稿；还可以检查账套、模拟稽查问询、受理合同和生成内控
制度。它不需要模型只凭记忆作答。

服务端提供三类能力：

- **19 个工具**：模型可以调用的函数，返回结构化 JSON。
- **3 个静态资源 + 1 个模板资源**：税种词表、算式骨架、模板清单和模板原文。
- **11 个 prompt**：把成稿模板与取证纪律封成可直接使用的指令。

所有工具都是只读的：不修改账套、合同或任何远端状态，也不调用付费模型。

## 它解决什么问题

普通聊天模型回答税务问题时，容易把旧文件、已废止条款或记忆中的比例当成
现行规定。这个服务把「查」和「写」分开：

1. 先用 `classify_tax_question` 判断题型和题面缺口；
2. 用 `plan_tax_analysis` 看清该分几轮、每轮查什么；
3. 用检索和阅读工具拿到现行文件、正文、官方解读和实务线索；
4. 用 `gather_tax_evidence` 或 `analyze_tax_question` 得到带分层的底稿；
5. 最后用 prompt 里的模板成稿，依据不足时明确写缺口。

模型仍然负责理解和表达，但依据来自工具返回的内容，而不是凭记忆补写。

## 快速开始

```bash
cd tax-analysis-mcp
python -m pip install -e .
```

在客户端的 MCP 配置里加入：

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
macOS 或 Linux 是 `<venv>/bin/tax-analysis-mcp`。在装它的虚拟环境里激活后，
用 `where tax-analysis-mcp`（PowerShell 同）或 `which tax-analysis-mcp`
可以看到实际位置。Windows 路径写进 JSON 时，反斜杠要写成 `\\`，或者直接
改用 `/`。

不同客户端的配置字段可能略有差异，但核心是告诉客户端启动哪个命令；如果
客户端支持 URL 连接，也可以改用下面的 HTTP 模式。

连上后先调用 `engine_health`：`status` 为 `ok`，`engine_root` 指向已安装
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

## 一次典型对话会调用什么

下面是工具之间的常见顺序，客户端和模型可以按实际情况调整：

1. 用户问：「我们公司招用退役士兵，能扣减哪些税？」
2. 模型调用 `classify_tax_question`，得到题型（优惠资格判定）、缺口和应
   覆盖的依据类型。
3. 模型调用 `plan_tax_analysis`，看到检索轮次与每轮目标。
4. 模型调用 `search_gov_policies` 或 `search_tax_policies` 检索文件，再用
   `read_law` 读正文；需要官方解读时调用 `find_official_interpretations`。
5. 模型调用 `analyze_tax_question`，得到 `primary`、`supporting`、
   `practice`、`to_verify` 四层底稿和缺口。
6. 模型或用户选择 `analysis_six_sections` prompt，按模板成稿。

如果检索中有源取数失败，工具会在 `meta.errors` 里说明；模型应把这个缺口
写进答案，而不是当成「该源没有内容」或补写未检索到的规定。

## 工具清单

19 个工具按任务分组。表格里的「联网」指是否会访问外部站点。

### 判型、覆盖与计划

| 工具 | 返回什么 | 联网 |
|---|---|---|
| `engine_health` | 引擎根、税种数、骨架数、模板数 | 否 |
| `classify_tax_question` | 题型与把握度、判型依据、前提缺口、追问探针、应覆盖的来源与结论要件 | 否 |
| `check_evidence_coverage` | 必备依据逐项状态（已满足、缺、不适用、待核）与覆盖率；`evidence=null` 是「没检」，`[]` 是「检了没有」 | 否 |
| `plan_tax_analysis` | 判型结果，加上每轮检索的目标、数据源与调用方式 | 否 |

### 检索与法规

| 工具 | 返回什么 | 联网 |
|---|---|---|
| `search_tax_policies` | 六源聚合的条目与 `meta.source_summary`、`meta.errors`、`meta.gaps` | 是 |
| `search_gov_policies` | 税务总局站内或法规库的条目，可按成文日期、时效、税种、文种、年份、文号、效力等级收窄 | 是 |
| `read_law` | 法规元数据，以及可选的正文字段 `text.paragraphs` | 是 |
| `find_official_interpretations` | 只来自 `.gov.cn` 的官方解读 | 是 |
| `find_related_web_pages` | 税屋、微信公众号与 360 全网的实务线索；`engine_error` 非空表示全网那一路没取成 | 是 |
| `gather_tax_evidence` | 计划、逐轮证据、定级结果与缺口；不写成稿 | 是 |
| `analyze_tax_question` | 分层底稿（主依据、其他法定依据、执行口径与实务认定、待核对线索）；不写最终答复 | 是 |

`search_tax_policies` 的六个来源是 `npc`、`chinatax`、`so360`、`shui5`、
`wechat`、`ctax`。多源聚合没有页码维度；要翻页时把 `sources` 限定为
`["npc"]`。`search_gov_policies` 的 `fgk` 一路没有页码，`body` 只对
`fgk` 有效。

### 算式、类案与检查件

| 工具 | 返回什么 | 联网 |
|---|---|---|
| `expand_calculation` | 代入步骤、分档与结果；缺参数报 `missing_params`，多余参数报 `invalid_parameter` | 否 |
| `find_tax_cases` | 税务总局查处通报与曝光典型的候选、过滤和去重台账 | 是 |
| `audit_ledger` | 八要素风险条目、判不动清单、参数缺口与数据缺口 | 否 |
| `simulate_inspection` | 逐问的六字段问询单元与三类缺口；没有作答也能生成 | 否 |
| `blank_intake_form` | 一份空白多合同受理表，含一条合同占位记录 | 否 |
| `intake_contracts` | 合同清单、九维度覆盖、待核抽取与没交的要素 | 否 |
| `reconcile_contracts` | 合同条款与履行记录之间的差异、无法确认条目和证据缺口 | 否 |
| `generate_controls` | 九要素内控制度文本；没有诊断时按通用模板生成并列出假设 | 否 |

`audit_ledger` 的 `ledger` 顶层形态是 `{"行": [...]}`，每行至少包含行号、
科目和金额；具体规则字段见包内 `runtime/data/ledger_rules.json` 的
「账套字段」。`blank_intake_form` 返回的 `document` 需要先填写合同名称等
字段，再交给 `intake_contracts`。

所有工具都标了 `readOnlyHint=true`；联网工具另标 `openWorldHint=true`。

## 资源与 Prompt

资源是客户端可以直接读取的只读数据，不需要模型先调用工具：

| URI | 内容 |
|---|---|
| `tax://tax-types` | 33 个税种或专题：名称、别名、上位法、路由归属与备注 |
| `tax://calc-skeletons` | 6 个算式骨架：公式、输入与结果字段 |
| `tax://output-templates` | 模板键与标题 |
| `tax://templates/{key}` | 单份模板原文 |

11 个 prompt 把输出模板和取证纪律封在一起：

| Prompt | 用途 |
|---|---|
| `analysis_six_sections` | 按「分析六段式」成稿 |
| `option_comparison` | 按「逐条比对式」成稿：选项与依据逐条对照 |
| `fill_in_the_blank` | 按「填空式」成稿：逐项落值并标依据 |
| `rule_statement` | 按「规则陈述式」成稿：条件、例外与生效时点 |
| `entitlement_matrix` | 按「资格判定条件矩阵式」成稿 |
| `calculation_writeup` | 按「计算式」成稿：参数来源、步骤与结果 |
| `insufficient_evidence` | 依据不足时成稿：写明缺口与已查范围，不补写规定 |
| `risk_self_check` | 按「风险自检专用输出」成稿 |
| `ledger_review` | 按「账套巡检条件块」成稿 |
| `intake_review` | 按「多合同受理条件块」成稿 |
| `inspection_interview` | 按「稽查模拟问询式」成稿 |

每个 prompt 接受可选的 `question`，返回的是一段完整指令：先调用工具取证，
再按模板成稿，取数失败或依据不足要写明。prompt 本身不取数；取证仍要靠
上面的工具。

## 错误语义

引擎错误保留原始 `code`，在工具调用中表现为带前缀的错误文本，例如：

```text
[page_not_supported] 多源聚合没有页码维度：...；details={"sources": [...]}
[unknown_question_type] ...；details={"allowed": [...]}
```

模板不存在时，客户端收到形如 `[unknown_template] ...` 的协议层错误。资源
读取失败同样是协议层错误，不会返回空字符串。

## 限制

- 不提供付费模型解读；`analyze_tax_question` 只跑检索与分层，不写最终答案。
- `analyze_tax_question` 与 `gather_tax_evidence` 是同步长任务，可能耗时
  数十秒；客户端要设置足够超时。`max_per_round` 控制每轮取数上限（1–200）。
- 多源聚合没有页码维度，`page>1` 在发请求前直接报错。
- `search_gov_policies` 的 `fgk` 一路没有页码；`body` 只对 `fgk` 有效。
- 部分源取数失败写在 `meta.errors`，不是「该源没有内容」。
- 所有工具只读，不写入账套、合同或外部系统。
- HTTP 模式默认监听回环地址，不带认证与 TLS；要跨机器访问，需自行加反向
  代理和访问控制。

## 独立运行与测试

包内 `src/tax_engine_mcp/runtime/` 是取数脚本、判据注册表与输出模板的只读
快照，`MANIFEST.json` 记录每个文件的 SHA-256；运行时不查环境变量，也不找
同级目录。

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

用例走 FastMCP 内存传输，不访问外部站点，也不调用付费模型。
