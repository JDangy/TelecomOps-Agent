# V0 → V5.1 技术复盘（基于真实代码 / commit / trace / eval）

> 复盘原则：一切结论以 git history、各版本 `results.json`、`*.v2.json` trace、
> 以及 `agents/` 当前实现为准；文档与实现冲突处以实现为准并标注。

---

## 一、V0 → V5.1 演进逻辑（每一步的观察→假设→改动→结果）

### V0 — 单 Agent 基线（commit 49ad3dd ~ afa5043）
- **观察**：原始 LLMAgent 在 24-task banking dev 上 ~25%。
- **假设**：评估驱动（"先造尺子"）——固定协议（seed 42 / bm25 / max_steps 60）让后续改动可归因。
- **实际**：V0 winter 运行 **8/24 = 33.3%**，tools=600（最高），max_steps=7（最多）。
- **结论**：简单任务（查余额、冻结单卡）能过；失败集中在长序列（A 类）与参数值错误（C 类）。
- **新问题**：单 Agent context 混入"业务对话+检索历史"，长任务重复查询（KA 侧 50+ 次 retrieval）。

### V1.1 — Per-Agent Working Memory（5c78d46 → 9bb1ec8）
- **观察**：长任务里 DA 反复忘记已确认身份/卡号（在 conversation 里但没用上）。
- **假设**：给 DA/KA 各自一块 memory，把确认事实外部化，减少重复。
- **实际**：memory 注入 context → **负结果**：handoff +53%，KA tokens +72%。
- **假设成立度**：部分——memory 确实记录了事实，但 **"全部事实无差别注入"成为新的膨胀源**，且"勿重复查询"清单反向激励 Agent 换说法继续搜。
- **教训（被后续支持）**：不加约束的记忆比没有记忆更糟；context 管理必须"按需"，不能"全量"。

### V1.2 — Selective Memory Retrieval/Reuse（da642e5 → 2849996）
- **观察**：V1.1 的膨胀根因是"全量注入"。
- **假设**：按当前 request 只返回相关 memory（hit/partial/miss 判定），能降重复。
- **实际**：**5 项效率指标全部达标**：KA tokens -74%、retrieval -68%。success 未显著升，但效率是真实收益。
- **新问题**：V1.2 的 memory 是**叙事级**（facts/constraints 文本），没有"对象/字段"结构——后续状态混用的根源在此埋下。

### V1.3 — Procedural Efficiency Instruction（ed9cb21 → 277db45）
- **观察**：想通过 prompt 让 DA"更高效"。
- **实际**：建议回退（自我判断），保留为 instruction variant（`two_agent_efficient`）供对照。教训：**prompt 调参在证据不足时是负优化**，后来被 V4 的 `[PLAN]` 行 0 遵守率再次证明。

### V2 — Action Harness（f8dc211 → 5be9bd8）
- **观察**：C 类失败（参数值错）属于**执行前可确定性拦截**的问题。
- **假设**：加执行层校验（schema/证据），拦错参数让 DA 自修。
- **实际**：机制工作（catch→reject→DA 修正→成功，0 误拦），但 `evidence_mismatch=0`——**banking 关键参数藏在 wrapper 内层 JSON 字符串里，harness 只看得到外层**。

### V2.1 — Inner-Parameter Harness Validation（2ab444c → a3f9f96）
- **观察**：V2 抓不到内层参数。
- **假设**：Resolver 穿透 wrapper 拿 inner tool/args/schema（signature+docstring enum）。
- **实际**：穿透成功（44 工具），但 **evidence 校验的根基被推翻**：KA 输出的 grounded_values 有大量病理（假枚举 `reversal_of_charge`、叙述文本当值 `retrieved via ...`、格式占位符 `MM/DD/YYYY`、跨 packet 矛盾），连修 4 轮后仍出现第 5 种形态（`value="user_id"`——把参数名当值）。
- **被推翻的假设**：**"KA 能给每个业务参数输出一个正确值"不成立**。KB 记录的是参数的**合法域**，case-specific 正确值来自用户/环境，不是 KB。修复成本随错误形态线性增长（开放集）。

