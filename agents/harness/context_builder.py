"""Context Builder（V4/V5/V6.1）—— DA 每轮 context 的统一出口。

收紧原则（用户指令 §3）:其他模块只提供【信息】,不各自拼 context:
    TaskState        → 状态块（V3 已有）
    Plan / Worklist  → 计划块 + 条目块
    Evidence View    → 证据分层块（confirmed/unverified/policy）
    Decision State   → checkpoint 待决块（由 agent 注入的 system 尾注,
                        不经过本模块）
    Recent Tool Result → 历史窗口（存根化策略,本模块的 V4 职责）

    最后全部由 ContextBuilder 统一整理给 Decision Agent——本模块
    是唯一的 context 组装点,不再有平行的 context 模块。

三层渐进（V4 原有,保留不动）：
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

V6.1 增量（全部经同一出口,无平行模块）:
  - evidence_block: EvidenceView 四段视图（confirmed / unverified /
    policy / conflict / preference）,空状态零渲染
  - worklist_block: 大步骤下的未完成条目视图（[x]/[ ] 形态）,
    空状态零渲染
  - current_date 前置（时间锚点,P1——任何"有效期/资格"判断的第一依据）
"""

from __future__ import annotations

from typing import Optional


# 近期窗口：最近 K 条消息保持原样（本轮动作的直接上下文）
RECENT_WINDOW = 12
# 触发轻量化的历史长度下限（更长才开始清旧 ToolResult）
COMPACT_TRIGGER = 24

# 各注入块的预算（防膨胀）
MAX_CONFIRMED = 6
MAX_UNVERIFIED = 4
MAX_POLICY = 4
MAX_PREFERENCES = 3
MAX_WORK_ITEMS = 8
BLOCK_MAX_CHARS = 1200


# ---------------------------------------------------------------------------
# V6.1 证据分层块（EvidenceView → 注入文本;ContextBuilder 唯一渲染点）
# ---------------------------------------------------------------------------
def evidence_block(evidence_view, max_chars: int = BLOCK_MAX_CHARS) -> str:
    """TaskStateV3 → 四段证据视图（空状态返回 ''——简单任务零开销）。

    组织（按事实类型分层,不按来源排名）:
        CONFIRMED FACTS       system_fact（工具/系统确认;时间锚点前置）
        UNVERIFIED CLAIMS     user_statement（用户说的,未对齐系统）
        USER PREFERENCES      user_preference（用户本人权威——不需要
                              系统验证,标注给决策层直接采信）
        POLICY RULES          kb_rule（KB 硬规则）
    矛盾 key 附加在 UNVERIFIED 段（用户说法与系统记录不一致时显式
    暴露——054/100 形态的确定性可见性）。
    """
    if evidence_view is None:
        return ""
    sections = []
    confirmed = evidence_view.confirmed_system_facts(MAX_CONFIRMED)
    if confirmed:
        lines = ["[Evidence — confirmed by system records:]"]
        for r in confirmed:
            v = str(r.value)
            if len(v) > 90:
                v = v[:90] + "…"
            ref = f" [from {r.source_ref}]" if r.source_ref else ""
            lines.append(f"- {r.key} = {v}{ref}")
        sections.append("\n".join(lines))
    unver = evidence_view.unverified_user_claims(MAX_UNVERIFIED)
    conflicts = set(evidence_view.conflict_keys())
    if unver:
        lines = ["[Evidence — stated by user, NOT yet verified by system "
                 "records — verify before relying on them:]"]
        for r in unver:
            v = str(r.value)
            if len(v) > 90:
                v = v[:90] + "…"
            flag = "  ! contradicts system record" if r.key in conflicts else ""
            lines.append(f"- {r.key} ≈ {v}{flag}")
        sections.append("\n".join(lines))
    prefs = evidence_view.user_preferences(MAX_PREFERENCES)
    if prefs:
        lines = ["[User preferences — the user is the authority on these:]"]
        for r in prefs:
            v = str(r.value)
            if len(v) > 90:
                v = v[:90] + "…"
            lines.append(f"- {r.key} = {v}")
        sections.append("\n".join(lines))
    policy = evidence_view.policy_rules(MAX_POLICY)
    if policy:
        lines = ["[Knowledge-base rules (hard constraints):]"]
        for r in policy:
            v = str(r.value)
            if len(v) > 110:
                v = v[:110] + "…"
            doc = f" [doc: {r.source_ref}]" if r.source_ref else ""
            lines.append(f"- {r.key}: {v}{doc}")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    text = "\n\n".join(sections)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (evidence view truncated)"
    return text


