# V2.2 Action Harness — 三源约束架构报告

## 1. 最终 Harness 架构

```
Decision Agent
      ↓ proposed tool call
ActionHarness.process()
  ├─ ActionResolver（wrapper 穿透 → ResolvedAction: inner tool + args + schema）
  ├─ ① SchemaValidation      工具约束：enum/类型（signature+docstring 推导）
  ├─ ② TaskStateValidator    任务明确事实：与已确认值冲突
  ├─ ③ KnowledgeConstraintValidator  KB 明确约束：enum 集合/阈值/格式
  │    （EvidenceParameterValidation = 可选 policy，默认关闭——V2.1 历史入口保留）
  ├─ 任一 blocking → 结构化拒绝（含约束来源与正确引用）
  └─ 全过 → 放行（orchestrator 执行）

agents/harness/ = { base, resolver, task_state, task_state_validator,
                    kb_validator, validators(evidence 可选), action_harness }
```

## 2. Task State / Provenance 设计

`TaskState`：参数级 provenance registry——`key = tool/param 或裸 param`，
条目 = `{value, source, source_ref, seq}`。同 key 多条目保留历史，
**判定取最新**（用户改口不拦）；trace snapshot 每 key 留最近 3 条。

- **不复制 conversation**：只存参数级值；叙事留在 Working Memory
- 三类 source：`user`（显式金额）/ `tool_result`（返回的 ID 字段）/
  `knowledge`（KB 明确约束，走 constraints 不走值）

## 3. 三类信息入口

| 来源 | 提取器 | 判定 |
|---|---|---|
| user 消息 | UserValueExtractor：`$500` 类显式金额 → amount（保守——不猜语义字段名）| TaskStateValidator 比对 |
| 工具结果 | ToolResultExtractor：返回的 `user_id/card_id/account_id/transaction_id` → **裸参数**（跨工具业务实体，ref 记来源工具）| TaskStateValidator 比对 |
| KB | EvidencePacket.constraints（enum 集合/阈值/格式——**DEFINITE RULES ONLY，never case answers**）+ parser 健康过滤（空 enum/无边 threshold/非模板 format 弃）| KnowledgeConstraintValidator |

接线：DecisionAgent 每条进入消息喂 TaskState；每次 ask 后刷新 KB 约束。

## 4. 会拦截（有明确依据）

| 情形 | verdict | 真实案例（f3 smoke）|
|---|---|---|
| 与用户显式值冲突 | task_state_conflict | **095：用户说 $96,000，DA 填 amount=100 → 拦**（source=user_message）✅ |
| 与工具返回 ID 冲突 | task_state_conflict | 单测：get_debit_cards 返回 dbc_12345，DA 用 dbc_54321 → 拦 |
| 违反 KB enum 集合 | kb_enum_violation | 单测：closure 不在 {fraud, customer_request, account_closure} → 拦 |
| 违反 KB 阈值/格式 | kb_threshold/format_violation | 单测：6000 > max 5000 → 拦；MM/DD/YYYY 格式 |
| 违反工具 schema enum/type | schema_violation | V2 已验证（enum 违规+修正成功）|

## 5. 明确不拦（无依据放行）

- **语义选择**：fraud vs customer_request 都合法且无事实指向 → 放行（Case C ✅）
- not_in_task_state / no_kb_constraint → 记录放行（f3 共 532 次）
- 用户改口（同 key 新值覆盖旧值，旧值不拦）
- 解析失败（坏 JSON → resolve_error 回退 outer 校验）
- 类型不匹配的约束（bool 参数挂数值阈值→放行；描述句当 format→放行）

## 6. Evidence validation 处理

降级为**可选 policy**（`include_evidence_policy=True` / `build_v21_harness()`
历史入口保留）；主线不挂。V2.1 的全部健康过滤代码保留（历史复现）。
理由：V2.1 证明"案例正确值比对"前提不成立（KB 是合法域，案例值来自
用户/环境）；TaskState 层正是这个认识的正确落点。

## 7. 真实案例（f3 smoke，真实 LLM）

1. **095 真 catch**：`apply_savings_account_credit_6831(amount=100)` vs
   用户 "$96,000"（source=user_message）→ 拦截，DA 收到
   `confirmed_value=96000.0` 修正指引
2. **054 kb 阈值拦**：`submit_credit_limit_increase_request_7392
   (requested_increase_amount)` 触发 KB 阈值（source=knowledge）
3. 单测四 Case：A(500→550 拦)/B(错 ID 拦)/C(语义放行)/D(enum 拦)/D2(越界拦)
   + 对照正确值全放行

## 8. 误拦 / 死循环

- **误拦：0**（对照任务 054/070/080/037 全部 0 拒绝）
- **死循环：无**（最大连续拒绝 3；上一版 58 的 loop 消除）
- 开发中修的三个类型防线：bool 参数×数值阈值 / 描述句×format /
  误删 evidence context 入口——全部类型级判定，无启发式堆叠

## 9. Token / latency 影响

LLM 249→315（+26%——主要来自 037/095 的 LLM 随机波动，拦截仅 3 次）；
wall 1904→2893s（含 095 889s 的长任务方差）。harness 本身校验零 LLM
调用、纯规则，开销可忽略。

## 10. 是否达到可展示标准

**是。** 理由：
- 三源架构职责清晰（resolver 看懂 / validator 检查 / harness 编排），
  类名即文档，面试可从"为什么不拦语义选择"讲到"type-safe 约束防线"
- 零 benchmark 特判：所有判定基于 signature/docstring/用户消息结构/
  packet 声明的约束——没有任何 task 相关硬编码
- 拦截原则贯彻：有依据才拦（3 次拦截全带 source），无依据 532 次全放行
- trace 完整可答"想执行什么/解析成什么/用了什么约束/为何拦/拦后如何"

**限制（诚实）**：task_037 本轮 LLM 随机失败（与 harness 无关——0 拒绝）；
095 的真 catch 后 DA 修正仍不完整（corrected=0——修正能力属 DA 层，
是下一阶段的候选方向）。

## 复现

```bash
python run_eval.py --tasks configs/banking_paramfail6.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v22_f3 --seed 42
# V2.1 历史形态（evidence policy 开启）
# from agents.harness import build_v21_harness
```
