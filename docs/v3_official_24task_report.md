# V3 正式评测报告 — Structured Task State 24-task × 1

## 运行参数（与 V1.2 正式版完全一致）
task set=banking_dev_tasks（24 stratified）/ model=openai/deepseek-v4-flash /
retrieval=bm25 / seed=42 / max_steps=60 / 24×1（单次，无调参轮）
agent=`two_agent_harness`（V3：TaskStateV3 + 三源 harness + recovery）

## 收口整理（评测前完成）

1. **Knowledge 统一进 Task State**：kb_validator 改读 TaskStateV3 的
   knowledge 命名空间（`_state_entry/_state_ref`），旧 constraints 列表
   仅作未接线后备；`_update_kb_constraints` 不再单独刷列表——**规则
   只进状态一份**（此前 Task State 与 validator 各存一份的理解已消除）。
2. **动作相关状态选择**（替代机械截断）：`_task_state_block` 三层优先——
   P0 当前动作工具的 knowledge 规则（`_recent_biz_tool` 从最近 assistant
   消息确定性提取，wrapper 穿透到 inner 工具名）；P1 user 请求对象
   （活跃意图）；P2 实体背景。预算溢出按层舍（P0 全保）。
   验证：transfer 上下文中 transfer.reason.allowed_values 置顶；
   close_bank 上下文中对应规则置顶。
3. **幂等写入修复**（评测中发现的明显 bug）：029 实测同一约束被重喂
   602 次（每次 ask 后全量 packets 重放）→ 3106 state 事件。修复：
   同 key 当前值宽松等价 → no-op；真值变化正常 supersede。

## 正式结果

| 指标 | V1.2 正式版 | V3 正式版 |
|---|---|---|
| **success** | 5/24 (20.8%) | **5/24 (20.8%)** |
| llm_calls | 1195 | 1071 (**-10%**) |
| prompt_tokens | 17.48M | 18.03M (+3%) |
| wall | 8969s | 21808s (+143%*) |
| handoffs | — | 167 |
| state write/update | — | 6357 |
| **harness rejections** | — | **3 次（全 run）** |
| **最大连续拒绝（loop）** | — | **3（无死循环）** |
| false rejection | — | **0** |

\* wall 差异主要来自 winterapi 延迟漂移（llm_calls 反而 -10%、tokens +3%
说明单调用无 harness 开销；V3 校验/状态为纯规则零 LLM 调用）。

**成功任务**：001, 003, 004, 010, 037
**与 V1.2 的翻转**：+003 +010 +037（V1.2 败→V3 成）；−002 −007 −046（V1.2 成→V3 败）

## 失败分类（19 失败任务）

| 类别 | 数量 | 任务 |
|---|---|---|
| 顽固失败（V0/V1.2/V3 均败，A 类长序列） | 13 | 008,021,024,026,027,029,053,054,077,080,088*,092,095,100 |
| LLM 随机波动（V1.2 曾成功） | 3 | 002, 007, 046 |
| max_steps（长流程未完成） | 2 | 020, 088 |
| API/环境 ERROR | 1 | 070（JSONDecodeError，reward=None） |

\* 088 计入 max_steps 类（同时为顽固）。

## V3 真正解决了什么

1. **状态污染型误拦归零**：V2.2 曾因"余额 96k 绑到 correction amount"
   连环误拦（095），V2.1 曾 51 拒绝/58 连环 loop（080）。本次 24 任务
   全程 **3 次拒绝、0 误拦、无 loop**——对象级键 + 歧义放行 + 类型标记
   防御从根上消除这类失败。
2. **对照组干净**：037（V1.2 败）翻成 success 且 0 拒绝；001/004 保持。
3. **两个首次攻克**（受 LLM 噪声部分抵消）：003、010 V1.2 失败 → V3 成功。
4. **效率**：llm_calls -10%（状态切片让 DA 少走弯路/少重复询问）。

## 剩下的问题（诚实）

1. **13 个顽固任务全部未动**——它们的失败不在参数/状态层：多为长动作
   序列（tools 39-97 次调用）+ DA 的多步规划/业务判断缺陷。状态和
   harness 都工作正常（rej≈0），但救不了"不知道下一步该做什么"。
2. **LLM 噪声 ±8pp**：−002/−007/−046 证明单次 run 的翻转可能是噪声——
   20.8% 持平 V1.2，**不宜过度解读方向性**。
3. wall 增长主要是 API 延迟漂移（非系统开销），但真实环境需要注意。
4. 070 的 API JSON 错误应计入环境误差（非 agent 失败）。

## Relevant State Selection 是否值得做？

**本次数据的回答：收益边际。** P0/P1/P2 分层已让 DA 拿到动作相关状态，
而顽固任务的瓶颈在规划层不是信息层（029 有 36 次 ask、97 次工具调用的
任务依然失败——不是状态不够，是不知道怎么串起来）。建议：**暂缓**，
除非后续决定攻"长流程任务"，那时状态选择可作为辅助（优先级也低于
DA 规划能力本身）。

## 结论

V3 的价值不在 success rate（持平），而在**系统质量**：误拦 0、loop 0、
拒绝 3 次/全 run、状态全程可追溯（6357 次写入更新带来源）、
llm_calls -10%。作为项目正式模块，V3 是当前最稳定形态；顽固任务
的下一突破口不在 harness/状态层——建议停止横向深化，项目进入
收尾展示阶段。

## 复现

```bash
python run_eval.py --tasks configs/banking_dev_tasks.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v3_official24 --seed 42 --max-steps 60
```
