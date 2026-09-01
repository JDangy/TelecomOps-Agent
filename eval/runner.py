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
from eval.instrumentation import (
    TraceV2Recorder,
    install_llm_patch,
    set_active_recorder,
    wrap_environment,
)
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


def run_single_task(config: TextRunConfig, task, recorder=None) -> SimulationRun:
    """运行单个 task：构建 orchestrator 并跑完整仿真。

    注意：banking_knowledge 的 EnvEvaluator 会在回放时重建 retrieval 环境。
    build.py 通过 env_kwargs 传 retrieval_variant/retrieval_kwargs 给环境；
    run_simulation 的 env_kwargs 也会透传给 evaluator 的环境构造函数，
    因此这里需要把 retrieval 配置一并传入，否则回放时用默认变体（alltools，
    含 dense embedding）会导致与运行时的 bm25 不一致，甚至触发 OpenAI key 报错。

    recorder: TraceV2Recorder（可选）。传入时启用 tool 层插桩——
    environment.get_response 被无行为影响的代理包装（仅计时/记录）。

    V1.1 memory 生命周期：build_text_orchestrator 每 task 重建 agent，
    memory 随 agent 实例新建（天然零跨 task 泄漏）。这里补 start_task
    （激活）与 end_task（快照+清空）。agent 无 memory 属性时跳过（V0/baseline）。
    """
    env_kwargs = {}
    if config.domain == "banking_knowledge" and config.retrieval_config:
        env_kwargs["retrieval_variant"] = config.retrieval_config
        env_kwargs["retrieval_kwargs"] = dict(config.retrieval_config_kwargs or {})
    orchestrator = build_text_orchestrator(config, task, seed=config.seed)
    if recorder is not None:
        # 只观察不干预：包装代理不改 tool schema / retrieval config / 结果内容
        orchestrator.environment = wrap_environment(orchestrator.environment, recorder)

    # V1.1: 激活 per-agent working memory（task 开始）
    _start_memories(orchestrator, task.id, recorder)
    try:
        sim = run_simulation(
            orchestrator,
            evaluation_type=EvaluationType.ALL,
            env_kwargs=env_kwargs,
        )
    finally:
        # task 结束（含异常路径）：快照 + 清空，防泄漏
        _end_memories(orchestrator, recorder)
    return sim


def _iter_agent_memories(orchestrator):
    """收集 orchestrator.agent（及其 KnowledgeAgent 成员）上的 memory 实例。"""
    agent = getattr(orchestrator, "agent", None)
    seen = []
    for m in (getattr(agent, "memory", None),
              getattr(getattr(agent, "_knowledge_agent", None), "memory", None)):
        if m is not None and hasattr(m, "start_task") and not any(m is x for x in seen):
            seen.append(m)
    return seen


def _start_memories(orchestrator, task_id, recorder) -> None:
    for m in _iter_agent_memories(orchestrator):
        try:
            m.start_task(task_id)
        except Exception as exc:  # memory 失败不打断评测
            print(f"    (memory start_task 失败: {exc})")


def _end_memories(orchestrator, recorder) -> None:
    for m in _iter_agent_memories(orchestrator):
        try:
            snap = m.end_task()
            # memory_snapshot 事件：task end 写一次完整快照（分析用）
            if recorder is not None and snap:
                rec = recorder
                actor = getattr(m, "_actor", lambda: "system")() \
                    if hasattr(m, "_actor") else "system"
                rec.emit("memory_snapshot", actor,
                         parent_span_id=getattr(rec, "task_span_id", None),
                         snapshot=snap)
        except Exception as exc:
            print(f"    (memory end_task 失败: {exc})")


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


def _extract_handoff_summary(recorder) -> list[dict]:
    """从 v2 events 提取 handoff/Evidence Packet 摘要（注入 v1 trace 的 two_agent 区）。

    每个 handoff 一条：question + packet 关键字段。原始检索文本不进 v1 trace。
    """
    summary = []
    events = getattr(recorder, "events", [])
    pending = {}
    for e in events:
        if e["event_type"] == "handoff":
            pending[e["span_id"] if "span_id" in e else id(e)] = {
                "question": e.get("question"),
                "context": None,
            }
        elif e["event_type"] == "handoff_result":
            summary.append({
                "question": None,  # 由配对 handoff 填
                "latency_ms": e.get("latency_ms"),
                "evidence_packet_chars": e.get("evidence_packet_chars"),
                "facts": e.get("evidence_fact_count"),
                "document_ids": e.get("evidence_doc_ids"),
                "confidence": e.get("evidence_confidence"),
                "missing_information": e.get("missing_information_count"),
            })
    # handoff 与 result 按出现顺序一一对应（串行执行保证）
    hands = [e for e in events if e["event_type"] == "handoff"]
    for i, s in enumerate(summary):
        if i < len(hands):
            s["question"] = hands[i].get("question")
    return summary


