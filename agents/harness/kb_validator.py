"""Knowledge Constraint Validator（V2.2）。

使用原则（对照第 3 节——与 V2.1 的关键区别）：
KB 只提供**明确约束**，不提供"当前案例的正确值"：
- 合法 enum 集合（值是否在集合内——不决定该选哪个）
- 明确阈值（amount 上限/下限：KB 写 "maximum $5,000" → 越界拦）
- 明确格式（MM/DD/YYYY 等——格式违规拦）

与 evidence validation 的分工：
EvidenceParameterValidation（V2/V2.1，降级为可选 policy）尝试比对
"案例正确值"——V2.1 证明该前提对 case-specific 参数不成立，默认关闭。
本 validator 只用 KB 确定性约束，不做案例值猜测。

KB 约束的载体：Evidence Packet 的新字段 `constraints`（KA 只被要求输出
明确约束，不再输出"正确值"）：
    {"constraints": [
        {"tool_name": "...", "parameter_name": "...",
         "constraint_type": "enum|threshold|format",
         "allowed_values": [...],        # enum
         "min": 0, "max": 5000, "unit": "USD",   # threshold
         "format": "MM/DD/YYYY"}          # format
    ]}
无法确定为明确约束的信息留在 facts（不猜）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agents.harness.base import HarnessContext, ValidationVerdict, norm_param_name
from agents.harness.resolver import ResolvedAction


class KnowledgeConstraintValidator:
    """KB 明确约束校验（enum 集合 / 阈值 / 格式——不决定案例值选择）。"""

    name = "knowledge_constraints"

    def __init__(self, constraints: list):
        """constraints: packets 中收集的 KB 约束条目列表。"""
        self.constraints = constraints or []

    def _find(self, tool: str, param: str):
        """找该 tool/param 的约束（精确 → 裸参数名回退）。"""
        tkey = norm_param_name(tool)
        pkey = norm_param_name(param)
        fallback = None
        for c in self.constraints:
            if not isinstance(c, dict):
                continue
            ct = norm_param_name(c.get("tool_name") or "")
            cp = norm_param_name(c.get("parameter_name") or "")
            if ct == tkey and cp == pkey:
                return c  # 精确匹配优先
            if cp == pkey and not ct:
                fallback = c  # 无工具归属的裸约束兜底
        return fallback

    def validate_action(self, action: ResolvedAction,
                        context: HarnessContext) -> list:
        verdicts = []
        for f, proposed in action.arguments.items():
            c = self._find(action.tool_name, f)
            if c is None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict="no_kb_constraint", detail={},
                ))
                continue
            ctype = (c.get("constraint_type") or "").lower()

            if ctype == "enum":
                allowed = c.get("allowed_values") or []
                # 约束自身健康：allowed 为空/含空格词（说明句混入）→ 不可用
                if (not allowed or any(isinstance(v, str) and " " in v.strip()
                                       for v in allowed)):
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="no_kb_constraint", detail={},
                    ))
                    continue
                if proposed in allowed or self._soft_eq_any(proposed, allowed):
                    verdicts.append(ValidationVerdict(
                        field=f, verdict=ValidationVerdict.MATCHED,
                        detail={"kb_allowed_values": allowed,
                                "source_doc_id": c.get("source_doc_id")},
                    ))
                else:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="kb_enum_violation",
                        detail={"tool": action.tool_name,
                                "proposed": proposed,
                                "kb_allowed_values": allowed,
                                "source_doc_id": c.get("source_doc_id")},
                    ))
                continue

            if ctype == "threshold":
                mn, mx = c.get("min"), c.get("max")
                # 类型安全：threshold 只对数值参数有意义——proposed 是布尔
                # 或 min/max 不是数字（如 doc 的 "true or false" 混入）时，
                # 该约束不可用 → 放行（不猜）
                try:
                    v = float(proposed)
                except (TypeError, ValueError):
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="no_kb_constraint", detail={},
                    ))
                    continue
                def _num(x):
                    try:
                        return float(x)
                    except (TypeError, ValueError):
                        return None
                mn_v, mx_v = _num(mn), _num(mx)
                if mn_v is None and mx_v is None:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="no_kb_constraint", detail={},
                    ))
                    continue
                if mn_v is not None and v < mn_v:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="kb_threshold_violation",
                        detail={"proposed": proposed, "kb_min": mn,
                                "unit": c.get("unit"),
                                "source_doc_id": c.get("source_doc_id")},
                    ))
                elif mx_v is not None and v > mx_v:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="kb_threshold_violation",
                        detail={"proposed": proposed, "kb_max": mx,
                                "unit": c.get("unit"),
                                "source_doc_id": c.get("source_doc_id")},
                    ))
                else:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict=ValidationVerdict.MATCHED,
                        detail={"kb_range": [mn, mx]},
                    ))
                continue

            if ctype == "format":
                fmt = c.get("format") or ""
                if fmt and not self._matches_format(proposed, fmt):
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="kb_format_violation",
                        detail={"proposed": proposed, "kb_format": fmt,
                                "source_doc_id": c.get("source_doc_id")},
                    ))
                else:
                    verdicts.append(ValidationVerdict(
                        field=f, verdict=ValidationVerdict.MATCHED, detail={},
                    ))
                continue

            # 未知约束类型 → 视为无约束（记录不拦）
            verdicts.append(ValidationVerdict(
                field=f, verdict="no_kb_constraint", detail={},
            ))
        return verdicts

    @staticmethod
    def _soft_eq_any(proposed: Any, allowed: list) -> bool:
        for v in allowed:
            if isinstance(proposed, str) and isinstance(v, str):
                if proposed.strip().lower() == v.strip().lower():
                    return True
                continue
            try:
                if float(proposed) == float(v):
                    return True
            except (TypeError, ValueError):
                pass
        return False

    @staticmethod
    def _matches_format(value, fmt: str) -> bool:
        """格式检查（保守）：MM/DD/YYYY、YYYY-MM-DD、HH:MM 等占位符模板。"""
        if not isinstance(value, str):
            value = str(value)
        pattern = (fmt.replace("MM", r"\d{2}").replace("DD", r"\d{2}")
                   .replace("YYYY", r"\d{4}").replace("HH", r"\d{2}")
                   .replace("mm", r"\d{2}")
                   .replace("/", r"\/").replace(".", r"\."))
        try:
            return bool(re.fullmatch(pattern, value))
        except re.error:
            return True  # 模板异常 → 无法判定 → 不拦
