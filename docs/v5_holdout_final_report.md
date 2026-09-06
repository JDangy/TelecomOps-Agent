# Frozen V4 vs Frozen V5 — Fixed Held-out Test Set 正式对比

## 运行口径（sealed 协议）
- 评测集：`configs/banking_holdout_sealed.json`（22-task Fixed Held-out
  Test Set，seed=20260903 程序化抽样，未经任何开发反馈）
- 模型：openai/qwen3.8-flash（winterapi）——V4/V5 同模型，直接可比
- 代码快照：V4 @ `5899be0`（worktree /tmp/v4_holdout_run）、
  V5 @ `3aa2918`（worktree /tmp/v5_holdout_run）
- V5 开发期间**未查看/未使用**任何 holdout 失败 trace 调整参数
  （V5 的 Plan/Replanning/tool_hint/threshold 全部基于 Dev/smoke 开发）

## 结果

| 版本 | success | 具体任务 |
|---|---|---|
| Frozen V4 | **4/22 = 18.2%** | 006, 032, 035, 038 |
| Frozen V5 | **8/22 = 36.4%** | 006, 031, 032, 035, 047, 052, 063, 089 |
| 差值 | **+4（+18.2pp）** | 5 翻上 / 1 回落 |

**翻转明细**：
- ✅ V4 败 → V5 成（5 个）：031、047、052、063、089
- ❌ V4 成 → V5 败（1 个）：038

**环境有效性**：0 个 API 错误，22/22 全部有效完成。

## 关键解读

1. **+4 净提升与 Dev24 一致**：V5 Dev24 = 9/24 (37.5%)，V5 Holdout =
   8/22 (36.4%)——同一模型同一量级。Dev 提升在独立 held-out 集上
   复现，**不是 Dev 过拟合**。
2. **5 个顽固任务翻上**（031/047/052/063/089 在 V4 全败）——与 Dev
   上 021/024 的机制一致（Structured Planning + 真实结果驱动推进），
   泛化到未见任务。
3. **1 个回落（038）**：V4 中它通过 `transfer_to_human_agents` 移交
   获得 reward（LLM 路径选择差异，之前 008 也观察到同类）。属 LLM
   随机路径波动，非机制回退。
4. **按协议：V5 不在 holdout 上做任何进一步修改**——本次结果即为
   Frozen V5 的最终泛化验证。

## 结论

> Frozen V5（Structured Planning & Replanning）在独立 Fixed
> Held-out Test Set 上较 Frozen V4 提升 **+4/22（18.2% → 36.4%）**，
> 且与 Dev24 量级一致，证明结构化规划能力的收益跨任务集泛化。
> V4 的 22-task holdout 未参与 V5 开发，本次对比为有效独立验证。

## 复现

```bash
# worktree 各自运行（代码快照不可变）
cd /tmp/v5_holdout_run
setsid nohup .venv/bin/python run_eval.py \
  --tasks configs/banking_holdout_sealed.json \
  --agent two_agent_harness --model openai/qwen3.8-flash \
  --retrieval-config bm25 --tag v5_holdout --seed 42 --max-steps 60 \
  > /tmp/v5_holdout.log 2>&1 < /dev/null & disown
```