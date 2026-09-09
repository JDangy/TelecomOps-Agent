"""Context Builder（V4）—— DA 每轮看到什么。

三层渐进（对照验收 1/6/7/8）：
  短任务    → V3 原路径（memory block + task state block，全量历史）
  Plan Mode → 追加 Goal/Progress block + 近期窗口
  历史变长  → 旧 Tool Result 轻量化（事实已入 Task State 的原始
              ToolMessage 替换为单行存根——Trace 保留全文不受影响）

关键区分（V4 第 6 节）：
  Trace          = 完整原始历史（评测/调试）——不动
  Task State     = 当前可靠事实——不动
  LLM Context    = 本轮视图——这里做选择/替换

替换规则（确定性）：
  ToolMessage 的内容包含 Record 块（其事实已被 ToolResultStateExtractor
  提取入 TaskState）→ 且消息在 RECENT_WINDOW 之外 → 内容替换为：
      "[tool result archived: <已入库实体摘要>]（全文在 trace，不需要重看）"
  harness 拒绝的 error ToolMessage 保留（修正依据）；ask packet 保留
  近期部分（知识来源）；纯文本用户消息不替换（对白是任务语义）。
"""

from __future__ import annotations

from typing import Optional


# 近期窗口：最近 K 条消息保持原样（本轮动作的直接上下文）
RECENT_WINDOW = 12
# 触发轻量化的历史长度下限（更长才开始清旧 ToolResult）
COMPACT_TRIGGER = 24


def build_context(state, task_state, plan_tracker,
                  memory_block: str = "",
                  state_block: str = "") -> list:
    """构造 DA 本轮消息视图（确定性替换，零 LLM）。

    Args:
        state: tau2 AgentState（system_messages + messages）
        task_state: TaskStateV3（判断哪些 ToolResult 已外部化）
        plan_tracker: PlanTracker（Goal/Progress block）
        memory_block / state_block: V3 已有的注入块
    Returns:
        消息列表（不修改 state 本身——副本替换）。
    """
    msgs = list(state.messages)
    plan_block = plan_tracker.progress_block() if plan_tracker else ""

    # 组装 system 尾部块
    blocks = [b for b in (memory_block, state_block, plan_block) if b]
    if not plan_tracker or not plan_tracker.plan:
        # 非 Plan Mode：V3 路径（全量历史 + blocks）
        return _with_system(state, blocks) + msgs

    # Plan Mode：历史轻量化（旧 Tool Result → 存根）
    msgs = _compact_tool_results(msgs, task_state)
    return _with_system(state, blocks) + msgs


def _with_system(state, blocks):
    if not blocks or not state.system_messages:
        return list(state.system_messages)
    sys_msg = state.system_messages[0].model_copy(deep=True)
    sys_msg.content = (sys_msg.content or "") + "\n\n" + "\n\n".join(blocks)
    return [sys_msg] + list(state.system_messages[1:])


def _compact_tool_results(msgs, task_state) -> list:
    """旧 ToolMessage 的 Record 内容已入 TaskState → LLM 上下文中替换为存根。

    保守条件（全部确定性）：
      1) 消息总数 > COMPACT_TRIGGER（短历史不动）
      2) 该消息在 RECENT_WINDOW 之外
      3) 是非 error 的 ToolMessage
      4) 内容含 "Record ID" 块（其 ID 字段必已进 TaskState——提取器同模式）
      5) 至少一个实体 ID 在 TaskState 中确实存在（外部化已发生）
    """
    if len(msgs) <= COMPACT_TRIGGER:
        return msgs
    out = list(msgs)
    old_range = range(0, max(0, len(msgs) - RECENT_WINDOW))
    for i in old_range:
        m = out[i]
        try:
            from tau2.data_model.message import ToolMessage
            if not isinstance(m, ToolMessage) or m.error:
                continue
            content = m.content or ""
            if "Record ID" not in content:
                continue
            # 内容里的实体 ID 是否已入 TaskState（与提取器同口径粗检：
            # 任一 ID 字段值出现在状态对象名中）
            archived = _externalized(content, task_state)
            if archived:
                stub = (f"[tool result archived — key facts are in task state: "
                        f"{archived}]. Full text not needed; consult task state.")
                nm = m.model_copy(deep=True)
                nm.content = stub
                out[i] = nm
        except Exception:
            continue
    return out


def _externalized(content: str, task_state) -> str:
    """该 ToolResult 的实体是否已在 TaskState——返回实体摘要或空串。"""
    if task_state is None:
        return ""
    try:
        import re as _re
        hits = []
        for m in _re.finditer(r"(?:user_id|account_id|card_id|transaction_id|"
                              r"credit_card_account_id)\s*[:=]\s*(\S+)", content):
            val = m.group(1).strip("',.;)")
            for key, chain in getattr(task_state, "_entries", {}).items():
                cur = chain[-1] if chain else None
                if cur and str(cur.value) == val and cur.is_current:
                    hits.append(val[:24])
                    break
            if len(hits) >= 3:
                break
        return ", ".join(hits)
    except Exception:
        return ""
