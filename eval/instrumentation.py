"""
执行插桩（Phase 1: Observability）。

原则：只观察，不干预 Agent 行为。
- 不修改 tau2 submodule 源码
- 不向 Agent context 注入任何内容
- 不改变 retrieval config / top-k / prompt / seed 等评测参数

三层插桩：
1. LLM 层    —— monkey-patch 各调用方模块的 `generate` 符号，记录
                model/actor/latency/usage/finish_reason/tool_calls/error。
2. Tool 层   —— 包装 orchestrator.environment.get_response，记录
                每次工具调用的 name/args/latency/结果大小/错误。
3. Task 层   —— runner 循环内直接计时（wall time / retry 计数）。

所有事件写入 TraceV2Recorder（event-sourced trace v2 格式）。

设计说明：为什么不 patch tau2.utils.llm_utils.generate 本身？
  调用方都是 `from tau2.utils.llm_utils import generate`——import 时已把符号
  绑定进各自模块命名空间。改源头模块属性不影响已绑定的引用，
  因此必须逐个 patch 调用方模块（agent/llm_agent、user/user_simulator、
  evaluator/*）里的 `generate` 符号。call_name 参数自带调用方标识
  （agent_response / user_simulator_response / nl_assertions_eval 等），
  据此区分 actor。
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# ContextVar：把事件流路由到"当前正在运行的那个 task"的 recorder。
# run_eval 串行执行任务，同一时刻只有一个 active recorder；用 ContextVar
# 而非全局变量，为将来并行评测留余地。
# ---------------------------------------------------------------------------
_active_recorder: ContextVar[Optional["TraceV2Recorder"]] = ContextVar(
    "trace_v2_recorder", default=None
)


def get_active_recorder() -> Optional["TraceV2Recorder"]:
    return _active_recorder.get()


def set_active_recorder(recorder: Optional["TraceV2Recorder"]) -> Any:
    """设置当前 active recorder（返回 token，可用于恢复）。"""
    return _active_recorder.set(recorder)


# ---------------------------------------------------------------------------
# Trace v2 recorder
# ---------------------------------------------------------------------------
class TraceV2Recorder:
    """Event-sourced trace v2 记录器。

    events: [
      {"seq": 1, "timestamp": <epoch_float>, "event_type": "task_start",
       "actor": "system", "span_id": "...", "parent_span_id": None, ...}
    ]

    span 组织：
      task span（无 parent）
      ├── llm span  (agent / user simulator)
      └── tool span (retrieval / business tool)
    每个事件带 span_id/parent_span_id，可重建 execution tree。
    """

    def __init__(self, run_id: str, task_id: str, trial_id: str = "trial_1"):
        self.run_id = run_id
        self.task_id = task_id
        self.trial_id = trial_id
        self.events: list[dict] = []
        self._seq = 0
        self._span_counter = 0
        self.task_span_id: Optional[str] = None  # mark_task_start 时赋值
        # 统计字段（供 metrics 提取）
        self.task_started_at: Optional[float] = None
        self.task_finished_at: Optional[float] = None
        self.rate_limit_retry_count = 0
        self.rate_limit_wait_seconds = 0.0
        self.attempt_count = 1  # 默认 1 次尝试成功；runner 重试时递增

    # -- 内部工具 --------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _new_span(self, parent: Optional[str]) -> str:
        self._span_counter += 1
        return f"span_{self._span_counter:04d}"

    # -- 公共 API --------------------------------------------------------
    def emit(self, event_type: str, actor: str, *,
             parent_span_id: Optional[str] = None,
             span_id: Optional[str] = None,
             **fields: Any) -> str:
        """追加一条事件。返回本事件的 span_id（便于配对 start/end）。"""
        sid = span_id or self._new_span(parent_span_id)
        ev = {
            "seq": self._next_seq(),
            "timestamp": self._now(),
            "event_type": event_type,
            "actor": actor,
            "span_id": sid,
            "parent_span_id": parent_span_id,
        }
        ev.update(fields)
        self.events.append(ev)
        return sid

    def mark_task_start(self, attempt: int = 1) -> str:
        self.task_started_at = self._now()
        self.attempt_count = attempt
        # task span 是所有子 span 的根
        self.task_span_id = self._new_span(None)
        self.emit("task_start", "system", span_id=self.task_span_id, attempt=attempt)
        return self.task_span_id

    def mark_task_end(self, reward: Optional[float],
                      termination_reason: Optional[str]) -> None:
        self.task_finished_at = self._now()
        self.emit(
            "task_end", "system",
            span_id=self.task_span_id,
            reward=reward,
            termination_reason=termination_reason,
            wall_seconds=(self.task_finished_at - self.task_started_at)
            if self.task_started_at else None,
        )

    def mark_rate_limit_wait(self, wait_seconds: float, attempt: int) -> None:
        self.rate_limit_retry_count += 1
        self.rate_limit_wait_seconds += wait_seconds
        self.emit("rate_limit_wait", "system",
                  parent_span_id=self.task_span_id,
                  wait_seconds=wait_seconds, attempt=attempt)

    def mark_retry(self, reason: str, attempt: int) -> None:
        self.emit("retry", "system",
                  parent_span_id=self.task_span_id,
                  reason=reason, attempt=attempt)

    def to_dict(self) -> dict:
        return {
            "trace_version": "v2",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trial_id": self.trial_id,
            "events": self.events,
            "summary": {
                "task_wall_seconds": (
                    (self.task_finished_at - self.task_started_at)
                    if self.task_started_at and self.task_finished_at else None
                ),
                "attempt_count": self.attempt_count,
                "rate_limit_retry_count": self.rate_limit_retry_count,
                "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 3),
            },
        }


# ---------------------------------------------------------------------------
# LLM 层插桩：包装调用方模块里的 generate 符号
# ---------------------------------------------------------------------------
# call_name -> actor 的映射（tau2 源码里各调用点的 call_name 取值）
_CALL_NAME_TO_ACTOR = {
    "agent_response": "agent",
    "agent_gt_response": "agent_gt",
    "agent_solo_response": "agent_solo",
    "user_simulator_response": "user_simulator",
    "classify_authentication": "evaluator",
    "nl_assertions_eval": "evaluator",
    "llm_judge_hallucination_check": "evaluator",
}


def actor_from_call_name(call_name: Optional[str]) -> str:
    if not call_name:
        return "unknown"
    return _CALL_NAME_TO_ACTOR.get(call_name, f"other:{call_name}")


def make_wrapped_generate(original_generate: Callable) -> Callable:
    """生成一个包装版 generate：签名与原函数一致，前后记录事件。

    只读不改：不触碰 messages/tools/kwargs 的内容——原样传递，
    响应原样返回。失败时记录 llm_call_error 并原样 raise。
    """
    import functools

    @functools.wraps(original_generate)
    def wrapped_generate(model, messages, tools=None, tool_choice=None,
                          call_name=None, **kwargs):
        rec = _active_recorder.get()
        if rec is None:
            # 没有 active recorder（评测器独立调用等场景）——透传，不记录
            return original_generate(
                model, messages, tools=tools, tool_choice=tool_choice,
                call_name=call_name, **kwargs,
            )

        actor = actor_from_call_name(call_name)
        t0 = time.perf_counter()
        sid = rec.emit(
            "llm_call_start", actor,
            parent_span_id=getattr(rec, "task_span_id", None),
            model=model,
            call_name=call_name,
            n_messages=len(messages),
            n_tools=len(tools) if tools else 0,
        )
        try:
            message = original_generate(
                model, messages, tools=tools, tool_choice=tool_choice,
                call_name=call_name, **kwargs,
            )
        except Exception as exc:
            err_type = type(exc).__name__
            is_rl = _looks_like_rate_limit(exc)
            rec.emit(
                "llm_call_error", actor,
                span_id=sid,
                parent_span_id=getattr(rec, "task_span_id", None),
                model=model,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error_type=err_type,
                error_message=str(exc)[:300],
                rate_limit=is_rl,
            )
            raise
        latency_ms = (time.perf_counter() - t0) * 1000
        # 从返回的 AssistantMessage 上读取 tau2 已记录的元数据（只读）
        usage = getattr(message, "usage", None) or {}
        finish_reason = None
        n_tool_calls = 0
        # raw_data 里有完整 litellm 响应；小心读取，读不到就记 None，不猜。
        raw = getattr(message, "raw_data", None) or {}
        try:
            choices = raw.get("choices") or []
            if choices:
                finish_reason = choices[0].get("finish_reason")
                n_tool_calls = len(
                    (choices[0].get("message") or {}).get("tool_calls") or []
                )
        except Exception:
            finish_reason = None
        rec.emit(
            "llm_call_end", actor,
            span_id=sid,
            parent_span_id=getattr(rec, "task_span_id", None),
            model=model,
            latency_ms=round(latency_ms, 1),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=finish_reason,
            n_tool_calls=n_tool_calls,
            response_chars=len(getattr(message, "content", None) or ""),
        )
        return message

    return wrapped_generate


def _looks_like_rate_limit(exc: Exception) -> bool:
    """与 eval.runner.is_rate_limit_error 相同口径，避免循环 import。"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name:
        return True
    import re
    return bool(
        re.search(r"code:?\s*429\b", msg)
        or "rate limit" in msg
        or "too many requests" in msg
    )


