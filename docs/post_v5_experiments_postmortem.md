# Post-V5 Experiments Postmortem — Step 0–3 的 9/24 → 5/24 回退归因

> 分析原则：**不重新调用任何 LLM/API**。证据只来自
> `runs/v5_final24_20260908_171622`（Step 0–3 candidate，24-task Dev，5/24）、
> `runs/v5_dev24_20260904_173828` + `runs/v5_dev24r_20260904_195336`
> （Frozen V5 基线 9/24，前段 5 + 环境故障续跑 19，同代码同参）、
> commit `b075bc5…0aac7b6` 的 diff，以及 v2 trace 事件流。
> 归因三级：**confirmed mechanism regression / likely LLM variance / uncertain**。
> 不为支持 revert 而强行归因。

---

## 一、实验背景

- **基线**：Frozen V5（commit `3aa2918`，Dev 9/24，Holdout 8/22）
- **Candidate**：V5 + Step 0–3（commit 链 `b075bc5`→`808b07b`）
  - Step 0：双 Plan 清理（PlanStore 唯一 Plan 来源，plan_tracker 降级 telemetry）
  - Step 1：Plan–Execution Consistency（连续执行与 current plan 脱节提醒）
  - Step 2A：查询复用提示（Task State 已有实体 → 提示复用）
  - Step 2B：KA 检索预算（单次 handoff 最多 8 次 KB_search）
  - Step 3：Goal Coverage（用户数量词 vs 计划覆盖检查）
- **Targeted smoke**（3–4 task）：工具调用明显下降（077: 47→12，080: 42→10 量级），
  机制全部触发，没有观察到大问题 → 决定跑全量 24-task Dev
- **全量结果**：**9/24 → 5/24（-4）**，tools 306→180（**-41%**），tokens -25%

## 二、回退任务逐个归因（V5 成功 → candidate 失败的 6 个）

成功集合对比：

| | 成功任务 |
|---|---|
| V5 基线（9） | 001, 002, 003, 004, 007, 010, 021, 024, 037 |
| Candidate（5） | 001, 002, 004, 008, 046 |
| **回退（6）** | **003, 007, 010, 021, 024, 037** |
| 翻上（2） | 008, 046 |

### 机制介入矩阵（candidate 24-task run 的 v2 trace 实测）

| task | Step1 consistency | Step2A reuse hint | Step2B 预算触顶 | Step3 coverage | 归因 |
|---|---|---|---|---|---|
| 003 | 0 | **0** | **是**（8/8/8 vs 基线 18/10） | 0 | uncertain（倾向 variance） |
| 007 | 0 | **0** | **是**（8/8 vs 基线 17/15） | 0 | likely LLM variance |
| 010 | 0 | **3 次**（全拦） | 是（8/8/8） | 0 | **confirmed mechanism regression** |
| 021 | 0 | **3 次**（全拦） | 是（8/8/8/8） | 0 | **confirmed mechanism regression** |
| 024 | 0 | **0** | **是**（8/8/8 vs 基线 24/30/15） | 0 | **confirmed mechanism regression** |
| 037 | 0 | **10 次**（全拦） | 是 | 0 | **confirmed mechanism regression** |

补充事实：**Step 1（consistency）在整个 24-task candidate run 中一次都没有触发**（0 条
"diverged" 注入；targeted smoke 中也未观察到它在 Dev 任务形态下命中）。
Step 3（coverage）触发 6 次，但全部发生在 **026/077/080/092/095**——这些任务在
基线和 candidate 里**都失败**，与回退无关。

### task_010 —— confirmed：复用提示拦截循环挡掉了合法的首次查询

- 基线：DA 调 `get_referrals_by_user(user_id=…)` **1 次即成功**，拿到 4 条 referral
  记录，逐条解释，用户满意，reward=1。
- Candidate：DA **3 次提出** `get_referrals_by_user`（参数正确），**3 次全部被
  Step 2A 的复用提示拦截**（`state_reuse_hint` 事件与 proposed/ executed 计数
  3:0 证实）——提示注入后重新生成，DA 再提、再被拦，最终放弃并
  `transfer_to_human_agents`，reward=0。
- 机制介入：Step 2A 是**直接原因**。user_id 在 Task State 里（来自
  `get_user_information_by_name`），但**referral 记录从未查询过**——"实体已知"
  不等于"查询结果已有"，这正是该提示的误判形态。

### task_021 —— confirmed：同上（accounts 查询被拦，只完成 2 个 dispute 中的 1 个）