def _extract_two_agent_metrics(v2_trace: dict) -> dict:
    """从 v2 events 提取 2-Agent 协作指标（V0 单 Agent 时返回空 dict）。"""
    events = v2_trace.get("events") or []
    hands = [e for e in events if e["event_type"] == "handoff"]
    results = [e for e in events if e["event_type"] == "handoff_result"]
    if not hands and not results:
        return {}
    ka_llm_end = [e for e in events
                  if e["event_type"] == "llm_call_end" and e.get("actor") == "knowledge_agent"]
    ka_llm_all = [e for e in events
                  if e["event_type"] in ("llm_call_end", "llm_call_error")
                  and e.get("actor") == "knowledge_agent"]
    ka_retr = [e for e in events
               if e["event_type"] == "tool_call_end" and e.get("agent") == "knowledge_agent"]
    da_llm_end = [e for e in events
                  if e["event_type"] == "llm_call_end" and e.get("actor") == "agent"]
    return {
        "handoff_count": len(hands),
        "knowledge_agent_llm_calls": len(ka_llm_all),
        "knowledge_agent_prompt_tokens": sum(e.get("prompt_tokens") or 0 for e in ka_llm_end),
        "knowledge_agent_completion_tokens": sum(e.get("completion_tokens") or 0 for e in ka_llm_end),
        "knowledge_agent_max_prompt_tokens": max(
            (e.get("prompt_tokens") or 0 for e in ka_llm_end), default=None
        ),
        "knowledge_retrieval_calls": len(ka_retr),
        "evidence_packet_chars_total": sum(e.get("evidence_packet_chars") or 0 for e in results),
        "evidence_packet_chars_avg": (
            round(sum(e.get("evidence_packet_chars") or 0 for e in results) / len(results), 1)
            if results else None
        ),
        "decision_agent_llm_calls": len(da_llm_end),
        "decision_agent_prompt_tokens": sum(e.get("prompt_tokens") or 0 for e in da_llm_end),
        "decision_agent_completion_tokens": sum(e.get("completion_tokens") or 0 for e in da_llm_end),
        "decision_agent_max_prompt_tokens": max(
            (e.get("prompt_tokens") or 0 for e in da_llm_end), default=None
        ),
        # ---- V1.1 memory 指标 ----
        **_memory_metrics(events, ka_retr, hands, results),
    }


def _norm_query(q: str) -> str:
    return " ".join((q or "").lower().split())


