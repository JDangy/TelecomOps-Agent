"""ActionHarness（V2.2 重构）—— 三源确定性约束的执行层。

主路径（默认启用）：
    proposed → resolve → SchemaValidation（enum/type/required 观察）
            → TaskStateValidator（用户显式值 / 工具返回 ID 的冲突）
            → KnowledgeConstraintValidator（KB enum 集合/阈值/格式）
            → 任一 blocking → 结构化拒绝（含约束来源）
            → 全过 → 放行（validate_only 由 orchestrator 执行）

可选 policy（保留历史，默认关闭）：
    EvidenceParameterValidation（V2/V2.1 的案例值比对）——V2.1 证明
    "所有参数须有 evidence 正确值"前提不成立；代码保留（历史实验入口
    two_agent_harness_v21 与复现脚本仍可用），主线不挂。

trace 事件链（V2.2 增强）：
    action_proposed → action_resolved → action_validation
        （每 verdict 带 constraint_source）→ action_rejected
        （含 conflicting_source: user/tool_result/kb）/ action_executed

拦截原则（贯穿所有 validator）：
    有明确依据才拦；unknown/not_in_task_state/no_kb_constraint
    一律放行并记录。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from agents.harness.base import (
    HarnessContext,
    ValidationPolicy,
    ValidationVerdict,
)
from agents.harness.resolver import ActionResolver, ResolvedAction
from agents.harness.task_state import TaskState
from agents.harness.validators import (
    EvidenceParameterValidation,
    SchemaValidation,
)
from agents.harness.task_state_validator import TaskStateValidator
from agents.harness.kb_validator import KnowledgeConstraintValidator


# blocking verdict 集（各 validator 产出；不在集合内的一律放行）
BLOCKING_VERDICTS = {
    ValidationVerdict.SCHEMA_VIOLATION,       # 违反工具 schema（enum/type）
    ValidationVerdict.EVIDENCE_MISMATCH,      # （仅当 evidence policy 启用）
    "task_state_conflict",                    # 与用户显式值/工具返回值冲突
    "kb_enum_violation",                      # 违反 KB enum 集合
    "kb_threshold_violation",                 # 违反 KB 阈值
    "kb_format_violation",                    # 违反 KB 格式
}


def _is_blocking(v: ValidationVerdict) -> bool:
    return v.verdict in BLOCKING_VERDICTS


# verdict → 约束来源（trace 观测用）
VERDICT_SOURCE = {
    "schema_violation": "tool_schema",
    "task_state_conflict": None,       # 动态：user / tool_result
    "kb_enum_violation": "knowledge",
    "kb_threshold_violation": "knowledge",
    "kb_format_violation": "knowledge",
    "evidence_mismatch": "knowledge",
}


class ActionHarness:
    """Agent 与业务工具之间的确定性约束执行层（V2.2 三源架构）。"""

    def __init__(self, policies: Optional[list[ValidationPolicy]] = None,
                 include_evidence_policy: bool = False):
        """policies: 自定义 policy 列表（默认 None → 三源主路径）。

        include_evidence_policy: True 时附加 V2.1 的
        EvidenceParameterValidation（历史实验复现用；主线默认关闭）。
        """
        if policies is not None:
            self.policies = list(policies)
        else:
            self.policies = [
                SchemaValidation(),
                TaskStateValidator(TaskState()),  # 占位——由 wire() 换真实实例
                KnowledgeConstraintValidator([]),
            ]
            if include_evidence_policy:
                self.policies.append(EvidenceParameterValidation())
        self.resolver = ActionResolver()

    # ------------------------------------------------------------------
    # 装配（DecisionAgent 构造时调用——三源接线）
    # ------------------------------------------------------------------
    def wire(self, task_state: TaskState,
             kb_constraints: Optional[list] = None) -> None:
        """把 Decision Agent 维护的 TaskState / KB 约束接进 validators。

        task_state: 参数级 provenance registry（DecisionAgent 每
        user 消息/工具结果喂入）。
        kb_constraints: packets 中收集的明确 KB 约束列表（DecisionAgent
        从 Evidence Packet 的 constraints 字段累积）。
        """
        for p in self.policies:
            if isinstance(p, TaskStateValidator):
                p.task_state = task_state
            if isinstance(p, KnowledgeConstraintValidator):
                p.constraints = kb_constraints or []

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def process(self, tool_call, execute: Callable[[dict], Any],
                context: HarnessContext, validate_only: bool = False
                ) -> tuple[bool, str, dict]:
        outer_name = getattr(tool_call, "name", "(unknown)")
        outer_args = dict(getattr(tool_call, "arguments", None) or {})

        self._emit("action_proposed", outer_name,
                   proposed_arguments=outer_args)

        # 解析（wrapper 穿透）
        wrapper_tool = getattr(tool_call, "_harness_tool", None)
        if wrapper_tool is not None and not self.resolver._inner_registry:
            try:
                self.resolver.__init__(wrapper_tool)
            except Exception:
                pass
        resolved = self.resolver.resolve(tool_call)
        self._emit("action_resolved", outer_name,
                   inner_tool_name=resolved.tool_name,
                   inner_arguments=resolved.arguments,
                   is_wrapper=resolved.is_wrapper,
                   resolve_error=resolved.resolve_error)

        # 校验（三源；validator 异常不拦——放行）
        verdicts: list[ValidationVerdict] = []
        for policy in self.policies:
            try:
                if hasattr(policy, "validate_action"):
                    verdicts.extend(policy.validate_action(resolved, context))
                else:
                    verdicts.extend(policy.validate_arguments(
                        wrapper_tool, resolved.arguments, context))
            except Exception:
                continue

        self._emit("action_validation", resolved.tool_name,
                   verdicts=[self._verdict_trace(v) for v in verdicts])

        blocking = [v for v in verdicts if _is_blocking(v)]
        if blocking:
            failed_fields = [v.field for v in blocking]
            sources = [v.detail.get("value_source")
                       or VERDICT_SOURCE.get(v.verdict)
                       or "unknown" for v in blocking]
            refs = [v.detail.get("source_doc_id") for v in blocking
                    if v.detail.get("source_doc_id")]
            self._emit("action_rejected", resolved.tool_name,
                       failed_fields=failed_fields,
                       constraint_sources=sources,
                       evidence_references=refs,
                       verdicts_summary=[{"field": v.field, "verdict": v.verdict}
                                         for v in blocking],
                       outer_tool_name=outer_name)
            content = self._rejection_message(resolved.tool_name, blocking)
            return False, content, {
                "verdicts": verdicts, "final_arguments": None,
                "rejected": True, "resolved": resolved,
            }

        if validate_only:
            return True, "", {
                "verdicts": verdicts,
                "final_arguments": resolved.arguments,
                "rejected": False, "validate_only": True, "resolved": resolved,
            }

        try:
            result = execute(outer_args if resolved.is_wrapper else resolved.arguments)
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            success = True
        except Exception as exc:
            content = f"Error: {type(exc).__name__}: {exc}"
            success = False
        self._emit("action_executed", resolved.tool_name,
                   final_arguments=resolved.arguments, success=success)
        return True, content, {
            "verdicts": verdicts, "final_arguments": resolved.arguments,
            "rejected": False, "resolved": resolved,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _verdict_trace(v: ValidationVerdict) -> dict:
        d = {"field": v.field, "verdict": v.verdict,
             "detail": v.detail}
        d["constraint_source"] = (v.detail.get("value_source")
                                  or VERDICT_SOURCE.get(v.verdict))
        return d

    @staticmethod
    def _rejection_message(tool_name: str, blocking: list) -> str:
        lines = [f"Harness validation failed for {tool_name}. "
                 "Do NOT retry with the same values."]
        for v in blocking:
            d = v.detail
            src = d.get("value_source") or VERDICT_SOURCE.get(v.verdict) or ""
            lines.append(f"\nfield: {v.field}" + (f" (conflicts with {src} value)"
                                                  if "confirmed_value" in d else ""))
            if d.get("error"):
                lines.append(f"error: {d['error']}")
            for k, label in (("proposed", "proposed"), ("proposed_value", "proposed"),
                             ("allowed_values", "allowed_values"),
                             ("confirmed_value", "confirmed_value"),
                             ("kb_allowed_values", "allowed_values"),
                             ("kb_max", "kb_max"), ("kb_min", "kb_min"),
                             ("evidence_value", "evidence_value")):
                if k in d:
                    lines.append(f"{label}: {json.dumps(d[k])}")
            if d.get("source_ref"):
                lines.append(f"from: {d['source_ref']}")
        lines.append("\nFix the listed fields and call the tool again "
                     "with corrected arguments.")
        return "\n".join(lines)

    def _emit(self, event_type: str, tool_name: str, **fields) -> None:
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
                     tool_name=tool_name, harness=True, **fields)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 兼容入口（历史实验保留）：V2.1 形态 = 三源 + evidence policy
# ---------------------------------------------------------------------------
def build_v21_harness() -> ActionHarness:
    """V2.1 复现入口：主路径 + EvidenceParameterValidation（默认关闭的对照）。"""
    return ActionHarness(include_evidence_policy=True)


# packets constraints 收集（DecisionAgent 用）
def constraints_from_packets(packets: list) -> list:
    """从 Evidence Packets 收集明确 KB 约束（新 constraints 字段）。"""
    out = []
    for p in packets or []:
        for c in p.get("constraints") or []:
            if isinstance(c, dict) and c.get("parameter_name"):
                out.append(c)
    return out


# 兼容 V2/V2.1 的 context_from_packets（历史入口保留）
def context_from_packets(packets: list[dict],
                         user_values: Optional[dict] = None) -> HarnessContext:
    from agents.harness.validators import context_from_packets as _cfp
    return _cfp(packets, user_values)
