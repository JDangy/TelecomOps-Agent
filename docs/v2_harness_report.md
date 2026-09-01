# V2 Evidence-Grounded Action Harness — Targeted Diagnostic 报告

## 研究问题

> Agent 知道该做什么、动作序列能走完，但执行工具时关键参数填错
> （金额/reason code/账户类型/枚举值）——在执行前拦住并纠正，能否救活
> 这类"流程对但值错"的任务？

## 实现（全部完成）

### Harness abstraction（agents/harness/，与 memory/agent 三层分离）

```
Decision Agent
      ↓ proposed tool call
ActionHarness.process()
  ├─ SchemaValidation      （复用工具 openai_schema：required/type/enum）
  ├─ EvidenceParameterValidation（proposed vs Evidence Packet grounded_values）
  ├─ 全过 → validate_only 放行 → 原样交 orchestrator 执行（消息流兼容 V1.2）
  └─ 拦截 → 结构化错误回传（field/proposed/allowed/evidence_value+doc_id）
           → DA 自行修正重试
```
- 拒绝分类：schema_violation / evidence_mismatch；缺证据 = not_grounded
  **放行**（防误拦）；user-context 参数 = not_applicable 放行
- Permission/Retry/Budget/Safety policy：接口预留，NotImplementedError 不实现
- Trace v2 新事件：action_proposed / action_validation / action_rejected /
  action_executed（含 proposed/final arguments、failed_fields、evidence doc 引用）
- 指标：proposed/validated/rejections/schema失败/evidence_mismatch/
  corrected_after_rejection + verdict_counts

### Evidence Packet 增强
- 新增 `grounded_values: [{name, value, value_type, source_doc_id, unit}]`
- KA prompt 强制 **verbatim 保留**：文档写 `account_closure` 就输出
  `account_closure`，禁止改写成 "closure"/"关闭账户"；数值保留精确数字与单位
- `_parse_packet` 提取时丢弃缺 name/value 条目（不伪造）

### 插入位置
DecisionAgent.generate_next_message 拦截层：业务工具调用在交给
orchestrator 执行前过 harness（validate_only 模式——通过的调用原样放行，
保持与 V1.2 的消息流完全兼容；rejection 以 error ToolMessage 回填 agent
state，由 DA 修正后重新发起）。

## Targeted set（configs/banking_paramfail6.json）

5 个"动作走完但值错"任务 + 1 对照组（task_037：V1.2 成功、含 9 个枚举值
参数——验证 harness 不误伤）。

## 结果

| task | V1.2 | V2 | 拒绝 | schema | evidence | 修正成功 |
|---|---|---|---|---|---|---|
| 054（22枚举值）| 0.0 | 0.0 | 1 | 1 | 0 | 1 ✅ |
| 053（21枚举值）| 0.0 | 0.0 | 0 | 0 | 0 | — |
| 095（12枚举值）| 0.0 | 0.0 | 0 | 0 | 0 | — |
| 070（账户类错）| 0.0 | 0.0 | 1 | 1 | 0 | 1 ✅ |
| 080（49枚举值）| 0.0 | 0.0 | 0 | 0 | 0 | — |
| 037（对照组）| **1.0** | **1.0** | 0 | 0 | 0 | — ✅不误伤 |

success: V1.2 1/6 → V2 1/6（持平）；对照组未误伤（0 false rejection）。

### 关键发现：harness 只拦到了 wrapper 层

仅有的 2 次拒绝都是 `call_discoverable_agent_tool` 的 **`arguments` 类型**
问题（schema 声明 string、DA 传 dict）——DA 修正后成功执行 ✅。
但**零次 evidence_mismatch**，原因（结构性的）：

1. **Discoverable tools 的真实参数藏在 wrapper 内层**。banking 域的
   关键业务动作（冻结/关卡/转账/reason code）都通过
   `call_discoverable_agent_tool(tool_name, arguments)` 间接执行，
   arguments 是 JSON 字符串——harness 校验的是外层 wrapper 的
   `{tool_name: string, arguments: string}`（无 enum/数值约束），
   **穿不进内层的 reason/amount/账户类**
2. **not_grounded 占绝对多数**（34~63 次/任务）：Evidence Packet 的
   grounded_values 覆盖的参数名与 wrapper 层参数（tool_name/arguments）
   天然对不上——evidence 比对在正确的层（业务参数）发生，而 harness
   在 wrapper 层校验
3. LLM/时间成本：LLM +10~60%、wall +5~110%（修正轮 + 更多 handoff）

### 对照组验证 ✅
task_037（V1.2 成功任务）在 V2 下仍成功、零拒绝——**无误拦**。
真正的 false rejection = 0（两次拒绝都是真 catch：类型错误 + DA 修正成功）。

## 诚实结论

**机制本身按设计工作**（单元验证 8/8：enum 拦截/evidence mismatch/防误拦/
修正回传全通过；现场也实现了 catch→修正→成功执行），**但 V2 当前形态
在 banking 域抓不到目标错误**——不是因为校验逻辑弱，而是因为：

> 该域的关键参数错误的现场（discoverable tool 内层 JSON）在 wrapper
> 之后的动态执行层，harness 处于 wrapper 之前，看不到内层参数。

修正路线明确（下一步建议，未实施）：
- **穿透 wrapper 校验**：当 tool 为 call_discoverable_agent_tool 时，
  解析其 arguments JSON 字符串，对内层工具（list_discoverable_agent_tools
  可枚举其 schema）递归应用两类校验——这是让 evidence_mismatch 真正
  生效的唯一路径
- grounded_values 命名对齐：KA prompt 引导用工具参数语义名
  （如 transfer_reason 而非 reason_code）

## 回退状态

默认 agent 仍为 two_agent（V1.2）。`two_agent_harness` 保留在 registry，
harness 代码/trace/指标全部保留——wrapper 穿透是增量改动。

## 复现

```bash
python run_eval.py --tasks configs/banking_paramfail6.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v2_param6 --seed 42
```