def _memory_metrics(events, ka_retr, hands, results) -> dict:
    """从 memory events / 检索事件提取 V1.1 新指标。

    重复检测口径：query/doc 与本 task 更早出现过的相同（规范化后）。
    retrieval/doc 层面从事件流重建（memory_update 只是增量通知，不重复计）。
    """
    # KA 侧：retrieval query 序列（tool_call_start 上有 query）
    ka_retr_starts = [e for e in events
                      if e["event_type"] == "tool_call_start"
                      and e.get("agent") == "knowledge_agent"
                      and e.get("query")]
    queries = [_norm_query(e.get("query") or "") for e in ka_retr_starts]
    unique_q, repeated_q = 0, 0
    seen_q = set()
    for q in queries:
        if q in seen_q:
            repeated_q += 1
        else:
            seen_q.add(q)
            unique_q += 1
    # KA docs 序列（tool_call_end 上有 doc_ids）
    doc_seq: list = []
    for e in ka_retr:
        doc_seq.extend(e.get("doc_ids") or [])
    unique_docs = len(set(doc_seq))
    repeated_doc_hits = len(doc_seq) - unique_docs

    # memory snapshot（task end 记录的完整快照）
    snaps = [e for e in events if e["event_type"] == "memory_snapshot"]
    ka_snap = next((e.get("snapshot") or {} for e in snaps
                    if e.get("actor") == "knowledge_agent"), {})
    da_snap = next((e.get("snapshot") or {} for e in snaps
                    if e.get("actor") == "decision_agent"), {})

    # ---- V1.2 Selective Retrieval 指标 ----
    mem_hits = [e for e in events if e["event_type"] == "memory_hit"]
    mem_partial = [e for e in events if e["event_type"] == "memory_partial_hit"]
    mem_miss = [e for e in events if e["event_type"] == "memory_miss"]
    mem_verdicts = len(mem_hits) + len(mem_partial) + len(mem_miss)
    skipped = sum(1 for e in mem_hits if e.get("retrieval_skipped"))
    prog_events = [e for e in events if e["event_type"] == "retrieval_progress"]
    low_prog = [e for e in prog_events if e.get("progress") == "low"]

    # per-retrieval new docs（流式：第一次见到的才算 new）
    seen_stream: set = set()
    new_docs_per_retr: list = []
    for e in ka_retr:
        docs = e.get("doc_ids") or []
        new_n = sum(1 for d in docs if d not in seen_stream)
        seen_stream.update(docs)
        new_docs_per_retr.append(new_n)

    return {
        "ka_unique_queries": unique_q,
        "ka_repeated_queries": repeated_q,
        "ka_unique_documents_seen": unique_docs,
        "ka_repeated_document_hits": repeated_doc_hits,
        "retrievals_per_handoff": (
            round(len(ka_retr) / len(hands), 2) if hands else None
        ),
        "ka_memory_fact_count": len(ka_snap.get("facts_found") or []),
        "ka_memory_doc_count": len(ka_snap.get("documents_seen") or []),
        "ka_memory_queries_count": len(ka_snap.get("queries_tried") or []),
        "da_memory_constraint_count": len(da_snap.get("user_constraints") or []),
        "da_memory_fact_count": len(da_snap.get("verified_facts") or []),
        "evidence_status_counts": {
            s: sum(1 for r in results if r.get("evidence_status") == s)
            for s in ("sufficient", "partial", "insufficient")
        },
        # ---- V1.2 Selective Memory Retrieval ----
        "memory_hit_count": len(mem_hits),
        "memory_partial_hit_count": len(mem_partial),
        "memory_miss_count": len(mem_miss),
        "memory_hit_rate": (
            round(len(mem_hits) / mem_verdicts, 3) if mem_verdicts else None
        ),
        "retrieval_avoided_count": skipped,
        "new_documents_per_retrieval": (
            round(sum(new_docs_per_retr) / len(new_docs_per_retr), 2)
            if new_docs_per_retr else None
        ),
        "repeated_document_ratio": (
            round(repeated_doc_hits / len(doc_seq), 3) if doc_seq else None
        ),
        "low_progress_retrieval_count": len(low_prog),
    }


def _harness_metrics(events: list) -> dict:
    """V2 Action Harness 指标（无 harness 事件时返回空 dict）。"""
    proposed = [e for e in events if e["event_type"] == "action_proposed"]
    rejected = [e for e in events if e["event_type"] == "action_rejected"]
    if not proposed:
        return {}
    # verdict 分类（从 action_validation 事件聚合）
    verdict_counts: dict = {}
    mismatched_fields = []
    for e in [x for x in events if x["event_type"] == "action_validation"]:
        for v in e.get("verdicts") or []:
            verdict_counts[v.get("verdict")] = verdict_counts.get(v.get("verdict"), 0) + 1
            if v.get("verdict") == "evidence_mismatch":
                mismatched_fields.append(v.get("field"))
    schema_fails = sum(
        1 for e in rejected
        if any(v.get("verdict") == "schema_violation"
               for v in e.get("verdicts_summary") or [])
    )
    evidence_rej = len(rejected) - schema_fails
    # rejected 工具在后续 proposed 中再次出现 = 修正重试（近似）
    tool_seq = [e.get("tool_name") for e in proposed]
    rej_tools = [e.get("tool_name") for e in rejected]
    corrected = 0
    for i, tn in enumerate(rej_tools):
        idx = proposed[i].get("tool_name")  # 不可靠——改用 rejected 事件在 proposed 序列中的位置
    # 更正实现：遍历 events 顺序，rejected 后同工具再次 proposed 即计一次修正
    ev_order = [e for e in events if e["event_type"] in ("action_proposed", "action_rejected")]
    corrected = 0
    last_rej_tool = None
    for e in ev_order:
        if e["event_type"] == "action_rejected":
            last_rej_tool = e.get("tool_name")
        elif e["event_type"] == "action_proposed" and last_rej_tool is not None:
            if e.get("tool_name") == last_rej_tool:
                corrected += 1
            last_rej_tool = None
    return {
        "proposed_tool_calls": len(proposed),
        "validated_tool_calls": len([e for e in events if e["event_type"] == "action_validation"]),
        "rejections": len(rejected),
        "schema_validation_failures": schema_fails,
        "evidence_mismatches": evidence_rej,
        "corrected_after_rejection": corrected,
        "verdict_counts": verdict_counts,
        "mismatched_fields": list(dict.fromkeys(mismatched_fields))[:10],
    }