### V2.2 — 三源约束（904811b → 89c7f00）
- **观察**：V2.1 暴露证据层不可靠；但 user/tool-result 的值是可靠的。
- **假设**：Harness 只拦"有明确依据"的冲突：①tool schema（enum/类型）②Task State 已确认值（user/tool）③KB 明确约束（enum 集合/阈值/格式）。其余放行。
- **实际**：三源校验 + 0 误拦 + 死循环消除（58→3）。**"有明确依据才拦"成为贯穿 V2.2-V5.1 的不变原则**。

### V2.3 — Recovery（7338384 → c3c3840）
- **观察**：V2.2 能拦错，但 DA 被拒后修正率 0（080 曾 51 拒/58 loop）。
- **假设**：结构化错误信息（`correction: set amount to 500`）+ 修正预算（同 field 最多拒 2 次）。
- **实际**：闭环成立（拦→修→过→执行）；关键附带发现：**V2.2 的"真 catch"其实是误拦**（用户 `savings of $96,000` 被绑到裸 amount，正确的 correction amount=100 被拦）—— 直接引出 V3。

### V3 — Structured Task State（63af389 → 93a4a11）
- **观察**：V2.3 的金额误绑根因是**参数级裸键**（amount/card_id 无对象归属）。
- **假设**：对象级状态 `object.field = value [source]` + supersede 历史 + 实体解析，能根治跨对象混淆。
- **实际**：Dev 上 095 首次 SUCCESS（此前 V2.2 被它连环误拦）；**多对象同名不产生裸约束**。同时暴露 tool-result 入库的两个真实 bug（tau2 单结果是裸 ToolMessage 无 name；schema 文档混入 `account_id: string`）。
- **Dev24（v3_official24, deepseek）5/24**——比 V0 的 8/24 低！**重要：V3 的 5/24 是 deepseek 模型，V0 的 8/24 也是 deepseek，模型同，架构差异真实**。V3 的 harness+state 提升了系统质量（0 误拦/loop）但没救活顽固任务，且 2-agent 架构让 LLM calls 翻倍（1195 vs V0 646）。

### V4 / V4.1 — Adaptive Long-Horizon（08343d8 → 42f2caa）
- **观察**：顽固任务失败不是"不知道做过什么"（Task State 已解决），而是**不知道下一步做什么**。
- **假设**：执行中按确定性信号升级到 Plan Mode + Context Builder（旧 tool result 存根化）+ completion guard。
- **实际**：`[PLAN]` 文本协议**遵守率 0**（080 激活后 0 行输出）——V1.3 的教训再次验证。改为**行为观察**（工具调用统计）后仍无 reward 收益：080 激活+goal 捕获+32 工具依然 max_steps。
- **被部分推翻的假设**：长任务失败 = "不知道下一步"，但 V4 证明**即使把"你调了 X 5 次"喂给 LLM，它也不会换路**——重复是"不知道做什么对"的结果不是"忘了做过什么"。

### V5 / V5.1 — Structured Planning & Replanning（49cc6f5 → 3aa2918）
- **观察**：行为观察不是 Plan；需要**未来的步骤**而非**过去的历史**。
- **假设**：给 DA 三个 planning tool（write_plan/update_plan/read_plan）落 runtime state；步骤完成由**真实 tool result + 实体绑定**驱动（多对象同工具不误完成）；条件 replanning（blocker/add_step/remove_step）；completion guard 用真 pending。
- **实际**：
  - **Dev24（qwen3.8）：9/24 = 37.5%**，V3→V5 +4 翻转（002/007 恢复 + 021/024 顽固首次攻克），零回退。
  - **Holdout（qwen3.8）：8/22 = 36.4%** vs **Frozen V4 4/22（同样 qwen3.8）**——+5 翻上/1 回落，**同模型公平对比（V4/V5 holdout 均 qwen3.8-flash），+4 全部归因于 V5 机制，无模型红利**。Dev 与 Holdout 同量级（9/24≈8/22），提升泛化而非 Dev 过拟合。
  - V5.1 修复 080 的 tool_hint 绑定（steps_done 0→4）。
