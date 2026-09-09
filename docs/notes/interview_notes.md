# TelecomOps-Agent 项目笔记（面经版，V5 收口定稿）

> 给自己看的：项目怎么一步步建起来、踩了什么坑、怎么解决的。
> 面试时按这套问答讲。所有数字都是真实测出来的，不是编的。
> **当前状态：Frozen V5（commit 3aa2918）是最终 Runtime，项目已收口。**

---

## 一、30 秒介绍这个项目（先背熟）

> 我做了一个**评估驱动的 Agent Runtime 项目**，基于 tau2-bench（τ³-bench）的
> banking 域——银行客服场景，97 个任务、698 篇政策文档、44 个工具，Agent 要
> 同时做知识检索、身份核验、多步业务操作。我的方法论是"**先造尺子再造
> Agent**"：冻结分层抽样的 24-task dev set + 独立 sealed 22-task holdout set、
> 固定 seed/检索/模型协议，然后每一版只加一个机制假设，用事件级 trace
> （Trace v2）归因每次变化。
> Runtime 从 V0 单 Agent 逐步演进：2-Agent 上下文隔离 → selective memory →
> **deterministic action harness**（三源校验、有明确依据才拦）→ **structured
> task state**（对象级、supersede、provenance）→ context management →
> **structured planning & replanning**（runtime PlanStore、tool-result 实体绑定
> 推进）。最终 V5 在同模型 holdout 上把 V4 的 4/22 提到 8/22（+4，纯机制红利），
> Dev 9/24 历史新高。过程中一堆负结果——全量 memory 注入、evidence 值信任、
> 文本 plan 协议、post-V5 的复用提示——每一个都被诚实记录、归因并回退。

**一句话版**：先造尺子，再造 Agent；每步改动量着来；证伪和成功一样认真。

**两个必须能脱口而出的数字**：
- Frozen V5：**Dev 9/24（37.5%）、Holdout 8/22（36.4%）**；
- **同模型 holdout 对比：V4 4/22 → V5 8/22（+4）**——最干净的机制证据。

---

## 二、2 分钟完整介绍（演进逻辑：评测 → context → harness → state → planning → holdout）

**先讲为什么这么做**（30 秒）：tau2-bench 的 banking 域同时考知识检索和长程
工具编排，单 Agent 基线 8/24，失败集中在长序列和参数错误。但 LLM 有 ±8pp 的
run-to-run 波动，所以我第一件事不是优化 Agent，而是把尺子做硬：分层抽样
24-task dev（按 required_documents 难度 3/7/14）+ seed=42 + BM25 + 固定模型，
另抽 22-task sealed holdout，开发期间零接触。

**然后按层讲演进**（60 秒）：
1. **Context 层（V1）**：2-Agent（Decision + Knowledge）物理隔离，KA 只回
   Evidence Packet。全量 memory 注入是负结果（KA tokens +72%），改成按需
   selective 检索后 KA tokens -74%。
2. **执行层（V2）**：参数值错误在执行前可确定性拦截——Action Harness 三源
   校验（tool schema / task state / KB 约束），原则"有明确依据才拦"。中间
   走过弯路：想用 KA 的 grounded values 校验每个参数，被证伪——KB 只有
   合法域没有 case-specific 正确值。
3. **状态层（V3）**：金额误绑的根因是参数级裸键——改成对象级
   `object.field=value[source]`，supersede 保留历史，实体解析。095 类多对象
   任务首次成功。
4. **上下文管理（V4）**：长任务旧 ToolResult 存根化。V4 也证明了文本
   `[PLAN]` 协议遵守率为 0——计划不能靠 prompt 约定。
5. **规划层（V5）**：planning tools 落 runtime PlanStore；步骤完成由真实
   tool result + 实体绑定驱动（多对象同工具不误完成）；条件 replanning；
   bounded completion guard。
6. **Holdout 验证（收口）**：V4 和 V5 在同一 holdout 上同模型对比——
   4/22 → 8/22，5 个 V4 全败任务首次翻上，Dev/Holdout 同量级证明不是过拟合。

**最后收尾**（30 秒）：freeze 之后我又做了一轮 post-V5 实验（一致性提醒/
状态复用提示/检索预算/覆盖检查），targeted smoke 看着都不错（tools -41%），
但全量 Dev 9/24→5/24，逐 trace 归因发现 4 个回退有明确机制因果链——
整体 rollback，保守冻结 V5。这个"最后一课"也进文档。

---

## 三、面经问答（V5 完整版）

### Q1：为什么先做评测框架，不先做 Agent？

