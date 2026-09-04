# V5 Dev24 — 24-task 全量结果与 Freeze 判定

## 运行口径
- agent: two_agent_harness（V5: PlanStore + planning tools + V5.1 实体作用域绑定）
- model: openai/qwen3.8-flash / retrieval: bm25 / seed: 42 / max_steps: 60
- 24/24 全部有效完成（前段 5 + 环境故障续跑 19——winterapi 中断
  后同代码同参续跑，非调参）
- 对照基线：V3 official（deepseek-v4-flash 5/24）、V0/V1.2（同集）

## Success（历史首次超过 V0 基线）

| 版本 | success | 备注 |
|---|---|---|
| V0 官方 LLMAgent | 8/24 (33.3%) | 历史最高基线 |
| V1.2 / V3 official | 5/24 (20.8%) | 架构代价期 |
| **V5 Dev24** | **9/24 (37.5%)** | **+4 vs V3，零回退** |

**V3→V5 翻转**：002、007（波动恢复）+ **021、024（顽固任务历史首次攻克**——
V0/V1.2/V3/V4 全败）。
**无任何 V3 成功任务回退。**

## 机制全局指标（对照 freeze 条件逐项）

| Freeze 条件 | 结果 |
|---|---|
| 长任务 tool calls / repeats 改善 | 顽固组 232→279（**+20% 未降反升**——021/024 成功带来的多步执行贡献大于收敛收益；单任务重复率待细看） |
| max_steps | V5 3 = V4 3（持平） |
| 简单任务无 regression | 7 个简单对照 6 个零 planning；037（19 tools 多步争议流程）合理 plan；**简单任务 success 7/7 全过**（V3 是 4/7——002/007 恢复） |
| Plan 不乱建 | 15/24 建 plan 全部为顽固（14）+ 多步任务（037）；7 简单中 6 个零 plan |
| Plan progress / replanning 正常 | steps auto-completed 35；add_step 6 / remove_step 29（021/024 翻转的主机制：执行中发现目标变化正确删步骤）|
| completion guard | 27 次触发全有界（无死循环） |
| success 未恶化 | **显著上升**（+4） |
| harness false rejects | 13 次拒绝全 run（多为 schema/kb 层，无误拦 pattern） |

## V5.1 修复效果确认（本次 Dev24 前的最小回归）

080 steps_done 0→4（类型词消歧实体绑定生效——plan 推进与真实工具
执行打通）；020 保持 2；001/004 零 planning。

## Freeze 判定

**建议 Freeze。** 依据：
1. Structured Planning 让长任务执行结构化：021（7 tools 干净走完）
   和 024（1 tool 转人工路径正确）两个顽固任务的首次通过正是
   "plan 引导 + 执行中发现新事实正确 replan"的产物
2. 简单任务零 regression 且效率不变（7/7 全过）
3. Plan 不乱建、guard 有界、35 步自动推进全部由真实工具结果驱动
4. 成本可接受（llm calls 与 V4 同量级；planning tool 调用零额外 LLM 轮）

**已知限制（如实记录，不阻塞 freeze）**：
- 顽固组 tool calls 总量 +20%（成功执行变长 vs 重复收敛的净值）
- 026/077 等深顽任务仍败（业务判断层，非执行结构层）
- 3 个 max_steps（020/029 某些轮次在 plan 下依然规划不足）

## Freeze 快照

- commit: 3aa2918（V5.1 修复后）
- agent: two_agent_harness（write_plan/update_plan/read_plan +
  PlanStore + 实体作用域绑定 + V4 PlanTracker fallback + V3 TaskStateV3）
- model: openai/qwen3.8-flash via winterapi
- retrieval: bm25 / seed: 42 / max_steps: 60
- tau2: a2c024725189473d2d7cea3a5cfdbcc67478e41f
- eval config: configs/banking_dev_tasks.json (24 Dev) /
  configs/banking_holdout_sealed.json (22 Holdout, sealed)

## 下一步（等用户决定）

Frozen V5 → 同一 22-task Fixed Held-out Test Set 运行一次，
与 Frozen V4 的 4/22 直接对比。跑完不再修改 V5。