- **已证明**：结构化计划 + 真实执行推进 + guard，确实让长任务执行更有结构（021/024/031/047/052/063/089 首次成功）。
- **未证明（诚实）**：①Dev24 的 V3→V5 对比混模型（V3=deepseek → V5=qwen3.8），其中 +2（002/007）是 qwen3.8 相对 deepseek 的模型红利，不能计入 V5 机制；②但 **Holdout 的 V4→V5 对比同模型（均 qwen3.8），+4 为纯机制红利**——这是 V5 机制提升的最干净证据；③仍有个别任务的成功路径是 LLM 随机选择（024/063 无 plan 也成功；038 V5 回落是 transfer_to_human 路径差异）。

### 模型口径（重要归因前提）
- V0/V1.2/V3 official = **deepseek-v4-flash**；V4.1 后 = **qwen3.8-flash**。
- **V0 8/24（deepseek）vs V3 5/24（deepseek）是模型同的公平对比**——V3 架构在 success 上**不如 V0**，但在质量指标（0 误拦/loop、tools 600→251）占优。2-agent + harness + state 的收益在 success 上为负/中性。
- V5 Dev 9/24（qwen）vs V3 Dev 5/24（deepseek）**混入了模型变化**，不能完全归因于 V5 机制。
- **V4/V5 Holdout 均为 qwen3.8-flash（同模型）**——4/22 → 8/22 的对比完全干净，+4 全部归因于 V5 机制；这是 V5 价值的最强证据。

---

## 二、V5.1 真实系统架构与信息流（沿代码核实）

### 模块职责（agents/ 当前实现）
- **Decision Agent**（`two_agent.py:766 DecisionAgent(LLMAgent)`）：业务决策 + planning + execution。拦截 `ask_knowledge_agent` 与三个 planning tool。
- **Knowledge Agent**：KB 检索 → Evidence Packet（answer/facts/grounded_values/constraints），不规划。
- **Working Memory**（`agents/memory/`）：DA 叙事级 notes（user_constraints/verified_facts/current_goal/open_questions），KA 侧类似；V1.2 的 `retrieve()` 选择性检索仍启用。
- **Task State**（`task_state_v3.py TaskStateV3`）：对象级事实 `object.field=value[source]`，supersede 历史，实体索引。
- **Plan State**（`plan_store.py PlanStore`）：goal + steps（pending/in_progress/completed/failed/blocked/removed），`tool_hint`+`entities` 绑定，`_bound_tool` 观察字段。
- **Context Management**（`context_builder.py build_context`）：非 Plan Mode=V3 原路径（memory block + task state block + 全量历史）；Plan Mode 追加 plan block 且**旧 tool result 存根化**（>24 条消息+窗口外+实体已入 Task State 才替换）。
- **Harness**（`action_harness.py`）：三源校验（schema/task-state/kb constraints），resolver 只读**已 unlock 工具的 tool_info**（integrity 边界），`BLOCKING_VERDICTS` 集中声明。
- **Recovery**（`two_agent.py` 拦截循环）：rejection message 带 `correction` 行 + recovery 预算（同 field 2 次后放行）。
- **Trace**（`eval/instrumentation.py`）：v2 事件流（action_proposed/resolved/validation/rejected/executed、state_write/update、plan_written/updated/step_progressed/completion_guard）。

### 一次 DA 决策时真正进入 context 的内容（代码路径核实）
```
_build_llm_context (two_agent.py:1240)
├─ v5_plan_block   = plan_store.plan_block()（仅 PlanStore.active；≤800 chars）
├─ mem_block       = memory.context_block()（Working Memory _render：user_constraints/
│                     verified_facts/current_goal/open_questions；≤budget）
├─ state_block     = _task_state_block(tool_name=_recent_biz_tool(state))
│                     （Task State 当前有效条目；P0 该工具规则→P1 user 意图→P2 实体背景；≤900 chars）
└─ build_context(state,...)
   ├─ non-plan：system(以上 blocks) + 全量 state.messages（含完整 conversation history）
   └─ plan mode：同 + _compact_tool_results（旧 ToolMessage → 单行存根）
```
- **Conversation History**：始终全量进入（除 plan mode 下旧 tool result 存根化）。未做摘要/截断。
- **Working Memory / Task State / Plan State**：三个 block 都注入（plan 置顶），预算分别控制。
- **证据不足项**：`_recent_biz_tool` 只取最近一次 assistant 的工具调用作排序提示——当 DA 上一轮是纯文本时为空，P0 选择退化。

---

