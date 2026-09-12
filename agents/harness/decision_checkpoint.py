"""Decision Checkpoint（V6.0）—— 关键动作前的轻量证据检查点。

动机（Frozen V5 Dev24 trace 归因,docs/v6_design.md §2-C/D）:
    task_054: agent 在推荐/执行前陈述了错误事实（"dispute 与 CLI 独立"
    ——KB 明确相反）;21 次 handoff 重复问 CLI 问题,但关键冲突规则
    从未进入一个可审计的"决策前状态"。
    task_070: 两个 promotion doc 都被检索到,但 packet status=
    sufficient 只反映检索充分性;推荐决策需要的 current_date 从未
    与 promo 有效期对齐 → 推荐 Lime Green（gold: Sky Blue）。

设计（严格对照用户 V6 指令 §三-3,含三条硬约束）:
    1. **选择性触发**——只有关键动作（枚举）;普通查询零触发
       （010/021/024/037 成功路径保护——postmortem:对合法查询的
       介入=灾难）。
    2. **短且可审计**——artifact 是固定字段的结构化短文,不含
       chain-of-thought;LLM 判定输出仅一个短 JSON。
    3. **Runtime 不做业务判断**——Runtime 只产确定性的证据分段
       （confirmed/unverified/policy/missing）;READY/NOT_READY 与
       "补什么证据"由 LLM 判;"Sky Blue > Lime Green"永远不归
       Runtime（不写任何推荐/排序逻辑）。

执行流（防 postmortem 的"拦截-再生循环"形态——Step 2A 教训）:
    触发 → 构建 artifact → （可选）一次独立短 LLM 调用判 READY/
    NOT_READY → NOT_READY 时把 missing 列表以一条 system note 回注,
    **然后放行原调用**（不拦执行;提示有界 3 次;超限只记 trace）。
    唯一的硬拦条件不新增:参数与 TaskState 冲突的拦截 V5 已有
    （task_state_conflict）,checkpoint 不重复拦。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 触发范围（确定性枚举——用户 V6 指令的建议范围落地）
# ---------------------------------------------------------------------------
# 不可逆/用户影响的变更类动作:模式匹配工具名前缀/动词（通用,无
# task 硬编码）。读类（get_/search/lookup）与 ask/plan 工具不触发。
MUTATION_TOOL_RES = (
    re.compile(r"^(order|close|file|submit|approve|deny|pay|apply|open|"
               r"cancel|update|modify|create|delete|remove|transfer|"
               r"freeze|unfreeze|activate|replace|dispute|refer|give|"
               r"send|move|block|unblock|update_transaction)", re.I),
    re.compile(r"_(dispute|transfer|closure|replacement|referral|"
               r"limit_increase|rewards|verification)$", re.I),
)
# 特殊显式名单
ALWAYS_TRIGGER = {"transfer_to_human_agents"}

# 明确不触发（读/检索/规划/知识——保护 V5 成功路径）
NEVER_TRIGGER_PREFIXES = (
    "get_", "kb_search", "search", "lookup", "read_", "ask_knowledge",
    "write_plan", "update_plan", "read_plan", "noop", "unlock_",
    "log_verification",  # 记录类,非用户影响变更（log_verification 是审计记录）
)


def should_trigger_checkpoint(tool_name: str) -> bool:
    """触发判定（纯函数,确定性）。

    读类工具 / planning tools / ask_knowledge_agent → False（测试 #7）
    变更类（MUTATION 匹配）→ True（测试 #6）
    """
    name = (tool_name or "").strip()
    if not name:
        return False
    low = name.lower()
    if name in ALWAYS_TRIGGER:
        return True
    for p in NEVER_TRIGGER_PREFIXES:
        if low.startswith(p):
            return False
    for rex in MUTATION_TOOL_RES:
        if rex.search(low):
            return True
    return False


# ---------------------------------------------------------------------------
# Checkpoint artifact
# ---------------------------------------------------------------------------
@dataclass
class CheckpointArtifact:
    """短、可审计的决策检查点（不保存长推理——用户指令 §三-3）。"""
    goal: str = ""
    action_tool: str = ""
    action_summary: str = ""          # 参数摘要（截断）
    confirmed_facts: list = field(default_factory=list)   # ledger confirmed
    unverified_claims: list = field(default_factory=list)  # user_claim
    policy_evidence: list = field(default_factory=list)   # knowledge_base
    missing_evidence: list = field(default_factory=list)  # 确定性计算
    conflict_keys: list = field(default_factory=list)     # claim vs tool 矛盾

    def render(self) -> str:
        """渲染 artifact（trace 持久化 + LLM 判定 prompt 的核心段）。"""
        lines = ["[DECISION CHECKPOINT — pre-action evidence review]"]
        if self.goal:
            lines.append(f"Goal: {self.goal[:200]}")
        lines.append(f"Action about to execute: {self.action_tool} "
                     f"{self.action_summary[:160]}")
        if self.confirmed_facts:
            lines.append("Confirmed facts (tool/system-verified):")
            lines.extend(f"  - {f}" for f in self.confirmed_facts[:6])
        if self.unverified_claims:
            lines.append("Unverified user claims this decision may rely on:")
            lines.extend(f"  - {f}" for f in self.unverified_claims[:4])
        if self.policy_evidence:
            lines.append("Relevant policy evidence (KB):")
            lines.extend(f"  - {f}" for f in self.policy_evidence[:4])
        if self.missing_evidence:
            lines.append("Missing evidence (deterministic detection):")
            lines.extend(f"  - {f}" for f in self.missing_evidence[:5])
        if self.conflict_keys:
            lines.append("Claim/system conflicts (user claim contradicts "
                         "system record):")
            lines.extend(f"  - {k}" for k in self.conflict_keys[:4])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 触发器（DecisionAgent 持有;全状态有界）
# ---------------------------------------------------------------------------
class DecisionCheckpoint:
    """checkpoint 运行时(确定性构建 + 有界提示;LLM 判定可选注入)。

    预算:
      CHECKPOINT_MAX_PER_TASK  每任务最多 LLM 判定次数（超限只记
                                trace 不再调用——成本保护）
      PROMPT_LIMIT             NOT_READY 回注上限（防循环烧轮次——
                                postmortem 教训:提示类机制必须有界）
    """

    CHECKPOINT_MAX_PER_TASK = 8
    PROMPT_LIMIT = 3

    def __init__(self):
        self.llm_calls = 0
        self.prompts_injected = 0
        self._last_missing: list = []

    # ------------------------------------------------------------------
    # 构建 artifact（零 LLM;输入全部来自 runtime state）
    # ------------------------------------------------------------------
    def build_artifact(self, tool_name: str, arguments: dict,
                       ledger, plan_store=None, worklist=None,
                       open_questions: Optional[list] = None) -> CheckpointArtifact:
        """构建检查点 artifact。

        确定性内容:
        - goal: PlanStore.goal（无 plan 用空——不猜）
        - confirmed/unverified/policy: ledger 分段渲染
        - missing: 两类确定性可判项:
            (a) 声明的 needed_information/open_questions 未闭合项
                （来自 DA memory 的 open_questions——ask 时 LLM 自己
                 列的缺口;未被后续 packet/工具结果覆盖则仍 open）
            (b) 待执行动作参数引用的实体在 ledger/TaskState 中无
                confirmed 记录（实体级缺失——如 tenure 未查过）
                这一项只对 ID 型参数做（确定性:参数名匹配实体模式
                且值不出现在任何 tool_result 记录中）。
        - conflict_keys: ledger.keys_with_conflict()（用户自述与系统
          记录矛盾——070/100 形态的确定性暴露）
        """
        art = CheckpointArtifact()
        art.action_tool = tool_name or ""
        try:
            art.action_summary = json.dumps(arguments or {}, default=str)[:200]
        except Exception:
            art.action_summary = str(arguments or "")[:200]
        if plan_store is not None and getattr(plan_store, "goal", None):
            art.goal = str(plan_store.goal)
        if ledger is not None:
            art.confirmed_facts = [
                f"{k} = {v}"
                for k, v in self._ledger_confirmed_pairs(ledger)]
            art.unverified_claims = [
                f"{r.fact_key} ≈ {str(r.value)[:80]} (from {r.source_ref or 'user'})"
                for r in ledger.unverified_claims()]
            art.policy_evidence = [
                f"{r.fact_key}: {str(r.value)[:100]}"
                + (f" [doc: {r.source_ref}]" if r.source_ref else "")
                for r in ledger.policy_facts()]
            art.conflict_keys = list(ledger.keys_with_conflict())
            # (b) 实体级缺失:参数值无任何 tool-confirmed 来源
            art.missing_evidence.extend(self._entity_gaps(arguments, ledger))
        # (a) open questions 未闭合
        for q in (open_questions or [])[:5]:
            art.missing_evidence.append(f"open question: {str(q)[:120]}")
        self._last_missing = list(art.missing_evidence)
        return art

    # ------------------------------------------------------------------
    # LLM 判定（可选;单轮短调用,输出仅一个短 JSON）
    # ------------------------------------------------------------------
    CHECKPOINT_SYSTEM = (
        "You are reviewing a decision checkpoint before a critical action "
        "in a banking customer-service task. Given the goal, the action, "
        "and evidence organized by reliability (confirmed system facts, "
        "unverified user claims, KB policy rules, detected missing "
        "evidence), decide only ONE thing: is the evidence state READY "
        "for this action, or NOT_READY (more evidence must be gathered "
        "first)? You are NOT choosing products or making the business "
        "decision itself — only judging evidence sufficiency. Respond "
        "with ONLY a short JSON: "
        '{"decision_status": "READY" or "NOT_READY", '
        '"missing": ["<what key fact is still unverified>"], '
        '"next": "<one short sentence: the immediate evidence-gathering '
        'step>"}'
    )

    def run_llm_check(self, artifact: CheckpointArtifact, generate_fn):
        """执行一次 LLM 判定（调用方传 generate_fn——便于测试 stub 与
        统一 instrumentation;超预算返回 None 只记 trace）。

        返回 dict {decision_status, missing, next} 或 None（预算尽/
        调用失败——宽容,不阻塞主流程）。
        """
        if self.llm_calls >= self.CHECKPOINT_MAX_PER_TASK:
            self._emit("checkpoint_budget_exhausted",
                       used=self.llm_calls, limit=self.CHECKPOINT_MAX_PER_TASK)
            return None
        self.llm_calls += 1
        if generate_fn is None:
            return None
        try:
            from tau2.data_model.message import SystemMessage, UserMessage
            msgs = [SystemMessage(role="system", content=self.CHECKPOINT_SYSTEM),
                    UserMessage(role="user", content=artifact.render())]
            resp = generate_fn(model=None, messages=msgs, tools=None,
                               call_name="decision_checkpoint")
            content = (getattr(resp, "content", "") or "").strip()
            # 解析短 JSON（宽容:剥 ``` 与前后杂文）
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                obj = json.loads(content[s:e + 1])
                status = str(obj.get("decision_status", "")).upper()
                if status not in ("READY", "NOT_READY"):
                    status = "NOT_READY" if obj.get("missing") else "READY"
                out = {"decision_status": status,
                       "missing": [str(m)[:150] for m in (obj.get("missing") or [])][:5],
                       "next": str(obj.get("next", ""))[:200]}
                self._emit("checkpoint_decision", status=out["decision_status"],
                           missing=out["missing"], next_action=out["next"],
                           llm_call_no=self.llm_calls)
                return out
        except Exception:
            pass
        self._emit("checkpoint_decision", status="UNKNOWN",
                   missing=self._last_missing, llm_call_no=self.llm_calls)
        return None

    # ------------------------------------------------------------------
    # NOT_READY 回注文本（有界;不拦截）
    # ------------------------------------------------------------------
    def prompt_note(self, verdict: dict) -> Optional[str]:
        """NOT_READY → 一条 system note 文本（放行原调用,由 LLM 决定
        是否先补证据）。超过 PROMPT_LIMIT → None（只记 trace）。"""
        if not verdict or verdict.get("decision_status") != "NOT_READY":
            return None
        if self.prompts_injected >= self.PROMPT_LIMIT:
            self._emit("checkpoint_prompt_suppressed",
                       used=self.prompts_injected, limit=self.PROMPT_LIMIT)
            return None
        self.prompts_injected += 1
        missing = "; ".join(verdict.get("missing") or [])[:400]
        note = (
            "Evidence checkpoint: the decision you are about to make "
            "still relies on unverified information or has missing "
            f"evidence: [{missing}]. Consider verifying these facts "
            "(e.g. via a system lookup or a focused knowledge check) "
            "before executing the critical action. If you have already "
            "verified them and the records are simply unavailable, you "
            "may proceed with your best judgment and say so.")
        self._emit("checkpoint_prompt_injected", nth=self.prompts_injected,
                   missing=verdict.get("missing"))
        return note

    # ------------------------------------------------------------------
    def should_prompt(self, verdict: dict) -> bool:
        """是否回注提示（预算内 + NOT_READY + missing 非空）。

        用户指令 §三-3:Missing evidence != empty → 优先补证据——
        Runtime 的表达 = 提示,不是拦截。
        """
        return bool(verdict and verdict.get("decision_status") == "NOT_READY"
                    and verdict.get("missing")
                    and self.prompts_injected < self.PROMPT_LIMIT)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _ledger_confirmed_pairs(self, ledger) -> list:
        out = []
        for r in ledger.confirmed_facts():
            v = str(r.value)
            if len(v) > 80:
                v = v[:80] + "…"
            ref = f" [from {r.source_ref}]" if r.source_ref else ""
            out.append((r.fact_key, f"{v}{ref}"))
            if len(out) >= 8:
                break
        return out

    _ENTITY_ARG_RE = re.compile(
        r"^(user_id|account_id|card_id|credit_card_account_id|"
        r"transaction_id|record_id)$", re.I)

    def _entity_gaps(self, arguments: dict, ledger) -> list:
        """确定性实体缺失:ID 型参数值从未被任何 tool_result 记录见过。

        表达"实体已知 ≠ 查询结果已知"的正确方向:checkpoint 只标
        "这个 ID 在系统证据里没有记录",**不**因此阻止查询类调用
        （查询本来就该执行——postmortem Step2A 教训）;只对变更类
        动作（触发 checkpoint 的动作）标记。
        """
        gaps = []
        for k, v in (arguments or {}).items():
            if not self._ENTITY_ARG_RE.match(str(k)):
                continue
            if v in (None, ""):
                continue
            val = str(v)
            if not ledger._entries:   # ledger 空——整个状态缺失
                gaps.append(f"{k}={val} has no system record yet "
                            "(entity not seen in any tool result)")
                continue
            seen = any(
                r.provenance in ("tool_result", "system")
                and (val == str(r.value) or val in str(r.value))
                for chain in ledger._entries.values()
                for r in chain if r.is_current)
            if not seen:
                gaps.append(f"{k}={val} has no system record yet "
                            "(entity not seen in any tool result)")
        return gaps[:4]

    def _emit(self, event_type: str, **fields) -> None:
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            return
        if rec is None:
            return
        try:
            rec.emit(event_type, "decision_agent",
                     parent_span_id=getattr(rec, "task_span_id", None),
                     **fields)
        except Exception:
            pass

    def reset(self) -> None:
        self.llm_calls = 0
        self.prompts_injected = 0
        self._last_missing = []
