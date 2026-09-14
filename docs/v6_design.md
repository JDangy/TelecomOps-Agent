# V6.0 设计 — Stateful, Evidence-Grounded Agent Runtime

> **⚠️ V6.1 架构收紧修订（2026-09,本文档下文为 V6.0 原始设计）**
> 用户 2026-09 指令"先收紧架构,不再建平行状态系统"落地后的修订。
> 本节是当前唯一有效的 V6 架构说明;下文 V6.0 内容仅作历史参考。
>
> **V6.1 核心改变（在 V5 模块上补能力,零平行系统）**:
>
> 1. **TaskStateV3 仍是唯一事实源**。上一版的 EvidenceLedger
>    （独立事实存储 + provenance 矩阵 + 双喂入路径）已删除;
>    替代为 `evidence_view.py` 的 **EvidenceView**——零存储的只读
>    视图,每次从 TaskStateV3 历史链现算四段:
>    用户说的（user_statement）/ 工具确认的（system_fact）/
>    KB 规则（kb_rule）/ 用户偏好（user_preference）。
>    数据流严格单向:`Tool Result / User / KB → TaskState → EvidenceView`。
> 2. **权威按事实类型,不做全局 source 排名**（§5 原则）:
>    StateEntry 新增 fact_type 字段;authority 表为
>    system_fact→系统 / kb_rule→KB / user_preference→用户本人 /
>    user_statement→unverified。"开户时间问工具、业务规则问 KB、
>    加急与否问用户"由类型表通用表达,零 task 硬编码。
> 3. **PlanStore 仍是任务进度主模块**;Worklist 是其条目级展开:
>    WorkItem 挂在 PlanStep 下（step_id 反向引用）,步骤 removed →
>    条目移除;实体绑定谓词单一实现
>    （plan_store.args_reference_entity,PlanStore/Worklist 共用）;
>    上一版 observe_entities 的"runtime 观察"旁路入口已删
>    （Worklist 不再有脱离 plan 的第二来源）。
>    通用能力增量在 TaskState 提取器层:UserStateExtractor 补通用
>    duration 自述提取与用户偏好模式;ToolResultStateExtractor 补
>    get_current_time → system.current_date（同一数据流,无旁路）。
> 4. **ContextBuilder 是最终 context 的统一出口**
>    （context_builder.build_context 唯一签名）。上一版独立的
>    context_organization.py 已删;evidence_block / worklist_block
>    都是 build_context 的输入,空状态零渲染（V5 简单任务零开销）。
> 5. **DecisionCheckpoint 轻量化**:不再每次触发调 LLM 判
>    READY/NOT_READY（那是"再问一次确定吗"的变体）——零额外 LLM
>    调用。触发即确定性构建 6 问 artifact（目标/已确认事实/用户
>    自述/硬规则/缺失证据/可否执行——固定字段,无 chain-of-thought）
>    注入到该关键动作**执行前**的一轮生成:首次提出 → 停一轮注入
>    六问;同签名重提 → 直接放行。签名集合结构性防死循环
>    （每动作恰好多一轮 generate,不是预算兜底）。触发枚举覆盖
>    不可逆/高影响变更、transfer、转人工;读类/检索/规划零触发。
> 6. **guard 消费 worklist**:plan completion guard 同时纳入未完成
>    条目（多实体任务不因"做完一个对象"整体结束）,仍走同一
>    GUARD_LIMIT=2 有界提醒,零新机制。
> 7. **未新增任何 Agent/Planner/Memory**;闭环为用户指定的单向流:
>    Observe → TaskState → Plan/Worklist → Decision → ActionHarness →
>    Tool Result → TaskState 更新 → 下一轮。
> 8. trace 事件收敛:TaskState 的 state_write/state_update 已含全部
>    事实变更（唯一事实源的好处——审计点只有一个）;worklist 发
>    work_item_created/completed/failed/removed;checkpoint 发
>    checkpoint_triggered/missing_evidence/budget_exhausted。
>    evidence_* 事件族删除（不再有第二套事实流）。
>
> **V6.1 文件清单**:
> | 文件 | 动作 |
> |---|---|
> | `agents/harness/task_state_v3.py` | 修改:fact_type + authority 表 + duration/preference 提取 + current_date + 视图查询 |
> | `agents/harness/plan_store.py` | 修改:args_reference_entity 单一谓词导出 |
> | `agents/harness/evidence_view.py` | 新增（替代 evidence_ledger.py,后者删除） |
> | `agents/harness/worklist.py` | 重写:挂 step 下/删旁路/零渲染 |
> | `agents/harness/decision_checkpoint.py` | 重写:6 问 artifact/零 LLM/签名防重 |
> | `agents/harness/context_builder.py` | 修改:统一出口（evidence/worklist block;context_organization.py 删除） |
> | `agents/two_agent.py` | 修改:单路径喂入/checkpoint 动作前注入/guard 含条目 |
> | `eval/runner.py` `eval/instrumentation.py` | 修改:事件词汇表更新 |
> | `tests/test_unit.py` | V6 用例重写（13 个,37/37） |
> | `tests/test_integrity.py` | 新增 test_no_task_hardcoding_in_runtime |
> | `scripts/run_v6_tests.sh` | 移除 replay build（未获批阶段不碰 replay） |
>
> **测试**:37/37 unit + 8/8 integrity 全绿（含 DSH_V6_DISABLE 回退、
> 零 LLM 源码断言、空状态零渲染、失败不误完成、多实体不整体结束、
> 旁路模块不存在断言）。尚未跑 replay/API/Dev24/Holdout——待审。
>
> ---