## 三、当前失败任务的 failure modes（trace 归纳）

对 V5 Dev24 失败 + V5 holdout 失败的 trace 逐任务分析，收敛为 **5 个真实 failure mode**：

1. **策略漂移后 Plan 未跟随（plan 变摆设）**——080（max_steps）：plan 写"verify identity → lookup cards → confirm temp limit"，实际执行中途改为 `file_credit_card_transaction_dispute ×3 + close ×4`（业务策略变化），plan 只有 2 次 update（add/block），**新策略从未写回 plan**，plan 与实际执行脱节。`steps_done=0`。→ **plan 存在但不指挥执行**。

2. **过早收尾（guard 被绕过但有界）**——092（user_stop，reward=0）：guard 触发 2 次（上限）后，DA 用 `remove_step(5/6)` 把剩余步骤删掉并给用户收尾，用户道谢结束。**guard 只能提醒 2 次，不能阻止"DA 认为做完了但 evaluator 不认"**。这是当前完成判定（steps completed 由 tool result 驱动）与 evaluator 的**事实口径差异**：DA 自认为 5 张卡处理完（有 tool 证据），但 evaluator 要求全部条件（含 activate 等后续）。

3. **重复 unlock / 高重复工具调用**——080/077/092：`unlock_discoverable_agent_tool` 10-12 次、`get_debit_cards_by_account_id` 8 次。Task State 有实体，但 DA 每次需要信息时重新解锁/重查（KA 侧也 162 次 KB_search@063）。**信息已外部化但 DA 不信任/不检索自己的状态**——Task State 的可信利用没有闭环。

4. **KB 检索爆炸 + 知识未收敛**——063（成功但 162 KB_search / 13 handoff）：DA 反复向 KA 问同类问题，KA 重复检索（v4 038 有 repeated_document_ratio 0.833）。V1.2 的 selective retrieval 缓解了 KA 侧，但 DA 侧"问一次不够"的行为仍在。

5. **业务判断错误（参数/策略层）**——026/053/054/070 等：工具序列执行正确、plan 推进正常，但最终业务决策错（如转人工 vs 自助、dispute reason 选错、余额计算错）。这些不在 harness/plan/state 层可达，属 DA 判断质量。

**主要失败层判断**：当前失败**主要不在执行结构层**（V5.1 已把执行结构做扎实：plan 有、guard 有、state 有、recovery 有），而在：
- **策略-执行一致性**（plan 不指挥 → mode 1）
- **完成判定口径**（DA 完成 vs evaluator 完成 → mode 2）
- **状态利用意愿**（有 state 不用 → mode 3）
- **业务判断质量**（mode 5）

这些共同点是：**确定性机制已经到边，剩下的是 LLM 决策质量与机制之间的"最后一公里"**。

---

## 四、Structured Planning 证明了什么 / 没证明什么

**已证明（trace/eval 支持）**：
- 计划被真实保存并可被工具结果推进（V5.1 实体绑定后 080 steps_done 0→4；021/031/047/052/089 均有 plan+steps_done+remove_step 证据）。
- 条件 replanning 真实发生且带业务原因（088: block→add→unblock→remove；089: add+remove）。
- completion guard 有界不产生死循环，且正确响应（DA 收到提醒后 set_current 继续或 remove_step 收敛）。
- Holdout 上 V4→V5 同模型（均 qwen3.8）提升 +4/22 → **纯机制红利，V5 最干净证据**；Dev 上 V3→V5 混模型（deepseek→qwen），其同量级提升不能全部归因机制（见一）。

**未证明（证据不足）**：
- **Planning 本身对 success 的因果贡献**（Dev 侧）：024/063 无 plan 也成功（LLM 路径 lucky）；Dev24 的 +2（002/007）可能是模型红利。**注意：Holdout 的 V4→V5 对比同模型、+4 纯机制，是 planning 有效性的强证据；但"同模型下 plan vs 无 plan"的细分 A/B 仍缺。**
- **Plan 能显著降低重复**：080/077 在 plan 激活下仍 10+ 次 unlock/8 次 query——plan 没减少重复。
- **Plan 对策略漂移的韧性**：080 证明 plan 在策略变化时**没有**被可靠维护。
- **完成判定的可靠性**：092 证明"tool 证据完成"与"evaluator 完成"仍可能不一致。