**答**：因为不固定评测尺度，任何"改进"都无法证明。我实测同一 seed 跑两次
24 任务，成功率 25% 和 29.2%——LLM 天然有 ±8pp 波动。所以我反过来：
1. **冻结 dev set**：按 required_documents 分层抽样 24 个（3 简单/7 中等/
   14 困难），写进配置永不改动；另抽 22-task sealed holdout，开发期间零接触
2. **固定协议**：seed=42、BM25、固定模型、max_steps=60
3. **事件级 trace（Trace v2）**：每个动作一条事件（seq/span/event_type/
   actor），能重建执行树、做 token 成本核算——后来所有失败归因都靠它

做完这一步，每次改动都有可比性。**后来 post-V5 实验 9/24→5/24 的逐 task
归因（哪些是机制回退、哪些是 variance）也是直接翻 trace 翻出来的。**

---

### Q2：分层抽样怎么做的？为什么不随机抽？

**答**：banking 任务天然有难度维度——required_documents（完成任务要参考的
文档数，1~30 篇）。按它分层（1-4 简单、5-9 中等、10+ 困难）再按比例抽样。
随机抽的问题：旧 5-task 集成功率 80%，24-task 分层集只有 25~33%——随机集
偏简单，基线严重虚高。分层后难度曲线清晰：1-4 篇 67%、10-14 篇 10%、15+ 篇 0%。

---

### Q3：为什么用了 2-Agent？后来为什么没加第三个（Planner Agent）？

**答**：2-Agent 的动机是 V0 实测痛点——单 Agent 每次检索把 10 篇文档全文
塞进 context，42 次检索后从 3.7K 涨到 63K token。Knowledge Agent 物理隔离
（DA 的工具列表里根本没有 KB_search——结构性给不了，不是 prompt 说"别用"），
只回结构化 Evidence Packet（claim + source_doc_id，不许编文档 ID）。

**为什么没有 Planner Agent**：planning 我做成了 **runtime state + 结构化
tool call**（write_plan/update_plan/read_plan 落 PlanStore），而不是独立
Agent。三个理由：①计划更新是确定性操作，不需要另一个 LLM 的判断和不确定性；
②每次 handoff 有固定开销（V4 038 里 KA 占一半 LLM 调用）——再加一个 Agent
就是再加一份 context、再加一倍协调成本；③V4 已经证明**让 LLM 生成计划
文本没意义**（[PLAN] 遵守率 0），V5 的答案是让计划变成可编程状态，
执行层（实体绑定推进）自己做确定性计算。Planner Agent 解决不了
V4 暴露的真问题——"知道下一步做什么"不缺推理，缺的是**可持久化、可被
工具结果驱动推进的状态**。

---

### Q4：Task State 和 Memory 有什么区别？

**答**：这是项目里最容易混的一对概念，我用三次迭代才分清：

| | Working Memory（V1） | Task State（V3） |
|---|---|---|
| 形态 | 叙事级文本（facts/constraints/goals） | 对象级三元组 `object.field=value[source]` |
| 回答 | "用户想要什么"（摘要） | "世界现在是什么"（事实） |
| 结构 | 无 schema，靠检索相关性 | 有 supersede 链、实体索引、来源 provenance |
| 用途 | 给 LLM 的 context 块 | **给 Harness 做确定性校验的约束源** |

关键转折在 V2.3→V3：参数级的 `amount=96000` 会把"savings 余额"误绑到
"transfer 金额"上。改成 `account_sav_x.balance=96000` 和
`transfer_request.amount=500` 之后，同类误拦根治（095 首次成功）。
**Memory 是给 LLM 看的提示，Task State 是给确定性代码用的结构化事实**——
后者能参与"拦/不拦"的判定，前者不能。

---

### Q5：Harness 为什么是 deterministic 的？

**答**：因为要拦的动作必须有**可解释的依据**。Harness 每次拒绝都带
constraint_source（tool_schema / task_state / knowledge）和 correction 指令
（"set amount to 500"），Agent 拿到就能修。如果是另一个 LLM 来判断拦不拦，
那 rejection 本身就不可复现、不可调试，Agent 也不知道该信还是该辩。

三源校验："有明确依据才拦，不确定放行"。这个原则是被教训逼出来的：
V2.1 我曾想用 KA 的 grounded values 校验每个参数，结果 KA 输出过假枚举、
`MM/DD/YYYY` 占位符、参数名当值——**"知识库能给出 case-specific 正确值"
这个前提本身不成立**。所以最终的拦截依据只留三种可靠源：工具 schema（官方
枚举/类型）、Task State（用户/工具确认的值）、KB 明确约束（enum 集合/阈值）。