# V6.0 原始设计（历史参考）

> 状态:已实现(deterministic 部分见 §8)。基线:Frozen V5(commit 3aa2918,
> Dev24 9/24 + Holdout 8/22,agent=two_agent_harness,qwen3.8-flash,bm25,seed 42)。
> 本文档是 V6 的唯一设计来源;trace 证据全部来自 Frozen V5 Dev24 原始 trace
> (`runs/v5_dev24_20260904_173828` + `runs/v5_dev24r_20260904_195336`),
> 不含任何 Holdout 失败信息。

---

## 1. V5 现架构(实测理解,非文档转述)

```
User ──▶ DecisionAgent (LLMAgent 子类,拦截循环)
            │  generate_next_message:
            │    _feed_task_state(user/tool 消息 → TaskStateV3)      [State 喂入]
            │    harness.process(业务调用, validate_only=True)       [确定性校验]
            │      ├─ SchemaValidation(公开/已unlock schema)
            │      ├─ TaskStateValidator(user显式值/工具ID冲突)
            │      └─ KnowledgeConstraintValidator(KB enum/阈值/格式)
            │    ask_knowledge_agent → KnowledgeAgent(独立context,
            │      BM25→EvidencePacket) → packet facts 入 DA memory
            │    write_plan/update_plan → PlanStore(结构化计划,
            │      on_tool_result 由真实执行+实体绑定推进)
            │    completion guard(pending steps → 一次性 system 提醒)
            │    ContextBuilder(V3轻量 + plan_block + 旧ToolResult存根化)
```

关键机制(全部保留,不推翻):
- **TaskStateV3**(对象级事实):`set(object, field, value, source, source_ref)`,
  supersede 历史链、实体索引、bare 歧义放行。来源三类:user/tool/knowledge。
- **PlanStore**(意图):PlanStep(tool_hint+entities),on_tool_result 两层绑定
  推进(hint 命中+实体命中);多对象歧义不推进(宁 pending 不猜)。
- **ActionHarness**:拦截原则"有明确依据才拦",V2.3 recovery 预算
  (同 (tool,field) 拒 2 次后放行)。
- **V4 PlanTracker**:行为观察(total≥12/distinct≥8/repeats≥6 升级信号),
  V5 后仅作 fallback 入口 B 的信号源。
- **Tracing**:TraceV2Recorder event-sourced;state_write/state_update 由
  runner 在 task 结束 flush(postmortem 发现:这是"延迟 flush",见 §3-F)。

## 2. Frozen V5 Dev24 Failure Taxonomy(trace 逐个归因)

15 个失败任务,按"闭环哪一环断裂"分类(一类通用 failure,不是一个 task):

### A. 证据未分层 —— user_claim 被当成 confirmed(3 例)
- **100(65天tenure)**:用户自称"约四个月",DB 实际 09/10 开户=65天。
  Agent 调了 `get_current_time` 但**从未查 accounts**(gold 第一个动作就是
  `get_all_user_accounts_by_user_id_3847`),直接采信用户自述 → World Blue
  referral(需90天)提交失败;且 assistant 自己调 submit_referral(gold 是
  user 侧调用)。**State 层缺失:claim 与 confirmed 无法区分。**
