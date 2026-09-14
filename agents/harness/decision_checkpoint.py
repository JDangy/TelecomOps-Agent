"""Decision Checkpoint（V6.1 架构收紧）—— 关键动作前的轻量证据检查点。

动机（Frozen V5 Dev24 trace 归因 docs/v6_design.md §2-C/D）:
    task_054: agent 在 CLI 前陈述了错误事实（"dispute 与 CLI 独立"
    ——KB 明确相反）,关键冲突规则从未进入一个可审计的决策前状态。
    task_070: 推荐 promotion 时 current_date 从未与两个 promo 的
    有效期对齐——packet status=sufficient 只反映检索充分性。

收紧后的设计（用户指令 §4,与上一版 V6 的核心差异）:
    - **不是**"再问模型一次你确定吗"——上一版每次触发都调一次
      LLM 判 READY/NOT_READY（预算 8 次/task）,那就是变相确认循环。
      已删除。Runtime 零额外 LLM 调用。
    - 触发时 Runtime 【确定性】构建 6 问 artifact（短、固定字段,
      不含 chain-of-thought）,注入到该关键动作所属的下一轮生成——
      让 Decision Agent 自己在动作前显式检查这六个问题。执行权
      永远在 Decision Agent。
    - 触发范围严格按用户枚举的 7 类（见 TRIGGER 说明）;普通工具
      调用零触发,零 LLM。

六个问题（§4 原文,checkpoint artifact 的固定字段）:
    1. 当前目标是什么?                ← PlanStore.goal
    2. 哪些关键事实已经确认?          ← EvidenceView.confirmed_system_facts
    3. 哪些只是用户自己说的?          ← EvidenceView.unverified_user_claims
    4. 有什么硬规则?                  ← EvidenceView.policy_rules
    5. 还缺不缺关键证据?              ← 确定性 missing 计算
    6. 现在是否可以执行?              ← Decision Agent 在回复里回答

    硬约束:不做拦截（NOT_READY 不存在了——没有第二次 LLM 调用,
    也就没有"提示后放行"的循环形态）;不生成 chain-of-thought;
    checkpoint 结果不进对话历史,只注入下轮 system 尾部 + trace。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 触发范围（用户指令 §4 的枚举落地;确定性,无 task/domain 硬编码）
# ---------------------------------------------------------------------------
# 1. 不可逆/高影响操作 + transfer + finish 的模式特征（通用动词词形,
#    banking 域所有 submit/order/close/pay/apply/open/freeze/… 全覆盖）
MUTATION_TOOL_RES = (
    # 不可逆/变更类动词前缀（submit/order/close/pay/apply/open/approve/
    # deny/freeze/unfreeze/activate/replace/refer/file/cancel/delete…）
    re.compile(r"^(order|close|file|submit|approve|deny|pay|apply|open|"
                r"cancel|update|modify|create|delete|remove|freeze|"
                r"unfreeze|activate|replace|refer|send|move|block|"
                r"unblock)", re.I),
    # 后缀特征（_dispute/_transfer/_referral/_limit_increase/…——
    # 名词化的关键动作,如 update_transaction_rewards、give_* 不在其中）
    re.compile(r"_(dispute|transfer|closure|replacement|referral|"
               r"limit_increase)$", re.I),
)
# 2. transfer_to_human_agents（显式名单）
ALWAYS_TRIGGER = {"transfer_to_human_agents"}

# 明确不触发（读类/检索/规划/知识/审计——普通工具调用零 checkpoint;
# 保护 V5 成功路径:postmortem 教训——对合法查询的介入=灾难）
NEVER_TRIGGER_PREFIXES = (
    "get_", "kb_search", "search", "lookup", "read_", "ask_knowledge",
    "write_plan", "update_plan", "read_plan", "noop", "unlock_",
    "log_verification",
)


def should_trigger_checkpoint(tool_name: str) -> bool:
    """触发判定（纯函数,确定性）。

    覆盖用户枚举的:
    - 不可逆或高影响操作 / transfer / finish(转人工)
      → MUTATION_TOOL_RES / ALWAYS_TRIGGER
    - 最终推荐、多方案选择、规则冲突、顺序影响资格、依赖未确认信息
      → 这五类的【确定性可判】部分由调用侧在推荐轮显式触发
      （two_agent 侧 trigger_recommendation_checkpoint;语义性的
      "多方案选择"无法从工具名枚举,由推荐/提交流程统一触发,此处
      不做业务判断）。
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
# 最终推荐意图（V6.1 修复 #3）—— 覆盖"tool call 之前就发生的推荐"
#
# task_070 形态:Agent 在关键日期/条件尚未对齐时就向用户做了最终产品推荐
# （"I recommend Lime Green."）——tool-name 触发枚举覆盖不到纯文本推荐。
#
# 设计约束（用户指令）:
#   - 通用 recommendation / selection 意图判断,**绝不含具体产品词或
#     task 专名**（禁止 `if "Lime Green" in text`）。
#   - 保守:宁可少触发,不大面积误触发。普通陈述（"已查到账户"/"卡是
#     active"/"还需确认一个信息"）不触发。
#   - 只识别"最终选择 / 最终推荐"这一语义形态。
# ---------------------------------------------------------------------------
RECOMMENDATION_INTENT_RES = (
    # 第一人称推荐/建议
    re.compile(r"\bi(?:\s+would|\s+will|'d)?\s+recommend\b", re.I),
    re.compile(r"\bmy recommendation\b", re.I),
    re.compile(r"\bi\s+suggest\b", re.I),
    # 明确让用户选某个（"You should choose X."）
    re.compile(r"\byou should (?:choose|go with|pick|select|open|"
               r"apply for|take)\b", re.I),
    re.compile(r"\bi(?:'d|\s+would)\s+(?:go with|choose|pick|select)\b", re.I),
    # 最高级判定形态（"The best option is X." / "X is the most suitable
    # account."）——限定到"选项/账户/卡/产品/方案"这类选择对象名词,
    # 不匹配普通描述。
    re.compile(r"\bthe (?:best|most suitable) "
               r"(?:option|choice|account|card|product|plan)\b", re.I),
    re.compile(r"\b(?:is|would be) the (?:best|most suitable) "
               r"(?:option|choice|account|card|product|plan)\b", re.I),
)


