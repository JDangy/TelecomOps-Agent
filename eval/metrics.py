"""
从 SimulationRun 计算 per-task 指标，并汇总成 run 级 summary。

指标口径（文档化，保证跨 run 可比）：
  - success:      reward >= 1.0（tau2 的 pass^1 判定）
  - turns:        conversation 中 user 消息条数（agent 需要响应的轮数）
  - tool_calls:   assistant 发出的全部工具调用次数
  - tokens:       从 messages 逐条 usage 汇总（prompt/completion）
  - cost:         sim.agent_cost + sim.user_cost（美元）

Retrieval 指标（banking_knowledge，RAG ablation 用）：
  - retrieval_calls:            该 task 里 agent 发起的 retrieval 工具调用次数
  - documents_retrieved:         该 task 累计返回的文档条数（跨所有 retrieval 调用）
  - avg_documents_per_call:      documents_retrieved / retrieval_calls
  - required_document_recall:    task.required_documents 中被检索系统返回过（任意 rank）的比例
  - hit_at_k:                    对每个 required doc 取"所有调用中的最低 rank"，
                                  计算 best_rank <= k 的比例（k=1,3,5,10）

注意：required_documents 只用于 evaluator 侧指标统计，绝不注入 agent prompt/context。
"""

from __future__ import annotations

from typing import Any, Optional

from tau2.data_model.simulation import SimulationRun

# hit@k 评测的 k 值（仅当有 retrieval 数据时计算）
HIT_AT_KS = [1, 3, 5, 10]


def count_user_turns(messages) -> int:
    return sum(1 for m in messages if getattr(m, "role", None) == "user")


def count_tool_calls(messages) -> int:
    n = 0
    for m in messages:
        if getattr(m, "role", None) == "assistant":
            n += len(getattr(m, "tool_calls", None) or [])
    return n