- 基线：`get_credit_card_accounts_by_user` 1 次执行成功（2 个账户），随后
  两次把 dispute 工具交给用户，**两笔 dispute 都提交**，reward=1。
- Candidate：accounts 查询 **2 次提案、0 次执行**（reuse hint 全拦），DA 只基于
  transactions 列表工作，**只交付了 Chipotle dispute**，Everlane 从未交付，
  reward=0。`plan_step_progressed` 只有 1 步（基线 2 步）。

### task_037 —— confirmed：最严重案例，10 次复用提示把任务打入人工转接

- 基线：完整成功——accounts 1 次查询、replacement order + 2 笔 fraud dispute
  全部完成，reward=1。
- Candidate：`get_credit_card_accounts_by_user` **9 次提案、1 次执行**——且那
  1 次执行的参数是残缺的 `user_id="890"`（拦截-再生循环中的畸形输出），返回
  "No records found"。DA 转而绕道 discoverable wrapper（多次 unlock 失败），
  最终 `transfer_to_human_agents`，reward=0。LLM 调用 81 次 vs 基线 58 次——
  拦截循环本身还在**烧额外轮次**。

### task_024 —— confirmed：KA 检索预算切掉了决定性证据

- 基线（无预算）：handoff 1 执行 24 次 KB_search，packet 含
  `business_bronze_rewards_card_001`（$500 welcome bonus 事实），DA 推荐
  **Business Bronze**（gold answer），reward=1。
- Candidate（预算=8）：三次 handoff 分别在第 8 次 KB_search 触顶强制收尾。
  第 2 个 handoff 里 bronze_001 **被检索到**（seq 140，`tool_call_end.doc_ids`
  含它）但**没能进 packet**（seq 170 `evidence_doc_ids` 无 bronze_001，
  bronze_002 也缺席）。DA 拿不到 Bronze $500 的事实，推荐了 Business Silver
  （~$800），用户申请了 Silver，reward=0。
- 机制介入：Step 2B 是直接原因——证据在检索层出现过、在 packet 层被预算截断。

### task_003 —— uncertain（机制有介入证据，但归因不完全干净）

- 基线：DA 并列推荐 Silver + Gold（Silver 在前，且明确"你有 Rho-Bank+ 所以
  Silver 0% 汇费"），用户申请 **Silver**（gold answer），reward=1。
- Candidate：DA 把 Gold 放在首位作为"best confirmed match"（用户确实有
  Rho-Bank+，Silver 也满足全部三项硬条件），用户申请 Gold，reward=0。
- 机制介入：candidate 该 task **零 DA 侧机制事件**（无 hint/无 plan/无
  coverage），但 Step 2B 预算触顶（8/8/8 vs 基线 18/10），且 candidate 的
  KA 走了 3 个 handoff（基线 2 个），packet 的组织方式不同（第 1 个 packet
  26 facts vs 基线 14 facts，但第 2 个只有 4 facts、status=insufficient）。
- 判断：**不能排除预算改变了证据呈现顺序/完整度**，但 DA 的 packet 里明确
  同时有 Silver（0% with subscription）和 Gold 的事实，最终选择 Gold 是业务
  判断差异。归因 **uncertain**（预算介入存在，因果链不闭合）。

### task_007 —— likely LLM variance

- 基线：DA 先调 `get_current_time`（2025-11-14），列出当期 active bonus
  （含 EcoCard），用户申请 EcoCard（gold answer），reward=1。
- Candidate：DA **没有调 get_current_time**，回答里写"as of June 22, 2026"
  （幻觉日期），结论"当前没有 active 的 dated promo"，用户失望离开，
  未申请任何卡，reward=0。
- 机制介入：candidate 该 task **零机制事件**（无 hint、无 plan、无 coverage）。
  预算触顶存在（8/8 vs 17/15），但两个 packet 都含 EcoCard 文档
  （ecocard_001/009 都在 packet 里）——证据不缺，是 DA 忘了查时间、
  编了日期。归因 **likely LLM variance**。

### 翻上的 2 个（同样要诚实归因）

- **task_008（0→1）**：candidate 零机制事件。基线 transfer 的 reason 字符串
  写错（`unconfirmed…`），candidate 恰好写了 gold 要求的
  `customer_demands_after_unavailable_offer_refusal`。**纯 LLM 路径/字符串
  variance**（与 Holdout 038 反向案例同形态）。
- **task_046（0→1）**：candidate 有 4 次 reuse hint（挡掉了重复的 accounts
  查询）+ plan。但成功的直接决定因素是 DA 输出的 arguments JSON 字符串
  格式恰好与 gold 匹配（`{"user_id": "…"}` 带空格，基线不带空格）。
  **uncertain**——hints 可能轻微帮助（少绕路），但成败在 token 级格式差异。

