"""Evidence Ledger（V6.0）—— 事实来源分层的证据账本。

动机（Frozen V5 Dev24 trace 归因,docs/v6_design.md §2-A）:
    task_100: 用户自称 tenure "约四个月",DB 实际 65 天。agent 采信
    自述 → World Blue referral(需 90 天)失败。V5 的 TaskStateV3 有
    source 字段(user/tool/knowledge),但两类证据在决策路径上同权
    —— 没有"关键决策只认 confirmed"的表达。

设计原则（严格对照用户 V6 指令）:
    1. 不推翻 TaskStateV3 —— 独立轻量层,只服务 Decision Checkpoint
       的证据视图渲染与 missing-evidence 计算。**不进 harness 拦截链**
       （不改任何 V5 校验行为——freeze 保护）。
    2. 不写 task-specific / referral-specific 规则 —— 通用 provenance
       分层:user_claim / tool_result / knowledge_base / system。
    3. Runtime 不做业务判断 —— ledger 只回答"这个事实的来源等级",
       不回答"该不该推荐/该选哪个"。

语义:
    provenance（来源类型,固定枚举）:
        user_claim     用户口头陈述（含金额、tenure、意图）
        tool_result    工具执行结果（账号数据、日期、状态）
        knowledge_base KB 检索确认（policy 规则,常量 status=policy_rule）
        system         环境系统信息（get_current_time 等）
    status（可信状态）:
        unverified     user_claim 默认状态
        confirmed      tool_result / system 默认状态
        policy_rule    knowledge_base 默认状态
        superseded     被更新记录取代（历史保留）

核心规则（确定性,无 LLM）:
    R1 tool-confirmed supersedes user claim:同 fact_key 上,
       tool_result/system 写入会把 user_claim 记录标记 superseded。
    R2 user_claim 永不 supersede tool_result/knowledge_base
       —— 自述不能覆盖系统事实（task_100 的 tenure 陷阱形态）。
    R3 knowledge_base 之间按 seq 后者取代前者（KB 更新,罕见）;
       knowledge_base 不被 user_claim/tool_result 覆盖（policy 规则
       与事实正交——promo 文档 vs 账户余额互不冲突）。
    R4 幂等:同 key 同 provenance 同值重写 no-op（防事件爆炸,
       与 TaskStateV3 同教训）。

对外查询:
    get_decision_grade(key) -> (grade, value, record)
        grade: "confirmed"  最新有效记录是 tool_result/system
               "unverified" 最新有效记录是 user_claim
               "policy"     最新有效记录是 knowledge_base
               None         无记录
    —— 唯一消费方:checkpoint/context 的分段渲染 + missing 计算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# provenance 枚举
USER_CLAIM = "user_claim"
TOOL_RESULT = "tool_result"
KNOWLEDGE_BASE = "knowledge_base"
SYSTEM = "system"

PROVENANCES = (USER_CLAIM, TOOL_RESULT, KNOWLEDGE_BASE, SYSTEM)

# status 枚举
UNVERIFIED = "unverified"
CONFIRMED = "confirmed"
POLICY_RULE = "policy_rule"
SUPERSEDED = "superseded"

# provenance → 默认 status
_DEFAULT_STATUS = {
    USER_CLAIM: UNVERIFIED,
    TOOL_RESULT: CONFIRMED,
    SYSTEM: CONFIRMED,
    KNOWLEDGE_BASE: POLICY_RULE,
}

# R1/R2: 覆盖方向矩阵。can_override[a][b] = a 的新写入能否取代 b 的现存记录
_CAN_OVERRIDE = {
    TOOL_RESULT: {TOOL_RESULT: True, SYSTEM: True, USER_CLAIM: True,
                  KNOWLEDGE_BASE: False},
    SYSTEM: {TOOL_RESULT: True, SYSTEM: True, USER_CLAIM: True,
             KNOWLEDGE_BASE: False},
    USER_CLAIM: {TOOL_RESULT: False, SYSTEM: False, USER_CLAIM: True,
                 KNOWLEDGE_BASE: False},
    KNOWLEDGE_BASE: {TOOL_RESULT: False, SYSTEM: False, USER_CLAIM: True,
                     KNOWLEDGE_BASE: True},
}

# 当前时间类工具的确定性识别（070/007 失败形态的事实锚点）
_CURRENT_TIME_TOOL_RE = re.compile(r"(get_current_time|current_time)", re.I)
# 工具结果中时间文本格式（tau2 banking: "The current time is 2025-11-14 03:40:00 EST."）
_TIME_VALUE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class EvidenceRecord:
    """一条证据记录（不可变语义——supersede 只改指针不改内容）。"""
    fact_key: str            # 规范化事实名（"tenure_days"/"current_date"/…）
    value: Any
    provenance: str          # PROVENANCES 之一
    source_ref: str = ""     # 工具名 / doc_id / "user_turn_N"
    status: str = UNVERIFIED
    seq: int = 0
    superseded_by: Optional[int] = None

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None

    def to_dict(self) -> dict:
        return {"fact_key": self.fact_key, "value": self.value,
                "provenance": self.provenance, "source_ref": self.source_ref,
                "status": self.status, "seq": self.seq}


class EvidenceLedger:
    """分层证据账本（确定性,零 LLM）。

    trace 事件经 recorder.emit 实时发出（与 plan_* 事件同模式,
    不走 TaskStateV3 的 task-end 延迟 flush——postmortem §3-F 教训）。
    recorder 缺失时静默（单测/无评测环境）。
    """

    # 每条 value 的字符串截断（渲染与 trace 防膨胀）
    VALUE_TRACE_MAX = 120

    def __init__(self):
        self._entries: dict[str, list[EvidenceRecord]] = {}
        self._seq = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(self, fact_key: str, value: Any, provenance: str,
            source_ref: str = "") -> Optional[EvidenceRecord]:
        """记录一条证据。返回新记录;非法输入返回 None（宽容,不抛）。

        规则 R1-R4 见模块 docstring。user_claim 对已 confirmed 的 key
        仍然记录（历史可见）但**不取代**现有记录——查询时 grade 仍
        由 tool_result 决定,这正是"自述不能覆盖系统事实"的表达。
        """
        if fact_key in (None, "") or value in (None, ""):
            return None
        if provenance not in PROVENANCES:
            return None
        key = str(fact_key).strip().lower()
        chain = self._entries.setdefault(key, [])

        # R4 幂等:同 key 同 provenance 同值（宽松等价）→ no-op
        for rec in chain:
            if rec.is_current and rec.provenance == provenance and \
                    _loose_eq(rec.value, value):
                return rec

        self._seq += 1
        rec = EvidenceRecord(
            fact_key=key, value=value, provenance=provenance,
            source_ref=str(source_ref or "")[:80],
            status=_DEFAULT_STATUS[provenance], seq=self._seq,
        )

        # supersede 判定（R1/R2/R3）:新记录按覆盖矩阵标记旧 current 记录
        overridden = None
        if chain:
            cur = chain[-1]
            if cur.is_current and _CAN_OVERRIDE[provenance][cur.provenance]:
                cur.superseded_by = self._seq
                overridden = cur
        chain.append(rec)

        # trace（实时）
        self._emit("evidence_record_added", rec,
                   overridden=overridden is not None)
        if overridden is not None:
            self._emit("evidence_superseded", rec,
                       old_value=overridden.value,
                       old_provenance=overridden.provenance,
                       old_source=overridden.source_ref)
        return rec

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def current(self, fact_key: str) -> Optional[EvidenceRecord]:
        """key 的最新有效记录（None = 无记录）。

        有效性判定（R2 的查询侧表达）:从链尾向前找第一条
        **未被取代**的记录;user_claim 记录若排在 tool_result/
        system/knowledge_base 之后（后来者无权取代前者,两者并存）
        → 跳过 claim 取权威记录（"自述不能覆盖系统事实"）。
        """
        chain = self._entries.get(str(fact_key).strip().lower())
        if not chain:
            return None
        authoritative = (TOOL_RESULT, SYSTEM, KNOWLEDGE_BASE)
        for rec in reversed(chain):
            if not rec.is_current:
                continue  # 被取代——跳过
            if rec.provenance in authoritative:
                return rec
            # user_claim:其后若还有未取代的权威记录（顺序在前）→ 用权威
            has_auth = any(r.is_current and r.provenance in authoritative
                           for r in chain)
            if has_auth:
                # 链上存在权威记录（此 claim 在其之后追加）→ claim 不作数
                for r in reversed(chain):
                    if r.is_current and r.provenance in authoritative:
                        return r
            return rec
        return None

    def get_decision_grade(self, fact_key: str):
        """决策等级查询（checkpoint/context 渲染的唯一入口）。

        返回 (grade, value, record):
            grade "confirmed" / "unverified" / "policy" / None
        """
        rec = self.current(fact_key)
        if rec is None:
            return None, None, None
        if rec.provenance in (TOOL_RESULT, SYSTEM):
            return "confirmed", rec.value, rec
        if rec.provenance == USER_CLAIM:
            return "unverified", rec.value, rec
        return "policy", rec.value, rec

    def unverified_claims(self) -> list[EvidenceRecord]:
        """所有仍有效的 user_claim 记录（渲染 Unverified 段）。"""
        return [c[-1] for c in self._entries.values()
                if c[-1].is_current and c[-1].provenance == USER_CLAIM]

    def confirmed_facts(self) -> list[EvidenceRecord]:
        """所有有效 confirmed 记录（tool/system）。"""
        return [c[-1] for c in self._entries.values()
                if c[-1].is_current and c[-1].provenance in (TOOL_RESULT, SYSTEM)]

    def policy_facts(self) -> list[EvidenceRecord]:
        """所有有效 policy 记录（knowledge_base）。"""
        return [c[-1] for c in self._entries.values()
                if c[-1].is_current and c[-1].provenance == KNOWLEDGE_BASE]

    def keys_with_conflict(self) -> list[str]:
        """同 key 上 user_claim 与 tool_result 并存且值矛盾的 key 列表。

        （"矛盾"= 宽松不等价。供 checkpoint 的确定性 missing 计算参考;
        不用于拦截——渲染告知 LLM。）
        """
        out = []
        for key, chain in self._entries.items():
            cur = chain[-1]
            if not cur.is_current:
                continue
            # 找链上未被取代前最近的 user_claim 与 tool_result
            last_claim = next((r for r in reversed(chain)
                               if r.provenance == USER_CLAIM), None)
            last_tool = next((r for r in reversed(chain)
                              if r.provenance in (TOOL_RESULT, SYSTEM)), None)
            if last_claim and last_tool and \
                    not _loose_eq(last_claim.value, last_tool.value):
                out.append(key)
        return out

    def __len__(self) -> int:
        return sum(1 for c in self._entries.values()
                   if c and c[-1].is_current)

    # ------------------------------------------------------------------
    # 便捷喂入（从既有 V5 数据流确定性映射,零 LLM）
    # ------------------------------------------------------------------
    @classmethod
    def feed_tool_result(cls, ledger: "EvidenceLedger", tool_name: str,
                         result_text: str) -> list[EvidenceRecord]:
        """工具结果 → ledger（tool_result 来源）。

        当前只做**一类**确定性识别:current_time 工具 → fact_key=
        "current_date"（070/007 失败形态的事实锚点）。其余工具结果
        的结构化事实提取不在 ledger 层做——实体/余额等仍由 V5 的
        ToolResultStateExtractor 管（TaskStateV3 已覆盖）,ledger 只收
        checkpoint 视图需要的少量关键事实。不猜、不过度提取。
        """
        added = []
        if ledger is None or not result_text:
            return added
        name = tool_name or ""
        if _CURRENT_TIME_TOOL_RE.search(name):
            m = _TIME_VALUE_RE.search(str(result_text))
            if m:
                rec = ledger.add("current_date", m.group(1), SYSTEM,
                                 source_ref=name)
                if rec is not None:
                    added.append(rec)
        return added

    @classmethod
    def feed_user_claim(cls, ledger: "EvidenceLedger", fact_key: str,
                        value: Any, source_ref: str = "") -> Optional[EvidenceRecord]:
        """用户自述 → ledger（user_claim 来源）。"""
        if ledger is None:
            return None
        return ledger.add(fact_key, value, USER_CLAIM, source_ref)

    @classmethod
    def feed_packet_facts(cls, ledger: "EvidenceLedger", packet: dict) -> list:
        """EvidencePacket → ledger（knowledge_base 来源）。

        packet 的 facts 声明进 ledger:key 用 claim 的规范化短语义
        （不是完整句子——用 claim 前 6 个词做 key 保证可查）。
        constraints 进 ledger 用 "<tool>.<param>.<kind>" key。
        """
        added = []
        if ledger is None or not isinstance(packet, dict):
            return added
        for f in packet.get("facts") or []:
            if isinstance(f, dict) and f.get("claim"):
                claim = str(f["claim"])
                key = _norm_fact_key(claim)
                rec = ledger.add(key, claim[:200], KNOWLEDGE_BASE,
                                 source_ref=f.get("source_doc_id") or "")
                if rec is not None:
                    added.append(rec)
        for c in packet.get("constraints") or []:
            if isinstance(c, dict) and c.get("parameter_name"):
                tool = (c.get("tool_name") or "rule").strip().lower()
                param = str(c["parameter_name"]).strip().lower()
                kind = str(c.get("constraint_type", "")).lower()
                key = f"{tool}.{param}.{kind}" if kind else f"{tool}.{param}"
                val = c.get("allowed_values") or c.get("max") or \
                    c.get("min") or c.get("format") or ""
                if val not in (None, ""):
                    rec = ledger.add(key, val, KNOWLEDGE_BASE,
                                     source_ref=c.get("source_doc_id") or "")
                    if rec is not None:
                        added.append(rec)
        return added

    # ------------------------------------------------------------------
    # 渲染（context_organization 用;条目硬上限防膨胀）
    # ------------------------------------------------------------------
    def render_confirmed_block(self, max_items: int = 6) -> str:
        recs = sorted(self.confirmed_facts(), key=lambda r: -r.seq)[:max_items]
        if not recs:
            return ""
        lines = []
        for r in recs:
            ref = f" [from {r.source_ref}]" if r.source_ref else ""
            v = str(r.value)
            if len(v) > 90:
                v = v[:90] + "…"
            lines.append(f"- {r.fact_key} = {v}{ref}")
        return "\n".join(lines)

    def render_unverified_block(self, max_items: int = 4) -> str:
        recs = sorted(self.unverified_claims(), key=lambda r: -r.seq)[:max_items]
        if not recs:
            return ""
        lines = []
        for r in recs:
            ref = f" (stated in {r.source_ref})" if r.source_ref else ""
            v = str(r.value)
            if len(v) > 90:
                v = v[:90] + "…"
            lines.append(f"- {r.fact_key} ≈ {v}{ref} — NOT verified by system")
        return "\n".join(lines)

    def render_policy_block(self, max_items: int = 4) -> str:
        recs = sorted(self.policy_facts(), key=lambda r: -r.seq)[:max_items]
        if not recs:
            return ""
        lines = []
        for r in recs:
            v = str(r.value)
            if len(v) > 110:
                v = v[:110] + "…"
            doc = f" [doc: {r.source_ref}]" if r.source_ref else ""
            lines.append(f"- {r.fact_key}: {v}{doc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # trace（实时 emit;无 recorder 静默）
    # ------------------------------------------------------------------
    def _emit(self, event_type: str, rec: EvidenceRecord, **extra) -> None:
        try:
            from eval.instrumentation import get_active_recorder
            rec_ = get_active_recorder()
        except Exception:
            return
        if rec_ is None:
            return
        try:
            rec_.emit(
                event_type, "decision_agent",
                parent_span_id=getattr(rec_, "task_span_id", None),
                fact_key=rec.fact_key,
                value=str(rec.value)[:self.VALUE_TRACE_MAX],
                provenance=rec.provenance, status=rec.status,
                source_ref=rec.source_ref, **extra)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._entries.clear()
        self._seq = 0


def _norm_fact_key(claim: str) -> str:
    """claim 句子 → 稳定事实 key（前 6 个语义词,下划线连接）。

    目的只是"同义 claim 幂等"（R4 需要 key 可比较）,不做语义索引
    ——查询方（checkpoint）用显式 key（current_date 等）+ 渲染列表,
    不依赖对 claim 的语义匹配。
    """
    words = re.findall(r"[a-z0-9]+", (claim or "").lower())
    return "_".join(words[:6]) if words else "fact"


def _loose_eq(a, b) -> bool:
    """宽松等价（与 TaskStateV3._loose_eq 同口径）。"""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return False