- **027/029(用户谎称 disputes 已 resolved)**:027 中 agent 其实做了正确校验
  (get_user_dispute_history 显示 SUBMITTED/无记录)并拒绝更新——但
  `get_user_dispute_history_7291` 查的是 *transaction_disputes*(信用卡),
  现金卡 disputes 在 *cash_back_disputes* 表,工具不存在 → 转人工。
  判定失败在"证据源不存在"的沟通层;029 类似。**同属 A 类变体:
  关键事实(dispute 状态)没有 tool-confirmed 来源时,agent 无法闭合。**
- **053(顺序对但 DB 变异)**:CLI submit→approve→dispute 顺序与 gold 一致、
  参数一致,但多给了 user 工具的 arguments(gold 不带)、外加查询顺序不同
  → user DB hash 不匹配。判定:边缘性 DB 污染,非闭环断裂。

### B. 长任务 worklist 管理 —— 做一半忘记/交错重复(3 例)
- **026(多笔 dispute+correction)**:33 工具调用中 5 次 submit dispute 后,
  Phase 2 的 update_transaction_rewards 交错执行又反复重新查 transactions,
  max_steps。plan 只写过一次,plan_step_progressed 仅 2 次,guard 2 次后
  remove_step×2 + block_step×2 —— 计划状态与实际执行脱节。
  **Worklist 层缺失:没有"每笔 transaction 的 dispute→correction"
  条目级进度,agent 靠记忆在 6 笔交易间往返。**
- **080(5卡+3dispute)**:47 工具调用,freeze/unfreeze/close/order/dispute
  交错;unfreeze 了 3 张卡(gold 只 unfreeze green 1 张);从未调
  get_bank_account_transactions_9173(查 pending);最后 activate 缺席 →
  max_steps。**同 B 类:多实体操作序列无条目级跟踪,重复+遗漏并发。**
- **077(2卡+credit发现)**:47 调用,plan 重写 2 次,步骤推进 5 次但
  close→unfreeze→close 循环;max_steps 82 LLM 调用。**同 B 类。**

### C. 冲突规则未进关键决策(2 例)
- **054(CLI×dispute×replacement 三方冲突)**:agent 在 turn 28 明确说
  "dispute 和 replacement 是独立动作,filing 不影响 limit increase"——
  这是**错误的事实**(KB doc_credit_cards_credit_card_account_logistics_007
  Step 2 明确:No Pending Disputes / No Pending Replacement Cards)。
  21 次 handoff 里 memory_hit 15 次(重复问同义 CLI 问题,packet 高度雷同),
  但关键冲突规则从未被装进一个可审计的"关键决策前状态"。先 dispute 后 CLI
  → CLI 永久 blocked,转人工收场。**Decision 层缺失:关键动作前没有
  "我依赖了哪些事实、哪些还是 unverified"的检查点。**
- **053**:同规则正面案例(顺序对了)但败在 DB 污染(A 类备注),
  说明该规则检索得到但**不稳定**——21 个 packet 里 CLI 冲突规则时有时无。

### D. 关键决策前证据不完整(1 例)
- **070(promotion 推荐)**:用户四条件 → 3 个候选(Sky Blue/Lime Green/
  Hunter Green)。KB 有两个 promo doc(013 active 11/01-11/30:
  Sky Blue>Lime Green;014 expired 10/12-11/12:Lime Green>Hunter Green)。
  **两个 doc 都被检索到了**(\_013 与 \_014 都在 136 个去重 doc 里),但
  handoff 问题只问"哪个账户满足需求",packet 状态 sufficient/conf-high
  ——KA 没有把"promo 优先级取决于 current_date,而 packet 里没有 current_date"
  作为 missing_information 报出来。Agent 调过 get_current_time(2025-11-14)
  但从未在推荐时把两个 promo 与当前日期对齐 → 推荐 Lime Green(gold:
  Sky Blue)。**这是"packet 状态 sufficient 但对最终决策而言 insufficient"
  ——status 的语义只覆盖检索充分性,不覆盖决策充分性。**

### E. 顽固知识/沟通失败(6 例)
- **008(拒绝性任务)**:用户死缠烂打要三张卡,agent 拒绝 54 LLM 调用/58
  检索——判定与沟通层,KB 无此卡, refusal 正确但 communicate 检查不过
  (具体未过项未取;Stubborn 类历史一致)。
