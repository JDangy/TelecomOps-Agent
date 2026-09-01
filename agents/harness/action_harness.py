"""ActionHarness —— 校验编排 + 执行 + trace 记录。

一次业务工具调用的完整流（由 DecisionAgent 拦截层调用 process()）：

    proposed action ──→ validators（schema → evidence，按序）
                          │
              全部放行 ──┴── 任何 blocking ──→ 结构化错误回传（Agent 自己修正）
                          │
                          ↓
                     execute（原样调用原工具）
                          ↓
                     结果 ToolMessage 回传 + trace 全程记录

错误消息格式（回传给 Decision Agent 的 ToolMessage content）——
结构化、机器可读，Agent 据此修正参数后重试：

    Harness validation failed. Do NOT retry with the same values.
    field: reason
    error: enum_violation
    proposed: "closure"
    allowed_values: ["account_closure", "fraud", "customer_request"]
    Fix the listed fields and call the tool again with corrected arguments.

trace v2 事件（eval/instrumentation recorder，不存在时静默）：
    action_proposed  {tool_name, proposed_arguments}
    action_validation {tool_name, verdicts: [{field, verdict, detail}]}
    action_rejected  {tool_name, failed_fields, evidence_references}
    action_executed  {tool_name, final_arguments, success}
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from agents.harness.base import (
    HarnessContext,
    ValidationPolicy,
    ValidationVerdict,
)
from agents.harness.validators import (
    EvidenceParameterValidation,
    SchemaValidation,
)


class ActionHarness:
    """Agent 与业务工具之间的确定性执行层（见 base.py 模块 docstring）。"""

    def __init__(self, policies: Optional[list[ValidationPolicy]] = None):
        # 默认只挂 V2 的两类校验；policies 列表即未来扩展插槽
        self.policies: list[ValidationPolicy] = policies or [
            SchemaValidation(),
            EvidenceParameterValidation(),
        ]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def process(self, tool_call, execute: Callable[[dict], Any],
                context: HarnessContext, validate_only: bool = False
                ) -> tuple[bool, str, dict]:
        """校验并（若通过且非 validate_only）执行一个 proposed tool call。

        Args:
            tool_call: tau2 ToolCall（name + arguments）
            execute: callable(arguments) -> Any —— 实际执行原工具的回调
                     （由 DecisionAgent 提供——通常调 environment 侧）
            context: 校验上下文（evidence_values / user_context_values）
            validate_only: True 时只校验不执行（DecisionAgent 的拦截层用：
                     通过的调用原样留给 orchestrator 执行，保持消息流兼容；
                     此时返回 executed=True 仅表示"校验通过"）

        Returns:
            (executed, content, meta):
              executed=True  → content = 工具原始结果文本（validate_only 时为空串）
              executed=False → content = 结构化错误消息（Agent 修正后重试）
              meta = {"verdicts": [...], "final_arguments": ...} 供 trace/指标
        """
        tool = self._tool_for(tool_call)
        arguments = dict(getattr(tool_call, "arguments", None) or {})
        name = getattr(tool_call, "name", "(unknown)")

        self._emit("action_proposed", name,
                   proposed_arguments=arguments)

        # 校验（按 policy 顺序）
        verdicts: list[ValidationVerdict] = []
        for policy in self.policies:
            verdicts.extend(policy.validate_arguments(tool, arguments, context))

        self._emit("action_validation", name,
                   verdicts=[{"field": v.field, "verdict": v.verdict, "detail": v.detail}
                            for v in verdicts])

        blocking = [v for v in verdicts if v.is_blocking]
        if blocking:
            # 拦截：结构化错误回传
            failed_fields = [v.field for v in blocking]
            evidence_refs = [v.detail.get("source_doc_id") for v in blocking
                             if v.detail.get("source_doc_id")]
            self._emit("action_rejected", name,
                       failed_fields=failed_fields,
                       evidence_references=evidence_refs,
                       verdicts_summary=[{"field": v.field, "verdict": v.verdict}
                                         for v in blocking])
            content = self._rejection_message(name, blocking)
            return False, content, {
                "verdicts": verdicts, "final_arguments": None,
                "rejected": True,
            }

        if validate_only:
            # 只校验：通过即返回（执行交给调用方/orchestrator）
            return True, "", {
                "verdicts": verdicts, "final_arguments": arguments,
                "rejected": False, "validate_only": True,
            }

        # 放行：执行原工具（原样调用——Harness 不改参数）
        try:
            result = execute(arguments)
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            success = True
        except Exception as exc:
            content = f"Error: {type(exc).__name__}: {exc}"
            success = False
        self._emit("action_executed", name,
                   final_arguments=arguments, success=success)
        return True, content, {
            "verdicts": verdicts, "final_arguments": arguments,
            "rejected": False,
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _tool_for(self, tool_call):
        """校验用工具对象——由 DecisionAgent 预注册（_tool_lookup）。"""
        return getattr(tool_call, "_harness_tool", None)

    @staticmethod
    def _rejection_message(tool_name: str, blocking: list) -> str:
        """给 Decision Agent 的结构化修正指令。"""
        lines = [f"Harness validation failed for {tool_name}. "
                 "Do NOT retry with the same values."]
        for v in blocking:
            d = v.detail
            lines.append(f"\nfield: {v.field}")
            if d.get("error"):
                lines.append(f"error: {d['error']}")
            if "proposed" in d or "proposed_value" in d:
                lines.append(f"proposed: {json.dumps(d.get('proposed', d.get('proposed_value')))}")
            if "allowed_values" in d:
                lines.append(f"allowed_values: {json.dumps(d['allowed_values'])}")
            if "evidence_value" in d:
                lines.append(f"evidence_value: {json.dumps(d['evidence_value'])}"
                             + (f" (source: {d['source_doc_id']})" if d.get("source_doc_id") else ""))
        lines.append("\nFix the listed fields and call the tool again "
                     "with corrected arguments.")
        return "\n".join(lines)

    def _emit(self, event_type: str, tool_name: str, **fields) -> None:
        """trace v2 事件（无 recorder 静默；绝不影响执行）。"""
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
# Evidence Packet grounded_values → HarnessContext 的构建辅助
# ---------------------------------------------------------------------------
def context_from_packets(packets: list[dict],
                         user_values: Optional[dict] = None) -> HarnessContext:
    """从本任务收到过的 Evidence Packets 构建 HarnessContext。

    packets: Decision Agent 侧累积的 packet dict 列表（grounded_values 索引来源）。
    user_values: 用户明确提供的参数值（名称->值），这些参数跳过 KB 比对。
    """
    ev: dict = {}
    for p in packets or []:
        for gv in p.get("grounded_values") or []:
            if isinstance(gv, dict) and gv.get("name"):
                # 同名参数以最新 packet 为准（后写覆盖）
                ev[gv["name"]] = {
                    "value": gv.get("value"),
                    "value_type": gv.get("value_type"),
                    "source_doc_id": gv.get("source_doc_id"),
                    "unit": gv.get("unit"),
                }
    return HarnessContext(evidence_values=ev, user_context_values=user_values or {})
