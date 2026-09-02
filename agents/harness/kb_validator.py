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
    """KB 明确约束校验（enum 集合 / 阈值 / 格式——不决定案例值选择）。

    V3 统一：约束唯一事实源是 TaskStateV3 的 knowledge 命名空间条目
    （object="tool名"，field="param.allowed_values/.max/.min/.format"）。
    旧 constraints 列表仅作后备（V2.2 兼容，正常路径不再单独维护）。
    """

    name = "knowledge_constraints"

    def __init__(self, constraints: list = None, task_state=None):
        """task_state: TaskStateV3（V3 主路径——统一事实源）。
        constraints: 旧列表（V2.2 兼容后备，wire() 不传时才用）。
        """
        self.constraints = constraints or []
        self.task_state = task_state

    def _find(self, tool: str, param: str):
        """找该 tool/param 的约束（TaskState knowledge 条目 → 旧列表后备）。"""
        tkey = norm_param_name(tool)
        pkey = norm_param_name(param)
        # V3 主路径：TaskStateV3 knowledge 命名空间
        if self.task_state is not None:
            av = self._state_entry(tkey, f"{pkey}.allowed_values")
            if av is not None:
                return {"constraint_type": "enum",
                        "allowed_values": av,
                        "source_doc_id": self._state_ref(tkey, f"{pkey}.allowed_values")}
            mx = self._state_entry(tkey, f"{pkey}.max")
            mn = self._state_entry(tkey, f"{pkey}.min")
            if mx is not None or mn is not None:
                return {"constraint_type": "threshold", "max": mx, "min": mn,
                        "source_doc_id": self._state_ref(tkey, f"{pkey}.max")}
            fmt = self._state_entry(tkey, f"{pkey}.format")
            if fmt is not None:
                return {"constraint_type": "format", "format": fmt,
                        "source_doc_id": self._state_ref(tkey, f"{pkey}.format")}
            return None  # 状态里没有 → 无约束（不再看旧列表——单一事实源）
        # V2.2 兼容后备（未接状态时）
        fallback = None
        for c in self.constraints:
            if not isinstance(c, dict):
                continue
            ct = norm_param_name(c.get("tool_name") or "")
            cp = norm_param_name(c.get("parameter_name") or "")
            if ct == tkey and cp == pkey:
                return c
            if cp == pkey and not ct:
                fallback = c
        return fallback

    # -- TaskStateV3 knowledge 条目读取 --------------------------------
    def _state_entry(self, obj: str, field: str):
        try:
            entries = getattr(self.task_state, "_entries", {})
            chain = entries.get(f"{obj}.{field}")
            if chain and chain[-1].is_current and chain[-1].source == "knowledge":
                return chain[-1].value
        except Exception:
            pass
        return None

    def _state_ref(self, obj: str, field: str) -> Optional[str]:
        try:
            entries = getattr(self.task_state, "_entries", {})
            chain = entries.get(f"{obj}.{field}")
            if chain and chain[-1].is_current:
                return chain[-1].source_ref
        except Exception:
            pass
        return None

    def validate_action(self, action: ResolvedAction,
                        context: HarnessContext) -> list:
        verdicts = []
        # 参数类型表（inner schema 的 properties.type——resolver 从方法
        # signature 推导）——threshold/enum 约束不得跨类型施用
        props = (action.inner_schema or {}).get("properties", {}) or {}
        for f, proposed in action.arguments.items():
            c = self._find(action.tool_name, f)
            if c is None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict="no_kb_constraint", detail={},
                ))
                continue
            ctype = (c.get("constraint_type") or "").lower()
            param_type = (props.get(f) or {}).get("type")

            # 类型安全：threshold 只对数值参数有意义——参数类型是布尔
            # （signature 推导），或 proposed 是 bool → 约束不可用放行。
            # （KA 曾把"最低消费 $25"的数值阈值挂到布尔参数上：float(True)
            # =1.0 < 25 → 误拦。签名推导的类型是可靠防线。）
            if ctype == "threshold" and (param_type == "boolean"
                                         or isinstance(proposed, bool)):
                verdicts.append(ValidationVerdict(
                    field=f, verdict="no_kb_constraint",
                    detail={"reason": "threshold_on_boolean_param"},
                ))
                continue

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
                # 格式约束只在占位符模板时可用（MM/DD/YYYY、YYYY-MM-DD…）。
                # KA 可能输出描述句当 format（"string (account-level...)"）——
                # 无占位符 token → 不是模板 → no_kb_constraint 放行（不猜）。
                if not fmt or not re.search(r"MM|DD|YYYY|HH|mm", fmt):
                    verdicts.append(ValidationVerdict(
                        field=f, verdict="no_kb_constraint",
                        detail={"reason": "format_not_placeholder_template"},
                    ))
                    continue
                if not self._matches_format(proposed, fmt):
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
