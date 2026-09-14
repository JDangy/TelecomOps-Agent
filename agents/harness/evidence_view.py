"""Evidence View（V6.1 架构收紧）—— TaskStateV3 的只读证据视图。

动机（用户 2026-09 V6 收紧指令 §1/§5）:
    上一版 V6 的 EvidenceLedger 自建了一份事实存储（_entries +
    supersede 链 + provenance 矩阵）,与 TaskStateV3 形成**两套平行
    事实系统**——同一个 Tool Result 走两条喂入路径,以后必然漂移。

    收紧后的唯一数据流:

        Tool Result / User Message / KB Packet
                ↓ （唯一写入路径:TaskStateV3 提取器）
             TaskStateV3          ← 事实状态的唯一来源
                ↓ （只读现算,零存储）
           EvidenceView           ← 分层视图,供 Decision/Context 使用

    EvidenceView 不保存任何事实——每次调用从 TaskStateV3 的历史链
    现算出四段视图:
        - 用户说的（user_statement,默认 unverified）
        - 工具确认的（system_fact,系统工具是权威）
        - KB 规则（kb_rule,知识库是权威）
        - 用户偏好（user_preference,用户本人是权威）

权威规则（§5:不做全局可信度排名）:
    authority 由 TaskStateV3.StateEntry.fact_type 决定,不是
    provenance 全局排名:
        账户开户时间 → system_fact（工具最可信）
        业务规则     → kb_rule（KB 最可信）
        想不想加急   → user_preference（用户最可信）
    本模块只读,不写,不猜,不做业务判断,无任何 task/domain 硬编码。
"""

from __future__ import annotations

from typing import Optional

# 渲染预算（各段条目上限,防 context 膨胀）
MAX_CONFIRMED = 6
MAX_UNVERIFIED = 4
MAX_POLICY = 4
MAX_CONFLICT = 4


class EvidenceView:
    """TaskStateV3 → 分层证据视图（纯函数式,零存储,零 LLM）。

    用法:每次需要时从当前 task_state 现算（构造极轻）。
    trace 不需要单独事件——视图无状态变更,一切事实变更都已经在
    TaskStateV3 的 state_write/state_update 事件里（单一事实源
    的直接好处:审计点只有一个）。
    """

    def __init__(self, task_state):
        self.task_state = task_state

    # ------------------------------------------------------------------
    # 四段视图数据（全部只读现算）
    # ------------------------------------------------------------------
    def confirmed_system_facts(self, max_items: int = MAX_CONFIRMED) -> list:
        """工具/系统确认的系统事实（system_fact 类型的最近记录）。

        current_date 等时间锚点置前（seq 倒序中优先提升——确定性,
        与具体任务无关:时间锚点对任何"有效期/资格"判断都是 P1）。
        """
        recs = self.task_state.latest_system_records()
        # 只保留 system_fact 类型（kb_rule 由 policy 段单独渲染）
        recs = [r for r in recs
                if r.effective_fact_type == "system_fact"]
        recs = sorted(recs, key=lambda r: (r.key != "system.current_date",
                                           -r.seq))
        return recs[:max_items]

    def unverified_user_claims(self,
                               max_items: int = MAX_UNVERIFIED) -> list:
        """用户说的但未被系统确认的陈述（user_statement,当前有效）。"""
        recs = self.task_state.user_claim_records()
        return [r for r in recs
                if r.effective_fact_type == "user_statement"][:max_items]

    def user_preferences(self, max_items: int = 4) -> list:
        """用户偏好/意愿（user_preference——用户本人权威,无需验证）。"""
        recs = self.task_state.user_claim_records()
        return [r for r in recs
                if r.effective_fact_type == "user_preference"][:max_items]

    def policy_rules(self, max_items: int = MAX_POLICY) -> list:
        """KB 业务规则（kb_rule——knowledge 来源的规则命名空间记录）。"""
        recs = self.task_state.evidence_records(
            fact_types=("kb_rule",), max_entries=max_items * 2)
        return recs[:max_items]

    def conflict_keys(self, max_items: int = MAX_CONFLICT) -> list:
        """用户陈述与系统事实矛盾的 key（确定性宽松比较）。"""
        return self.task_state.conflicting_statement_keys()[:max_items]

    # ------------------------------------------------------------------
    # 渲染（供 ContextBuilder 调用——渲染出口在 ContextBuilder 统一,
    # 这里只提供数据;无状态 = 不需要事件/预算/幂等）
    # ------------------------------------------------------------------
    def grade(self, key: str) -> Optional[str]:
        """单 key 的决策等级（checkpoint/调试用）。

        返回 "confirmed" / "unverified" / "policy" / "preference" / None。
        按【当前 current 条目】的类型定级,与分段视图同口径。
        """
        chain = getattr(self.task_state, "_entries", {}).get(
            str(key).strip().lower())
        if not chain:
            return None
        cur = next((r for r in reversed(chain) if r.is_current), None)
        if cur is None:
            return None
        ft = cur.effective_fact_type
        return {"system_fact": "confirmed",
                "user_statement": "unverified",
                "kb_rule": "policy",
                "user_preference": "preference"}.get(ft)
