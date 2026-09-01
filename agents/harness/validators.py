"""V2 启用的两类确定性校验。

1. SchemaValidation —— 复用 tau2 工具自身 openai_schema（enum/required/type/
   numeric bounds），不另造 schema system；执行前明确检查并记录。
2. EvidenceParameterValidation —— 关键参数与 Evidence Packet 的
   grounded_values 比对（V2 核心）：proposed 值与证据值不一致 → 拦截
   并回传 evidence_value + source_doc_id。
   参数来源区分：user context 提供的值 not_applicable 放行；
   evidence 缺该参数 not_grounded 放行（不假设缺证据=错误）。
"""

from __future__ import annotations

import re
from typing import Any

from agents.harness.base import HarnessContext, ValidationVerdict


class SchemaValidation:
    """基于工具自身 openai_schema 的执行前校验。"""

    name = "schema"

    def validate_arguments(self, tool, arguments: dict,
                           context: HarnessContext) -> list[ValidationVerdict]:
        verdicts = []
        schema = tool.openai_schema.get("function", {}) if tool else {}
        params_schema = schema.get("parameters", {}) or {}
        props = params_schema.get("properties", {}) or {}
        required = set(params_schema.get("required", []) or [])

        # required 字段缺失
        for field in required:
            if field not in arguments or arguments[field] in (None, ""):
                verdicts.append(ValidationVerdict(
                    field=field,
                    verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "missing_required", "proposed": arguments.get(field)},
                ))

        for field, value in arguments.items():
            fs = props.get(field) or {}
            # enum 校验（精确匹配——枚举值不改写）
            enum_vals = fs.get("enum")
            if isinstance(enum_vals, list) and enum_vals:
                if value not in enum_vals:
                    verdicts.append(ValidationVerdict(
                        field=field,
                        verdict=ValidationVerdict.SCHEMA_VIOLATION,
                        detail={
                            "error": "enum_violation",
                            "proposed": value,
                            "allowed_values": enum_vals,
                        },
                    ))
                    continue
            # 类型校验（轻量：只在 schema 明确声明 type 时查）
            t = fs.get("type")
            if t == "string" and not isinstance(value, str) and value is not None:
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "type_mismatch", "expected": "string",
                            "proposed": value, "actual_type": type(value).__name__},
                ))
            elif t == "number" and isinstance(value, (int, float, bool)) is False and value is not None:
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "type_mismatch", "expected": "number",
                            "proposed": value, "actual_type": type(value).__name__},
                ))
            elif t == "integer" and (isinstance(value, bool) or not isinstance(value, int)) and value is not None:
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.SCHEMA_VIOLATION,
                    detail={"error": "type_mismatch", "expected": "integer",
                            "proposed": value, "actual_type": type(value).__name__},
                ))
        return verdicts


class EvidenceParameterValidation:
    """证据接地参数校验（V2 核心）。

    比对逻辑（确定性，无 LLM）：
    1. 参数值来源优先级：user context 明确提供 → not_applicable 放行
       （用户说的值是权威，如"转 $500"）
    2. evidence_values 有该参数（名称规范化匹配 + 值类型宽松等价比较）
       → 与 proposed 比较：
           一致 → matched
           不一致 → evidence_mismatch 拦截（回传 evidence_value + source_doc_id）
    3. evidence 没有该参数 → not_grounded 放行（不假设缺证据=错误）
    """

    name = "evidence"

    # 需要比对的关键参数名模式（金额/枚举/类型/阈值/条件——通用模式，非 benchmark 硬编码）
    KEY_PARAM_PATTERNS = re.compile(
        r"(reason|type|kind|code|amount|fee|rate|percentage|threshold|"
        r"limit|category|method|option|plan|status|action|class)",
        re.IGNORECASE,
    )

    def validate_arguments(self, tool, arguments: dict,
                           context: HarnessContext) -> list[ValidationVerdict]:
        verdicts = []
        for field, proposed in arguments.items():
            # 1) user context 提供的值：不做 KB 比对
            if field in context.user_context_values:
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.NOT_APPLICABLE,
                    detail={"source": "user_context"},
                ))
                continue
            # 2) evidence 有该参数？
            from agents.harness.base import norm_param_name
            key = norm_param_name(field)
            ev = None
            for gv_name, gv in context.evidence_values.items():
                if norm_param_name(gv_name) == key:
                    ev = gv
                    break
            if ev is None:
                # 3) 缺证据 → 放行（不拦）
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.NOT_GROUNDED,
                    detail={},
                ))
                continue
            ev_value = ev.get("value")
            if self._values_equivalent(proposed, ev_value, ev.get("value_type")):
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.MATCHED,
                    detail={"evidence_value": ev_value,
                            "source_doc_id": ev.get("source_doc_id")},
                ))
            else:
                verdicts.append(ValidationVerdict(
                    field=field, verdict=ValidationVerdict.EVIDENCE_MISMATCH,
                    detail={
                        "proposed_value": proposed,
                        "evidence_value": ev_value,
                        "value_type": ev.get("value_type"),
                        "source_doc_id": ev.get("source_doc_id"),
                    },
                ))
        return verdicts

    @staticmethod
    def _values_equivalent(proposed: Any, evidence: Any, value_type: str = None) -> bool:
        """宽松等价：字符串/数值/枚举可比（"500" vs 500、大小写不敏感字符串）。"""
        if proposed == evidence:
            return True
        # 数值等价
        try:
            if float(proposed) == float(evidence):
                return True
        except (TypeError, ValueError):
            pass
        # 字符串等价（大小写/空白不敏感——但保留内容精确性）
        if isinstance(proposed, str) and isinstance(evidence, str):
            return proposed.strip().lower() == evidence.strip().lower()
        return False