- **020(cash back 调查,无 Phase 2)**:提交 6 dispute 后用户不推进
  (非对抗版),agent 在 user_db 留下多余 give_discoverable_user_tool 参数
  → DB hash 不匹配(与 053 同型边缘污染)。
- **046(claims no disputes)**:agent 正确查了 dispute history 并告知
  blocker——但**多执行了 pay_credit_card_from_checking($125)**
  (gold 无此动作)→ DB 污染。判定:动作过界,非证据问题。
- **088/092/095/092(decline code/PIN/APY 诊断)**:多阶段诊断流,
  各自独立的知识缺口,失败在 KA packet 质量(部分)+ DA 多阶段推进。

### taxonomy → 通用 failure classes(驱动 V6 设计)

| 类 | 通用 failure | V6 对应能力 |
|---|---|---|
| A | user_claim ≠ tool_confirmed 但被同权采信 | **Evidence provenance/confidence 分层**(§4.1) |
| B | 多实体长序列:重复查询+条目遗漏+max_steps | **Goal/Worklist 条目级 tool-driven 进度**(§4.2) |
| C | 关键动作依赖的事实从未被显式确认 | **Selective Decision Checkpoint**(§4.3) |
| D | packet sufficient ≠ 决策 sufficient | **Missing-evidence 判定进 checkpoint**(§4.3) |
| E | 知识/沟通/动作过界 | 不在本期机制范围(已知限制,§9) |

## 3. 与用户设计案的冲突点(以仓库/trace 证据为准)

1. **"PlanTracker 与 PlanStore 职责重复"**——属实,但不建议本期清理:
   PlanTracker 是 V4 行为观察(fallback 信号源 + progress_block 渲染),
   PlanStore 是 V5 结构化计划。V5 freeze 依赖两者协作(入口 B 信号来自
   tracker)。**V6 维持现状,Worklist 建在 PlanStore 之上**——清理是
   技术债,按"先保证行为"原则推迟。
2. **"state fact added/updated 的 trace event 实时可见"**——现状是
   TaskStateV3.drain_trace() 在 task 结束才 flush(postmortem 里
   775 条 state_update 一次涌入)。V6 的实时事件从新模块的 emit 直接
   进 recorder(与 plan_* 事件同模式);TaskStateV3 的 flush 机制不动。
3. **"checkpoint 阻止证据不足下推荐"**——Runtime 只能发现
   `current_date = missing`(参数级、确定性),不能发现
   "promo comparison 需要 current_date"(那是业务语义)。§4.3 的设计
   严格区分:Runtime 产 missing-evidence 清单(确定性),LLM 判
   READY/NOT_READY 与 next action(语义)。070 类问题需要 §4.4 的
   context 分层(把 current_date 作为 confirmed fact 呈现)+ checkpoint
   的 NOT_READY 语义由 LLM 给出——Runtime 不做 Sky Blue>Lime Green 判断。
4. **"工具失败不能标记 work item 完成"**——PlanStore 已实现(on_tool_result
   ok=False → failed)。V6 Worklist 复用同一语义,不重写。
5. **"(user) 不泄漏 task notes 中 Agent 不可见内容"**——注意:tau2 的
   task.description.notes 与 user_scenario.instructions 是 evaluator/user
   侧信息。Replay 的输入构建(§7)只允许使用:v1 trace 的 system_prompt +
   conversation(即当时 agent 实际可见)+ 工具结果原文。**我们的 replay
   实现直接复用 trace 里已存的 agent-visible 数据,天然合规;单测里断言
   replay 输入不含 notes/instructions/evaluation_criteria 字段。**
6. **"054 是执行顺序冲突"**——trace 显示不止顺序:agent 陈述了错误的
   独立性事实(C 类)。V6 机制必须覆盖"错误事实陈述"(checkpoint 的
   Relevant confirmed facts 段),不只是顺序。

## 4. V6.0 设计(三个统一能力 + context + trace)

### 4.1 Evidence-aware State(增强 TaskStateV3,不推翻)

新增独立轻量层 `EvidenceLedger`(agents/harness/evidence_ledger.py),
不并入 TaskStateV3(避免动 freeze 过的 supersede/查询逻辑):

```python
@dataclass
class EvidenceRecord:
    fact_key: str        # 规范化事实名,如 "tenure_days", "current_date",
                         # "promo_priority.business_checking", "dispute_status"
    value: Any
    provenance: str      # "user_claim" | "tool_result" | "knowledge_base"
                          #   + "system" (get_current_time 等)
    source_ref: str      # 工具名 / doc_id / "user_turn_N"
    status: str          # "unverified" | "confirmed" | "superseded" | "policy_rule"
    seq: int
    task_phase: str = ""  # 可选阶段标签(如 "pre_dispute")
```