---

### Q6："有明确依据才拦"具体是什么意思？

**答**：字面意思——**拒绝一个调用必须能指出依据在哪，指不出来就放行**。
反例驱动设计：
- V2.2 之前：evidence 校验拦截了"用户 savings $96,000"绑到 transfer
  amount——依据本身是错的（把描述当指令），这是误拦
- V3 之后：多对象同名 amount、bare 字段歧义 → latest() 直接返回 None 放行，
  哪怕"可能有问题"——因为没有唯一明确依据
- 好处：0 误拦（V2.2 起全程保持）+ 拒绝信息可执行（correction 行）。
  Agent 被拒后能自修（recovery 闭环：拦→修→过→执行）。

**对比 post-V5 的教训**：Step 2A 的复用提示名义上"非硬拦"，但实现走的是
拦截-再生循环（注入提示后该轮调用不执行）——**设计意图是提示，控制流
是阻断**。010/021/037 三个任务里 `get_referrals_by_user` 这类首次合法查询
被反复"提示"到任务失败。所以现在我对任何"提醒"机制都会先问：它的控制流
到底拦不拦执行？

---

### Q7：为什么 V3 success 比 V0 低（5/24 vs 8/24）还保留 V3？

**答**：因为 V3 买到的东西不在 success 上：
1. **质量指标全面占优**：tools 600→251、max_steps 7→2、误拦 0、死循环 0
   （V0 时代 58 次）；095 这种多对象任务 V0/V1/V2 全败、V3 首过
2. **它是后续所有层的地基**：没有对象级 Task State，V5 的"步骤实体绑定
   推进"根本没法做——PlanStore 的 entities 就是对 Task State 对象的引用
3. **同模型公平对比**：V0 和 V3 都是 deepseek，5/24 vs 8/24 是真实的架构
   代价——2-agent 让 LLM calls 翻倍（646→1071），success 上没赚回来。
   但 V5 证明了这笔投资是前置的：9/24 是踩在 V3 的地基上拿到的。

一句话：V3 是**付出了 success 短期代价换执行结构确定性**的阶段——
后来 V5 的全部收益都建立在这个结构上。

---

### Q8：为什么 V5 planning 有价值？怎么证明不是 benchmark hacking？

**答（两个子问题）**：

**价值**：V4 证明顽固任务不是"忘了做过什么"（Task State 已解决），是
"不知道下一步做什么"。行为观察（"你调了 X 5 次"）也没用——重复是
"不知道什么是对的"的结果。V5 给的是**未来的步骤**（plan steps）+
**确定性推进**（tool result + 实体绑定 → step completed）+
**条件 replanning**（blocker/add/remove）。021/024（Dev）和
031/047/052/063/089（Holdout）这些之前全败的顽固任务首次通过。

**怎么证明不是 hacking**：
1. **Runtime 不接触评测内部**：不读 evaluation_criteria/required_documents/
   gold actions——有 integrity 测试（5 项，CI 常跑）扫描代码访问模式
2. **工具发现走官方路径**：discoverable tool schema 只能通过
   unlock_discoverable_agent_tool 解锁后从 unlock state 读——Resolver
   代码里明确禁止调 get_discoverable_tools()（那是上帝视角），integrity
   测试守着这条边界
3. **Prompt 零泄漏**：扫描 agents/ 全部字符串常量，真实工具名 0 出现
4. **Dev/Holdout 分离**：22-task holdout 是 seed 程序化抽样、开发期间
   零接触，只在 freeze 后各跑一次；仓库里没有 holdout 的运行产物
   （integrity 测试验证）
5. **最干净的证据形态**：V4→V5 holdout 同模型（均 qwen3.8-flash）对比
   4/22→8/22——没有模型红利、没有 holdout 调参空间，+4 只能是机制

---

### Q9：Dev 和 Holdout 怎么设计的？

**答**：Dev 24-task：分层抽样、开发期间反复用，所有失败分析/调参都在它
上面做。Holdout 22-task：从剩余任务里 seed=20260903 程序化抽样、配置文件
sealed、开发期间**一次都没跑过、一个 trace 都没看过**。Freeze 后 V4/V5 各在
独立 worktree（代码快照不可变）跑一次。结果：V5 Dev 9/24 与 Holdout 8/22
同量级——说明 Dev 提升泛化，不是在 Dev 上过拟合。协议上"V5 不在 holdout
结果出来后做任何修改"，包括那次 038 回落（LLM 路径波动）也没有据此调参。

---

### Q10：为什么 Step 0-3 targeted 有效但最后还是回退了？