---

## 五、成本与效率复盘（可支持的数据）

| 版本 | tools | LLM calls | max_steps | 备注 |
|---|---|---|---|---|
| V0 (deepseek) | 600 | 646 | 7 | 单 agent，最重但 success 8/24 |
| V1.2 (deepseek) | 295 | 1195 | 5 | 2-agent，LLM 翻倍 |
| V3 (deepseek) | 251 | 1071 | 2 | harness+state，tools/max_steps 最优 |
| V5 (qwen3.8) | 306 | 1117 | 3 | planning 阶段 |

- **2-agent 是恒定成本**：LLM calls 相对 V0 翻倍（646→1071+），主要来自 KA handoff（V4 038 里 KA llm=26、DA llm=20，KA 占一半）。
- **tools 251→306（V3→V5）**：planning 阶段 tools 略升——因为部分任务真正做完（021/024 需要更多工具）而非更高效。**无证据证明 V5 降低重复**（080/077/092 的重复调用与 V3 同量级）。
- **prompt tokens**：V3 official prompt=17.5M，V5 Dev=18.0M——plan block + state block 注入是净增但 <3%，不构成膨胀。
- **证据不足项**：跨版本 LLM calls/成功任务比无控制（成功任务本来就更多轮）；context 峰值未系统统计（V4 038 DA max prompt 31k，KA max 87k——KA 的 87k 值得关注但单点样本）。

---

## 六、成熟度评估与技术债

### 已较成熟（可用、有证据）
1. **Task State（V3）**：对象级 + 来源 + supersede + 实体解析——机制完整，是后续所有层的基石。
2. **Harness 三源校验（V2.2）+ integrity 边界（cleanup）**：0 误拦、recovery 预算、unlock 边界、integrity tests 5/5——工程质量高。
3. **Trace v2**：事件源完整（state/plan/harness/action），支持任意失败复盘——本项目最被低估的资产。

### 只有初步效果
1. **Plan State（V5）**：机制全，但"plan 指挥执行"的意愿与维护未闭环（mode 1/2）。
2. **Context Builder 存根化**：实现正确，但触发少（>24 条消息+窗口外），效果未在指标上体现。
3. **Completion Guard**：有界、正确，但只能"提醒 2 次"，挡不住 DA 自主 remove_step 收尾。

### 基本没解决
1. **业务判断质量**（mode 5）——026/053/054/070 等，无任何机制触及。
2. **状态可信利用**（mode 3）——DA 有 Task State 仍重复 unlock/query。
3. **完成判定口径对齐**（mode 2）——DA 完成 ≠ evaluator 完成。
4. **KB 检索爆炸**（mode 4）——KA 侧 162 KB_search 的极端情况。

### 技术债（按严重度）
1. **plan 与策略漂移无联动**：DA 改策略时 plan 不更新——需 plan 的"自检/一致性"机制。
2. **Task State 无"使用证明"**：不知道哪些 state 被 DA 真正读过；重复 unlock 说明有状态不读取。
3. **V4 plan_tracker（行为观察）与 V5 plan_store 双轨**：`plan_tracker` 仍被 context builder 调用（`plan_block`），但 V5 已用 `plan_store.plan_block()`——**两条计划渲染并存**，代码债且易混。
4. **KA prompt tokens 高**（V4 038 KA 87k）：KA 全量 history + memory，无 V5 式 context 管理。
5. **模型未固定到 V0 起**：跨版本 success 归因混入模型红利，复盘与未来对比需显式声明。

---

## 七、下一阶段方向（针对真实 failure mode，未实现）

### 方向 A：Plan-Execution 一致性自检（针对 mode 1/2）
- **问题**：080 策略漂移后 plan 变摆设；092 guard 挡不住自主收尾。
- **方案**：lightweight"计划健康检查"——每次 tool result 后比对"plan 中 current step 的 tool_hint/entities"与"实际执行的 inner tool"是否一致；连续 N 步不一致则注入一条提醒："你的执行与计划偏离，请 update_plan 或说明原因"。不新增 LLM，确定性。
- **为什么值得**：直接解决"plan 存在但不指挥"这一 V5 未证明的核心。
- **影响**：PlanStore + 拦截循环；不改 prompt/agent。
- **成本**：零额外 LLM。
- **最小验证**：080 复现——检查偏离提醒后 plan 是否跟随（steps_done>0 + remove/add 合理）。