规则(全部确定性,无 LLM):
- `provenance=user_claim` 的记录**永不覆盖**同 key 的 `tool_result` 记录
  (tool-confirmed supersedes user claim);反过来 tool 结果覆盖 claim
  (superseded,历史保留)。
- 查询接口 `get_decision_grade(key)`:返回
  `("confirmed", value, record)` / `("unverified", value, record)` /
  `(None, None, None)`。**用途只有一个:Decision Checkpoint 的
  "Relevant confirmed facts" vs "Unverified" 分段渲染(§4.4)与
  checkpoint 的 missing-evidence 输入。**
- 事实来源自动喂入(不改 V5 提取器):
  - `ToolResultStateExtractor` 同源结果(Record 块)→ ledger
    (provenance=tool_result);
  - `UserStateExtractor` 的金额绑定 → ledger(provenance=user_claim);
  - EvidencePacket facts/constraints → ledger(provenance=knowledge_base,
    status=policy_rule);
  - `get_current_time` 的结果(新增一个极小的确定性识别:工具名匹配)
    → ledger(key="current_date")——070/007 类问题的事实锚点。

**不做**:自动判断"哪些 key 是决策关键的"(业务语义,LLM 负责)。
**不做**:拦截行为(ledger 不进 harness 拦截链——分层证据只影响
context 组织与 checkpoint,不影响 V5 校验行为)。

### 4.2 Goal/Worklist(建在 PlanStore 之上)

新模块 `agents/harness/worklist.py`:

```python
@dataclass
class WorkItem:
    item_id: int
    goal: str              # 该条目的业务语义("dispute txn_A")
    tool_hint: str | None   # 绑定工具
    entities: list[str]     # ["txn_..."] / ["card_dbc_..."]
    status: str             # pending/in_progress/completed/failed/blocked
    source: str             # "plan" (从 PlanStep 派生) | "runtime" (工具观察)
```

设计要点:
- **Worklist 不替代 PlanStore**:PlanStep 是"意图的步骤描述",WorkItem 是
  "每个实体的完成条目"。`Worklist.sync_from_plan(plan_store)` 把带
  entities 的 PlanStep 展开成 per-entity 条目(026:6 笔 txn 的
  dispute/correction;080:5 张卡各自 freeze/close/order/activate)。
- **tool-result-driven**(复用 PlanStore 已验证语义):`on_tool_result(
  inner_tool, ok, arguments, task_state)` 与 PlanStore.on_tool_result 同
  口径——hint+实体绑定推进;**ok=False → failed**(不误标完成);
  同一 (goal, entity) 的工具第二次成功且条目已 completed → 幂等
  (重复调用不重置)。这直接解决 026/080 的"交错重复":重复查询在
  worklist 上可见("already completed: dispute txn_A")。
- **进度渲染进 context**(§4.4 的 CURRENT WORK ITEM 段),
  `unfinished_count>0` 时不硬拦结束——由现有 completion guard
  (PlanStore.guard_should_remind,已有界 2 次)承担提醒,避免新拦截。
- 通用性:结构只认 (goal, entities, tool_hint),无任何 task/域硬编码。

### 4.3 Selective Decision Checkpoint

新模块 `agents/harness/decision_checkpoint.py`:

触发范围(确定性,枚举;普通查询零触发):
```
TRIGGER_CONDITIONS:
  - tool_name in MUTATION_TOOLS   (order/close/file/submit/pay/approve/
                                    deny/unfreeze/freeze/transfer/apply/open)
  - tool_name == "transfer_to_human_agents"
  - assistant 纯文本且 plan/worklist 有 pending(结束意图)→ 复用现有 guard
    (不新增机制,checkpoint 只在动作前)
```
读工具(get_*)与 ask_knowledge_agent **不触发**——保护 010/021/024/037
的现有成功路径(postmortem 教训:任何对合法查询的介入都危险)。

