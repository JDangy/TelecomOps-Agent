"""
从 SimulationRun 提取人类可读的轨迹（trace）。

关键点：AssistantMessage.tool_calls 里的每条 ToolCall 有 ``id``，
对应的 ToolMessage 也有相同的 ``id``（可能是 MultiToolMessage 内的多条），
我们据此把 "工具调用 -> 工具返回" 精确配对，得到完整 trajectory。

对于 retrieval 工具（banking_knowledge 的 KB_search / grep / shell 等），
额外记录：
  - retrieval query（从 tool call arguments 提取）
  - retrieval tool / config
  - top_k（如果工具参数里有 k）
  - returned document IDs（从 tool result 文本里解析）
  - rank（每个 doc 的顺序位置）
  - retrieval call count（per task 统计）
"""

from __future__ import annotations

import re
from typing import Any, Optional

# 已知的 retrieval 工具名（banking_knowledge domain）
_RETRIEVAL_TOOLS = {
    "KB_search", "KB_search_bm25", "KB_search_dense", "grep", "shell",
}

# 正则：从 KB_search 返回文本里提取 ID 和 rank
# 格式: "1. Title\n   ID: doc_xxx\n   Score: 12.83\n"
_DOC_ID_RANK_RE = re.compile(r"(\d+)\.\s.*?\n\s+ID:\s*(\S+)")


def _parse_kb_results(content: str) -> list[dict]:
    """从 KB_search 的返回文本中解析 (rank, doc_id) 列表。"""
    results = []
    for match in _DOC_ID_RANK_RE.finditer(content):
        rank = int(match.group(1))
        doc_id = match.group(2)
        results.append({"rank": rank, "doc_id": doc_id})
    return results


def _to_jsonable(v: Any) -> Any:
    """把任意值转成可 JSON 序列化的形式。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "model_dump"):
        return v.model_dump()
    return str(v)


def extract_trace(
    sim,
    task,
    *,
    domain: Optional[str] = None,
    retrieval_config: Optional[str] = None,
    retrieval_config_kwargs: Optional[dict] = None,
) -> dict:
    """把一个 SimulationRun 转成结构化的、人工可读的 trace 字典。"""
    messages = sim.messages or []

    # 第一遍：收集 assistant 发出的所有工具调用定义（id -> name/arguments）
    tool_call_defs: dict[str, dict] = {}
    for m in messages:
        if getattr(m, "role", None) == "assistant":
            for tc in getattr(m, "tool_calls", None) or []:
                tool_call_defs[tc.id] = {
                    "name": tc.name,
                    "arguments": tc.arguments,
                }

    # 第二遍：生成有序的对话记录 + 工具调用轨迹
    conversation: list[dict] = []
    tool_calls: list[dict] = []  # 按执行顺序，含参数与结果
    system_prompt: Optional[str] = None

    # retrieval 记录
    retrieval_calls: list[dict] = []
    retrieval_count = 0

    for m in _iter_flat(messages):
        role = m.role
        if role == "system":
            if system_prompt is None:
                system_prompt = m.content
            continue

        if role == "assistant":
            entry: dict = {"turn": m.turn_idx, "role": "assistant"}
            if m.content:
                entry["content"] = m.content
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                entry["tool_calls"] = [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in calls
                ]
            conversation.append(entry)

        elif role == "user":
            entry = {"turn": m.turn_idx, "role": "user", "content": m.content}
            conversation.append(entry)

        elif role == "tool":
            defn = tool_call_defs.get(m.id, {"name": None, "arguments": None})
            name = defn["name"]
            entry = {
                "turn": m.turn_idx,
                "role": "tool",
                "tool_call_id": m.id,
                "name": name,
                "arguments": defn["arguments"],
                "content": m.content,
            }
            if m.error:
                entry["error"] = True
            conversation.append(entry)
            tool_calls.append(
                {
                    "id": m.id,
                    "name": name,
                    "arguments": defn["arguments"],
                    "result": m.content,
                    "error": m.error,
                }
            )

            # 如果是 retrieval 工具，解析结果
            if name in _RETRIEVAL_TOOLS:
                retrieval_count += 1
                kwargs = {}
                if isinstance(defn["arguments"], dict):
                    kwargs = defn["arguments"]
                parsed = _parse_kb_results(m.content or "")
                doc_ids = [p["doc_id"] for p in parsed]
                ranks = [p["rank"] for p in parsed]
                retrieval_calls.append({
                    "name": name,
                    "query": kwargs.get("query") or kwargs.get("pattern") or "",
                    "top_k": kwargs.get("k") or None,
                    "num_docs": len(doc_ids),
                    "doc_ids": doc_ids,
                    "ranks": ranks,
                })

    # 任务元信息
    task_info = {
        "task_id": getattr(task, "id", None),
        "description": _to_jsonable(getattr(task, "description", None)),
        "user_scenario": _to_jsonable(getattr(task, "user_scenario", None)),
    }

    trace = {
        "task_id": task_info["task_id"],
        "task": task_info,
        "reward": sim.reward_info.reward,
        "termination_reason": sim.termination_reason,
        "agent_cost": sim.agent_cost,
        "user_cost": sim.user_cost,
        "system_prompt": system_prompt,
        "conversation": conversation,
        "tool_calls": tool_calls,
    }

    # Retrieval 信息（仅 banking_knowledge 等 RAG domain）
    if domain == "banking_knowledge" or retrieval_config:
        trace["retrieval"] = {
            "config": retrieval_config,
            "config_kwargs": retrieval_config_kwargs or {},
            "total_calls": retrieval_count,
            "calls": retrieval_calls,
        }

    return trace


def _iter_flat(messages) -> Any:
    """把 messages 展平：ToolMessage 原样 yield，MultiToolMessage 展开其 tool_messages。"""
    for m in messages:
        subs = getattr(m, "tool_messages", None)
        if subs is not None:
            for tm in subs:
                yield tm
        else:
            yield m