def is_final_recommendation(text: str) -> bool:
    """纯文本是否为"最终选择/最终推荐"意图（通用、确定、保守）。

    只在助手【准备向用户给出最终选择】时返回 True——普通进展陈述
    （查询结果/状态说明/继续确认信息）不匹配。宁可少触发。
    """
    if not text:
        return False
    for rex in RECOMMENDATION_INTENT_RES:
        if rex.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# Checkpoint artifact（6 问,短,可审计）
# ---------------------------------------------------------------------------
@dataclass
class CheckpointArtifact:
    """关键动作前的 6 问检查点（固定字段,无 chain-of-thought）。"""
    goal: str = ""                     # 问题1:当前目标
    action_tool: str = ""
    action_summary: str = ""            # 参数摘要（截断）
    confirmed_facts: list = field(default_factory=list)    # 问题2
    unverified_claims: list = field(default_factory=list)  # 问题3
    policy_evidence: list = field(default_factory=list)    # 问题4
    missing_evidence: list = field(default_factory=list)   # 问题5
    conflict_keys: list = field(default_factory=list)      # 矛盾暴露
    # 问题6（"现在是否可以执行"）不在 artifact 里——那是 Decision
    # Agent 在下轮生成里自己回答的,Runtime 不代答。

    def render(self) -> str:
        """渲染为注入下轮 system 尾部的短检查块（+trace 持久化）。"""
        lines = ["[DECISION CHECKPOINT — verify before executing the "
                 "action below]"]
        if self.goal:
            lines.append(f"1. Current goal: {self.goal[:200]}")
        lines.append(f"2. Action pending: {self.action_tool} "
                     f"{self.action_summary[:140]}")
        if self.confirmed_facts:
            lines.append("3. Confirmed by system (safe to rely on):")
            lines.extend(f"   - {f}" for f in self.confirmed_facts[:6])
        if self.unverified_claims:
            lines.append("4. Stated by user but NOT verified by system "
                         "(verify before relying):")
            lines.extend(f"   - {f}" for f in self.unverified_claims[:4])
        if self.policy_evidence:
            lines.append("5. Hard rules from the knowledge base:")
            lines.extend(f"   - {f}" for f in self.policy_evidence[:4])
        if self.missing_evidence:
            lines.append("6. Missing key evidence (deterministic "
                         "detection):")
            lines.extend(f"   - {f}" for f in self.missing_evidence[:5])
        if self.conflict_keys:
            lines.append("   ! User statements contradicting system "
                         "records: " + ", ".join(self.conflict_keys[:4]))
        lines.append("Before executing, answer briefly (to yourself, "
                     "no long reasoning): goal / verified facts / "
                     "hard rules / missing evidence — execute now or "
                     "gather evidence first?")
        return "\n".join(lines)

    def render_audit(self) -> str:
        """trace 用审计渲染（同内容,标注各段来源模块）。"""
        return self.render()


