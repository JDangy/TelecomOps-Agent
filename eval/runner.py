"""
核心 evaluation runner。

工作流程：
  1. 从 tasks_file 读取固定 task ID 列表（如 configs/dev_tasks.json / configs/banking_dev_tasks.json）
  2. 用 tau2 Python API 逐个加载并运行 task（build_text_orchestrator + run_simulation）
  3. 收集每个 task 的 SimulationRun（或异常），提取 trace（含 retrieval 记录）
  4. 计算 summary，写入 runs/<run_id>/{summary.json, results.json, traces/}

设计说明：
  - 用 tau2 的 Python API 而非 CLI，完全控制存储与将来 agent 注入。
  - 每个 task 独立 try/except：单个任务失败不影响整个 run。
  - seed 传给 build_text_orchestrator，同一 seed 下结果可复现。
  - domain 支持 telecom / banking_knowledge；banking_knowledge 支持 retrieval_config
    （如 bm25），retrieval_config_kwargs 透传给 tau2 的 resolve_variant。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from tau2.data_model.simulation import SimulationRun, TextRunConfig
from tau2.evaluator.evaluator import EvaluationType
from tau2.runner import build_text_orchestrator, get_tasks, run_simulation

from agents.registry import resolve_agent
from eval.metrics import compute_summary, task_metrics
from eval.trace import extract_trace

# 记录 trace 里该次 run 用到的 retrieval 信息（供 eval/trace 解析 KB 结果用）
_RETRIEVAL_TOOLS = {"KB_search", "KB_search_bm25", "KB_search_dense", "grep"}


def get_tau2_version() -> str:
    """返回已安装的 tau2 版本号。"""
    try:
        import importlib.metadata

        return importlib.metadata.version("tau2")
    except Exception:
        return "unknown"


def make_run_id(run_tag: str) -> str:
    """生成唯一 run_id，形如 v0_20260825_160700。"""
    return f"{run_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def load_task_ids(tasks_file: Path) -> tuple[str, list[str]]:
    """从 tasks 配置文件读取 (domain, task_ids 列表)。"""
    data = json.loads(tasks_file.read_text(encoding="utf-8"))
    ids = data.get("task_ids") or []
    if not ids:
        raise ValueError(f"{tasks_file} 缺少非空 task_ids 字段")
    domain = data.get("domain", "telecom")
    return domain, ids


def sanitize_filename(s: str) -> str:
    """把 task id（含 []/:/ 等字符）转成安全文件名。"""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", s)


def run_single_task(config: TextRunConfig, task) -> SimulationRun:
    """运行单个 task：构建 orchestrator 并跑完整仿真。

    注意：banking_knowledge 的 EnvEvaluator 会在回放时重建 retrieval 环境。
    build.py 通过 env_kwargs 传 retrieval_variant/retrieval_kwargs 给环境；
    run_simulation 的 env_kwargs 也会透传给 evaluator 的环境构造函数，
    因此这里需要把 retrieval 配置一并传入，否则回放时用默认变体（alltools，
    含 dense embedding）会导致与运行时的 bm25 不一致，甚至触发 OpenAI key 报错。
    """
    env_kwargs = {}
    if config.domain == "banking_knowledge" and config.retrieval_config:
        env_kwargs["retrieval_variant"] = config.retrieval_config
        env_kwargs["retrieval_kwargs"] = dict(config.retrieval_config_kwargs or {})
    orchestrator = build_text_orchestrator(config, task, seed=config.seed)
    sim = run_simulation(
        orchestrator,
        evaluation_type=EvaluationType.ALL,
        env_kwargs=env_kwargs,
    )
    return sim


def is_rate_limit_error(exc: Exception) -> bool:
    """判断异常是否为 API 限流（429）——这类错误是环境问题，应重试而非算作任务失败。

    兼容 litellm.RateLimitError 及其底层异常。注意不能裸匹配 "429" 子串——
    消息里含 "4290"（token 数）或 "$429"（金额）的非限流错误会被误判，
    导致 400 这类确定性错误被无意义重试 5 次。
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name:
        return True
    # 消息层面：只认 "code: 429" / "status 429" 这类明确上下文，
    # 避免金额 $429、token 数 4290 等被误判
    return bool(
        re.search(r"code:?\s*429\b", msg)
        or "rate limit" in msg
        or "ratelimiterror" in msg
        or "too many requests" in msg
    )


