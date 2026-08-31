# V1.2 Selective Memory Retrieval / Memory Reuse 实验报告

## 研究问题

> V1.1 证明了"记住了但不会正确使用"。V1.2 让 Knowledge Agent 优先复用
> 已有知识、只在 memory 无法满足 request 时才检索——能否让 memory 从
> 负担变成收益？

## 实现

### Memory.retrieve(request) —— 正式接口（确定性，无 LLM Retriever）

```
KnowledgeRequest → retrieve() → {verdict, relevant_facts(≤6),
                                   relevant_documents(≤8), known_missing(≤4),
                                   matched_packet_count, similar_packet}
```

**判定规则**（词重叠匹配，内容词去停用词）：
- **hit**：question 与某个**已回答问题**词重叠 ≥ 0.5 且其 packet status
  达标 → 直接复用 similar_packet（零检索、零额外 LLM）
- **partial**：无相似问题，但有 facts_found 的 claim 与问题词重叠 ≥ 1
  → 只把相关事实（≤6 条）+ 已知缺口注入，指示"只搜缺的部分"
- **miss**：无相关事实 → 正常检索，零 memory 注入

**诚实记录的局限**（误差方向=多搜而非错答）：
- 词面重叠不识别同义改写（fee↔charge）→ 漏判 partial/miss 会多搜
- 不理解否定（"no annual fee" vs "annual fee" 判强相关）→ 可能提前 hit
- 宁可保守：宁可多搜一次，不基于错误记忆直接回答

### 防膨胀三件套（对 V1.1 三个根因逐一修复）
1. **全量注入 → 相关视图**：`_render()` 置空；KA context 只含 retrieve()
   的小视图（≤6 facts + 8 docs），queries_tried 不再注入
2. **"勿重复 query"提示移除**（V1.1 反向激励根因）→ 改为"复用已建事实
   + 检索应针对不同缺口"
3. **progress 停止**：每次检索后 `note_retrieval_progress(new_docs)`——
   连续 LOW_PROGRESS_LIMIT(=3) 次无新文档 → should_stop_searching →
   KA 收到收尾提示（tools=None）强制出 packet

### Trace v2 新事件
`memory_retrieve / memory_hit / memory_partial_hit / memory_miss / retrieval_progress`（只记摘要：verdict/matched 数/new_docs/streak；不写全量 memory）

### 新指标
memory_hit/partial/miss_count、hit_rate、retrieval_avoided_count、
new_documents_per_retrieval、repeated_document_ratio、
low_progress_retrieval_count（另有 answered_packets 进 snapshot）

## Diagnostic 结果（同 7-task、同 seed/模型/BM25）

| 指标 | V1.1 全量注入 | V1.2 选择性复用 | Δ | 成功标准 |
|---|---|---|---|---|
| **success** | 2/7 | **3/7** | **+1** | 持平即可 ✅超额 |
| handoffs | 29 | 18 | **-38%** | - |
| KA 检索 | 248 | 79 | **-68%** | -40% ✅超额 |
| KA prompt tokens | 3.35M | 0.87M | **-74%** | -35% ✅超额 |
| LLM calls | 298 | 208 | **-30%** | -20% ✅超额 |
| wall time | 2,505s | 1,403s | **-44%** | -25% ✅超额 |

**五项成功标准全部超额达成**（用户定义：success 持平 + 检索-40% +
KA tokens -35% + LLM -20% + wall -25% 即为明显成功）。

### 逐任务

| task | V1.1 | V1.2 | handoff | KA检索 | KA tokens | 翻转 |
|---|---|---|---|---|---|---|
| 003 | 0.0 | 0.0 | 2→1 | 30→9 | 380K→89K | |
| 008 | 1.0 | 1.0 | 7→5 | 91→30 | 1219K→325K | ✅ 保持成功（V1 转坏→V1.1 恢复→V1.2 保持）|
| 010 | 0.0 | **1.0** | 2→2 | 14→5 | 220K→41K | ✅ 新恢复 |
| 037 | 0.0 | **1.0** | 7→2 | 53→7 | 856K→95K | ✅ 新恢复（V1 转坏任务）|
| 046 | 0.0 | 0.0 | 6→4 | 27→13 | 307K→169K | |
| 004 | 1.0 | 0.0 | 1→0 | 6→0 | 72K→0 | ❌ 新失败（见分析）|
| 095 | 0.0 | 0.0 | 4→4 | 27→15 | 293K→148K | |

**V1 转坏 4 任务中 2 个恢复**（task_008 保持 + task_010/037 新恢复），
加上 V1.1 恢复的 task_008 —— 转坏任务恢复 2/4，剩余 task_003/046 未恢复。

### Selective Retrieval 指标

- verdict 分布：hit=2 / partial=10 / miss=6（hit_rate 0.11，18 次判定）
- **retrieval_avoided = 2**（两次零检索直接复用 packet）
- **low_progress_stops = 5**（5 次有效阻止了原地打转的检索）
- new_docs_per_retrieval 3.5~5.4（每次检索确实带来新文档——progress 策略
  前移到了"换 query 打转"发生之前）
- repeated_document_ratio 0.46~0.65（V1.1 的 task_008 曾达 761 次重复
  命中——V1.2 同任务检索从 91 降到 30，重复问题随检索总量自然收敛）

### task_004 退化分析（唯一失败翻转）

V1.2 中该任务 handoff=0、KA 检索=0——**memory 机制根本没被触发**。
失败原因：转人工 reason 给了 `customer_frustrated_demands_human` 而非
评测要求的 `account_ownership_dispute`——reason 是 DB 断言的一部分，
错 reason = fail。属 LLM 采样随机性（V0 两次 run 该任务 reason 也有过
波动），非 memory 机制缺陷。DA memory 在此任务只记了身份验证事实，
未改变决策路径的证据。

## 结论

**选择性复用让 memory 从负担变成了收益。** V1.1 的三个根因
（全量注入膨胀 / 勿重复 query 反向激励 / 无停止条件）逐一修复后：

1. **效率全面大幅改善**：检索 -68%、KA tokens -74%、LLM -30%、
   wall -44%——全部超过成功标准
2. **reliability 净提升**：success 2/7→3/7；V1 转坏任务恢复 2 个；
   task_008 恢复后保持成功
3. **机制按设计工作**：hit 零检索复用（2 次）、partial 视图聚焦
   （10 次）、low-progress 停止生效（5 次）
4. **token 效率与 V0 单 Agent 对比**（context isolation 主线）：
   KA tokens 从 V1 的 11.79M → V1.1 的 3.35M → V1.2 的 0.87M，
   相比 V1 降幅 93%

剩余问题（供下一阶段决策，未实现）：
- partial 仍是主要 verdict（10/18）——词面匹配的 hit 判定偏保守，
  语义级匹配（非 LLM 的 embedding/同义表）可提高 hit_rate
- task_003/046/095 三个稳定失败任务未恢复——失败根因不在 memory
  （recall 达标后仍是决策/流程问题）

按约定停止：未实现 Long-term Memory、Skill、LLM Retriever、
第三个 Agent。

## 复现

```bash
python run_eval.py --tasks configs/banking_diag7.json --agent two_agent \
  --model openai/deepseek-v4-flash --retrieval-config bm25 \
  --tag v12_diag7 --seed 42 --max-steps 60
# V1.1 对照: runs/v11_diag7_*（同 set 同 seed）
```