## 三、tools -41% 的来源分解

| 分组 | 工具节省 | 占总节省比例 |
|---|---|---|
| 双失败任务（13 个，两边都 fail） | **-112** | **89%** |
| 回退任务（6 个） | -6 | 5% |
| 翻上任务（2 个） | -8 | 6% |

- 效率收益几乎全部来自 **077（-35）、080（-32）、026（-20）** 这类双失败顽固
  任务——reuse hint + 预算确实把死循环式重复查询压下去了，但这些任务
  **依然失败**，省下的是"无效动作的 token"，不产生 reward。
- 同时 KA 侧 KB_search 总量只降 3%（900→869）——预算是**把检索从少数
  handoff 里的大量检索，摊到更多 handoff 上**（handoff 总数 119→156），
  并没有真正减少知识获取的总成本。

## 四、"Step 0–3 导致 success 回退"结论的证据等级

| 判定 | 数量 | 明细 |
|---|---|---|
| **confirmed mechanism regression** | 4 | 010、021、037（Step 2A 拦截循环）、024（Step 2B 预算截证据） |
| **likely LLM variance** | 1 | 007（幻觉日期、漏查时间；零机制事件） |
| **uncertain** | 1 | 003（预算有介入、证据链不闭合；倾向 variance 但不能排除） |

结论：**6 个回退里 4 个有逐 trace 的机制因果链支持，"Step 0–3 导致回退"
对其中 4 个成立；007/003 更可能是 LLM run-to-run variance。**
因此 9→5 的净 -4 不能全部记在机制头上——机制可解释的回退约 -4 中的大部分
（010/021/024/037），但 003/007 的失败形态（卡片推荐选择、忘查时间）在
V5 基线自身的 run-to-run 波动（历史观察：同一 seed 两次 run 25% vs 29.2%）
范围内也会自然发生。

## 五、为什么最终仍然 rollback（决策依据）

1. **净效果为负且方向明确**：效率收益（-41% tools）集中在双失败任务上，
   而 success 净 -4。freeze 条件"24-task Dev 无明显退化"不满足。
2. **Step 2A 的结构性缺陷被证实**：复用提示的判定条件是"参数引用的实体在
   Task State"——但**实体已知 ≠ 查询结果已知**（accounts/referrals 的记录
   内容只有查过才知道）。该提示以"非硬拦"注释设计，实际实现走的是
   拦截-再生循环（注入 system note 后 `continue`，该轮调用不执行），
   对首次合法查询构成事实上的阻断。010/021/037 三个回退全是这个形态。
3. **Step 2B 预算切证据有实证**（024：bronze_001 被检索到但没进 packet）。
4. **Step 1 在全量 Dev 上零触发**——targeted smoke 的正结果不能外推。
5. **收益与代价不对称**：项目目标已达成（V5 已有同模型 Holdout +4 的干净
   机制证据），而继续修 Step 2A 的判定条件（比如"只有重复同参查询才提示"）
   需要再跑全量 Dev 验证，成本高、期望收益不确定。

> **最终表述**：新机制显著改善了效率指标（tools -41%，tokens -25%），但完整
> Dev success 从 9/24 回退到 5/24。逐 task trace 证实其中 4 个回退
> （010/021/037 = Step 2A 拦截循环；024 = Step 2B 预算截证据）有明确机制
> 因果链；007 更像 LLM variance，003 不能排除预算影响。由于部分回退可能
> 没有直接机制介入，-4 不能全部归因于机制；但机制可解释的回退足以让净效果
> 为负。考虑项目目标与风险，采取 **conservative rollback 至 Frozen V5**
> （commit `3aa2918` 的 runtime 状态），Step 0–3 整体归档为负结果实验。

## 六、留给未来的笔记（不再实施）

- Step 2A 若要重做，判定条件应改为"**同 (tool, 参数组合) 已成功执行过且结果
  已入 Task State**"才提示，且**提示后必须放行原调用**（提示与执行不互斥），
  否则就是本次的拦截循环。
- Step 2B 若要重做，预算应作用于"同一问题的换词重搜"而不是"总检索数"，
  或至少保证 forced-stop 前把已检索文档的 facts 全部并入 packet。
- 这次实验本身验证了一个元结论：**targeted smoke 的机制触发率/效率收益
  不能预测全量 success**——037 在 targeted 观察里只是"重复查询减少"，
  全量里却是灾难性的拦截循环。全量 Dev 是唯一可信的 freeze 门槛。