Checkpoint artifact(短、可审计,存 runtime + trace,不进对话历史):
```
DECISION CHECKPOINT (pre-action)
Goal: (PlanStore.goal 或最近 user 文本)
Action about to execute: <tool> <args 摘要>
Relevant confirmed facts: (EvidenceLedger.get_decision_grade ==
  confirmed 的、与参数实体相关的记录,≤6条)
Unverified claims being relied on: (provenance=user_claim 的记录,≤4条)
Relevant policy evidence: (ledger 的 knowledge_base 记录,≤4条)
Missing evidence: (LLM 在 ask 调用时声明的 needed_information 未闭合项
  + ledger 中 action 参数引用的实体无 confirmed 记录的——确定性可判)
Decision status: 由 LLM 判(见下)
```
**执行流(关键设计——Runtime 不做业务判断)**:
1. Runtime 在触发条件命中时,把 checkpoint artifact 注入为一次
   **单轮结构化生成**(不是新 Agent,复用 DecisionAgent 的 LLM 一次调用,
   `call_name="decision_checkpoint"`,输出仅一个短 JSON:
   `{"decision_status": "READY"|"NOT_READY", "missing": [...], "next": "..."}`)。
   ——这与 V5 的 planning tool 拦截同构:runtime 注入、LLM 判定、
   结果落 trace,不改对话历史。
2. **NOT_READY + missing 非空** → Runtime 把 missing 列表回注
   (一条 system note,同 V5 fallback 提示模式),让 DA 先补证据。
   **不做硬拦截**(postmortem:拦截循环是 Step 2A 灾难形态);
   提示后放行原调用——若 LLM 仍然执行,执行权在 LLM。
3. 提示有界(每任务最多 CHECKPOINT_PROMPT_LIMIT=3 次 NOT_READY 回注,
   之后 checkpoint 只记录不再提示)——防循环烧轮次。
4. **Runtime 确定性拦的仅一种情况**:checkpoint 自身参数级缺失——
   待执行动作的**必填参数值**引用了 ledger 里 provenance=user_claim
   且同 key 存在矛盾的 tool_result(superseded 记录)→ 这其实就是 V5
   TaskStateValidator 已有的 task_state_conflict 拦截,**不新增拦截类别**。
   其余一切(推荐哪个产品、顺序对不对)只通过 checkpoint 的
   confirmed/unverified 分段给 LLM 证据视图。

成本控制:每任务 checkpoint 触发上限(CHECKPOINT_MAX_PER_TASK=8,
超限只记 trace 不再调用 LLM);每次 1 个短 LLM 调用(prompt ~600 token)。

### 4.4 Context Builder 统一(context_organization.py)

新函数 `build_v6_context_view(...)` 产出一个**高价值信息块**
(注入 system 尾部,与 V5 blocks 并列,不替换):
```
[Current situation — organized by evidence quality]
GOAL: (PlanStore.goal / worklist.goal)
CURRENT WORK ITEM: (worklist 当前条目 + 已完成条目摘要)
CONFIRMED FACTS (tool/system-verified): current_date=2025-11-14, ...
UNVERIFIED USER CLAIMS: tenure≈4 months (user_turn_3), ...
RELEVANT POLICY (KB-verified): provisional credit ≤2 disputes..., 
MISSING EVIDENCE: (未闭合 needed_information / 参数实体无 confirmed)
```
要点:
- 与 V5 的 plan_block/task_state_block **并存**(V6 block 置于其后,
  简单任务——无 plan、无 ledger 记录时——block 为空字符串,零开销,
  保护 001/002/003/004/007 的零成本路径)。
- 渲染优先级:P0 与当前动作(最近 biz tool)参数实体相关的 confirmed;
  P1 current_date/user 声明;P2 policy。条目数硬上限(防膨胀)。
- V5 的 _task_state_block 保留不动(freeze 行为)。

### 4.5 Tracing(新事件,全实时)

新事件直接经 recorder.emit(实时,不走 drain_trace):
```
evidence_record_added      (key, value, provenance, status)
evidence_superseded        (key, old, new, by_provenance)
work_item_created          (item_id, goal, entities, source)
work_item_progressed       (item_id, tool, ok)
work_item_completed        (item_id, tool)
work_item_failed           (item_id, tool, note)
checkpoint_triggered       (tool, reason)
checkpoint_missing_evidence(list)
checkpoint_decision        (status=READY/NOT_READY, missing, next)
checkpoint_prompt_injected(nth, bounded)
checkpoint_budget_exhausted()
action_executed (V5 已有 action_validation/executed,复用)
state_update_caused_by     (tool_result → ledger 变更的关联事件)
```
失败归因四分法:State(ledger)/Decision(checkpoint)/Action(harness)/
Update(worklist)——每个新事件带 span,从事件流即可判断断在哪一环。

