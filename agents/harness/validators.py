"""V2/V2.1 确定性校验——针对 ResolvedAction 的 inner 业务参数。

V2.1 关键变化：
- 两类校验器都基于 resolver 输出的 ResolvedAction（wrapper 已穿透）
- SchemaValidation：inner schema 的 required/type + docstring enum
  （enum_from_doc——提取不到不猜，为空时跳过该项）
- EvidenceParameterValidation：以 "tool_name/parameter_name" 精确匹配
  evidence grounded_values（V2.1 新格式），回退裸参数名（V2 兼容）
- user provenance：user_context_values 命中 → not_applicable 放行
- 缺证据 → not_grounded 放行（不假设缺证据=错误——V2.1 保持）
"""

from __future__ import annotations

import re
from typing import Any

from agents.harness.base import HarnessContext, ValidationVerdict
from agents.harness.resolver import ResolvedAction


def _validate_against(action: ResolvedAction, context: HarnessContext,
                       schema_validator, evidence_validator) -> list:
    """在 ResolvedAction 上运行校验器（resolver/harness 内部使用）。"""
    out = []
    out.extend(schema_validator.validate_action(action, context))
    out.extend(evidence_validator.validate_action(action, context))
    return out


class SchemaValidation:
    """基于 inner schema（required/type）+ docstring enum 的执行前校验。"""

    name = "schema"

    # 兼容 V2 接口（tool/arguments 版本——由 harness 构造 ResolvedAction 后不再走）
    def validate_arguments(self, tool, arguments: dict,
                           context: HarnessContext) -> list:
        schema = tool.openai_schema.get("function", {}).get("parameters", {}) if tool else {}
        return self._check(schema or {}, arguments, {})

    # V2.1 主入口
    def validate_action(self, action: ResolvedAction,
                        context: HarnessContext) -> list:
        schema = action.inner_schema or {}
        return self._check(schema, action.arguments, action.tool_name)

    def _check(self, schema: dict, arguments: dict, tool_name: str) -> list:
        verdicts = []
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        enums = schema.get("enum_from_doc", {}) or {}
        tool_label = f"{tool_name}: " if tool_name else ""

        # required 缺失
        for f in required:
            if f not in arguments or arguments[f] in (None, ""):
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "missing_required", "tool": tool_label.strip(": "),
                            "proposed": arguments.get(f)},
                ))
        # enum（docstring 提取的合法值——"Must be one of: 'x' | 'y'"）
        for f, allowed in enums.items():
            if f in arguments and arguments[f] not in (None, ""):
                if arguments[f] not in allowed:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                        detail={"error": "enum_violation", "tool": tool_label.strip(": "),
                                "proposed": arguments[f], "allowed_values": allowed},
                    ))
        # 类型
        for f, value in arguments.items():
            fs = props.get(f) or {}
            t = fs.get("type")
            if t == "string" and not isinstance(value, str) and value is not None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "type_mismatch", "expected": "string",
                            "proposed": value, "actual_type": type(value).__name__},
                ))
            elif t == "number" and (isinstance(value, (str, bool)) or value is None):
                if isinstance(value, str):
                    try:
                        float(value)
                        continue  # 数字字符串容忍（宽松等价在 evidence 层处理）
                    except ValueError:
                        pass
                if value is not None:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                        detail={"error": "type_mismatch", "expected": "number",
                                "proposed": value, "actual_type": type(value).__name__},
                    ))
        return verdicts


class EvidenceParameterValidation:
    """证据接地参数校验（V2.1：tool/param 精确匹配 inner 参数）。

    比对逻辑（确定性，无 LLM）：
    1. user context 提供该工具该参数 → not_applicable 放行
    2. evidence 有（tool/param 精确或裸名回退）→ 宽松等价比较：
       一致 matched / 不一致 evidence_mismatch 拦截（回传 evidence 值+doc）
    3. 都没有 → not_grounded 放行
    """

    name = "evidence"

    # 参数名模式（通用关键参数——非 benchmark 硬编码）
    KEY_PARAM_PATTERNS = re.compile(
        r"(reason|type|kind|code|amount|fee|rate|percentage|threshold|"
        r"limit|category|method|option|plan|status|action|class)",
        re.IGNORECASE,
    )

    def validate_arguments(self, tool, arguments: dict,
                           context: HarnessContext) -> list:
        tool_name = getattr(tool, "name", "") if tool else ""
        return self._check(tool_name, arguments, context)

    def validate_action(self, action: ResolvedAction,
                        context: HarnessContext) -> list:
        return self._check(action.tool_name, action.arguments, context)

    def _check(self, tool_name: str, arguments: dict,
               context: HarnessContext) -> list:
        verdicts = []
        for f, proposed in arguments.items():
            # 1) user context
            uv = context.user_value_for(tool_name, f)
            if uv is not None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.NOT_APPLICABLE,
                    detail={"source": "user_context"},
                ))
                continue
            # 2) evidence
            ev = context.evidence_for(tool_name, f)
            if ev is None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.NOT_GROUNDED, detail={},
                ))
                continue
            ev_value = ev.get("value")
            if self._values_equivalent(proposed, ev_value, ev.get("value_type")):
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.MATCHED,
                    detail={"evidence_value": ev_value,
                            "source_doc_id": ev.get("source_doc_id")},
                ))
            else:
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.EVIDENCE_MISMATCH,
                    detail={
                        "tool": tool_name,
                        "proposed_value": proposed,
                        "evidence_value": ev_value,
                        "value_type": ev.get("value_type"),
                        "source_doc_id": ev.get("source_doc_id"),
                    },
                ))
        return verdicts

    @staticmethod
    def _values_equivalent(proposed: Any, evidence: Any, value_type: str = None) -> bool:
        """宽松等价：精确相同 / 数值等价（"500"=500）/ 字符串大小写空白不敏感。"""
        if proposed == evidence:
            return True
        try:
            if float(proposed) == float(evidence):
                return True
        except (TypeError, ValueError):
            pass
        if isinstance(proposed, str) and isinstance(evidence, str):
            return proposed.strip().lower() == evidence.strip().lower()
        return False
