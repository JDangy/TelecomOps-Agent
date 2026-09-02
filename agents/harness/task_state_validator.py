"""Task-State Validator（V2.2 核心新增）。

检查 proposed 动作是否与"当前任务中已明确确认的值"冲突：
- 用户显式说的金额（amount=500）vs Agent 填 550 → 拦截
- 前一工具返回的引用 ID（card_id=X）vs Agent 用错 ID → 拦截

边界（拦截原则：有明确依据才拦）：
- 值完全等价（含数值/布尔宽松等价）→ matched 放行
- TaskState 无该参数 → not_in_task_state 放行（记录）
- 同参数存在 user 与 tool 冲突条目 → 以最新为准判定；被覆盖的旧值
  不再拦（用户改口/工具更新状态是正常流）
- 语义选择（两个合法枚举、无明确事实指向）→ 与 TaskState 无关 → 放行
"""

from __future__ import annotations

from typing import Any

from agents.harness.base import HarnessContext, ValidationVerdict
from agents.harness.resolver import ResolvedAction
from agents.harness.task_state import TaskState


def _equivalent(a: Any, b: Any) -> bool:
    """宽松等价（与 evidence validator 同口径）：数值/布尔/字符串大小写。"""
    if a == b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        def to_bool(x):
            if isinstance(x, bool):
                return x
            if isinstance(x, str) and x.strip().lower() in ("true", "false"):
                return x.strip().lower() == "true"
            return None
        ba, bb = to_bool(a), to_bool(b)
        if ba is not None and bb is not None:
            return ba == bb
    try:
        if float(a) == float(b):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return False


class TaskStateValidator:
    """与 TaskState（参数级 provenance registry）比对的校验器。"""

    name = "task_state"

    def __init__(self, task_state: TaskState):
        self.task_state = task_state

    def validate_action(self, action: ResolvedAction,
                        context: HarnessContext) -> list:
        verdicts = []
        for f, proposed in action.arguments.items():
            # V3: 传入 proposed 值——支持实体解析（ID 错引检测）与
            # 多对象歧义保护（同名 amount 多对象 → 不命中 → 放行）
            entry = self.task_state.latest(action.tool_name, f,
                                            proposed_value=proposed)
            if entry is None:
                verdicts.append(ValidationVerdict(
                    field=f, verdict="not_in_task_state", detail={},
                ))
                continue
            if _equivalent(proposed, entry.value):
                verdicts.append(ValidationVerdict(
                    field=f, verdict=ValidationVerdict.MATCHED,
                    detail={"task_state_value": entry.value,
                            "value_source": entry.source,
                            "source_ref": entry.source_ref},
                ))
            else:
                verdicts.append(ValidationVerdict(
                    field=f, verdict="task_state_conflict",
                    detail={
                        "tool": action.tool_name,
                        "proposed_value": proposed,
                        "confirmed_value": entry.value,
                        "value_source": entry.source,
                        "source_ref": entry.source_ref,
                        "confirmed_key": getattr(entry, "key", f),
                        "seq": getattr(entry, "seq", 0),
                    },
                ))
        return verdicts

    def is_blocking(self, verdict: ValidationVerdict) -> bool:
        return verdict.verdict == "task_state_conflict"