## 5. 修改文件清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `agents/harness/evidence_ledger.py` | 新增 | EvidenceRecord/EvidenceLedger(§4.1) |
| `agents/harness/worklist.py` | 新增 | WorkItem/Worklist(§4.2) |
| `agents/harness/decision_checkpoint.py` | 新增 | 触发判定+artifact 构建+LLM 单轮判定(§4.3) |
| `agents/harness/context_organization.py` | 新增 | V6 context view 渲染(§4.4) |
| `agents/two_agent.py` | 增量修改 | ① 构造时实例化四件套;② `_feed_task_state` 里喂 ledger;③ generate_next_message 里 biz_calls 处增加 checkpoint 触发点(在 harness 校验后、放行前);④ checkpoint 的 NOT_READY 回注;⑤ `__init__.py` 导出新符号。全部用 try/except 包裹+开关(默认 on,可 env 关闭),保 V5 回退 |
| `agents/harness/__init__.py` | 增量 | 导出新模块符号 |
| `eval/replay.py` | 新增 | Replay harness(§7) |
| `configs/banking_v6_replay.json` | 新增 | replay case 清单 |
| `tests/test_unit.py` | 增量 | 新增 10 组用例(§10) |
| `scripts/run_v6_tests.sh` | 新增 | 一键 L0 |
| `docs/v6_design.md` | 本文档 | |

**不修改**:`task_state_v3.py`、`plan_store.py`、`action_harness.py`、
`validators.py`、`resolver.py`、`context_builder.py`(V5 全部原样)。

## 6. Replay Harness 设计(§7 详述)

见 §7(独立节)。要点:从 Frozen V5 v2+v1 trace 截取"关键决策前"的
agent-visible state,构建单轮决策 replay;输入只用 trace 已存的
agent 可见数据(system_prompt/conversation/tool_calls+results),
不含 notes/instructions/evaluation_criteria;LLM 可选离线(第一阶段
只构建,不调用——单测里用 stub LLM 验证管线)。

## 7. Replay 设计(详细)

`eval/replay.py`:

```
ReplayCase:
  task_id, checkpoint_label ("pre_recommendation"/"pre_mutation"/...)
  cut_point: v2 trace 中的 seq —— 决策前最后一个 llm_call_end
  agent_view: {system_prompt, messages, recent_tool_results}
  expected_properties: (从 gold 动作推出的"决策质量断言"——
     只用于人看报告,不进 prompt;单测断言不含 gold 字段)
build_case(trace_v2_path, trace_v1_path, cut_strategy):
  1. v1 trace 取 system_prompt + conversation 到 cut 点
     (conversation 本身就是 agent-visible 记录)
  2. v2 trace 定位事件(llm_call_start seq),重建 DecisionAgent 的
     task_state/plan_store/ledger 状态:重放 v2 里的
     state_write/update + plan_* + (V6 的) evidence/worklist 事件
     ——Frozen V5 trace 没有新事件,回放旧事件到 PlanStore/TaskStateV3
     的等价状态(尽力重建;重建不了的记 skip 原因,不猜)
  3. 输出 ReplayCase JSON(落 configs/banking_v6_replay.json 供审计)
run_case(case, llm=None):
  - llm=None: dry-run(只构建 agent_view + 打印 context 组织,
    验证不泄漏;不调用模型)
  - llm 给定: 单轮 generate(agent_view + V6 context view),
    输出动作提案;与 trace 里实际下一步对比(差异报告)
```
关键决策点选择(Level 1 用):
- 054: pre_dispute 轮(错误独立性陈述前)
- 070: pre_recommendation 轮(Lime Green 推荐前)
- 100: pre_referral 轮(submit_referral 前)
- 026: pre_phase2 轮(第一笔 update 前)+ phase2 中段
- 077/080: pre_close / pre_dispute 轮
- 010/021/024/037: 成功路径关键轮(regression——V6 view 不得引入
  误导信息,动作提案应与实际成功动作一致率 ≥ 基线)
单测断言:replay case 的 agent_view 字段白名单
(system_prompt/messages/tool_results/derived_state),
**任何含 evaluation_criteria/notes/user_scenario 的字段直接 fail**。

## 8. 实现状态（已完成——Level 0 全绿）

