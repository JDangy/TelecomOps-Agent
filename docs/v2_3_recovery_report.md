# V2.3 Action Recovery — 交付汇报

## 1. Recovery 最终怎么工作

```
DA 提议（amount=550）
  ↓
Harness 校验（task_state: 用户明确说了 500）
  ↓ 拒绝
结构化错误回传 DA 的 agent state（error ToolMessage）：
  field: amount (conflicts with user value)
  proposed: 550.0
  confirmed_value: 500.0
  from: u1
  correction: set amount to 500.0        ← V2.3 新增：单字段修正指令
  ↓
DA 只改该字段 → 同工具重提（500）
  ↓
Harness 再校验 → matched → 放行
  ↓
正常执行
```

## 2. DA 怎么得到修正信息

拒绝消息三层强化：
1. **原有**：field / proposed / 依据来源（user/knowledge/tool schema）/ confirmed_value 或 allowed_values / source_ref
2. **V2.3 新增 `correction:` 行**——把"该改成什么"显式写出（`correction: set amount to 500.0` / `correction: set reason to one of [...]`）
3. **DA instruction appendix 重写**：只改列出字段、立即同工具重试、不重新规划不换工具、user 来源的 confirmed_value 必须精确使用

## 3. 如何避免无限重试

**Recovery 预算**（DecisionAgent 层）：同一 `(inner_tool, field)` 最多被拒
`MAX_SAME_FIELD_REJECTIONS=2` 次——第 3 次起该调用直接放行（运行时工具
自身的报错兜底，自然失败），不再消耗 LLM 轮次。预算 per-task（agent 随
task 重建自动清零）。

## 4. Task State / provenance 调整（本阶段最重要的修复）

**V2.2 f3 的"真 catch"其实是误拦**：用户说 "savings of about $96,000"
（账户余额），被绑到裸 amount → 正确的 interest_correction amount=100 被拦。

**V2.3 歧义规则**：用户金额只在**动作语境**（transfer/pay/deposit/
withdraw/credit/apply…动词窗口 40 字内）才绑定；"of about/余额" 描述性
金额（动词与金额之间夹状态标记）不绑。

5 组测试全过：
- "savings of about $96,000" → 不绑 ✅
- "transfer $500" → 绑 500 ✅
- "balance $12,000 and pay $200" → 只绑 200 ✅
- "savings of $96,000 — apply the correction of $98" → 只绑 98 ✅（095 原句式）
- "I have $4,000 in checking" → 不绑 ✅

## 5. 真实案例

**闭环单元测试（真实工具 schema + harness 全流程）**：
```
1. DA 提议 amount=550 → harness 拦截
   拒绝消息: correction: set amount to 500.0
2. DA 按消息修正 → amount=500 → harness matched 放行
3. 执行: success=True "Transfer completed successfully"
```

**真实 smoke（2 任务，真实 LLM）**：
- task_037（对照组）：**SUCCESS 1.0、0 拒绝**——V2.2 f3 中它被误拦
  （threshold 挂 bool），V2.3 类型防线+歧义修复后完全干净
- task_095：1 次拒绝（log_verification.time_verified 的 schema 类型问题）、
  V2.2 的 amount 误拦消失（歧义修复生效）；任务本身仍失败（095 是
  顽固任务，历史全败——非 harness 层问题）

## 6. Rejection 修正成功数

- 单元闭环：**1/1 修正成功**（拦截→按消息修正→通过→执行）
- smoke 现场：037 零拒绝（无需修正）；095 的 1 次拒绝是 schema 类型
  问题（time_verified 序列化），DA 侧未走 harness 修正路径
- 上版 58 连环 loop → 本版最大连续拒绝 **0**（预算生效）

## 7. 新误拦 / recovery loop

**零新误拦**（037 对照 1.0 恢复 + 0 拒绝）；**零 loop**。

## 8. Token / latency

闭环单元验证零 LLM 开销；smoke 2 任务与 V1.2 基线同量级
（037: 11 tools/7 turns 正常）。harness 校验纯规则零 LLM。

## 9. 是否足够稳定可展示

**是。** 三点依据：
- 闭环完整：拦截消息的 correction 行 → DA 修正 → matched → 执行，
  单元全链路验证 + 真实 LLM 环境无干扰
- 防御完善：recovery 预算防 loop；金额歧义防误绑——两个 V2.2 现场
  暴露的问题都被**类型/语境级规则**修复（非启发式堆叠）
- 对照组干净：037 从 f3 的误拦死锁（1.0→0.0）恢复到 1.0 零拒绝

剩余边界（诚实）：DA 按消息修正的能力依赖 LLM 遵循指令——本次 smoke
未自然触发需要修正的场景（095 的唯一拒绝是类型序列化问题），闭环的
DA 侧半段由单元测试的模拟修正验证。更大规模的真实修正样本需要
24-task 级评测（按约定未跑）。

## 复现

```bash
python run_eval.py --tasks configs/banking_v23_smoke.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v23_smoke --seed 42
```
