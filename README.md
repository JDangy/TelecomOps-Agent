# TelecomOps-Agent

Evaluation-driven Agent system 实习项目：在电信客服 domain 上构建、评估并逐步改进一个客服 Agent。

底层 benchmark 使用 [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench)（τ³-bench）的 **telecom** domain。**本项目代码与官方 benchmark 解耦**：tau2-bench 只作为底层依赖（`third_party/tau2-bench`，editable 安装），所有 evaluation 逻辑、结果存储、指标计算都在本项目内实现。

## 当前阶段：V0 baseline

- **Agent**：tau2 官方 `llm_agent`（DeepSeek 驱动），作为可对比的 baseline
- **Eval set**：固定的 dev sets，任务 ID 已冻结——V0/V1/V2 永远在同一批任务上对比
  - telecom：20 个 task（`configs/dev_tasks.json`），来源为 tau2 telecom small split
  - banking_knowledge（RAG）：24 个 task（`configs/banking_dev_tasks.json`），按 required_documents 分层抽样（简单 3 / 中等 7 / 困难 14），BM25 检索；旧 5-task 集保留于 `banking_dev_tasks_v1_5task.json`
- **评测协议**：`bash run_benchmark.sh [telecom|banking] [repeats]`，模型固定 `openai/deepseek-v4-flash`（保留 thinking）、seed=42、max_steps=60；任务级 429 限流自动重试（不污染评测数据）
- **输出**：每次 run 一个独立 `runs/<run_id>/` 目录

**本阶段明确不做**：query rewriter、Memory、reranker、verifier、RL、多 Agent、复杂 Planner、网页 UI。Agent 看不到 `evaluation_criteria` / `required_documents` / 标准答案。

### V0 实测结果（baseline，deepseek-v4-flash + thinking）

| 场景 | success rate | 备注 |
|---|---|---|
| telecom（20 task）| **90%**（18/20）| 失败 2 个：loop / wrong_transfer |
| banking + BM25（24 task 分层）| **27.1%**（6.5/24 平均）| 两次完整 run 取平均：25%（6/24）+ 29.2%（7/24）|
| banking + BM25（旧 5 task，参考）| 80%（4/5）| 5 个任务偏简单（required_docs 1~6），不代表全库难度 |

> 注意：24-task 分层集是对 banking_knowledge 全库（97 task，required_docs 1~30）的代表性抽样。**V0 banking 基线 = 27.1%（两次运行平均）**；两次运行相差 4.2pp（25% vs 29.2%），说明 LLM 有随机性，对比时应看平均而非单次。旧的 80% 因任务集偏简单而虚高。V1 改进将以此为对比锚点。

## 版本历史

| 版本 | 内容 | 状态 |
|---|---|---|
| V0 | 评估框架 + telecom baseline + banking_knowledge/BM25（24-task 分层 dev set + 429 自动重试）| ✅ 当前 |
| V1 | 计划中：基于失败分析的 Agent 改进 | ⏳ |

## 安装

```bash
# Python 3.12–3.13（本项目在 3.13 验证）
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt          # 会 editable 安装 third_party/tau2-bench

# 配置 API key
cp env.example .env                          # 然后编辑 .env 填入 API key（见下）
# 或: export DEEPSEEK_API_KEY=...
```

`.env` 支持三种 LLM 提供商（选一即可）：DeepSeek 官方 / OpenAI 兼容端点（如 api.b.ai）/ Anthropic 兼容端点。本项目实测用 api.b.ai 的 OpenAI 兼容端点 + `openai/deepseek-v4-flash`（保留 thinking）：

> 注意：本仓库将 `.env.example` 命名为 `env.example`（你的 Claude Code 全局权限 deny 了 `./.env.*` 的读取，无法创建标准命名）。手动 `mv env.example .env.example` 即可恢复标准命名。

## 运行 baseline

一条命令跑固定 20 个 task：

```bash
python run_eval.py --tasks configs/dev_tasks.json --agent baseline
```

