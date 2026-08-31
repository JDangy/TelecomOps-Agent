# V1.1 Per-Agent Memory 实验报告（diagnostic 结果：负结果）

## 研究问题

> 给每个 Agent 加 task-scoped working memory，能否减少 V1 的重复检索、
> handoff 循环、token 浪费，从而改善 reliability-efficiency？

## 实现（全部完成）

### Memory 抽象（agents/memory/）
- `base.py`：AgentMemory 统一接口（start_task / read / update / snapshot /
  end_task / reset + context_block 预算渲染）；MemoryError 生命周期保护
- `working_memory.py`：
  - DecisionWorkingMemory：user_constraints / verified_facts / current_goal /
    open_questions / decisions_made（事件驱动更新，无 LLM）
  - KnowledgeWorkingMemory：queries_tried / documents_seen / facts_found /
    open_questions / previous_requests（检索 query/doc、packet facts 程序化写入）
- `long_term.py`：仅预留接口（retrieve/store 抛 NotImplementedError，
  enabled=False）——不做向量库/embedding/跨任务检索/Skill

### 集成
- **隔离保证**：双 memory 实例独立；KA/DA 互不读对方 memory；跨 Agent 信息
  只走 KnowledgeRequest / EvidencePacket
- **跨 task 零泄漏**：tau2 每 task 重建 agent（factory 调用）→ memory 实例
  天然 task-scoped；runner 补 start_task/end_task 钩子（end 发
  memory_snapshot 完整快照进 trace）
- **KA context 不累积**：每次 handoff context = system + memory block + 本次
  request（不带历史 handoff 原始 messages）
- **DA memory 动态注入**：每次 generate 前把 memory block 拼到 system 消息
  尾部（_messages_with_memory，不改 state）
- KnowledgeRequest 结构化：question / known_constraints / known_facts /
  needed_information（DA 已知约束/事实自动补充进 request）
- EvidencePacket 增加 **status**（sufficient / partial / insufficient，与
  confidence 分离——"对事实有信心"≠"证据够完成任务"）
- Trace v2 新事件：memory_update（只记增量）/ memory_reset / memory_snapshot
  （task end 一次完整快照）
- 新指标：ka_unique/repeated_queries、unique_documents_seen、
  repeated_document_hits、retrievals_per_handoff、memory_fact/doc_count、
  evidence_status_counts

## Diagnostic 实验（7-task，同 seed/模型/BM25/协议）

任务构成：V1 转坏的 4 个（task_003/008/010/037）+ 转好的 1 个（task_046）+
稳定成功 1 个（task_004）+ 稳定失败 1 个（task_095）。

### 结果

| 指标 | V1 无memory | V1.1 有memory | Δ |
|---|---|---|---|
| success | 2/7 | 2/7 | 持平 |
| handoffs | 19 | 29 | **+53%** ❌ |
| KA 检索 | 150 | 248 | **+65%** ❌ |
| KA prompt tokens | 1.94M | 3.35M | **+72%** ❌ |
| wall time | 1,300s | 2,505s | **+93%** ❌ |

逐任务翻转：task_008 0→1（恢复）✅；task_046 1→0（新失败）❌；其余不变。

### Memory 指标揭示的根因

| task | unique_q | repeated_q | unique_docs | repeated_doc_hits | mem_facts |
|---|---|---|---|---|---|
| 003 | 30 | 0 | 94 | 206 | 22 |
| 008 | 88 | 3 | 150 | **761** | **40(满)** |
| 037 | 53 | 0 | 97 | **457** | **40(满)** |
| 046 | 27 | 0 | 104 | 166 | **40(满)** |

三个病因：

**① "勿重复 query" 清单反向激励**。repeated_q≈0（KA 确实不重复 query 了），
但 repeated_doc_hits 暴涨（task_008 达 761）——KA 拿着"这些 query 搜过了"的
清单，就不断**发明新 query**（unique_q 30→88），每个新 query 仍命中同一批
文档，10 篇全文照旧进 context。**约束了 query 重复，没约束语义重复。**

**② memory 自身膨胀成新 context 源**。facts_found 频繁打满 40 条上限
（task_008/037/046 都 40），memory block 注入逼近 1.5K chars 预算——
KA 每次 handoff 背着越来越重的"已知事实"清单，但没有机制判断哪些 facts
对当前问题有用，全量注入徒增 prompt。

**③ handoff 更多了**（19→29）：DA 看到自己的 memory（open_questions 等）
后更倾向"再确认一次"，且结构化 request 让 DA 觉得提问成本低——
task_037 的 7 次 handoff 全是合理但过细的子问题（"换卡地址能不能改"
"正确的 transfer reason code 是什么"…），单次 handoff 平均 7.6 次检索。

task_046（V1 唯一转好）在 V1.1 反而转坏：检索 5→27、wall 67→368s——
memory 打断了这个简单任务的直接路径。

### 一句话根因

**Memory 记住了"做过什么"，但没有能力判断"什么值得再做/不用做"——
在缺乏相关性判断的情况下，记忆清单变成了探索激励。**

## 对照：机制有效的部分

- task_004（KA tokens 282K→72K，-74%；检索 17→6）：问题单一、memory 直接
  命中已建 facts 时，防重复**确实大幅省**
- task_095（检索 43→27，KA tokens 586K→293K，-50%）：同上
- task_008 成功率恢复（V1 转坏→V1.1 转好）
- 隔离/生命周期/事件记录等**机制全部按设计工作**（无泄漏、无崩溃、
  trace 完整）

即：**当任务的自然信息需求收敛时，memory 有效；当任务需要发散探索时，
memory 的"勿重复"约束弊大于利。**

## 结论

Per-agent working memory 的**机制实现是完备的**（隔离、生命周期、事件、
指标全部可观测），但**当前形态的 memory 无净收益**——efficiency 全面
恶化，success 持平。诊断价值明确，指明了 memory 要有效必须解决：

1. **语义级去重**：不是记 query 字符串，而是记"这个信息需求已满足"
   （packet 级缓存：相同问题直接返回既有 packet）
2. **facts 相关性过滤**：注入 memory 前按当前 request 筛选，而非全量
3. **handoff 成本感知**：DA 决定 ask 前先查自己的 verified_facts（已做）
   且 KA 对"已回答过的问题"短路返回（未做，这是下一步最高杠杆的修复）

按计划停止：不实现 Long-term Memory / Skill / 第三个 Agent /
packet 缓存（留给 V1.2 决策）。

## 复现

```bash
# V1.1 diagnostic（memory 版）
python run_eval.py --tasks configs/banking_diag7.json --agent two_agent \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v11_diag7 --seed 42
# V1 对照数据来自 runs/banking_v1_2agent_*（24-task run 的同 7 任务子集）
```