def install_llm_patch() -> Callable[[], None]:
    """patch 各调用方模块的 generate 符号。返回 uninstall 函数。

    patch 目标（调用方模块，而非 tau2.utils.llm_utils 本身）：
      - tau2.agent.llm_agent            (call_name=agent_response)
      - tau2.user.user_simulator        (call_name=user_simulator_response)
      - tau2.evaluator.*                (call_name=classify_authentication 等)

    未 patch 的调用方（streaming / voice / interface_agent）本项目评测不经过；
    若将来经过，会在 llm_call_start 缺失——不产生错误数据。
    """
    import importlib

    targets = [
        "tau2.agent.llm_agent",
        "tau2.user.user_simulator",
        "tau2.evaluator.auth_classifier",
        "tau2.evaluator.evaluator_nl_assertions",
        "tau2.evaluator.hallucination_reviewer",
        "tau2.evaluator.review_llm_judge",
        "tau2.evaluator.review_llm_judge_user_only",
    ]
    originals: dict[str, Callable] = {}

    for mod_name in targets:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue  # 模块不存在（voice/知识域未装）——跳过
        original = getattr(mod, "generate", None)
        if original is None or getattr(original, "_trace_v2_patched", False):
            continue
        wrapped = make_wrapped_generate(original)
        wrapped._trace_v2_patched = True  # type: ignore[attr-defined]
        setattr(mod, "generate", wrapped)
        originals[mod_name] = original

    def uninstall() -> None:
        for mod_name, original in originals.items():
            try:
                mod = importlib.import_module(mod_name)
                setattr(mod, "generate", original)
            except Exception:
                pass

    return uninstall