快速 smoke test（只跑前 5 个 task）：

```bash
python run_eval.py --tasks configs/dev_tasks.json --agent baseline --num-tasks 5
```

可选参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tag` | `v0` | run 前缀，run_id 形如 `v0_20260825_160700` |
| `--model` / `--user-model` | `deepseek/deepseek-chat` | agent / user simulator 模型（LiteLLM 格式） |
| `--max-steps` | 60 | 每个 task 最大对话轮数 |
| `--seed` | 42 | 随机种子 |
| `--runs-dir` | `runs` | 输出根目录 |
| `--domain` | 取自 tasks 文件 | 覆盖 domain（如 `telecom` / `banking_knowledge`） |
| `--retrieval-config` | 无 | retrieval 变体名（banking_knowledge 用，如 `bm25` / `bm25_grep` / `no_knowledge`） |
| `--retrieval-config-kwargs` | 无 | retrieval 配置覆盖参数，JSON（如 `'{"top_k": 5}'`） |

## 运行 banking_knowledge（RAG）

banking_knowledge domain 需要指定 retrieval 配置（vanilla baseline + BM25）：

```bash
python run_eval.py --domain banking_knowledge --retrieval-config bm25 \
  --tasks configs/banking_dev_tasks.json --agent baseline
```

支持 retrieval 变体（见 tau2 `RETRIEVAL_VARIANTS`）：`bm25` / `bm25_grep` / `no_knowledge` / `openai_embeddings` / `qwen_embeddings` 等。embedding 类变体需要额外的 API key（OpenAI / OpenRouter）。

> 说明：
> - `openai/` 前缀模型（OpenAI 兼容端点，如 api.b.ai 的 `https://api.b.ai/v1`）会**保留 thinking**
>   （reasoning），并自动读取 `OPENAI_BASE_URL` 作为端点。推荐使用，评测置信度更高。
> - `anthropic/` 前缀模型（Anthropic 兼容端点）会自动禁用 thinking（litellm 多轮不回传
>   reasoning_content 会触发 400 错误）。能用 OpenAI 端点时优先用 OpenAI 端点。
> - 非标准模型的 cost 可能无法计算（litellm 无价格表），此时 cost 记为 $0.0。

### api.b.ai 端点示例

```bash
export OPENAI_API_KEY="<你的 key>"
export OPENAI_BASE_URL="https://api.b.ai/v1"

python run_eval.py --domain banking_knowledge --retrieval-config bm25 \
  --tasks configs/banking_dev_tasks.json --agent baseline \
  --model openai/deepseek-v4-flash
```

## 查看结果

每次 run 生成：

```
runs/
└── v0_20260825_160700/
    ├── summary.json     # run 级指标
    ├── results.json     # per-task 指标
    └── traces/          # 每任务一份完整轨迹
        └── _mobile_data_issue_....json
```

`summary.json` 含：`total_tasks`、`success_count`、`success_rate`、`average_reward`、`average_turns`、`average_tool_calls`、`total_tokens`、`average_tokens`、`estimated_cost`。

每个 trace 含完整 `system_prompt`、`conversation`（对话 + 工具调用 + 工具返回，按 `tool_call id` 精确配对）、`tool_calls`（有序 trajectory）。打开失败任务的 trace 即可人工定位失败原因。

## 失败分类与统计

12 类失败 taxonomy 定义在 `eval/taxonomy.py`（与 tau-bench 论文一致）：
`planning / missing_information / wrong_tool / wrong_tool_args / policy_violation / observation_error / premature_stop / loop / bad_communication / wrong_transfer / environment_error / unknown`

人工查看失败 trace，在 `failure_labels.json` 里标注每个失败 task 的类型，然后：

```bash
python analyze_failures.py runs/v0_20260825_160700 --labels failure_labels.json
python analyze_failures.py runs/v0_20260825_160700 --labels failure_labels.json --csv failures.csv
```