**答**：这是收口阶段最重要的一课。四个机制（plan-execution 一致性提醒、
状态复用提示、KA 检索预算、goal coverage）在 targeted smoke（3-4 task）
上全都触发、tools 显著下降（077: 47→12）。但全量 24-task Dev 从 9/24 掉到
5/24。逐 trace 归因（不跑任何新 LLM，只翻 results.json + trace v2 + diff）：

- **4 个回退有明确机制因果链**：010/021/037 是复用提示的拦截循环——
  判定条件"实体在 Task State"错了，**实体已知 ≠ 查询结果已知**（accounts
  记录没查过，但 user_id 查过 → 提示触发 → 调用不执行 → DA 再提 → 再拦）；
  024 是检索预算在第 8 次截断，bronze_001（$500 bonus 事实）检索到了但
  没进 evidence packet，DA 推荐了错误的卡
- **2 个更像 variance**：007 是 DA 忘查时间、幻觉了个日期（零机制介入）；
  003 证据链不闭合
- **tools -41% 的去向**：89% 的节省来自 13 个两边都失败的任务（省的是
  死循环 token，不产生 reward）——效率收益和 success 回退发生在不同任务群

**元教训**：targeted smoke 的机制触发率不能预测全量 success——037 在
targeted 视角只是"重复查询减少"，全量里是灾难。**全量 Dev 是唯一可信的
freeze 门槛**。最终 conservative rollback，因为冻结条件（"无明显退化"）
不满足，且修复 Step 2A 判定条件需要再跑全量验证，风险/收益不划算。

---

### Q11：为什么没有继续做 V6？

**答**：三个理由：
1. **核心结论已经拿到**：V5 在同模型 holdout 上 +4 是干净的机制证据，
   Dev/Holdout 同量级证明泛化——"structured planning 有效"这个故事完整了
2. **剩余失败不在我能确定性解决的层**：逐 task 看，剩下的 026/053/054/070
   是业务判断错误（选错 dispute reason、算错余额）——工具序列对、plan 推进
   对、状态对，最后一步的"判断"错。这一层（D 方向）证据不足
3. **post-V5 实验恰好说明了继续堆机制的风险**：我试了四个"确定性提醒"
   机制，结果证明**在执行结构层继续加干预，收益递减、扰动递增**。
   在 9/24 的基线上，-4 的下行风险远大于 +1~2 的期望收益

工程判断：项目目标（评估驱动的 Agent Runtime + 干净的机制证据 + 完整的
正负结果记录）已达成，边际投入的期望值是负的。**知道何时停比知道怎么
继续更难**。

---

### Q12：这个项目最重要的失败实验是什么？

**答**：候选有四个，按时间讲（面试官要听的是"你怎么对待失败"）：

1. **V1.1 全量 memory 注入**（最早）：假设"外部化记忆减少重复"，结果
   KA tokens +72%——不加约束的记忆比没有更糟。教训：context 管理必须
   按需不能全量
2. **V2.1 信任 KA grounded values**（最深刻）：穿透 wrapper 校验内层参数
   机制上成功了，但**根基假设被证伪**——"KA 能给每个业务参数一个正确值"
   不成立（KB 记录的是合法域，case-specific 正确值来自用户/环境）。
   错误形态还是开放集（假枚举/占位符/参数名当值/跨 packet 矛盾）——
   修复成本随形态线性增长。教训：**校验机制的上限取决于它依据的数据
   质量**；换了三源结构（schema/state/KB 明确约束）才到 0 误拦
3. **V4 文本 [PLAN] 协议**（最干脆）：遵守率 0。计划必须存在于 runtime
   state，不能靠 prompt 约定。V5 的 planning tools 直接是它的答案
4. **Post-V5 Step 0-3**（最新、离收口最近）：机制在 targeted 上有效、
   全量上 -4，逐 trace 归因后果断 rollback。教训：**smoke 的正结果不能
   外推；"非硬拦"的设计意图要核对控制流；效率收益和 success 回退可能
   发生在不同任务群**

如果只能讲一个：讲 V2.1——它最完整地展示了"机制成功但假设错误"时
怎么办（降级假设而不是硬修机制），以及这如何直接塑造了贯穿到 V5 的
"有明确依据才拦"原则。

---

### Q13：遇到过最坑的工程 bug 是什么？（保留原版高频题）

**答**：
1. **429 限流污染评测数据**：runner 加任务级重试（冷却递增，最多 5 次）；
   后来发现裸匹配 `"429" in msg` 会误判 "4290 tokens"——收紧成 code 匹配