- [x] evidence_ledger.py — provenance 分层（user_claim/tool_result/
      knowledge_base/system）、supersede 方向矩阵（tool>claim、
      policy 不被覆盖）、幂等、decision-grade 查询、实时 trace
- [x] worklist.py — sync_from_plan 展开带-entities 步骤；
      on_tool_result（hint+实体绑定,失败→failed,幂等）;
      observe_entities 对称补条目（防"做一半"）
- [x] decision_checkpoint.py — 触发枚举（读类/ask/plan 零触发）;
      artifact 构建（confirmed/unverified/policy/missing/conflict
      分段）;LLM 单轮判定（stub-ready）;NOT_READY 有界回注
      （PROMPT_LIMIT=3）;预算 CHECKPOINT_MAX_PER_TASK=8
- [x] context_organization.py — V6 evidence view（空状态零渲染;
      current_date 前置;分段硬上限）
- [x] two_agent.py 接线 — 构造时实例化;_v6_feed_user_message（
      tenure 类自述→user_claim）;_v6_feed_tool_result（current_time
      识别 + worklist 推进）;plan 变更同步 worklist;packet facts
      →ledger;checkpoint 触发在 harness 校验后放行前;DSH_V6_DISABLE
      环境开关一键回退
- [x] eval/instrumentation.py — decision_checkpoint actor 映射
- [x] eval/runner.py — _v6_runtime_metrics（事件计数,无 V6 事件
      返回空 dict = V5 回退形态零影响）
- [x] eval/replay.py + configs/banking_v6_replay.json — 10 case
      （6 失败关键决策点 + 4 成功 regression）;agent-visible-only
      构建已验证（integrity 断言全过）
- [x] tests/test_unit.py — 37/37（V5 24 + V6 13）
- [x] tests/test_integrity.py — 7/7（新增 replay/runtime V6 守卫）
- [x] scripts/run_v6_tests.sh — 一键 Level 0

验证记录:
- 端到端冒烟:tenure_claim(user)→plan→3 work items→tool 推进
  （1 completed 2 pending）→context view 分段渲染 ✓
- 事件流:evidence_record_added/superseded、work_item_created/
  completed、checkpoint_triggered/missing_evidence/decision/
  prompt_injected 全部实时可见 ✓
- supersede 方向:tool>claim ✓;claim 不覆盖 tool/policy ✓;
  conflict keys 暴露 ✓

## 9. 已知风险与缓解

| 风险 | 依据 | 缓解 |
|---|---|---|
| checkpoint 提示被 LLM 忽略 | postmortem: 提示类机制生效率低([PLAN] 行 0 遵守) | checkpoint 是结构化 artifact+单轮独立生成,不是文本提示;NOT_READY 回注有界 3 次 |
| 新 context block 挤占注意力 | V5 简单任务零开销路径必须保住 | block 空状态零渲染;P0/P1/P2 + 条目硬上限;Level 3 regression 子集(010/021/024/037)先行 |
| LLM 判 NOT_READY 误报 → 阻塞 | Runtime 不硬拦;提示后放行 | postmortem 教训内建:绝不拦截-再生循环 |
| replay 状态重建不完整 | V5 trace 无新事件,PlanStore 状态重建依赖 plan_* 事件流 | 重建不了的 case 记 skip,不猜;Level 2 跑 3-5 个目标 task 前先 dry-run |
| 054 类错误事实陈述 | checkpoint 只给证据视图,LLM 仍可能说错 | confirmed/unverified 分段让错误陈述在 trace 里可见(checkpoint_decision 事件),便于 Level 1 迭代 prompt |

## 10. Unit tests 对应(1:1 用户要求)

1. user_claim/tool_confirmed 不混淆 — ledger 分层查询
2. tool-confirmed supersede 用户自述 — supersede 方向性
3. worklist 据真实 Tool Result 推进 — on_tool_result ok
4. 工具失败不标完成 — on_tool_result not ok → failed
5. 多实体不整体结束 — per-entity 推进+未完成可见
6. Decision Check 只在关键动作触发 — 枚举触发判定
7. 简单查询不触发 checkpoint — get_*/ask 零触发
8. missing evidence 正确标识 — checkpoint missing 计算
9. V5 成功行为不被硬拦 — ledger 不进拦截链(无新 blocking verdict)+ 简单任务零 checkpoint
10. 不访问 hidden/evaluator/gold — integrity 测试扩展(replay 白名单+checkpoint 源码扫描)