# ---------------------------------------------------------------------------
# 触发器（DecisionAgent 持有;有界,零 LLM）
# ---------------------------------------------------------------------------
class DecisionCheckpoint:
    """checkpoint 运行时（确定性构建 + 有界注入;零额外 LLM 调用）。

    预算:
        MAX_PER_TASK   每任务 checkpoint 触发上限（超限只记 trace
                       不再注入——context 保护,防长任务膨胀）
    注入语义:触发时构建 artifact 存 pending,下一次 generate 前由
    DecisionAgent 以 system 尾注注入【一次】。artifact 只呈现证据
    状态,不评价、不拦截、不要求模型回复确认——模型在正常执行该
    动作时自然会读到这六问。
    """

    MAX_PER_TASK = 8

    def __init__(self):
        self.triggered = 0
        self.pending: "CheckpointArtifact | None" = None

    # ------------------------------------------------------------------
    # 构建 artifact（零 LLM;输入全部来自 runtime state）
    # ------------------------------------------------------------------
    def build_artifact(self, tool_name: str, arguments: dict,
                       evidence_view, plan_store=None,
                       open_questions: list | None = None
                       ) -> CheckpointArtifact:
        """构建 6 问 artifact。

        确定性内容:
        - goal: PlanStore.goal（无 plan 用空——不猜）
        - confirmed/unverified/policy: EvidenceView 四段数据
          （TaskStateV3 现算,无独立存储）
        - missing: 两类确定性可判项:
            (a) 声明的 needed_information/open_questions 未闭合项
            (b) 待执行动作的 ID 型参数值从未被任何 tool_result 记录
                见过（实体级缺失——如引用了从未查询过的账户）
        - conflict_keys: 用户陈述与系统记录矛盾的 key
        """
        art = CheckpointArtifact()
        art.action_tool = tool_name or ""
        try:
            art.action_summary = json.dumps(arguments or {},
                                            default=str)[:200]
        except Exception:
            art.action_summary = str(arguments or "")[:200]
        if plan_store is not None and getattr(plan_store, "goal", None):
            art.goal = str(plan_store.goal)
        if evidence_view is not None:
            art.confirmed_facts = [
                f"{r.key} = {str(r.value)[:80]}"
                + (f" [from {r.source_ref}]" if r.source_ref else "")
                for r in evidence_view.confirmed_system_facts()]
            art.unverified_claims = [
                f"{r.key} ≈ {str(r.value)[:70]}"
                + (f" (stated by user)" if r.source == "user" else "")
                for r in evidence_view.unverified_user_claims()]
            art.policy_evidence = [
                f"{r.key}: {str(r.value)[:100]}"
                + (f" [doc: {r.source_ref}]" if r.source_ref else "")
                for r in evidence_view.policy_rules()]
            art.conflict_keys = list(evidence_view.conflict_keys())
            # (b) 实体级缺失（参数值无 tool 确认记录）
            art.missing_evidence.extend(
                self._entity_gaps(arguments, evidence_view.task_state))
        # (a) open questions 未闭合
        for q in (open_questions or [])[:5]:
            art.missing_evidence.append(f"open question: {str(q)[:120]}")
        return art

    # ------------------------------------------------------------------
    def note_for_next_turn(self, tool_name: str, arguments: dict,
                           evidence_view, plan_store=None,
                           open_questions: list | None = None,
                           reason: str = "critical_action",
                           skip_if_empty: bool = False) -> str | None:
        """触发入口:构建 artifact 并暂存为下轮 system 尾注。

        reason: 触发来源（"critical_action" / "final_recommendation"
                ——trace 区分,不改变注入语义）。
        skip_if_empty: True 时空证据 artifact 不注入、不计数（最终推荐
                路径的"克制"开关——空状态零渲染,保护简单任务零开销）。
        返回 None = 超预算或（skip_if_empty 且空）不注入;
        否则返回注入文本（调用方 append 到 state.messages 的
        system 尾部——一次性,下轮 generate 前生效）。
        """
        if self.triggered >= self.MAX_PER_TASK:
            self._emit("checkpoint_budget_exhausted",
                       used=self.triggered, limit=self.MAX_PER_TASK)
            return None
        art = self.build_artifact(tool_name, arguments, evidence_view,
                                  plan_store, open_questions)
        if skip_if_empty and not self.artifact_has_content(art):
            return None  # 空状态零渲染（简单任务不做推荐检查）
        self.triggered += 1
        self.pending = art
        self._emit("checkpoint_triggered", tool=tool_name,
                   reason=reason,
                   missing=art.missing_evidence or None)
        if art.missing_evidence:
            self._emit("checkpoint_missing_evidence", tool=tool_name,
                       missing=art.missing_evidence)
        return art.render()

    @staticmethod
    def artifact_has_content(art: "CheckpointArtifact") -> bool:
        """artifact 是否含任何可呈现证据（克制判断:全空则无检查价值）。"""
        if art is None:
            return False
        return bool(art.confirmed_facts or art.unverified_claims
                    or art.policy_evidence or art.missing_evidence
                    or art.conflict_keys)

    def consume_pending(self) -> "CheckpointArtifact | None":
        """取走暂存的 artifact（注入后清空——一次性语义）。"""
        art = self.pending
        self.pending = None
        return art

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    _ENTITY_ARG_RE = re.compile(
        r"^(user_id|account_id|card_id|credit_card_account_id|"
        r"transaction_id|record_id)$", re.I)

    def _entity_gaps(self, arguments: dict, task_state) -> list:
        """确定性实体缺失:ID 型参数值从未被任何 tool 写入见过。

        只对变更类动作（触发 checkpoint 的动作）标记,不阻止执行
        ——查询本来就该执行;这里只是把"这个 ID 在系统证据里没有
        记录"显式呈给决策层。
        """
        gaps = []
        seen_ids = set()
        try:
            for chain in getattr(task_state, "_entries", {}).values():
                for r in chain:
                    if r.source in ("tool", "knowledge") \
                            and r.field in ("id", "account_id", "card_id",
                                            "user_id", "transaction_id",
                                            "record_id",
                                            "credit_card_account_id"):
                        seen_ids.add(str(r.value))
        except Exception:
            return gaps
        for k, v in (arguments or {}).items():
            if not self._ENTITY_ARG_RE.match(str(k)) or v in (None, ""):
                continue
            if str(v) not in seen_ids:
                gaps.append(f"{k}={v} has no system record yet "
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
        self.triggered = 0
        self.pending = None