def aggregate_usage(messages) -> dict:
    """汇总所有消息的 token 用量。usage 可能是 None 或缺字段，防御性处理。"""
    prompt = completion = 0
    for m in messages:
        usage = getattr(m, "usage", None)
        if isinstance(usage, dict):
            prompt += int(usage.get("prompt_tokens", 0) or 0)
            completion += int(usage.get("completion_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _hit_at_k(required_docs: list[str], best_ranks: dict[str, Optional[int]]) -> dict:
    """对每个 required doc 统计 best_rank <= k 的比例。best_ranks 为 {doc_id: 最低rank}。"""
    out = {}
    total = len(required_docs)
    for k in HIT_AT_KS:
        if total == 0:
            out[f"hit_at_{k}"] = None
            continue
        hit = sum(
            1 for doc in required_docs
            if best_ranks.get(doc) is not None and best_ranks[doc] <= k
        )
        out[f"hit_at_{k}"] = round(hit / total, 4)
    return out


def compute_retrieval_metrics(
    required_documents: Optional[list[str]],
    retrieval: Optional[dict],
) -> dict:
    """从 trace 提取出的 retrieval 数据计算该 task 的 retrieval 指标。"""
    if not retrieval:
        return {
            "retrieval_calls": 0,
            "documents_retrieved": 0,
            "avg_documents_per_call": None,
            "required_document_recall": None,
            "hit_at_1": None,
            "hit_at_3": None,
            "hit_at_5": None,
            "hit_at_10": None,
        }

    calls = retrieval.get("calls") or []
    retrieval_calls = len(calls)
    documents_retrieved = sum(c.get("num_docs", 0) for c in calls)
    avg_docs = round(documents_retrieved / retrieval_calls, 2) if retrieval_calls else None

    required_docs = required_documents or []
    # 每个 required doc 的最低 rank（跨所有调用）
    best_ranks: dict[str, Optional[int]] = {doc: None for doc in required_docs}
    for c in calls:
        for doc_id, rank in zip(c.get("doc_ids", []), c.get("ranks", [])):
            if doc_id in best_ranks:
                if best_ranks[doc_id] is None or rank < best_ranks[doc_id]:
                    best_ranks[doc_id] = rank

    if required_docs:
        recalled = sum(1 for doc in required_docs if best_ranks[doc] is not None)
        recall = round(recalled / len(required_docs), 4)
    else:
        recall = None

    hits = _hit_at_k(required_docs, best_ranks)

    return {
        "retrieval_calls": retrieval_calls,
        "documents_retrieved": documents_retrieved,
        "avg_documents_per_call": avg_docs,
        "required_document_recall": recall,
        **hits,
    }


def task_metrics(
    sim: SimulationRun,
    *,
    error: Optional[str] = None,
    domain: Optional[str] = None,
    retrieval_config: Optional[str] = None,
    required_documents: Optional[list[str]] = None,
    retrieval: Optional[dict] = None,
) -> dict:
    """把单个 SimulationRun 转成简洁的 per-task 指标。error 非空表示 run 异常。"""
    task_id = sim.task_id if sim is not None else None
    if error is not None:
        return {
            "task_id": task_id,
            "reward": None,
            "success": False,
            "termination_reason": None,
            "turns": None,
            "tool_calls": None,
            "tokens": None,
            "agent_cost": None,
            "user_cost": None,
            "error": error,
        }

    messages = sim.messages or []
    reward = float(sim.reward_info.reward)

    out = {
        "task_id": task_id,
        "reward": reward,
        "success": reward >= 1.0,
        "termination_reason": sim.termination_reason,
        "turns": count_user_turns(messages),
        "tool_calls": count_tool_calls(messages),
        "tokens": aggregate_usage(messages),
        "agent_cost": sim.agent_cost,
        "user_cost": sim.user_cost,
        "error": None,
    }

    # Retrieval 指标（banking_knowledge 等 RAG domain）
    if domain == "banking_knowledge" or retrieval:
        out.update(compute_retrieval_metrics(required_documents, retrieval))
        out["retrieval_config"] = retrieval_config

    return out


def _avg(values: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _agg_retrieval(per_task: list[dict]) -> dict:
    """汇总 retrieval 相关 run 级指标（仅当有 retrieval 数据时）。"""
    has_retrieval = any(t.get("retrieval_calls") is not None for t in per_task)
    if not has_retrieval:
        return {
            "average_retrieval_calls": None,
            "average_documents_retrieved": None,
            "average_documents_per_call": None,
        }

    calls = [t["retrieval_calls"] for t in per_task if t.get("retrieval_calls") is not None]
    docs = [t["documents_retrieved"] for t in per_task if t.get("documents_retrieved") is not None]
    docs_per_call = [
        t["avg_documents_per_call"] for t in per_task if t.get("avg_documents_per_call") is not None
    ]
    # required_document_recall / hit@k 仅对"有 required docs 且发生了检索"的任务取平均
    recall_vals = [
        t["required_document_recall"] for t in per_task if t.get("required_document_recall") is not None
    ]
    out = {
        "average_retrieval_calls": _avg(calls),
        "average_documents_retrieved": _avg(docs),
        "average_documents_per_call": _avg(docs_per_call),
        "average_required_document_recall": _avg(recall_vals),
    }
    for k in HIT_AT_KS:
        vals = [t[f"hit_at_{k}"] for t in per_task if t.get(f"hit_at_{k}") is not None]
        out[f"average_hit_at_{k}"] = _avg(vals)
    return out


def compute_summary(run_meta: dict, per_task: list[dict]) -> dict:
    """汇总 run 级指标。run_meta 含 run_id / agent / 模型 / config / domain / retrieval 等。"""
    total = len(per_task)
    succeeded = [t for t in per_task if t["success"]]
    success_count = len(succeeded)
    success_rate = round(success_count / total, 4) if total else 0.0

    rewards = [t["reward"] for t in per_task if t["reward"] is not None]
    turns = [t["turns"] for t in per_task if t["turns"] is not None]
    tool_calls = [t["tool_calls"] for t in per_task if t["tool_calls"] is not None]

    total_tokens = sum(
        (t["tokens"]["total_tokens"] if t["tokens"] else 0) for t in per_task
    )
    cost_values = [
        (t["agent_cost"] or 0.0) + (t["user_cost"] or 0.0)
        for t in per_task
        if t.get("agent_cost") is not None or t.get("user_cost") is not None
    ]

    summary = {
        "run_id": run_meta["run_id"],
        "timestamp": run_meta["timestamp"],
        "agent": run_meta["agent"],
        "model": run_meta["model"],
        "domain": run_meta.get("domain"),
        "retrieval_config": run_meta.get("retrieval_config"),
        "retrieval_config_kwargs": run_meta.get("retrieval_config_kwargs"),
        "tau2_version": run_meta.get("tau2_version"),
        "config": run_meta["config"],
        "total_tasks": total,
        "success_count": success_count,
        "success_rate": success_rate,
        "average_reward": _avg(rewards),
        "average_turns": _avg(turns),
        "average_tool_calls": _avg(tool_calls),
        "total_tokens": total_tokens,
        "average_tokens": round(total_tokens / total, 1) if total else 0,
        "estimated_cost": round(sum(cost_values), 6) if cost_values else None,
        "num_errors": sum(1 for t in per_task if t.get("error")),
        "per_task": [
            {
                "task_id": t["task_id"],
                "reward": t["reward"],
                "success": t["success"],
                "turns": t["turns"],
                "tool_calls": t["tool_calls"],
                "trace_path": t.get("trace_path"),  # runner 保存 trace 后回填
            }
            for t in per_task
        ],
    }

    # Retrieval 聚合指标
    summary.update(_agg_retrieval(per_task))

    return summary
