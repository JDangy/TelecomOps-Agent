"""Execution Plan（V4）—— Adaptive Long-Horizon Execution 的持久计划。

设计（对照验收 13 问的 2/3/4/5）：

升级信号（确定性，零 LLM——从 v3_official24 实测分布标定）：
  成功任务 0-12 inner calls / 0-4 repeats；顽固任务 15-27 calls / 7-9
  distinct / 6-20 repeats。阈值取成功带外缘：
      total_inner_calls ≥ 12  或  distinct_tools ≥ 8  或  repeats ≥ 6
  → 进入 Plan Mode。升级前任务完全走 V3 轻量路径（简单任务不折腾）。

数据结构（朴素状态机——不用 DAG）：
  ExecutionPlan:
    goal: str                     # 用户目标（首个进入消息摘要，确定性截断）
    steps: list[PlanStep]
  PlanStep:
    description: str              # agent 计划一步做什么
    status: pending | in_progress | completed | failed
    tool_hint: str | None          # 关联的内层工具名（若有）
    started_seq / finished_seq     # trace seq

进度由真实执行驱动（不信 agent 口头"完成了"）：
  - agent 在 assistant 文本里写 PLAN 行（轻量格式，随正常推理输出，
    不新增调用）→ 解析为 pending 步骤
  - 步骤的完成判定 = 匹配的内层工具调用**成功执行**（ToolMessage
    .error=False 且 inner tool 匹配 tool_hint）→ completed
  - Tool 失败 → failed（可被 agent 重列步骤翻回 pending）
  - agent 声称但无工具证据的步骤保持 pending（防"做一半自称完成"）

completion check（结束前轻量检查）：
  agent 输出纯文本（准备结束）且 plan 有 pending 步骤 → 注入一条
  系统提醒（plan 上下文）让它继续；最多 REMIND_LIMIT 次（防死循环，
  超过后放行结束——尊重 agent 的"确实做完了"判断，如用户改需求）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---- 升级阈值（v3_official24 实测标定：成功任务 0-12 calls/0-4 repeats）----
PLAN_TRIGGER_TOTAL_CALLS = 12
PLAN_TRIGGER_DISTINCT_TOOLS = 8
PLAN_TRIGGER_REPEATS = 6

# ---- completion check 有界 ----
REMIND_LIMIT = 2

# ---- PLAN 行解析格式（agent 在文本里的轻量计划语言）----
# [PLAN] step description | tool: inner_tool_name
_PLAN_LINE_RE = re.compile(r"^\s*\[PLAN\]\s*(.+?)\s*(?:\|\s*tool:\s*(\S+)\s*)?$",
                           re.IGNORECASE | re.MULTILINE)
_PLAN_DONE_RE = re.compile(r"^\s*\[PLAN-DONE\]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class PlanStep:
    description: str
    status: str = "pending"          # pending/in_progress/completed/failed
    tool_hint: Optional[str] = None
    started_seq: Optional[int] = None
    finished_seq: Optional[int] = None


@dataclass
class ExecutionPlan:
    goal: str
    steps: list = field(default_factory=list)
    active: bool = False               # Plan Mode 是否激活
    reminders_used: int = 0            # completion check 次数（有界）


class PlanTracker:
    """DecisionAgent 侧的 plan 运行时（确定性，零额外 LLM）。

    与 Task State 的分工（V4 第 9 节）：
      Task State  = 现在是什么（事实）
      Plan/Progress = 还要做什么（进度）
    两者都在 Context Builder 汇成 DA 每轮视图。
    """

    def __init__(self):
        self.plan: Optional[ExecutionPlan] = None
        self._inner_tool_calls: list = []   # [(inner_name, ok)]
        self._last_messages_len = 0

    # ------------------------------------------------------------------
    # 升级判定（每次工具结果后调用；返回 True = 本次进入 Plan Mode）
    # ------------------------------------------------------------------
    def maybe_upgrade(self, inner_tool_name: str, ok: bool) -> bool:
        self._inner_tool_calls.append((inner_tool_name, ok))
        if self.plan is not None:
            return False  # 已激活
        total = len(self._inner_tool_calls)
        distinct = len({n for n, _ in self._inner_tool_calls})
        repeats = total - distinct
        if (total >= PLAN_TRIGGER_TOTAL_CALLS
                or distinct >= PLAN_TRIGGER_DISTINCT_TOOLS
                or repeats >= PLAN_TRIGGER_REPEATS):
            self.plan = ExecutionPlan(goal="(goal pending — state it in your [PLAN] lines)")
            self.plan.active = True
            return True
        return False

    # ------------------------------------------------------------------
    # agent 文本里的 PLAN 行解析（正常推理输出的一部分，零额外调用）
    # ------------------------------------------------------------------
    def ingest_agent_text(self, text: str) -> None:
        """从 assistant 文本解析 [PLAN] / [PLAN-DONE] 行到计划。"""
        if not self.plan or not text:
            return
        for m in _PLAN_LINE_RE.finditer(text):
            desc, tool = m.group(1), m.group(2)
            # 去重：已有相同描述的步骤不重复添加
            if not any(s.description == desc for s in self.plan.steps):
                self.plan.steps.append(PlanStep(description=desc, tool_hint=tool))
        for m in _PLAN_DONE_RE.finditer(text):
            desc = m.group(1)
            for s in self.plan.steps:
                if s.description == desc and s.status in ("pending", "failed", "in_progress"):
                    # agent 声称完成——只升 in_progress（真正 completed
                    # 需要 tool 证据，见 on_tool_result）
                    s.status = "in_progress"
                    break

    # ------------------------------------------------------------------
    # 真实 Tool Result 驱动进度（核心：不信口头，看执行）
    # ------------------------------------------------------------------
    def on_tool_result(self, inner_tool_name: str, ok: bool, seq: int = 0) -> None:
        """一次内层工具执行结束——驱动匹配步骤的完成/失败。

        匹配规则（保守）：步骤 tool_hint == 内层工具名 → 该工具所有
        pending/in_progress 步骤视为本步产物：ok→completed，失败→failed。
        无 tool_hint 的步骤不自动推进（描述匹配需要语义，缺依据不动）。
        """
        if not self.plan:
            return
        for s in self.plan.steps:
            if s.tool_hint == inner_tool_name and s.status != "completed":
                if ok:
                    s.status = "completed"
                    s.finished_seq = seq
                else:
                    s.status = "failed"
                    s.finished_seq = seq

    # ------------------------------------------------------------------
    # completion check（结束前）
    # ------------------------------------------------------------------
    def should_remind_continue(self) -> bool:
        """agent 要结束时：有未完成步骤且提醒未超限 → True（提醒继续）。"""
        if not self.plan or not self.plan.active:
            return False
        if self.plan.reminders_used >= REMIND_LIMIT:
            return False
        return any(s.status in ("pending", "in_progress", "failed") for s in self.plan.steps)

    def mark_reminded(self) -> None:
        if self.plan:
            self.plan.reminders_used += 1

    # ------------------------------------------------------------------
    # 渲染（Context Builder 用）
    # ------------------------------------------------------------------
    def progress_block(self) -> str:
        """渲染 Goal + 步骤状态（DA 每轮视图的 Plan 部分）。"""
        if not self.plan or not self.plan.active:
            return ""
        icons = {"completed": "[done]", "in_progress": "[current]",
                 "pending": "[ ]", "failed": "[failed]"}
        lines = ["Execution plan — track your progress with [PLAN] lines; a step only "
                 "counts as done after its tool call succeeds:"]
        lines.append(f"goal: {self.plan.goal}")
        for s in self.plan.steps:
            icon = icons.get(s.status, "[ ]")
            tool = f" (tool: {s.tool_hint})" if s.tool_hint else ""
            lines.append(f"{icon} {s.description}{tool}")
        return "\n".join(lines)

    def pending_summary(self) -> str:
        if not self.plan:
            return ""
        pend = [s for s in self.plan.steps if s.status in ("pending", "in_progress", "failed")]
        if not pend:
            return ""
        return "; ".join(f"{s.status}:{s.description}" for s in pend)

    def reset(self) -> None:
        self.plan = None
        self._inner_tool_calls = []
        self._last_messages_len = 0