### 方向 B：完成判定的双轨对齐（针对 mode 2/3）
- **问题**：DA"完成"与 evaluator"完成"口径不一（092）；DA 不读取自己的 Task State（080/077 重复 unlock）。
- **方案**：①把 Task State 的**实体清单**显式注入 plan（"本任务涉及实体：chk_blue/sav_gold/dbc_blue…，每张卡需完成：activate+close-or-keep"——由 DA 在 write_plan 时声明，之后 harness/guard 可校验"是否有实体未处理"）；②guard 在收尾时按实体清单提醒（"还有实体 X 无 completed 步骤"）。
- **为什么值得**：092/080 类失败都是"做了大部分、漏了尾巴"。
- **影响**：PlanStore 步骤+实体清单；guard 增强；write_plan schema 微调。
- **成本**：零额外 LLM。
- **最小验证**：092/080——看实体清单提醒能否触发继续处理。

### 方向 C：Task State 使用闭环（针对 mode 3/4）
- **问题**：有 state 不读（重复 unlock）；KA 检索爆炸（063 的 162 次）。
- **方案**：①在 `_recent_biz_tool` 之外，把"Task State 已有该实体信息"作为 system 级提示注入（"你已解锁 X，其 schema 在任务状态中"）；②KA 侧复用 V5 context 思路：KA 的 history 旧 packet 存根化（KA 侧目前无存根）。
- **为什么值得**：重复 unlock/query 是最直接的 token/tool 浪费，且是唯一有硬数据的（10-12 次 unlock）。
- **影响**：context_builder（DA+KA 两侧）。
- **成本**：零额外 LLM。
- **最小验证**：080/063——unlock 次数/KB_search 是否下降。

### 方向 D（更远、需实验）：业务判断质量（mode 5）
- 这层目前无机制可达。唯一有依据的假设：**把"业务规则"从 KA 的 narrative 提到 Task State 的 knowledge 命名空间并让 plan 显式引用**（V3 已有 knowledge 规则条目，但 plan 步骤未强制引用）。属于"知识→决策"的最后一公里，复杂度高、证据不足，**建议先做 A/B/C 再看**。

---

## 八、最终判断（区分证据等级）

**已经被 trace/eval 支持的结论**：
1. 确定性执行结构（V2.2-V5.1 harness+state+recovery）显著降低误拦/死循环（58→3→0），且 integrity 干净。
2. Structured Planning 让 5+ 个顽固任务首次成功（Dev 021/024；Holdout 031/047/052/063/089），Dev-Holdout 同量级 → 泛化。
3. 对象级 Task State 根治了 V2.2/V2.3 的跨对象金额误绑（095 首次成功）。
4. 2-agent + harness + state 在 success 上不优于 V0（5/24 vs 8/24，同模型公平对比），但在质量/可控性上占优。

**有迹象但证据不足**：
1. V5 planning 对 success 的因果贡献（混模型红利 + 无 A/B）。
2. Plan 能降低重复工具调用（080/077 数据相反）。
3. Context 存根化的实际收益（触发少）。

**仍是推测的假设**：
1. "业务判断质量"可通过机制改善（D 方向）。
2. 完成判定双轨对齐能显著提 success（B 方向未验证）。
3. Task State 使用闭环能降重复（C 方向未验证）。

**一句话总结**：V0→V5.1 的真实轨迹是"**评估驱动、每步用一个确定性机制解决一个被观察到的具体问题，同时不断用实验证伪自己的假设**"——被推翻的假设（全量 memory、evidence 正确值、`[PLAN]` 文本、行为观察）与成功的机制（对象级 state、三源 harness、实体绑定 plan 推进）同等重要。当前系统在**执行结构层已扎实**，剩下的 5 个 failure mode 集中在**策略-执行一致性、完成口径、状态利用、检索收敛、业务判断**——其中 A/B/C 三个方向有确定性机制可做且有最小验证路径，D 方向证据不足建议暂缓。V5.1 不是最终版；但它的不确定性已被收窄到"计划是否真正指挥执行"这一个可实验的核心问题上。