输出失败数、每种类型的数量与百分比、failed task IDs，可选导出 CSV。未标注的失败任务会归为 `unknown`。

## 版本对比

```bash
python compare_runs.py runs/v0_20260825_160700 runs/v1_20260825_180000
```

对比 success rate / average reward / average turns / average tool calls 的差值，并列出：
- V0 fail → V1 success 的 task（转好）
- V0 success → V1 fail 的 task（转坏）
- reward 变化但成功状态未变的 task

## 目录结构

```
TelecomOps-Agent/
├── run_eval.py              # evaluation CLI 入口
├── analyze_failures.py      # 失败分析 CLI
├── compare_runs.py          # 版本对比 CLI（含 retrieval config 对比）
├── configs/
│   ├── dev_tasks.json       # 固定的 20-task telecom dev set（已冻结）
│   └── banking_dev_tasks.json  # 固定的 24-task banking_knowledge (RAG) dev set（分层抽样）
├── agents/
│   ├── __init__.py
│   └── registry.py          # agent 注册表（baseline -> llm_agent）
├── eval/
│   ├── runner.py            # 核心：加载任务、跑仿真、保存 run
│   ├── metrics.py           # per-task 指标 + run summary（含 retrieval 指标）
│   ├── trace.py             # 轨迹提取（tool call 与返回配对，含 KB 检索记录）
│   ├── taxonomy.py          # 12 类失败分类
│   └── __init__.py
├── failure_labels.json      # 人工失败标注模板
├── runs/                    # run 输出（gitignored）
├── third_party/tau2-bench/  # 底层 benchmark（editable install）
├── env.example              # 环境变量模板（复制为 .env）
├── requirements.txt
└── .gitignore
```

## 指标口径（保证跨 run 可比）

- `success`：`reward >= 1.0`（tau2 的 pass^1 判定）
- `turns`：对话中 user 消息条数
- `tool_calls`：assistant 发出的工具调用总数
- `tokens`：从每条消息的 `usage` 字段汇总
- `cost`：`agent_cost + user_cost`（美元）

### Retrieval 指标（banking_knowledge，RAG ablation 用）

- `retrieval_calls`：该 task 里 agent 发起的 retrieval 工具调用次数
- `documents_retrieved`：该 task 累计返回的文档条数
- `required_document_recall`：`task.required_documents` 中被检索返回过（任意 rank）的比例
- `recall_at_k`：对每个 required doc 取"所有调用中的最低 rank"（best rank），统计 best_rank ≤ k 的比例（k=1,3,5,10）。
  **注意这是跨所有检索调用的 *cumulative* recall@k，不是传统单次 Hit@K**——agent 多次搜索后
  best rank 累积提升，指标会随检索次数增加而上升。
- `first_hit_call`：每个 required doc 第一次被命中的检索调用序号（1-based），未命中为 null。
  **衡量检索效率**：搜得越少越好。配套 `avg_first_hit_call` / `max_first_hit_call`
  （捞齐全部文档至少需要的检索次数）。
- run 级聚合：`average_retrieval_calls` / `average_documents_retrieved` / `average_required_document_recall` /
  `average_recall_at_k` / `average_avg_first_hit_call` / `average_max_first_hit_call`

> 为什么需要 first_hit_call：两个 Agent 可能有相近的 recall@k，但一个搜 2 次就捞全，
> 另一个狂搜 12 次——后者检索效率低。first_hit_call 让"检索效率"可量化，用于对比
> Agent 的查询策略好坏（对应 agent efficiency 评估）。

> 注意：`required_documents` 只用于 evaluator 侧指标统计，绝不注入 agent prompt/context。

## 添加新 agent（V1 等）

在 `agents/registry.py` 注册一行：

```python
AGENT_REGISTRY = {
    "baseline": "llm_agent",
    # "v1": my_factory,   # 或指向自定义 agent factory
}
```

然后 `python run_eval.py --agent v1`，与 V0 用 `compare_runs.py` 对比。