2. **timeout 杀掉 6 小时评测**：24 任务实际 2.5h，脚本 timeout 7200 在 21/24
   处强杀两次——改用 setsid + disown 脱离会话跑
3. **as_tool 的 name 不生效**：tau2 Tool name 是派生属性，构造参数无效——
   内层函数直接命名。读源码比猜快
4. **插桩 patch 打不到点**：各模块 `from x import generate`，import 时符号
   已绑定——必须 patch 7 个调用方模块，patch 源头没用

---

### Q14：如果重来一遍，你会改什么？

**答**：
1. **模型口径从 V0 就固定**——V3(deepseek) vs V5(qwen) 的 Dev 对比至今
   要带着"混模型"的脚注才能讲。Holdout 干净是因为运气好（V4/V5 都在
   切换后的 qwen 上）+ 后来主动固定。**评测口径变更本身就是需要管理的
   风险**
2. **更早做 sealed holdout**——它让我最后阶段的每个结论都有"泛化"背书
3. **对"提醒类"机制先做控制流审计**——post-V5 的教训：写机制之前先
   回答"它的实现到底拦不拦执行"，能省一轮全量 Dev
4. **2-agent 的 KA 从第一天就有跨 handoff memory**（V1 控制变量的代价，
   事先没估到空 packet 问题一半来自这个）

---

## 四、数字速查（面试时随口要能报）

| 项 | 数值 |
|---|---|
| Dev set | banking 24 任务（分层 3/7/14）+ telecom 20 任务（V0 期）|
| Holdout | 22 任务 sealed（seed=20260903，开发期零接触）|
| **V5 最终** | **Dev 9/24（37.5%）/ Holdout 8/22（36.4%）** |
| **同模型对比** | **V4 4/22 → V5 8/22（+4，均 qwen3.8-flash）** |
| V0 基线 | 8/24（deepseek）；telecom 90%（20 任务）|
| V3 | 5/24（deepseek，同 V0——架构代价真实）|
| 效率 | KA tokens -74%（V1.2）；tools 600→251（V0→V3）|
| 误拦/死循环 | 0 误拦（V2.2 起）；死循环 58→3→0 |
| Post-V5 | tools -41% 但 Dev 9/24→5/24 → rollback |
| 知识库 | 698 篇文档（2.9MB），BM25 毫秒级 |
| integrity | 5 项测试全绿（CI 常跑）|

---

## 五、可能的追问与陷阱

**Q：9/24 的绝对值不高，怎么讲？**
A：任务集是按难度分层的（14 个困难任务要 10+ 篇文档），15+ 篇那档 V0 就是
0%。重点从来不是绝对值，是**同口径下可归因的差值**：同模型 holdout +4/22
是最干净的机制证据。而且 V5 的成功集中在"长程多步任务"上——正是
structured planning 针对的那类。

**Q：V3→V5 的 Dev 提升是不是模型换的好事？**
A：承认混模型（deepseek→qwen），其中 002/007 两个翻转是模型红利——我在
复盘文档里明确标注不能计入机制。但两个反驳：①Holdout 的 V4→V5 是同模型，
+4 干净；②021/024 这两个 V0/V1.2/V3/V4 全败的顽固任务是 planning 机制
拿下的（有 plan_written/step_progressed 的 trace 证据）。

**Q：怎么保证 holdout 真的没泄漏？**
A：三层：流程上（worktree 隔离、开发期零接触）、代码上（integrity 测试
验证 resolver 不碰上帝视角、runtime 不读评测字段、无 holdout 运行产物
目录）、结果上（V5 Dev 与 Holdout 同量级，且 holdout 上也有 038 这种
回落——如果调过参不会留这个）。

**Q：为什么不用 LangChain/LlamaIndex？**
A：tau2-bench 提供端到端的用户模拟 + 环境工具 + DB 断言评分，这是 RAG
框架给不了的。而且这个项目的核心工作是 harness/state/plan 这些
**框架之上的确定性层**——自己写反而透明可审计。

**Q：telecom 和 banking 为什么分开维护？**
A：telecom 是纯工具编排难度（V0 期 90%），banking 是知识密集+长程难度。
本项目的主战场是 banking（V1 之后全部演进都在它上面），telecom 是 V0 的
对照域。

**Q：你个人在这个项目里最大的成长是什么？**
A：学会区分"机制成功"和"假设成立"——V2.1 穿透机制成功了但假设错了；
post-V5 效率机制成功了但 success 假设错了。以及：**负结果写清楚比正结果
写漂亮更值钱**——现在仓库里 5 个负结果都有完整归因，这是我能对着任何
一个 trace 讲清楚每一步决策的底气。
