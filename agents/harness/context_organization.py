"""Context Organization（V6.0）—— 每轮 context 的证据分层视图。

动机（用户 V6 指令 §四 + trace 证据）:
    V5 的 context 注入（task_state_block / plan_block）按"对象相关性"
    组织,但不区分证据可信度——user_claim 与 tool_result 同权呈现。
    task_100 形态（用户自称 tenure 4 个月 vs 系统 65 天）需要的不是
    更多历史,而是"这一轮真正需要知道的高价值信息"按证据质量分层。

设计:
    - **不替换** V5 blocks（task_state_block/plan_block/V4 progress
      block 全部保留——freeze 行为保护）;V6 view 作为追加块。
    - 空状态零渲染:无 plan、无 ledger 记录 → 返回空串,简单任务
      （001/002/003/004/007 类）零 token 开销。
    - 纯确定性渲染,零 LLM。

组织（对照用户指令的建议结构;标题语义对齐）:
    [Current situation — organized by evidence reliability]
    GOAL                       ← PlanStore.goal
    CURRENT WORK ITEM          ← Worklist 未完成条目
    CONFIRMED FACTS            ← ledger tool/system 记录
    UNVERIFIED USER CLAIMS     ← ledger user_claim 记录(带 NOT verified 标)
    RELEVANT POLICY EVIDENCE   ← ledger knowledge_base 记录
    MISSING INFORMATION        ← checkpoint 确定性缺失(实体 gap +
                                 未闭合 open questions)
"""

from __future__ import annotations

from typing import Optional

# 渲染预算（防膨胀;各段条目上限）
MAX_CONFIRMED = 6
MAX_UNVERIFIED = 4
MAX_POLICY = 4
MAX_WORK_ITEMS = 6
MAX_MISSING = 4
BLOCK_MAX_CHARS = 1100


def build_v6_context_view(ledger=None, plan_store=None, worklist=None,
                          open_questions: Optional[list] = None,
                          recent_arguments: Optional[dict] = None) -> str:
    """构建 V6 证据分层视图块（注入 system 尾部;空状态返回 ''）。

    Args:
        ledger: EvidenceLedger（证据分层来源）
        plan_store: PlanStore（goal 来源;无 plan 不渲染 goal 行）
        worklist: Worklist（CURRENT WORK ITEM 来源）
        open_questions: DA memory 的 open_questions（missing 段来源）
        recent_arguments: 即将执行动作的参数（实体 gap 检测——
            与 checkpoint 同一确定性逻辑的渲染版）

    渲染优先级:
        confirmed: current_date 等 P1 系统事实在前（按 seq 倒序,
                   current_date 提前——070/007 形态的关键锚点）
    """
    sections = []

    # GOAL（有 plan 才渲染——不猜）
    goal = getattr(plan_store, "goal", None) if plan_store else None
    if not goal and worklist is not None:
        goal = getattr(worklist, "goal", None)
    if goal:
        sections.append(f"GOAL: {str(goal)[:200]}")

    # CURRENT WORK ITEM
    if worklist is not None and worklist.items:
        unfinished = worklist.unfinished()
        if unfinished:
            first = unfinished[0]
            ent = first.entities[0] if first.entities else ""
            line = f"CURRENT WORK ITEM: {first.goal[:150]}"
            if ent:
                line += f" (object: {ent})"
            rest = len(unfinished) - 1
            if rest > 0:
                line += f" — {rest} more unfinished item(s)"
            sections.append(line)
        done = worklist.completed()
        if done:
            sections.append(f"Completed items this task: {len(done)} "
                            f"(latest: {done[-1].goal[:100]})")

    # CONFIRMED FACTS（tool/system）
    if ledger is not None:
        confirmed = ledger.confirmed_facts()
        if confirmed:
            # current_date 提前（P1——时间锚点,070/007 形态）
            confirmed = sorted(
                confirmed,
                key=lambda r: (r.fact_key != "current_date", -r.seq))
            lines = ["CONFIRMED FACTS (system-verified — safe to rely on):"]
            for r in confirmed[:MAX_CONFIRMED]:
                v = str(r.value)
                if len(v) > 90:
                    v = v[:90] + "…"
                ref = f" [from {r.source_ref}]" if r.source_ref else ""
                lines.append(f"- {r.fact_key} = {v}{ref}")
            sections.append("\n".join(lines))

        # UNVERIFIED USER CLAIMS
        unver = ledger.unverified_claims()
        if unver:
            lines = ["UNVERIFIED USER CLAIMS (stated by user, NOT yet "
                     "confirmed by system records):"]
            for r in unver[:MAX_UNVERIFIED]:
                v = str(r.value)
                if len(v) > 90:
                    v = v[:90] + "…"
                ref = f" (stated in {r.source_ref})" if r.source_ref else ""
                lines.append(f"- {r.fact_key} ≈ {v}{ref}")
            sections.append("\n".join(lines))

        # RELEVANT POLICY EVIDENCE
        policy = ledger.policy_facts()
        if policy:
            lines = ["RELEVANT POLICY EVIDENCE (knowledge-base rules):"]
            for r in policy[:MAX_POLICY]:
                v = str(r.value)
                if len(v) > 110:
                    v = v[:110] + "…"
                doc = f" [doc: {r.source_ref}]" if r.source_ref else ""
                lines.append(f"- {r.fact_key}: {v}{doc}")
            sections.append("\n".join(lines))

    # MISSING INFORMATION（确定性:未闭合 open questions + 实体 gap）
    missing_lines = []
    for q in (open_questions or [])[:MAX_MISSING]:
        missing_lines.append(f"- open question: {str(q)[:110]}")
    if recent_arguments and ledger is not None:
        from agents.harness.decision_checkpoint import DecisionCheckpoint
        cp = DecisionCheckpoint()
        for g in cp._entity_gaps(recent_arguments, ledger)[:3]:
            missing_lines.append(f"- {g}")
    if missing_lines:
        sections.append("MISSING INFORMATION (verify before critical "
                        "decisions):\n" + "\n".join(missing_lines[:MAX_MISSING + 3]))

    if not sections:
        return ""   # 空状态零渲染（简单任务零开销）

    header = ("[Current situation — organized by evidence reliability. "
              "Facts below are separated into system-verified, unverified "
              "user claims, and KB policy rules. Do not treat unverified "
              "claims as confirmed.]")
    text = header + "\n" + "\n\n".join(sections)
    if len(text) > BLOCK_MAX_CHARS:
        text = text[:BLOCK_MAX_CHARS] + "\n... (evidence view truncated)"
    return text