def _register_factory_agent(name: str, factory) -> None:
    """把自定义 agent factory 注册进 tau2 registry（幂等）。

    tau2 build.py 用 registry.get_agent_factory(name) 解析 agent；我们的
    agents/registry.py 只是本地映射。factory 签名遵循 tau2 约定：
    factory(tools, domain_policy, **kwargs)。
    """
    from tau2 import registry as tau2_registry
    if tau2_registry.get_agent_factory(name) is None:
        tau2_registry.register_agent_factory(factory, name)


def extract_timing_metrics(v2_trace: dict) -> dict:
    """从 trace v2 事件流提取 per-task timing 指标。

    只统计真实发生的事件；拿不到的记 None（不猜）。
    """
    events = v2_trace.get("events") or []
    llm_events = [e for e in events if e["event_type"] in ("llm_call_end", "llm_call_error")]
    agent_llm = [e for e in llm_events if e["event_type"] == "llm_call_end" and e["actor"] == "agent"]
    user_llm = [e for e in llm_events if e["event_type"] == "llm_call_end" and e["actor"] == "user_simulator"]
    retr_events = [e for e in events if e["event_type"] == "tool_call_end" and e.get("actor") == "retrieval"]
    tool_events = [
        e for e in events
        if e["event_type"] == "tool_call_end" and e.get("actor") == "environment"
    ]
    rl_waits = [e for e in events if e["event_type"] == "rate_limit_wait"]

    def _sum(evts, key):
        vals = [e.get(key) for e in evts if isinstance(e.get(key), (int, float))]
        return round(sum(vals), 3) if vals else None

    def _avg_ms(evts):
        vals = [e.get("latency_ms") for e in evts if isinstance(e.get("latency_ms"), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else None

    llm_success = [e for e in llm_events if e["event_type"] == "llm_call_end"]
    prompt_tokens = sum(e.get("prompt_tokens") or 0 for e in llm_success)
    completion_tokens = sum(e.get("completion_tokens") or 0 for e in llm_success)
    llm_total_ms = _sum(llm_success, "latency_ms")
    return {
        "task_wall_seconds": (v2_trace.get("summary") or {}).get("task_wall_seconds"),
        "llm_calls": len(llm_events),
        "agent_llm_calls": len(agent_llm),
        "user_llm_calls": len(user_llm),
        "llm_total_latency_seconds": (
            round(llm_total_ms / 1000, 3) if llm_total_ms is not None else None
        ),
        "llm_avg_latency_ms": _avg_ms(llm_success),
        "retrieval_calls": len(retr_events),
        "retrieval_total_latency_ms": _sum(retr_events, "latency_ms"),
        "tool_total_latency_ms": _sum(tool_events, "latency_ms"),
        "retry_count": (v2_trace.get("summary") or {}).get("attempt_count", 1) - 1,
        "rate_limit_wait_seconds": (v2_trace.get("summary") or {}).get("rate_limit_wait_seconds"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


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
    # --- agent 解析（baseline -> tau2 官方名；two_agent 等 -> 自定义 factory）---
    impl, is_factory = resolve_agent(agent_name)
    if is_factory:
        # 自定义 factory 由 tau2 build.py 按 (tools, domain_policy, llm, llm_args,
        # task, ...) 约定调用；config.agent 直接传逻辑名，registry 查不到时
        # 需要预先注册到 tau2 registry（见 _register_factory_agent）。
        _register_factory_agent(agent_name, impl)
        tau2_agent = agent_name
    else:
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

    # --- LLM 层插桩：patch 调用方模块的 generate（只观察不干预）---
    uninstall_llm_patch = install_llm_patch()
    run_wall_start = time.perf_counter()

    # --- 逐个运行（429 限流时自动重试）---
    per_task = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] running {task.id}")
        sys.stdout.flush()
        m = None
        trace = None
        recorder = TraceV2Recorder(run_id=run_id, task_id=task.id, trial_id="trial_1")
        token = set_active_recorder(recorder)
        try:
            for attempt in range(1, task_max_retries + 1):
                if attempt > 1:
                    recorder.attempt_count = attempt
                try:
                    if attempt == 1:
                        recorder.mark_task_start(attempt=attempt)
                    sim = run_single_task(config, task, recorder=recorder)
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
                    recorder.mark_task_end(
                        reward=m.get("reward"),
                        termination_reason=getattr(sim, "termination_reason", None),
                    )
                    break  # 成功，跳出重试循环
                except Exception as exc:
                    if is_rate_limit_error(exc) and attempt < task_max_retries:
                        wait = task_retry_cooldown * attempt
                        print(f"    !! RateLimit(429) attempt {attempt}/{task_max_retries}, "
                              f"wait {wait:.0f}s, retry...")
                        sys.stdout.flush()
                        recorder.mark_rate_limit_wait(wait, attempt)
                        time.sleep(wait)
                        continue
                    # 非限流错误 或 重试耗尽
                    print(f"    !! ERROR: {exc}")
                    m = task_metrics(None, error=f"{type(exc).__name__}: {exc}")
                    trace = None
                    recorder.mark_task_end(reward=None, termination_reason="error")
                    break  # 跳出重试循环（不重试）
        finally:
            set_active_recorder(token)
        # 重试循环后，m 一定不为 None
        m["task_id"] = task.id
        # v1 trace（保持原有文件名，向后兼容）
        if trace is not None:
            # 2-Agent：把 handoff/Evidence Packet 摘要注入 v1 trace
            # （拦截的 ask 调用发生在 agent state 内，orchestrator 对话里不可见；
            #  v2 events 已有完整记录，这里补一个 v1 侧可读的摘要区）
            handoff_summary = _extract_handoff_summary(recorder)
            if handoff_summary:
                trace["two_agent"] = handoff_summary
            trace_file = traces_dir / f"{sanitize_filename(task.id)}.json"
            trace_file.write_text(
                json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            m["trace_path"] = str(trace_file.relative_to(run_dir))
        # v2 event-sourced trace（新增；v1 不动）
        v2 = recorder.to_dict()
        v2_file = traces_dir / f"{sanitize_filename(task.id)}.v2.json"
        v2_file.write_text(
            json.dumps(v2, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        m["trace_v2_path"] = str(v2_file.relative_to(run_dir))
        m["timing"] = extract_timing_metrics(v2)
        # 2-Agent 协作指标（无 handoff 时为空 dict，不影响 V0）
        m["two_agent_metrics"] = _extract_two_agent_metrics(v2)
        # V2 harness 指标（无 harness 事件时为空 dict）
        m["harness_metrics"] = _harness_metrics(v2.get("events") or [])

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

    # --- run 级 timing 汇总（基于 trace v2）---
    uninstall_llm_patch()
    total_wall = time.perf_counter() - run_wall_start
    summary["timing"] = compute_run_timing(per_task, total_wall)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "results.json").write_text(
        json.dumps(per_task, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return summary


def compute_run_timing(per_task: list[dict], total_wall_seconds: float) -> dict:
    """聚合 run 级 timing 指标（per_task 的 timing 来自 extract_timing_metrics）。"""
    timings = [t.get("timing") or {} for t in per_task]

    def _vals(key: str) -> list[float]:
        return [t[key] for t in timings if isinstance(t.get(key), (int, float))]

    def _avg(key: str) -> Optional[float]:
        vals = _vals(key)
        return round(sum(vals) / len(vals), 3) if vals else None

    def _p(key: str, pct: float) -> Optional[float]:
        """pct=0.5 -> p50, pct=0.95 -> p95。样本不足时返回 None（不猜）。"""
        vals = sorted(_vals(key))
        if not vals:
            return None
        import math
        idx = min(len(vals) - 1, max(0, math.ceil(pct * len(vals)) - 1))
        return round(vals[idx], 3)

    wall_vals = _vals("task_wall_seconds")
    return {
        "total_wall_time": round(total_wall_seconds, 1),
        "average_task_wall_time": _avg("task_wall_seconds"),
        "p50_task_wall_time": _p("task_wall_seconds", 0.5),
        "p95_task_wall_time": _p("task_wall_seconds", 0.95),
        "total_llm_calls": sum(t.get("llm_calls") or 0 for t in timings),
        "average_llm_latency_ms": _avg("llm_avg_latency_ms"),
        "p95_llm_latency_ms": _p("llm_avg_latency_ms", 0.95),
        "total_rate_limit_wait_seconds": round(
            sum(t.get("rate_limit_wait_seconds") or 0 for t in timings), 3
        ),
        "throughput_tasks_per_minute": round(
            len(per_task) / (total_wall_seconds / 60), 3
        ) if total_wall_seconds > 0 else None,
    }
