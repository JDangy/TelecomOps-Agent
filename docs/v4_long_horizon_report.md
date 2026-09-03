# V4 Adaptive Long-Horizon Execution — 交付报告

## 机制实现（全部零额外 LLM 调用）

### 1. 简单任务不折腾（验收 1）
无前置分类器、无强制 Planning。升级信号纯确定性计数：
`total≥12 / distinct≥8 / repeats≥6`（v3_official24 实测标定：成功任务
0-12 calls/0-4 repeats，顽固 15-27/6-20）。未触发 → 完全 V3 路径。
（001: 0 calls、004: 2 calls 全程未升级，直接完成。）

### 2. 何时进入 Plan Mode（验收 2）
任一计数阈值命中即升级（工具结果到达时计数）——执行中升级、不要求
第一次预测复杂度。升级时捕获最近 user 消息为 goal。

### 3. Plan 保存与更新（验收 3）
`ExecutionPlan(goal, steps)` + `PlanTracker`（朴素状态机，非 DAG）。
**V4.1 关键修正**：LLM 对 `[PLAN]` 行记法遵守率实测为 0（080 激活后
0 行输出）→ 进度主信号改为**工具行为自观察**：每个内层工具的调用
次数/成败/完成态自动派生（"get_cards: called 5 times → done"——
重复直接可见，重复正是顽固任务最大特征）。[PLAN] 行保留为可选细化。

### 4. Tool Result 推动进度（验收 4）
`on_tool_result(inner, ok)`：成功→completed、失败→FAILED（单次失败
必显示）。口头声称（[PLAN-DONE]）只升 in_progress——completed 需
真实成功执行。计划描述想做什么，环境决定完成了什么。

### 5. 防提前结束（验收 5）
纯文本输出 + 显式 [PLAN] 步骤未完 → 注入系统提醒继续（REMIND_LIMIT=2
有界，超后放行尊重用户改需求）。自动步骤不触发提醒（行为观察无
"还该做什么"语义——不替 agent 决定任务完整性）。

### 6. Context Builder（验收 6）
`build_context`：非 Plan Mode=V3 原路径；Plan Mode=memory+state+
progress 三块注入 system + 历史。

### 7. 旧 Tool Result 处理（验收 7）
>24 条消息 + 近窗口(12)之外 + 非 error + 含 Record 块 + 实体已入
TaskState（五重确定性条件）→ 内容替换单行存根。**Trace 全文保留**
（LLM context 才是视图）。单元验证：28 ToolMessage → 22 存根 + 6 近窗口全文。

### 8. Compaction（验收 8）
未实现全对话压缩——两个理由：(a) 顽固任务的 context 峰值 ~40-60k
tokens 远低于模型窗口，无硬需求；(b) V4.1 的存根化已把最大冗余源
（Record 全文重发）处理掉。真需要时作为后备能力再加。

### 9. 新增 LLM/API 调用（验收 9）
**0**。升级计数=消息处理；进度=工具行为观察；context 选择/清理/检查
全确定性；remind 是规则注入非模型调用。

## 验证结果（诚实）

### 10. 长任务指标（验收 10）
| task | V3 | V4 首版 | V4.1 | 说明 |
|---|---|---|---|---|
| 008 | 0.0 | 0.0 | **1.0** | ⚠ 假突破——transfer_to_human_agents 路径选择差异（合法但非机制收益）|
| 020 | 0.0 | 0.0 | 0.0 | 顽固维持 |
| 021 | 0.0 | 0.0 | 0.0 | 顽固维持 |
| 029 | 0.0 | 0.0 | API⚠ | winterapi 中断 |
| 080 | 0.0 | 0.0 | API⚠ | winterapi 中断（diag 版 32 tools 仍败——规划层 LLM 级问题）|
| 088 | 0.0(max_steps) | 0.0(max_steps) | API⚠ | winterapi 中断 |

**结论：V4 机制工作正常（激活/进度/清理全部单元+实测验证），
但对顽固任务的成功率收益未被证明。** 080 diag 单任务显示：Plan Mode
激活、goal 捕获、32 工具执行——依然 max_steps（LLM 的多步规划能力
是瓶颈，上下文可见性不是）。重复率无下降（顽固任务的根本重复来自
不知道下一步做什么，不是忘了做过什么）。

### 11. 简单任务效率（验收 11）
| task | V3 | V4 | V4.1 |
|---|---|---|---|
| 001 | 1.0 | 1.0 | 0.0（LLM 噪声：0 工具，未升级，V4 未介入）|
| 004 | 1.0 | 0.0 | 1.0（噪声回摆：2 工具，未升级）|
| 010 | 1.0 | 0.0 | 0.0（6 distinct<8 未升级——V1.2 也败，V3 的过是噪声）|
| 037 | 1.0 | 1.0 | API⚠ |

未升级的简单任务上 V4 代码路径零差异（机制未介入），波动均为 LLM
噪声（与 V3 official 的 ±8pp 波动带一致）。**V4 无简单任务回归**
——但样本已不足以证明"绝对不变差"。

### 12. 长任务 Trace（验收 12）
最完整案例：`runs/v4_diag_*/traces/task_080.v2.json`——
Plan Mode 激活（goal 捕获 "I want a full refund for all three of
those charges"）→ 32 工具行为观察进度 → context 存根化 → 仍
max_steps。该 trace 同时展示了机制全链路和**边界**（可见性增强
不能替代规划能力），面试价值高。

### 13. 是否形成稳定 Adaptive Long-Horizon Execution 能力（验收 13）
**架构上是、效果上未证明。** 三层渐进（轻量→Plan→清理）全部落地且
零额外调用；但顽固任务的成功率没有变化，008 的翻转是路径选择不是
机制收益。核心发现（诚实）：

> **V4 之前的假设（长任务失败源于"不知道进行到哪/上下文太重"）
> 只对了一半。** 上下文管理（Task State + 存根化）已把"忘记做过
> 什么"解决；但顽固任务卡在"不知道接下来该做什么"——这是 DA 的
> 规划能力问题，不是状态可见性问题。行为观察进度把重复率显式化了
> （agent 能看到自己调了 5 次 X），但 LLM 拿着这个信息依然不会
> 换路——下一突破点在规划层（任务分解/子目标生成），超出本阶段
> "不加新 Agent"的边界。

## 建议
V4 作为**基础设施保留**（对更长 horizon 任务有用，零开销），但
不应宣称"解决长任务"——那需要 DA 规划层的下一个大版本（可能涉及
结构化 plan 生成 = 更强的 LLM 引导，或接受当前 LLM 能力上限）。

## 复现
```bash
python run_eval.py --tasks configs/banking_v4_smoke.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v41_smoke --seed 42
```
（注：v41_smoke 后 4 任务被 winterapi 余额耗尽打断；diag 版 080 完整）
