"""
Action Harness（V2）——Agent 与业务工具之间的确定性执行层。

定位（严格遵守）：
- Harness 不是 Agent；不调用 LLM；不重新规划任务
- 职责：接收 proposed tool call → 执行前确定性校验 → 通过则执行原工具 /
  失败则返回结构化错误让 Decision Agent 自己修正 → 全程记录 trace

架构：
    Decision Agent
          ↓ proposed tool call
    ActionHarness.process(action, context)
          ├─ validators（按序执行，全部通过才放行）
          │    ├── SchemaValidation        —— 复用工具自身 schema（enum/required/type）
          │    └── EvidenceParameterValidation —— 关键参数与 Evidence Packet 的
          │                                      grounded_values 比对（V2 核心）
          ├─ validate 通过 → execute（原样调用原工具）
          └─ validate 失败 → 结构化错误回传（validation_failed / parameter_mismatch）

扩展性：validator 是 policy 插槽。未来可加 Permission/Retry/Budget/Safety
Policy——当前只在 base 预留接口，一律不实现（NotImplementedError）。

False positive 防护（V2 设计核心约束）：
- rejection 必须分类：schema_violation / evidence_mismatch；
  evidence 缺失不拦（not_grounded → 放行）
- 参数来源区分：用户在本对话里明确提供的值（user context）不做
  KB 证据比对（not_applicable → 放行）——Harness 不假设所有参数
  都必须来自 Knowledge Agent
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 校验结果
# ---------------------------------------------------------------------------
class ValidationVerdict:
    """单条校验的判定（不抛异常——校验失败是正常业务流，不是 crash）。"""

    # 判定分类（第十一节的四类 + 放行类）
    MATCHED = "matched"            # 证据比对一致 / schema 合规 → 放行
    SCHEMA_VIOLATION = "schema_violation"  # 违反工具 schema（enum/required/type）→ 拦
    EVIDENCE_MISMATCH = "evidence_mismatch"  # 与 grounded_values 冲突 → 拦
    NOT_GROUNDED = "not_grounded"  # evidence 没有该参数 → 放行（不假设缺证据=错误）
    NOT_APPLICABLE = "not_applicable"      # 参数来自 user context → 放行（不要求 KB 证据）

    def __init__(self, field: str, verdict: str, detail: dict):
        self.field = field
        self.verdict = verdict
        self.detail = detail  # {proposed, expected/allowed, evidence_value, source_doc_id, ...}

    @property
    def is_blocking(self) -> bool:
        return self.verdict in (self.SCHEMA_VIOLATION, self.EVIDENCE_MISMATCH)


# ---------------------------------------------------------------------------
# Validator policy 接口
# ---------------------------------------------------------------------------
class ValidationPolicy(ABC):
    """校验策略插槽。Harness 按注册顺序逐个执行；任何一个 blocking 即拦截。"""

    name: str = "policy"

    @abstractmethod
    def validate_arguments(self, tool, arguments: dict, context: "HarnessContext"
                           ) -> list[ValidationVerdict]:
        """校验一组参数。返回逐字段 verdict 列表（空列表 = 全放行）。"""


# ---------------------------------------------------------------------------
# 未来 policy 预留（一律不实现——V2 只做 validation）
# ---------------------------------------------------------------------------
class PermissionPolicy(ValidationPolicy):
    """未来：工具权限（某 agent/状态下能否调用某工具）。当前不实现。"""

    name = "permission"

    def __init__(self):
        raise NotImplementedError(
            "PermissionPolicy 预留接口，V2 不实现（只做 parameter validation）。"
        )


class RetryPolicy(ValidationPolicy):
    """未来：失败重试策略。当前不实现。"""

    name = "retry"

    def __init__(self):
        raise NotImplementedError("RetryPolicy 预留接口，V2 不实现。")


class BudgetPolicy(ValidationPolicy):
    """未来：预算/成本控制。当前不实现。"""

    name = "budget"

    def __init__(self):
        raise NotImplementedError("BudgetPolicy 预留接口，V2 不实现。")


class SafetyPolicy(ValidationPolicy):
    """未来：高危动作二次确认。当前不实现。"""

    name = "safety"

    def __init__(self):
        raise NotImplementedError("SafetyPolicy 预留接口，V2 不实现。")


# ---------------------------------------------------------------------------
# Harness context：一次 proposed action 的校验上下文
# ---------------------------------------------------------------------------
class HarnessContext:
    """校验时需要的只读上下文。

    evidence_values: dict[str, dict] —— V2.1 新索引：以
        "tool_name/parameter_name"（规范化）为键 →
        {value, value_type, source_doc_id, unit}。兼容 V2 旧格式
        （裸参数名键）——匹配时先试精确键再退回裸名。
    user_context_values: dict[str, dict] —— 用户明确提供过的参数值：
        键 "tool_name/parameter_name"（规范化）→ {value}。
        这些参数不做 KB 证据比对（not_applicable）；不可靠的来源
        不猜（不构造键）——缺 provenance 保持 not_grounded 放行。
    """

    def __init__(self,
                 evidence_values: Optional[dict] = None,
                 user_context_values: Optional[dict] = None):
        self.evidence_values = evidence_values or {}
        self.user_context_values = user_context_values or {}

    def evidence_for(self, tool_name: str, param_name: str) -> Optional[dict]:
        """取某工具某参数的证据（V2.1 双键匹配：tool/param 精确 → 裸参数名回退）。"""
        key = f"{norm_param_name(tool_name)}/{norm_param_name(param_name)}"
        if key in self.evidence_values:
            return self.evidence_values[key]
        return self.evidence_values.get(norm_param_name(param_name))

    def user_value_for(self, tool_name: str, param_name: str) -> Optional[dict]:
        """取用户提供的参数值（同双键匹配）。"""
        key = f"{norm_param_name(tool_name)}/{norm_param_name(param_name)}"
        if key in self.user_context_values:
            return self.user_context_values[key]
        return self.user_context_values.get(norm_param_name(param_name))


def norm_param_name(name: str) -> str:
    """参数名规范化（用于跨命名风格匹配）：lowercase + 下划线。"""
    return (name or "").strip().lower().replace("-", "_").replace(" ", "_")
