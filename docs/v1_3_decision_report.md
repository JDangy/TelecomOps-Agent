# V1.3 Procedural Efficiency — 通过/回退决策报告

## 失败分析结论（Decision Agent 最大问题）

对 12 个跨版本全败任务的 trace 逐一分析 + evaluator 要求对照：

**最大问题 = 长动作序列执行持久力不足（procedural execution）**

```
动作数 ≤6  →  成功率 86%（19/22）
动作数 >6  →  成功率 1/10
动作数 >8  →  成功率 0/8
```
- 成功任务平均 2 个 evaluator 动作，失败任务平均 11.7 个
- max_steps 死亡任务全部停在 31 轮 assistant 响应：
  纯文本解说轮占 23%~58%、能并行的调用一半没并行、unlock/call 分离成两轮
- 这不是检索问题（这些任务 recall 达标）、不是单步决策问题（动作名基本都对）
  ——是 10~34 步流程走不完全程

失败模式细分（12 个顽固任务）：
- **A 类·序列执行不全**（8 个）：要求 6~34 个严格按序动作，完成率 6%~94%
- **C 类·参数/细节错**（4 个）：动作 100% 做完但值错（task_070 选错账户类、
  task_095 金额错、task_053 某参数错、task_004 reason 错）

## V1.3 改进内容（最小可回退）

唯一变量 = Decision Agent 的 instruction（`two_agent_efficient`）：
1. 多对象同流程 → 一轮并行发出所有独立调用
2. unlock 与后续 call 同轮合并（参数已知时）
3. 先讲完整计划再连续执行，不为每步单开解说轮

**回退方式：`--agent two_agent` 即 V1.2 原样**（V1.2 的 instruction/code 未动）。

## 实验结果

### diag7（7 任务常规集）

| | V1.2 | V1.3 |
|---|---|---|
| success | 3/7 | **4/7** |
| 翻转 | — | +3（003/046/004）/ -2（010/037）|

### stubborn6（顽固失败专项，公平对照：V1.2 与 V1.3 同 seed 各跑一次）

| | V0 | V1.2 | V1.3 |
|---|---|---|---|
| **success** | 0/6 | **0/6** | **0/6** |
| 动作完成率均值 | 62% | 61% | 70% |

逐任务动作完成率（三个版本）：

| task | V0 | V1.2 | V1.3 | V1.3 vs V1.2 |
|---|---|---|---|---|
| 080 (34动作) | 71% | 62% | 35% | ↓ |
| 092 (21动作) | 43% | 57% | **71%** | ↑ |
| 026 (11动作) | 64% | **100%** | **100%** | = |
| 054 (17动作) | 94% | 29% | 53% | ↑ |
| 070 (5动作) | 100% | 20% | **60%** | ↑ |
| 020 (6动作) | 100% | **100%** | **100%** | = |

效率指标（V1.2→V1.3）：handoffs 34→56、检索 133→126、LLM 276→302、
wall 2483→2887s——**无效率收益，反而略增**。

## 诚实结论

**V1.3 的批处理机制部分生效但未转化为 success**：
- 正面：完成率均值 61%→70%；092/054/070 三个任务完成率明显上升；
  026/020 在 V1.2/V1.3 都能做到 100% 动作完成
- 负面：最难的 080 完成率反降（62%→35%）；efficiency 提示让 DA 更倾向
  handoff（+65%）和多轮探索，抵消了轮次节省；success 全程 0/6

**顽固失败任务的真死因比"轮次不够"更深**：即使动作完成率 100%
（V1.2 的 026/020），reward 仍为 0——evaluator 还检查动作的**参数值**
（金额/reason/账户类）与 DB 终态，agent 走完流程但值错。
另一些任务（080）死因是流程中途发散。

## 建议：回退 V1.3，不合并为默认

理由：
1. diag7 的 +1 在 ±8pp 随机噪声内（翻转 5 个任务，方向混乱）
2. stubborn6 上 success 0/6 无变化，效率指标反而恶化
3. 最难任务（080）完成率反降——instruction 干扰了对超长流程的
   稳健执行

保留内容（有价值不浪费）：
- 失败分析本身：**顽固失败的真根因是"动作参数值错误"与"中途发散"，
  不是轮次预算**——这把 V2 的方向从"省轮次"修正为"参数正确性"
  （如 Evidence Packet 强化数值事实的精确提取 + DA 执行前核对）
- `two_agent_efficient` 保留在 registry（不删），供后续组合实验参考
- 4 组新 trace 数据入库

按约定停止。未实现：Verifier / 第三个 Agent / Long-term Memory / Skill。

## 复现

```bash
# V1.3 stubborn6（本次新跑）
python run_eval.py --tasks configs/banking_stubborn6.json --agent two_agent_efficient \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v13_stub6 --seed 42
# V1.2 对照（本次新跑）
python run_eval.py --tasks configs/banking_stubborn6.json --agent two_agent \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v12_stub6 --seed 42
```
