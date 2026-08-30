# V1 2-Agent 架构实验报告

## 研究问题

> Does context-isolated specialist decomposition improve the reliability-efficiency
> trade-off of a knowledge-intensive tool-using agent?

**V1 变量**：把知识检索与证据整理从主决策 Agent 的上下文中隔离出来
（Decision Agent + Knowledge Agent + Evidence Packet），其余全部不变。

## 架构

```
User → Decision Agent（业务工具 + ask_knowledge_agent）
         └─ handoff → Knowledge Agent（独立 context，BM25）
                        自主 query → 检索 → 判断证据充分性 →（多轮）
                        → Evidence Packet（answer + facts + source_doc_ids + confidence）
         ← 只返回压缩 packet，原始 KB 全文不进 Decision Agent
```

- Decision Agent 工具列表物理上无 KB_search/grep/shell（隔离是结构性的）
- Evidence Packet: answer / facts[{claim, source_doc_id}] / relevant_document_ids /
  missing_information / confidence；不返回大段原文；不许编造 doc ID

## 实验设置（严格同条件）

| 项 | 值 |
|---|---|
| 任务 | 24-task banking dev set（分层抽样，冻结）|
| 模型 | openai/deepseek-v4-flash（thinking 保留）|
| 检索 | bm25（同一 variant）|
| user simulator | 同模型 |
| grading | tau2 EvaluationType.ALL（同一 evaluator）|
| seed / max_steps | 42 / 60 |
| API | winterapi（3 账户 key，双 run 并行，0 限流）|
| trials | V0 = 1 trial, V1 = 1 trial |

V0 run: `banking_v0_winter_20260830_235613`　V1 run: `banking_v1_2agent_20260831_011111`

## 结果

### 核心对比

| 指标 | V0 单Agent | V1 2-Agent | Δ |
|---|---|---|---|
| **success rate** | **33.3%** (8/24) | **20.8%** (5/24) | **-12.5pp** ↓ |
| required-doc recall* | 80.4% | 84.2% | +3.8pp ↑ |
| 总 wall time | 7,681s (2.13h) | 8,969s (2.49h) | +16.8% ↑ |
| 平均 task wall | 320s | 374s | +16.8% ↑ |
| 总 LLM 调用 | 646 | 1,195 | +84.9% ↑ |
| 平均 LLM 延迟 | 11.9s | 6.9s | -42.5% ↓（每次调用更小）|
| **总 prompt tokens** | **20.16M** | **17.48M** | **-13.3% ↓** |
| 平均对话轮数 | 7.08 | 7.75 | +0.67 |

*V1 recall 从 v2 events 的 KA 检索记录计算（v1 trace 口径盲区，KA 检索不经 orchestrator；
v1 trace 的 retrieval_calls=0 是测量位置问题，KA 实际检索 904 次 / 平均 37.7 次/任务）。

### 2-Agent 分解（V1）

| 指标 | Decision Agent | Knowledge Agent |
|---|---|---|
| LLM 调用 | 423 (17.6/任务) | 586 (24.4/任务) |
| prompt tokens | 5.15M (214.6K/任务) | 11.79M (491.2K/任务) |
| completion tokens | 375K | 478K |
| max prompt (单次) | 37.6K | 116.0K |
| 检索次数 | 0（被隔离）| 904 (37.7/任务) |
| handoff 次数 | 124 (5.2/任务) | — |
| Evidence Packet | — | 平均 3.9K chars/次 |

### 逐任务翻转（同 seed 下）

- V1 转好（1）：task_046
- V1 转坏（4）：task_003, task_008, task_010, task_037
- 一致（19）：其余

## 回答研究问题

**1. Multi-Agent 是否降低 Decision Agent 的 context size？**
**是，显著。** Decision Agent max prompt = 37.6K tokens vs V0 单 Agent 同任务
实测 63K+（task_001 V0 val2 trace）。per-task 平均 214.6K DA tokens 中，
主要来自对话轮累积而非检索 dump——检索全文（KA 平均 491K/任务）被完全隔离在
Knowledge Agent 的独立 context 里。**Context isolation 机制本身有效。**

**2. 总 token 是否反而增加？**
**没有，总 prompt 反而少了 13.3%**（20.16M → 17.48M）。但注意分配：
DA 5.15M + KA 11.79M。KA 的 11.79M 说明 Knowledge Agent 在独立 context 里
反复读全文（平均每任务 37.7 次检索 × 每次 10 篇 × 全文），抵消了 DA 的
节省。若 KA 有 packet 级 stop 策略优化，还有很大下降空间。

**3. wall time 是否增加？**
**是，+16.8%。** 单次 LLM 延迟降 42.5%（每次处理的上下文更小），但调用
次数 +84.9%（handoff 开销 + KA 多轮），净效果变慢。

**4. retrieval quality 是否变化？**
**略升**：84.2% vs 80.4%。KA 的自主多轮检索在召回上有小优势。

**5. success/reliability 是否提升？**
**没有，下降 12.5pp**（33.3% → 20.8%）。注意两点：
(a) LLM 随机性：V0 基线两次完整 run 25%/29.2%，33.3% 本身偏高；V1 20.8%
可能偏低。但 4 个任务转坏 vs 1 个转好方向不利。
(b) 逐任务看转坏原因集中在：handoff 循环消耗轮数（KA 问 5.2 次/任务），
Decision Agent 在等待 packet 后倾向过早收尾。

**6. 新增了哪些 coordination failures？**
- **空 packet 浪费**：首次 handoff 太宽泛（"所有信用卡"）时 KA 返回空/低置信
  packet，DA 需再问——3-task smoke 里 task_001 前 2 次 handoff 全空
- **handoff 循环**：5.2 次/任务（smoke 单任务最多 6 次），每次 handoff =
  KA 全新 context 重新检索，无跨 handoff 记忆（V1 有意不做 memory）
- **过早收尾**：task_002 类任务 DA 收到 medium/low 置信 packet 后倾向直接
  回复用户而非追问 KA

## 结论与下一步

**Context isolation 达成了 token 层面的目标（-13.3% prompt、DA context 大幅缩小、
recall 略升），但 reliability 没有随之改善——瓶颈转移到了 coordination 层**
（handoff 循环、空 packet、过早收尾）。这印证了假设的前半句、证伪了后半句：
单纯职责隔离不自动带来可靠性提升，需要配合 coordination 优化（如 KA 跨 handoff
记忆、packet 质量门槛、handoff 数量控制）。

按计划停在 V1。未实现：Verifier / Memory / 第三个 Agent / Query Rewriter /
Dense / Reranker。

## 文件清单

- `agents/two_agent.py`（新建，648 行）：split_tools / EvidencePacket / KnowledgeAgent /
  DecisionAgent（拦截式 handoff）/ create_two_agent factory
- `agents/registry.py`：注册 two_agent
- `eval/runner.py`：factory 注册打通、handoff 摘要注入 v1 trace、
  _extract_two_agent_metrics 协作指标
- `eval/instrumentation.py`：knowledge_agent actor 映射 + agents.two_agent patch

## 复现

```bash
# V0
python run_eval.py --tasks configs/banking_dev_tasks.json --agent baseline \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag banking_v0 --seed 42
# V1
python run_eval.py --tasks configs/banking_dev_tasks.json --agent two_agent \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag banking_v1 --seed 42
```
