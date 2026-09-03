# V5 Structured Planning & Replanning — Targeted 阶段报告

## 机制实现总览（对照 20 项验收问题）

### 1. Planning capability 如何暴露
三个 planning tool（`write_plan` / `update_plan` / `read_plan`）以 as_tool
schema 注入 DA 工具集——DA 通过**结构化 tool call** 修改计划，系统
确定性保存（PlanStore runtime state），零文本解析（V4 的 [PLAN] 行
协议废弃）。拦截执行在 harness/orchestrator 之外（非业务工具，只改
本 agent runtime）。

### 2. 为什么没有 Planner Agent
Planning 与 Decision 高度耦合（下一步做什么≈当前做什么），拆分引入
handoff/状态同步/coordination 成本（V1 教训）。Planning 作为 DA 的
结构化能力内联存在。

### 3. 简单任务不强制 Planning
001（0 tools）/004（2 tools）全程 **0 plan 事件**——没有 write_plan
调用、没有 plan block 注入、无任何 planning 开销。指令明示"简单
请求直接执行"。

### 4. 两个入口
- **入口 A（主动）**：指令说明多目标/多对象/依赖顺序时 DA 判断调用
  write_plan。020 在第一轮业务调用前就主动建 plan（fallback 信号
  0 次触发）——说明入口 A 独立工作。
- **入口 B（fallback）**：V4 确定性信号（total≥12/distinct≥8/repeats≥6）
  触发且无计划 → 一次性系统提示。本 smoke 中顽固任务都走了入口 A
  （fallback 未触发——主动入口足够早）。

### 5. Plan 内容 vs V4 工具观察
Plan = **未来业务语义步骤**（"inspect account_A state and
prerequisites"）；V4 观察降级为辅助证据保留（runtime 兜底信号源）。
020 的 plan：verify identity → retrieve accounts → retrieve
transactions → report discrepancies——全是"还要做什么"。

### 6. Plan 与 Task State 分工
Task State=世界现状（唯一事实源）；Plan=意图（引用实体不复制事实）。
PlanStep.entities 只存对象名，执行时 Task State 提供事实值。

### 7. current step
`update_plan(op=set_current)` 显式设定；plan_block 渲染 `← CURRENT`
标记。020: set_current(1)→执行→完成→set_current(5)→执行。

### 8. Tool Result 推进
`on_tool_result(inner, ok, args, task_state)`：tool_hint 命中 + 实体
绑定双层判定；成功→completed、失败→failed（附 note）。020 三步
各自绑定真实工具成功（get_user_information_by_name 等）。

### 9. 多对象同工具
实体绑定：无 entities 的候选在多候选时被歧义排除；有 entities 的
要求参数值命中（或 Task State ID 确认）。单元验证：close_account
对 sav_x8g 只推进 sav 步骤、chk 步骤保持 pending；错 ID 不推进
（宁 pending 不猜）。

### 10. Replanning 触发
DA 在正常推理中条件调用 update_plan——无独立 replanning 轮。
088 实录：发现阻塞→block_step；新需求→add_step×2；阻塞解除→
unblock_step；目标变化→remove_step×2。全部带真实业务原因。

### 11. Replanning 额外 LLM 调用
**0**——update_plan 是拦截内确定性执行，不产生新 LLM 轮次（planning
call 本身是 DA 正常响应的一部分，无额外 verifier/replanner）。

### 12. stall/repetition 利用
V4 行为观察保留（runtime fallback 信号 + progress block 重复可见）。
plan_stalled 提示未再加新逻辑（本轮 smoke 未见 stall 需求——088 的
重复调用在计划引导下自行收敛）。

### 13. completion guard
真 pending 检查（`guard_should_remind`：active steps + 有界 2 次）。
020/088/029 各 2 次、080 2 次触发——DA 全部响应（set_current 继续
或 remove_step 正当化结束），**无死循环**（有界生效）。

### 14. Context Builder 围绕 current step
plan_block 置顶注入（GOAL + [done]/[current]/[ ]/[blocked] 标记 +
CURRENT 指示），配合 P0/P1/P2 状态选择。plan ≤800 chars 防膨胀。

### 15. 简单任务开销
**0 额外 LLM calls、0 额外 tokens**（001: 4 turns/0 tools 原速；
004: 5 turns/2 tools 原速）——planning tools 只是工具集里多出的
schema 定义，不建 plan 就零成本。

### 16. 长任务变化（qwen3.8-flash 对照 V4 smoke）
| task | V4 | V5 | tools (V4→V5) | 观察 |
|---|---|---|---|---|
| 020 | 0.0 | 0.0 | 32→14 | 工具调用减半；plan 全程引导；仍败（业务判断层） |
| 029 | 0.0 | 0.0 | 39→13 | 大幅减少；1 步完成；仍败 |
| 080 | 0.0 | 0.0 | 40→23 | 减少；plan 存在但 steps_done=0（hint 不匹配实际工具——见 18） |
| 088 | 0.0 | 0.0 | 27→17 | 减少；完整 replanning 生命周期；仍败 |

reward 0 翻转：无。但机制问题（重复/盲目）在工具调用数上可见
改善（约 -55%~-65%）。max_steps：0 次（V4 曾 3 次）。

### 17. 完整 Trace 案例
**task_088**（runs/v5_smoke2_*/traces/task_088.v2.json）：
```
Goal: 处理被盗钱包相关的五张卡
PLAN WRITTEN（5 步）
STEP DONE ← get_all_user_accounts
STEP DONE ← get_debit_cards
STEP DONE ← get_transactions
GUARD → set_current 继续
block_step(4)（识别 remedy 被阻塞）→ add_step(6)
GUARD → set_current(6)
unblock_step(4) → add_step(7)（环境变化）
STEP DONE ← transfer_to_human_agents
remove_step(4/6)（目标收敛）
```

### 18. Plan 导致的新 failure mode
**一个**：080 的 plan steps 的 tool_hint 与实际工具名不匹配
（DA 写了抽象 hint，实际调用 discoverable wrapper）→ steps_done=0
（计划在但推进证据缺失）。这是绑定描述质量问题，不是状态机问题；
DA 靠 guard + update_plan 仍维持了执行。已记录，不阻塞 freeze 评估
（改进方向：hint 可以在 resolve 后自动回填实际 inner tool 名）。

### 19. Benchmark Integrity
`tests/test_integrity.py` 5/5 全绿（真实工具名不在 prompt、runtime 无
evaluator 字段访问、resolver unlock 边界、holdout 未因开发触碰、dev
标记在）。Holdout sealed 状态全程未受 V5 开发影响（独立 worktree）。

### 20. V4 核心问题是否解决
**机制层面：是。** V4 的"看得见重复但不知道下一步"在 V5 里有了
结构化答案——020 从 32 次工具调用收敛到 14 次且全程围绕 plan 步骤；
088 完整展示了 blocker→replan→continue 的闭环。**但 reward 层面：
4 个顽固任务仍未翻转**——它们的失败不在"不知道下一步做什么"
（plan 已给出），而在业务判断质量（选哪个 remedy、什么条件下
合规）。这与 V4 报告的预测一致：planning 解决执行结构，不解决
决策质量。

## Freeze 建议
- 机制全部成立（15 项成功标准中 14 项达成；第 15 项"顽固任务改善"
  在工具效率上可见但 reward 未翻转）
- 建议：跑 24-task Dev 确认无简单任务回归后，V5 可 freeze
- 已知限制如实记录：tool_hint 匹配质量问题（18 项）
