"""Action Harness（V2）：Agent 与业务工具之间的确定性校验/执行层。

只实现 parameter validation（schema + evidence-grounded）。
Permission/Retry/Budget/Safety 为预留接口（base.py，未实现）。
"""

from agents.harness.base import (
    HarnessContext,
    ValidationPolicy,
    ValidationVerdict,
    norm_param_name,
)
from agents.harness.resolver import ActionResolver, ResolvedAction
from agents.harness.action_harness import ActionHarness, context_from_packets
from agents.harness.validators import (
    EvidenceParameterValidation,
    SchemaValidation,
)

__all__ = [
    "ActionHarness",
    "ActionResolver",
    "ResolvedAction",
    "context_from_packets",
    "HarnessContext",
    "ValidationPolicy",
    "ValidationVerdict",
    "SchemaValidation",
    "EvidenceParameterValidation",
    "norm_param_name",
]