# ---------------------------------------------------------------------------
# Tool / Retrieval 层插桩：包装 environment.get_response
# ---------------------------------------------------------------------------
def wrap_environment(env: Any, recorder: TraceV2Recorder) -> Any:
    """返回一个 get_response 被包装的 environment 代理。

    不复制 environment、不改其状态——仅拦截 get_response 调用计时。
    其余属性/方法通过 __getattr__ 透传原对象。
    """
    original_get_response = env.get_response

    def instrumented_get_response(tool_call):
        t0 = time.perf_counter()
        name = getattr(tool_call, "name", None) or "(unknown)"
        args = getattr(tool_call, "arguments", None)
        try:
            args_dict = dict(args) if isinstance(args, dict) else {"raw": str(args)[:100]}
        except Exception:
            args_dict = {}
        is_retrieval = name in ("KB_search", "KB_search_bm25", "KB_search_dense", "grep")
        actor = "retrieval" if is_retrieval else "environment"
        query = args_dict.get("query") or args_dict.get("pattern")
        sid = recorder.emit(
            "tool_call_start", actor,
            parent_span_id=getattr(recorder, "task_span_id", None),
            tool_name=name,
            query=query if is_retrieval else None,
            arguments=args_dict if not is_retrieval else {"query": query},
        )
        try:
            tool_message = original_get_response(tool_call)
        except Exception as exc:
            recorder.emit(
                "tool_call_error", actor,
                span_id=sid,
                parent_span_id=getattr(recorder, "task_span_id", None),
                tool_name=name,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
            raise
        content = getattr(tool_message, "content", None) or ""
        doc_ids: Optional[list[str]] = None
        ranks: Optional[list[int]] = None
        scores: Optional[list[float]] = None  # BM25 score：无法稳定获得，诚实置 None
        if is_retrieval and isinstance(content, str):
            doc_ids, ranks = _parse_doc_ids_ranks(content)
        recorder.emit(
            "tool_call_end", actor,
            span_id=sid,
            parent_span_id=getattr(recorder, "task_span_id", None),
            tool_name=name,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            success=not getattr(tool_message, "error", False),
            result_chars=len(content),
            doc_ids=doc_ids,
            ranks=ranks,
            scores=scores,
        )
        return tool_message

    class _InstrumentedEnv:
        """轻量代理：仅拦截 get_response，其余全部透传。"""

        def __init__(self):
            object.__setattr__(self, "_inner", env)
            object.__setattr__(self, "_get_response", instrumented_get_response)

        def get_response(self, tool_call):
            return self._get_response(tool_call)

        def __getattr__(self, item):
            return getattr(object.__getattribute__(self, "_inner"), item)

        def __setattr__(self, item, value):
            setattr(object.__getattribute__(self, "_inner"), item, value)

    return _InstrumentedEnv()


_DOC_LINE_RE = None  # 延迟初始化，避免 import 期编译开销


def _parse_doc_ids_ranks(content: str) -> tuple[Optional[list[str]], Optional[list[int]]]:
    """从 KB_search 返回文本解析 (doc_ids, ranks)。格式 'N. Title
   ID: doc_x'。

    与 eval/trace.py 的 _DOC_ID_RANK_RE 同一格式；这里返回 None
    表示解析失败（不伪造）。
    """
    global _DOC_LINE_RE
    import re as _re
    if _DOC_LINE_RE is None:
        _DOC_LINE_RE = _re.compile(r"(\d+)\.\s.*?\n\s+ID:\s*(\S+)")
    doc_ids: list[str] = []
    ranks: list[int] = []
    for m in _DOC_LINE_RE.finditer(content):
        ranks.append(int(m.group(1)))
        doc_ids.append(m.group(2))
    if not doc_ids:
        return None, None
    return doc_ids, ranks
