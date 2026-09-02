# Trace Demo — task_037（V3 Structured Task State 全链路）

> 来源：runs/v3_final_/traces/task_037.{json,v2.json}（真实 LLM，seed=42）
> 场景：信用卡客户对账单争议——身份验证 → 卡号信息 → 两笔争议提交 → 补卡地址确认
> 结果：**reward 1.0（SUCCESS）**，全程 Harness 0 误拦

## 一图看懂（系统关系）

```
User ──┬────────────────────────────┐
       │ "I don't recognize some of │
       │  these charges…"           │
       ↓                            │
Knowledge Agent ── KB 检索 ──► Evidence Packet（constraints: 明确规则）
       ↓
Structured Task State ────────────┐
  · user_890389b165.id / status（实体，来源=tool）
  · balance 更新链（supersede 历史）
  · dispute_reason.allowed_values（规则，来源=knowledge）
       ↓
Decision Agent ── 读取动作相关切片（P0 规则 / P1 意图 / P2 背景）
       ↓
Harness ── 三级查询校验（精确键 / 实体解析 / 歧义放行）
       ↓
13/13 动作放行 → orchestrator 执行 → SUCCESS
```

## 阶段 1 — 客户接触与身份验证

**用户**：*"my balance seems higher than expected… my name is Fatima Al-Hassan,
email is coffeelover_fati@protonmail.com"*

**工具**（身份查询）返回记录 → **Task State 写入**：
```
state_write: user_890389b165.user_id  = "890389b165"   [source: tool]
state_write: user_890389b165.id       = "890389b165"   [source: tool]
```
后续所有动作引用该用户时，Harness 的实体解析都能对照这个确认 ID——
**用错 ID 会被立即发现**（唯一实体场景 → task_state_conflict）。

## 阶段 2 — 知识规则入库（KA handoff）

Agent 调用 `ask_knowledge_agent`（dispute 流程）→ Knowledge Agent 检索
KB → packet 的 **constraints（只含明确规则，不含案例答案）** → Task State：
```
state_write: file_credit_card_transaction_dispute_4829.dispute_reason.allowed_values
             = ["unauthorized_fraudulent_charge","duplicate_charge","incorrect_amount",
                "goods_services_not_received","goods_services_not_as_described",
                "canceled_subscription_still_charging","refund_never_processed"]  [knowledge]
state_write: ...card_action.allowed_values = ["keep_active","cancel_and_reissue"]
```
这 7+ 条规则是后续 dispute 调用的**合法性边界**——但不替 DA 决定当前案例
选哪个值（V2.1 的教训）。

## 阶段 3 — 实体状态随业务演进（supersede 链）

查询/操作过程中余额变化，Task State 同步追踪（**旧值保留历史、判定只用最新**）：
```
state_write : user_890389b165.current_balance  null      → 1389.98
state_update: user_890389b165.current_balance  1389.98   → 4212.70
state_update: user_890389b165.current_balance  4212.70   → 1173.27
state_update: user_890389b165.current_balance  1173.27   → 927.36
```

## 阶段 4 — 核心动作校验（wrapper 穿透到业务参数层）

Agent 提出（外层是 discoverable wrapper）：
```
call_discoverable_agent_tool(agent_tool_name="file_credit_card_transaction_dispute_4829",
  arguments={transaction_id, card_action:"keep_active",
             dispute_reason:"unauthorized_fraudulent_charge", …})
```

Harness 链条（trace v2 可回放）：
```
action_proposed   → outer call_discoverable_agent_tool
action_resolved   → inner file_credit_card_transaction_dispute_4829 + inner args
                    （wrapper 穿透：44 工具注册表 + signature/docstring schema）
action_validation → 每参数三级查询:
                    · card_action ∈ allowed_values（knowledge 规则）→ 合法
                    · dispute_reason ∈ allowed_values → 合法
                    · transaction_id/card_id → 实体解析对照 → matched
                    · 无依据字段 → not_in_task_state → 放行（有明确依据才拦）
（无 blocking）→ 放行 → orchestrator 执行
```
13 个动作全部如此通过——**0 次误拦**（对照组红线保持）。

## 阶段 5 — 地址变更与收尾

**用户**：*"can you send it to my work address instead?"* → Agent 更新
寄送地址参数 → 同样经过校验放行 → 执行。

**最终**：`task_end: reward=1.0, termination_reason=user_stop`

## 这个 demo 说明什么（面试话术要点）

1. **三源汇一**：user 的身份/意图、tool 返回的实体与余额、KA 的明确规则——
   全部进**一份对象级 Task State**（`object.field = value [source]`），
   不再是散落在对话里的非结构化文本。
2. **拦截边界清晰**：本任务 0 误拦，因为校验只在"有明确依据"时才拦——
   合法值集合用 knowledge、确认值用 user/tool、其余放行。DA 的语义选择
   （如选哪个 dispute_reason）不被越权干涉。
3. **可回溯**：任何时刻都能回答"Agent 当时知道什么、某值是谁说的"——
   每条状态带 source，每次更新留 old→new，每个动作有 resolved→validated→
   executed 链。
4. **对照失败案例**（V2.2 之前的 095）：余额 $96,000 被误绑到 correction
   amount → 正确调用被拦 → 死循环。V3 的对象级键 + 类型标记防御 + 歧义
   放行从根上消除了这类"状态污染型误拦"，095 在 V3 首次 SUCCESS。

## 复现

```bash
python run_eval.py --tasks configs/banking_v3_smoke.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v3_final --seed 42
# trace: runs/v3_final_*/traces/task_037.v2.json
# 可视化: python eval/trace_v2_view.py runs/v3_final_*/traces/task_037.v2.json
```
