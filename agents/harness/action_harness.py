"""ActionHarness —— 解析 + 校验 + 执行 + trace 记录（V2.1）。

V2.1 流程（与 V2 的差异：先 resolve，校验 inner）：

    proposed tool call ──→ ActionResolver.resolve()
                            │（wrapper 穿透：inner tool + inner args + inner schema）
                            ↓ action_resolved
                          validators（schema → evidence，针对 ResolvedAction）
                          │
              全部放行 ───┴── 任何 blocking ──→ 结构化错误回传（Agent 自己修正）
                          │
                          ↓
                     execute（或 validate_only 放行原调用）
                          ↓
                     结果 + trace 全程记录

trace v2 事件链（第八节）：
    action_proposed（outer） → action_resolved（outer→inner+args）
    → action_validation（verdicts）→ action_rejected / action_executed

错误消息（回传 DA）：V2 格式 + tool 行（inner 工具名）。
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
from agents.harness.validators import (
    EvidenceParameterValidation,
    SchemaValidation,
)


class ActionHarness:
    """Agent 与业务工具之间的确定性执行层（resolver + validators）。"""

    def __init__(self, policies: Optional[list[ValidationPolicy]] = None):
        self.policies: list[ValidationPolicy] = policies or [
            SchemaValidation(),
            EvidenceParameterValidation(),
        ]
        self.resolver = ActionResolver()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def process(self, tool_call, execute: Callable[[dict], Any],
                context: HarnessContext, validate_only: bool = False
                ) -> tuple[bool, str, dict]:
        """解析 → 校验并（若通过且非 validate_only）执行一个 proposed tool call。

        Args:
            tool_call: tau2 ToolCall（name + arguments）
            execute: callable(arguments) -> Any（validate_only=False 时用）
            context: 校验上下文（V2.1: tool/param 双键 evidence 索引）
            validate_only: True 时只校验不执行（DecisionAgent 拦截层用：
                     通过的调用原样留给 orchestrator 执行保持消息流兼容）

        Returns:
            (passed, content, meta)：passed=False 时 content = 结构化错误。
        """
        outer_name = getattr(tool_call, "name", "(unknown)")
        outer_args = dict(getattr(tool_call, "arguments", None) or {})

        self._emit("action_proposed", outer_name,
                   proposed_arguments=outer_args)

        # ---- V2.1: 先解析（wrapper 穿透）----
        # resolver 需要看到 wrapper tool（挂在 _harness_tool）——惰性注册表
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

        # ---- 校验（针对 ResolvedAction 的 inner 参数）----
        verdicts: list[ValidationVerdict] = []
        for policy in self.policies:
            try:
                if hasattr(policy, "validate_action"):
                    verdicts.extend(policy.validate_action(resolved, context))
                else:  # V2 兼容旧接口（不应走到——validators 都已升级）
                    verdicts.extend(policy.validate_arguments(
                        wrapper_tool, resolved.arguments, context))
            except Exception:
                # 校验器异常不影响放行（校验层故障 ≠ 调用错误——不能因
                # harness 自身 bug 拦截正确调用）
                continue

        self._emit("action_validation", resolved.tool_name,
                   verdicts=[{"field": v.field, "verdict": v.verdict, "detail": v.detail}
                            for v in verdicts])

        blocking = [v for v in verdicts if v.is_blocking]
        if blocking:
            failed_fields = [v.field for v in blocking]
            evidence_refs = [v.detail.get("source_doc_id") for v in blocking
                             if v.detail.get("source_doc_id")]
            self._emit("action_rejected", resolved.tool_name,
                       failed_fields=failed_fields,
                       evidence_references=evidence_refs,
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

        # 执行（原样调用——Harness 不改参数；wrapper 调用由外层 execute 决定）
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
    def _rejection_message(tool_name: str, blocking: list) -> str:
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
# Evidence Packet grounded_values → HarnessContext（V2.1 键格式）
# ---------------------------------------------------------------------------
def context_from_packets(packets: list[dict],
                         user_values: Optional[dict] = None) -> HarnessContext:
    """从 Evidence Packets 构建校验上下文。

    V2.1 grounded_values 新格式（优先）：
        {tool_name, parameter_name, value, value_type, source_doc_id}
        → 索引键 "tool/param"（规范化）
    V2 旧格式（兼容回退）：
        {name, value, ...} → 裸参数名键
    user_values: {"tool/param" 或 "param": value}——用户明确提供过的参数值
    （不可靠来源不构造键；缺 provenance → not_grounded 放行）。
    """
    from agents.harness.base import norm_param_name
    ev: dict = {}
    for p in packets or []:
        for gv in p.get("grounded_values") or []:
            if not isinstance(gv, dict):
                continue
            entry = {
                "value": gv.get("value"),
                "value_type": gv.get("value_type"),
                "source_doc_id": gv.get("source_doc_id"),
                "unit": gv.get("unit"),
            }
            if gv.get("tool_name") and gv.get("parameter_name"):
                key = (f"{norm_param_name(gv['tool_name'])}/"
                       f"{norm_param_name(gv['parameter_name'])}")
                ev[key] = entry  # 同键最新 packet 覆盖
            elif gv.get("name"):
                ev[norm_param_name(gv["name"])] = entry
    uv: dict = {}
    for k, v in (user_values or {}).items():
        uv[norm_param_name(k)] = {"value": v}
    return HarnessContext(evidence_values=ev, user_context_values=uv)