def run_eval(
    *,
    tasks_file: Path,
    agent_name: str = "baseline",
    domain: Optional[str] = None,
    retrieval_config: Optional[str] = None,
    retrieval_config_kwargs: Optional[dict] = None,
    llm_agent_model: Optional[str] = None,
    llm_user_model: Optional[str] = None,
    max_steps: int = 60,
    seed: int = 42,
    max_errors: int = 10,
    run_tag: str = "run",
    runs_dir: Path = Path("runs"),
    num_tasks: Optional[int] = None,
    task_max_retries: int = 5,
    task_retry_cooldown: float = 20.0,
) -> dict:
    """运行一组 task 并保存结果。

    Args:
        domain: 覆盖 tasks_file 里的 domain。默认取 tasks_file 的 domain 字段。
        retrieval_config: retrieval 变体名（banking_knowledge），如 "bm25"。
        retrieval_config_kwargs: 传给 tau2 resolve_variant 的覆盖参数（如 top_k）。

    Returns:
        summary dict（含 run_id / run_dir / 各项指标）。
    """
    # --- agent 解析（V0: baseline -> tau2 官方 llm_agent）---
    impl, is_factory = resolve_agent(agent_name)
    if is_factory:
        raise NotImplementedError(
            "自定义 agent factory 尚未支持（V0 baseline 使用 tau2 官方 llm_agent）。"
        )
    tau2_agent = impl

    # --- 加载任务 ---
    file_domain, task_ids = load_task_ids(tasks_file)
    domain = domain or file_domain
    if num_tasks is not None:
        task_ids = task_ids[:num_tasks]
        print(f"[config] 只运行前 {num_tasks} 个任务 (num_tasks={num_tasks})")

    tasks = get_tasks(domain, task_ids=task_ids)
    loaded = {t.id for t in tasks}
    missing = [i for i in task_ids if i not in loaded]
    if missing:
        raise RuntimeError(f"以下 task 未在 tau2 {domain} 数据中找到: {missing}")

    # --- run 目录 ---
    run_id = make_run_id(run_tag)
    run_dir = runs_dir / run_id
    traces_dir = run_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    # --- 模型配置 ---
    model = {
        "agent": llm_agent_model,
        "user": llm_user_model,
    }

    config = TextRunConfig(
        domain=domain,
        agent=tau2_agent,
        llm_agent=llm_agent_model,
        llm_user=llm_user_model,
        max_steps=max_steps,
        max_errors=max_errors,
        seed=seed,
        num_trials=1,
        retrieval_config=retrieval_config,
        retrieval_config_kwargs=retrieval_config_kwargs,
    )

    # 限流友好：对 LLM 提供商（如 api.b.ai）设置更高的重试次数与冷却，
    # 降低 429 中断整个 task 的概率。api.b.ai 实测 429 后约 10s 恢复，
    # 因此重试窗口要足够覆盖持续的 burst 限流。
    # 仅对带 retrieval 的 domain 默认启用；telecom 保持默认行为。
    if domain == "banking_knowledge":
        llm_retry = {
            "num_retries": 15,
            "cooldown_time": 15,  # 429 后冷却秒数
        }
        config.llm_args_agent = {**config.llm_args_agent, **llm_retry}
        config.llm_args_user = {**config.llm_args_user, **llm_retry}

    # 模型协议适配：
    # 1) openai/ 前缀（OpenAI 兼容端点，如 api.b.ai /v1）—— 保留 thinking（reasoning_content
    #    由 litellm 原生处理，多轮不会触发 400），并显式传 api_base（来自 OPENAI_BASE_URL）。
    # 2) anthropic/ 前缀（Anthropic 兼容端点）—— api.b.ai 的 deepseek-v4-flash 默认启用
    #    thinking，但 litellm 多轮不回传 reasoning_content 会触发 400
    #    （"reasoning_content must be passed back"），故显式禁用 thinking。
    llm_openai = {}
    if "openai/" in (llm_agent_model or "") or "openai/" in (llm_user_model or ""):
        base = os.environ.get("OPENAI_BASE_URL")
        if base:
            llm_openai = {"api_base": base.rstrip("/")}
        print(f"[config] OpenAI 协议端点: api_base={llm_openai.get('api_base') or '(litellm 默认)'}  "
              f"(thinking 保留)")
    if "anthropic/" in (llm_agent_model or "") or "anthropic/" in (llm_user_model or ""):
        llm_openai = {
            "allowed_openai_params": ["thinking"],
            "thinking": {"type": "disabled"},
        }
        print("[config] Anthropic 协议端点: thinking 已禁用（规避 litellm 多轮回传 400）")
    if llm_openai:
        config.llm_args_agent = {**config.llm_args_agent, **llm_openai}
        config.llm_args_user = {**config.llm_args_user, **llm_openai}

    print(f"[config] agent={agent_name} (tau2:{tau2_agent}) "
          f"model={model['agent']} | {model['user']}")
    print(f"[config] domain={domain}  max_steps={max_steps} seed={seed} max_errors={max_errors}")
    if domain == "banking_knowledge":
        print(f"[config] retrieval_config={retrieval_config}  "
              f"retrieval_config_kwargs={retrieval_config_kwargs}")
    print(f"[run] run_id={run_id}  dir={run_dir}")
    print()

    # --- 逐个运行（429 限流时自动重试）---
    per_task = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] running {task.id}")
        sys.stdout.flush()
        m = None
        trace = None
        for attempt in range(1, task_max_retries + 1):
            try:
                sim = run_single_task(config, task)
                trace = extract_trace(
                    sim, task,
                    domain=domain,
                    retrieval_config=retrieval_config,
                    retrieval_config_kwargs=retrieval_config_kwargs,
                )
                m = task_metrics(
                    sim,
                    domain=domain,
                    retrieval_config=retrieval_config,
                    required_documents=list(getattr(task, "required_documents", None) or []),
                    retrieval=trace.get("retrieval"),
                )
                break  # 成功，跳出重试循环
            except Exception as exc:
                if is_rate_limit_error(exc) and attempt < task_max_retries:
                    wait = task_retry_cooldown * attempt
                    print(f"    !! RateLimit(429) attempt {attempt}/{task_max_retries}, "
                          f"wait {wait:.0f}s, retry...")
                    sys.stdout.flush()
                    time.sleep(wait)
                    continue
                # 非限流错误 或 重试耗尽
                print(f"    !! ERROR: {exc}")
                m = task_metrics(None, error=f"{type(exc).__name__}: {exc}")
                trace = None
                break  # 跳出重试循环（不重试）
        # 重试循环后，m 一定不为 None
        m["task_id"] = task.id
        if trace is not None:
            trace_file = traces_dir / f"{sanitize_filename(task.id)}.json"
            trace_file.write_text(
                json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            m["trace_path"] = str(trace_file.relative_to(run_dir))

        status = "SUCCESS" if m["success"] else "FAIL"
        cost = (m.get("agent_cost") or 0.0) + (m.get("user_cost") or 0.0)
        print(f"    -> {status}  reward={m['reward']}  turns={m['turns']}  "
              f"tools={m['tool_calls']}  cost=${cost:.4f}")
        if domain == "banking_knowledge":
            print(f"       retrieval_calls={m.get('retrieval_calls')}  "
                  f"docs_retrieved={m.get('documents_retrieved')}  "
                  f"req_recall={m.get('required_document_recall')}")
        per_task.append(m)

    # --- summary 与保存 ---
    run_meta = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "agent": agent_name,
        "model": model,
        "domain": domain,
        "retrieval_config": retrieval_config,
        "retrieval_config_kwargs": retrieval_config_kwargs,
        "tau2_version": get_tau2_version(),
        "config": {
            "tasks_file": str(tasks_file),
            "num_tasks_requested": len(task_ids),
            "max_steps": max_steps,
            "seed": seed,
            "max_errors": max_errors,
            "tau2_agent": tau2_agent,
        },
    }
    summary = compute_summary(run_meta, per_task)
    summary["run_dir"] = str(run_dir)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "results.json").write_text(
        json.dumps(per_task, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return summary
