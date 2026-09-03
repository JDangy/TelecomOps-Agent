"""Structured Plan Store（V5）—— Decision Agent 的 runtime 计划状态。

核心设计（对照第 3/6/13/14 节）：

Plan 不是文本协议（V4 的 [PLAN] 行遵守率 0 已证），而是 **runtime
state + 结构化 tool call**：
    DA 调用 update_plan(...) → 系统确定性保存 → 下轮 context 注入

三个 planning tool（非银行业务工具——只改本 Agent runtime）：
    write_plan(goal, steps)      初始/整体重写（planning 时刻）
    update_plan(op, ...)        步骤级增量更新（不改全计划）
        op = set_current / mark_step / add_step / remove_step /
             block_step / unblock_step
    read_plan()                 当前计划查看（DA 主动检查）

步骤语义（第 6 节）——业务层非微操作：
    description  "resolve remaining balance on account_A"（业务语义）
    tool_hint    绑定的业务动作（inner tool 名，可选）
    entities     目标实体列表（account_A / card_B…——来自 Task State
                 的对象命名，多对象区分的关键）
    status 状态机（第 13 节，朴素无 DAG）:
        pending → in_progress → completed
        pending → in_progress → failed →（replan 后）pending
        pending → blocked（外部阻塞，等待解除）
        任意 → removed（用户改目标/不再需要）

与 Task State 的分工（第 14 节，严格）：
    Task State = 世界现在是什么（事实，唯一事实源）
    Plan       = 还准备做什么（意图——只引用实体/事实，不复制）
    PlanStore 不存 balance/status 等事实字段；步骤实体引用 Task State
    的对象名（如 account_sav_lm83），执行时由 Task State 提供事实值。

完成推进的**绑定**规则（第 9 节——多对象同工具不误完成）：
    step.completed 需要 (tool_hint 命中) AND (该调用属于 step.entities)
    "属于"判定（确定性，两层）：
      1. step.entities 为空 → 该 tool_hint 的唯一 in_progress 步骤推进
         （单对象场景——无歧义才推进）
      2. step.entities 非空 → 调用参数中引用了其中实体（参数值命中
         Task State 中该实体的 ID——通过 EntityMatcher 确认）才推进
    两层都不确定 → 保持 pending（宁可不推进，不猜——有明确依据才动）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


# 步骤状态
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"
REMOVED = "removed"

ACTIVE_STATUSES = (PENDING, IN_PROGRESS, BLOCKED, FAILED)  # removed 除外


@dataclass
class PlanStep:
    step_id: int
    description: str                      # 业务语义（未来动作）
    tool_hint: Optional[str] = None        # 绑定的 inner 业务工具
    entities: list = field(default_factory=list)  # 目标实体（Task State 对象名或 ID）
    status: str = PENDING
    note: Optional[str] = None             # blocked/failed 原因

    def to_dict(self) -> dict:
        return {"id": self.step_id, "description": self.description,
                "status": self.status, "tool_hint": self.tool_hint,
                "entities": self.entities, "note": self.note}


class PlanStore:
    """DecisionAgent 的 runtime 计划（确定性，无 LLM）。

    trace 事件由调用方（two_agent）在每次变更后 emit。
    """

    def __init__(self):
        self.goal: Optional[str] = None
        self.steps: list[PlanStep] = []
        self._next_id = 1
        self._current_step_id: Optional[int] = None
        # guard 有界（第 12 节）
        self.guard_reminders = 0
        self.GUARD_LIMIT = 2

    # ------------------------------------------------------------------
    # planning tool 的后端（DA 调用 → 这里确定性执行）
    # ------------------------------------------------------------------
    def write_plan(self, goal: str, steps: list) -> dict:
        """整体写入/重写计划（初始规划或 replan）。

        steps: [{"description", "tool_hint"?, "entities"?}, ...]
        语义：**替换**当前计划（replan 的实现载体——新计划取代旧计划，
        旧步骤保留进 trace 便于回看，不留在 runtime state）。
        """
        self.goal = (goal or "").strip()[:300]
        self.steps = []
        self._current_step_id = None
        for s in steps or []:
            if not isinstance(s, dict) or not s.get("description"):
                continue
            st = PlanStep(
                step_id=self._next_id,
                description=str(s["description"])[:200],
                tool_hint=s.get("tool_hint"),
                entities=[str(e) for e in (s.get("entities") or [])][:4],
            )
            self.steps.append(st)
            self._next_id += 1
        return {"ok": True, "goal": self.goal, "n_steps": len(self.steps)}

    def update_plan(self, op: str, step_id: int = None,
                    description: str = None, entities: list = None,
                    note: str = None, tool_hint: str = None) -> dict:
        """步骤级增量更新（不重写全计划——第 8 节条件 replanning）。"""
        if op == "set_current":
            st = self._find(step_id)
            if st is None:
                return {"ok": False, "error": f"unknown step {step_id}"}
            if st.status == PENDING:
                st.status = IN_PROGRESS
            old = self._current_step_id
            self._current_step_id = st.step_id
            return {"ok": True, "changed": old != st.step_id,
                    "current": st.step_id}
        if op == "add_step":
            st = PlanStep(step_id=self._next_id,
                          description=str(description or "")[:200],
                          tool_hint=tool_hint,
                          entities=[str(e) for e in (entities or [])][:4])
            self.steps.append(st)
            self._next_id += 1
            return {"ok": True, "added": st.step_id}
        if op == "remove_step":
            st = self._find(step_id)
            if st is None:
                return {"ok": False, "error": "unknown step"}
            st.status = REMOVED
            if self._current_step_id == st.step_id:
                self._current_step_id = None
            return {"ok": True, "removed": st.step_id}
        if op == "block_step":
            st = self._find(step_id)
            if st is None:
                return {"ok": False, "error": "unknown step"}
            st.status = BLOCKED
            st.note = (note or "")[:200] or None
            return {"ok": True, "blocked": st.step_id}
        if op == "unblock_step":
            st = self._find(step_id)
            if st is None:
                return {"ok": False, "error": "unknown step"}
            if st.status == BLOCKED:
                st.status = PENDING
                st.note = None
                return {"ok": True, "unblocked": st.step_id}
            return {"ok": True, "unblocked": None, "note": "not blocked"}
        return {"ok": False, "error": f"unknown op {op}"}

    # ------------------------------------------------------------------
    # 执行证据推进（第 9 节——绑定 动作+实体）
    # ------------------------------------------------------------------
    def on_tool_result(self, inner_tool: str, ok: bool,
                       arguments: dict, task_state=None) -> Optional[PlanStep]:
        """一次内层业务工具执行结束 → 推进匹配的步骤。

        匹配两层（确定性，无猜）：
        1. tool_hint 命中的 in_progress/completed-able 步骤集合 M
        2. M 中只有一个：entities 为空（单对象无歧义）或
           调用参数值命中其 entities 之一（实体绑定）→ 推进该步骤
        多候选歧义 / 无候选 → None（保持现状）

        completed 推进仅在步骤是 current 或 in_progress 时生效；
        ok=False → failed（附 note）。
        """
        if not self.steps:
            return None
        matches = [s for s in self.steps
                   if s.tool_hint == inner_tool
                   and s.status in (IN_PROGRESS, PENDING)]
        if not matches:
            return None
        # 实体绑定过滤
        candidates = []
        for s in matches:
            if not s.entities:
                candidates.append((s, None))  # 无实体声明——单对象语义
            else:
                hit = self._args_reference_entity(arguments, s.entities, task_state)
                if hit:
                    candidates.append((s, hit))
        # 唯一候选（实体绑定确定）→ 推进（in_progress 优先于 pending——
        # 实体绑定的匹配步骤通常正是正在做的那个；binding 唯一性消歧）
        if len(candidates) == 1:
            st = candidates[0][0]
            if st.status == IN_PROGRESS or st.status == PENDING:
                if ok:
                    st.status = COMPLETED
                    st.note = None
                else:
                    st.status = FAILED
                    st.note = f"tool failed: {inner_tool}"
                return st
        return None

    @staticmethod
    def _args_reference_entity(arguments: dict, entities: list,
                               task_state) -> Optional[str]:
        """调用参数是否引用了 step.entities 中的实体。

        判定（确定性）：任一参数值（str 匹配）等于实体名/实体 ID——
        或在 Task State 中该实体对象的 id 字段值与参数值相等
        （account_sav_x 对象 .id = sav_x；调用 account_id="sav_x" 命中）。
        """
        vals = [str(v) for v in (arguments or {}).values()
                if v is not None and not isinstance(v, (dict, list))]
        for e in entities:
            e = str(e)
            for v in vals:
                if v == e or (e in v) or (v in e and len(v) >= 4):
                    return e
        # Task State 实体 ID 二次确认
        if task_state is not None:
            try:
                for key, chain in getattr(task_state, "_entries", {}).items():
                    cur = chain[-1]
                    if not cur or not cur.is_current:
                        continue
                    if cur.field in ("id", "account_id", "card_id", "user_id"):
                        for e in entities:
                            if cur.object.endswith(e) or e in cur.object:
                                if str(cur.value) in vals:
                                    return e
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # 读取 / 渲染
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self.steps) and self.goal is not None

    def current_step(self) -> Optional[PlanStep]:
        if self._current_step_id is None:
            return None
        return next((s for s in self.steps if s.step_id == self._current_id()
                     and s.status not in (REMOVED,)), None)

    def _current_id(self):
        return self._current_step_id

    def _find(self, step_id) -> Optional[PlanStep]:
        return next((s for s in self.steps if s.step_id == step_id
                     and s.status != REMOVED), None)

    def pending_steps(self) -> list:
        return [s for s in self.steps if s.status in ACTIVE_STATUSES]

    def guard_should_remind(self) -> bool:
        """completion guard（第 12 节）：有未完成步骤 + 未超限。"""
        if self.guard_reminders >= self.GUARD_LIMIT:
            return False
        return bool(self.pending_steps())

    def plan_block(self, max_chars: int = 800) -> str:
        """渲染注入 DA context 的计划块（围绕 current step——第 11 节）。"""
        if not self.active:
            return ""
        icons = {COMPLETED: "[done]", IN_PROGRESS: "[current]",
                 PENDING: "[ ]", FAILED: "[failed!]",
                 BLOCKED: "[blocked]", REMOVED: ""}
        lines = ["[Execution plan — work through current step; update via planning tools:]"]
        lines.append(f"GOAL: {self.goal}")
        for s in self.steps:
            if s.status == REMOVED:
                continue
            icon = icons.get(s.status, "[ ]")
            ent = f" (objects: {', '.join(s.entities)})" if s.entities else ""
            cur = "  ← CURRENT" if s.step_id == self._current_step_id else ""
            note = f"  note: {s.note}" if s.note else ""
            lines.append(f"{icon} {s.description}{ent}{cur}{note}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (plan truncated — use read_plan)"
        return text

    def reset(self) -> None:
        self.goal = None
        self.steps = []
        self._next_id = 1
        self._current_step_id = None
        self.guard_reminders = 0
