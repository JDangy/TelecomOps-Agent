# V3 Structured Task State — 交付汇报

## 1. Task State 最终设计

`agents/harness/task_state_v3.py` — `TaskStateV3`（对象级 registry）：

```
StateEntry: {object, field, value, source, source_ref, seq, superseded_by}
存储: key = "object.field" → 历史链（写入顺序，最新为当前）
```

三级查询（`latest(tool, param, proposed_value)`）：
1. **精确键**（tool/param 命名空间 + bare）
2. **ID 字段实体解析**——对象去重后：唯一实体 + proposed 不同 ID → 用已知 ID 约束（错 ID 拦）；多实体 → 歧义放行
3. **bare 唯一才用**——同名字段存在对象级条目（多对象歧义）→ bare 不命中 → 放行

## 2. 业务对象表示

- 用户请求 → 动作动词命名空间：`transfer_request.amount` / `refund_request.amount` / `payment_request` / `deposit_request` / `limit_request`
- 工具实体 → `<type>_<id>` 对象：`user_lm83h7k2p5.id` / `card_dbc_12345.status` / `account_sav_lm83.gold.balance`
- KB 规则 → 工具命名空间：`close_bank.reason.allowed_values` / `transfer.amount.max`

## 3. 来源记录

每条必带 `source ∈ {user, tool, knowledge}` + `source_ref`（user_message / tool_result / doc_id）。可回答"这个值为什么是 500、谁说的"。

## 4. 用户改口

同 key 重写 → 旧条目标 `superseded_by`（历史保留供 trace），**判定只看最新**。改口语境（"make it $300"）沿用最近金额对象命名空间。实测：`transfer $500 → make it $300` → 当前 300、链长 2、旧值不再拦。

## 5. Tool Result 入状态

`ToolResultStateExtractor`：按 `N. Record ID:` 分块 → 每块实体锚点 → 块内 ID/status/balance/amount/issue_reason 挂到该实体。修了两个真实 bug：tau2 单结果是裸 ToolMessage（无 name 字段，此前全部静默丢失）；schema 文档混入（`account_id: string` 被当数据）→ 构造层不变量：类型标记值（string/number/…）任何来源一律拒入库。

## 6. Knowledge 入状态

`KnowledgeStateExtractor`：packet constraints → 规则命名空间（`tool.param.allowed_values` / `.max` / `.min`）。只收明确规则（V2.2 起 KA prompt 已限制 DEFINITE RULES ONLY）。

## 7. Decision Agent 读取

`_task_state_block()`：当前有效条目（superseded 排除）渲染进 system 消息尾部，**≤900 chars 预算**，条目式（`- key = value [source: user (from user_message)]`）。不是全量注入，也不是每次全塞——状态变化自动反映。

## 8. Harness / Recovery 使用

- `TaskStateValidator`：`latest(tool, param, proposed_value)` 三级查询（含实体解析与歧义保护）；`confirmed_key` 进拒绝消息
- Recovery 闭环（V2.3）不变：拦截消息 `confirmed_value + correction: set` 行已含状态来源（`conflicts with user value`）
- 三源接线：DecisionAgent 每条消息喂状态（user/ToolMessage/MultiToolMessage）、_do_ask 后 constraints 刷新（同时进状态与 kb_validator）

## 9. 真实案例（v3_final smoke，真实 LLM）

| task | 结果 | 意义 |
|---|---|---|
| **095 多金额歧义原案** | **1.0 SUCCESS（历史首次！）** | V2.2 被它误拦（0/6），V3 状态分对象后通过；0 拒绝 0 污染 |
| 037 对照 | 1.0 SUCCESS，0 拒绝 | 对照组保持干净 |
| 080 多实体长序列 | 0 task_state 冲突 | V2.1 曾 51 拒绝/58 loop；剩 5 次拒绝全为 kb/schema 层，任务本身 max_steps（LLM 级，非状态层） |

状态活证据（095 trace）：`user_lm83.current_balance` 892.45→347.2→156.8 消费降级 supersede 链；`account_sav_gold.amount` 450→15000 更新链——**长任务里状态追踪与业务演进完全同步**。

## 10. 误绑 / 污染 / 膨胀检查

- 误绑：**0**（多对象同名 → 不命中放行；唯一实体错 ID → 拦——单元 8/8）
- 污染：'string' 占位 **0 条**（构造层防御，从提取启发式升级为不变量）
- Context 膨胀：DA 注入 ≤900 chars；matched 9 + 35 次放行 verdicts 正常

## 11. 是否成为统一系统

**是。** User / Tool Result / Knowledge → **一份对象级状态** → DA（按需切片读取）/ Harness（三级查询校验）/ Recovery（confirmed_value 修正）全部围绕同一状态工作；Trace 记录 state_write/update（对象/新旧值/来源）全程可回溯"Agent 当时到底知道什么"。三个原本各自为政的模块（V2.2 task-state、V2.3 recovery、KA constraints）现在共享一个事实源——**模块间不再各存一份对世界的理解**。

诚实边界：080 的 kb 拒绝（5 次）来自 KA constraints 仍偶有噪声（已由类型防线兜底）；TaskStateValidator 与 kb_validator 的约束读取存在受控冗余（后者仍读 V2.2 列表——统一到 V3 查询是后续小整理项，不影响行为）。

## 复现

```bash
python run_eval.py --tasks configs/banking_v3_smoke.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v3_final --seed 42
```