# ---------------------------------------------------------------------------
# V6.1 Worklist 块（PlanStore 步骤 → 条目级视图）
# ---------------------------------------------------------------------------
def worklist_block(plan_store, worklist,
                   max_items: int = MAX_WORK_ITEMS,
                   max_chars: int = 700) -> str:
    """大步骤下的条目级进度视图（空状态返回 ''）。

    形态（对照用户指令 §2 的展开关系）:
        [Worklist — per-object progress under the current plan:]
        GOAL: <PlanStore.goal>
        - dispute transaction A (object: txn_A) [x]
        - dispute transaction B (object: txn_B) [ ]
        已完成 N 条
    条目挂在大步骤下渲染（step 描述为行前缀——"一个大步骤下面有
    哪些具体对象还没处理"）;状态图标区分完成/未完成/失败。
    """
    if plan_store is None or worklist is None or not len(worklist):
        return ""
    lines = ["[Worklist — per-object progress under the current plan:]"]
    if getattr(plan_store, "goal", None):
        lines.append(f"GOAL: {str(plan_store.goal)[:200]}")
    icons = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]",
             "failed": "[!]", "blocked": "[=]"}
    order = {"in_progress": 0, "pending": 1, "failed": 2, "blocked": 3}
    items = sorted(worklist.unfinished(),
                   key=lambda it: order.get(it.status, 9))
    step_desc = {}
    for s in plan_store.steps:
        step_desc[s.step_id] = (s.description or "")[:60]
    for it in items[:max_items]:
        icon = icons.get(it.status, "[ ]")
        desc = step_desc.get(it.step_id, "")
        note = f"  note: {it.note}" if it.note else ""
        lines.append(f"{icon} {desc} (object: {it.entity}){note}")
    done = worklist.completed()
    if done:
        ents = ", ".join(it.entity for it in done[:6])
        more = f" (+{len(done) - 6} more)" if len(done) > 6 else ""
        lines.append(f"[x] {len(done)} item(s) completed: {ents}{more}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (worklist truncated)"
    return text


# ---------------------------------------------------------------------------
# 统一出口（V4 原签名 + V6.1 两个可选输入）
# ---------------------------------------------------------------------------
def build_context(state, task_state, plan_tracker,
                  memory_block: str = "",
                  state_block: str = "",
                  evidence_view=None,
                  plan_store=None,
                  worklist=None) -> list:
    """构造 DA 本轮消息视图（确定性替换，零 LLM）——统一出口。

    Args:
        state: tau2 AgentState（system_messages + messages）
        task_state: TaskStateV3（判断哪些 ToolResult 已外部化）
        plan_tracker: PlanTracker（Goal/Progress block）
        memory_block / state_block: V3 已有的注入块
        evidence_view: EvidenceView（V6.1 证据分层块来源;None = 无块）
        plan_store + worklist: V6.1 条目块来源（None = 无块）
    Returns:
        消息列表（不修改 state 本身——副本替换）。
    """
    msgs = list(state.messages)
    plan_block = plan_tracker.progress_block() if plan_tracker else ""

    # V6.1: 证据块 + 条目块（空状态零渲染——简单任务零开销）
    ev_block = evidence_block(evidence_view)
    wl_block = (worklist_block(plan_store, worklist)
                if (worklist is not None and plan_store is not None) else "")

    # 组装 system 尾部块（顺序:计划 → 记忆 → 状态 → 条目 → 证据）
    blocks = [b for b in (plan_block, memory_block, state_block,
                          wl_block, ev_block) if b]
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
