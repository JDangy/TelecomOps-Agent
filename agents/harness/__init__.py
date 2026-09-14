"""Action Harness（V2.2）：三源确定性约束的执行层。

主路径：Schema（工具约束）+ TaskState（任务明确事实）+ KB Constraints
（明确业务规则）。Evidence 案例值比对降级为可选 policy（V2.1 历史入口保留）。
"""

from agents.harness.base import (
    HarnessContext,
    ValidationPolicy,
    ValidationVerdict,
    norm_param_name,
)
from agents.harness.resolver import ActionResolver, ResolvedAction
from agents.harness.task_state import (
    TaskState,
    ProvenanceEntry,
    SOURCE_USER,
    SOURCE_TOOL,
    SOURCE_KNOWLEDGE,
    UserValueExtractor,
    ToolResultExtractor,
)
from agents.harness.task_state_v3 import (
    TaskStateV3,
    StateEntry,
    UserStateExtractor,
    ToolResultStateExtractor,
    KnowledgeStateExtractor,
)
from agents.harness.plan_store import PlanStore, PlanStep
from agents.harness.task_state_validator import TaskStateValidator
from agents.harness.kb_validator import KnowledgeConstraintValidator
from agents.harness.action_harness import (
    ActionHarness,
    build_v21_harness,
    constraints_from_packets,
    context_from_packets,
)
from agents.harness.validators import (
    EvidenceParameterValidation,
    SchemaValidation,
)
# V6.1（架构收紧）: TaskState 唯一事实源之上的三个只读/派生模块。
# - EvidenceView: TaskStateV3 → 分层证据视图（零存储,现算）
# - Worklist:    PlanStore 步骤的条目级展开（挂在 step 下,单一来源）
# - DecisionCheckpoint: 关键动作前的 6 问检查点（轻量,无额外 LLM 调用）
from agents.harness.evidence_view import EvidenceView
from agents.harness.worklist import Worklist, WorkItem
from agents.harness.decision_checkpoint import (
    DecisionCheckpoint,
    CheckpointArtifact,
    should_trigger_checkpoint,
)

__all__ = [
    "ActionHarness",
    "build_v21_harness",
    "constraints_from_packets",
    "context_from_packets",
    "ActionResolver",
    "ResolvedAction",
    "TaskState",
    "ProvenanceEntry",
    "SOURCE_USER",
    "SOURCE_TOOL",
    "SOURCE_KNOWLEDGE",
    "UserValueExtractor",
    "ToolResultExtractor",
    "PlanStore",
    "PlanStep",
    "TaskStateValidator",
    "KnowledgeConstraintValidator",
    "SchemaValidation",
    "EvidenceParameterValidation",
    "HarnessContext",
    "ValidationPolicy",
    "ValidationVerdict",
    "norm_param_name",
    # V6.1（架构收紧）
    "EvidenceView",
    "Worklist",
    "WorkItem",
    "DecisionCheckpoint",
    "CheckpointArtifact",
    "should_trigger_checkpoint",
]
