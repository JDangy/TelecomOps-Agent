"""Task-scoped Working Memory 实现。

两个 Agent 各自的 memory 内容不同：

DecisionWorkingMemory（压缩的任务状态，非聊天记录）：
  - user_constraints:  用户明确表达的约束（年费上限、卡种偏好…）
  - verified_facts:    已确认事实（身份验证完成、持有卡种…）
  - current_goal:      当前任务目标（文本，单值）
  - open_questions:    待解决的疑问
  - decisions_made:    已做的决定（推荐了什么、提交了什么）

KnowledgeWorkingMemory（跨 handoff 的检索工作状态）：
  - queries_tried:     已尝试的 query（去重）
  - documents_seen:    已见过的 doc_id（去重）
  - facts_found:       已确认事实（带 source_doc_id）
  - open_questions:    尚未解决的子问题
  - previous_requests: 之前收到的 KnowledgeRequest question 列表

更新原则：只从确定性事件直接提取（retrieval 结果、packet 内容、业务工具
返回），不调用 LLM 做总结。所有列表字段去重（按规范化 key）。
"""

from __future__ import annotations

from typing import Any, Optional

from agents.memory.base import AgentMemory, MemoryError


def _norm(text: str) -> str:
    """规范化字符串用于去重比较：lowercase + 压缩空白。"""
    return " ".join((text or "").lower().split())


class _WorkingMemory(AgentMemory):
    """通用 working memory 骨架：列表字段 + 单值字段 + 生命周期管理。

    子类声明：
      LIST_FIELDS: {field: normalizer}  —— 去重追加式字段
      SINGLE_FIELDS: [field]             —— 覆盖式单值字段
    """

    LIST_FIELDS: dict = {}
    SINGLE_FIELDS: list = []
    # 各字段上限（防膨胀；超限丢弃最旧的——保留最新的）
    FIELD_LIMITS: dict = {}

    def __init__(self):
        self._task_id: Optional[str] = None
        self._state: dict = {**{f: [] for f in self.LIST_FIELDS},
                             **{f: None for f in self.SINGLE_FIELDS}}

    # -- 生命周期 ---------------------------------------------------------
    def start_task(self, task_id: str) -> None:
        self._task_id = task_id
        self._state = {**{f: [] for f in self.LIST_FIELDS},
                       **{f: None for f in self.SINGLE_FIELDS}}
        self._emit_memory_event("memory_reset", _actor=self._actor(),
                                task_id=task_id)

    def end_task(self) -> dict:
        snap = self.snapshot()
        self.reset()
        return snap

    def reset(self) -> None:
        self._task_id = None
        self._state = {**{f: [] for f in self.LIST_FIELDS},
                       **{f: None for f in self.SINGLE_FIELDS}}

    # -- 读写 -------------------------------------------------------------
    def _require_active(self) -> None:
        if self._task_id is None:
            raise MemoryError("memory 未 start_task 就被使用——生命周期错误")

    def read(self) -> dict:
        return {k: v for k, v in self._state.items() if v}  # 空值字段不返回

    def update(self, event: dict) -> dict:
        """按事件类型分发。返回增量 {field: [新增项]}（空 = 无变化）。"""
        self._require_active()
        etype = event.get("type")
        handler = getattr(self, f"_on_{etype}", None)
        if handler is None:
            return {}  # 未知事件类型：静默忽略（memory 宽容，不抛错）
        delta = handler(event) or {}
        if delta:
            # 只记增量字段名 + 新增项（trace 不写全量）
            self._emit_memory_event(
                "memory_update", _actor=self._actor(),
                task_id=self._task_id,
                changed={k: v for k, v in delta.items()},
            )
        return delta

    def snapshot(self) -> dict:
        return {"task_id": self._task_id, **{k: list(v) if isinstance(v, list) else v
                                             for k, v in self._state.items()}}

    # -- 去重追加 -----------------------------------------------------------
    def _append_unique(self, field: str, item: Any, key_fn=None) -> list:
        """去重追加；返回本次新增项列表（空 = 重复被跳过）。"""
        norm = key_fn or (lambda x: x)
        existing_keys = {_norm(norm(x)) if isinstance(norm(x), str) else norm(x)
                         for x in self._state[field]}
        new_items = []
        for candidate in (item if isinstance(item, list) else [item]):
            k = _norm(norm(candidate)) if isinstance(norm(candidate), str) else norm(candidate)
            if k not in existing_keys:
                existing_keys.add(k)
                new_items.append(candidate)
        if not new_items:
            return []
        limit = self.FIELD_LIMITS.get(field)
        self._state[field].extend(new_items)
        if limit and len(self._state[field]) > limit:
            self._state[field] = self._state[field][-limit:]
        return new_items

    def _set_single(self, field: str, value: Any) -> Any:
        """覆盖单值字段；值变化时返回新值，未变返回 None。"""
        if self._state[field] == value:
            return None
        self._state[field] = value
        return value

    # -- 子类告知 -----------------------------------------------------------
    def _actor(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Decision Agent Working Memory
# ---------------------------------------------------------------------------
class DecisionWorkingMemory(_WorkingMemory):
    """Decision Agent 的任务状态记忆（压缩状态，非聊天记录）。

    字段（用户要求的最小集）：
      user_constraints —— 用户明确表达的约束
      verified_facts   —— 已确认事实（身份验证、持有卡种等）
      current_goal     —— 当前任务目标（单值覆盖）
      open_questions   —— 待解决疑问
      decisions_made   —— 已做的决定

    事件来源（全部确定性可提取，无 LLM）：
      user_constraint   —— Decision Agent 从用户消息提取的约束（由 prompt
                            结构化输出提供，非模型外总结）
      verified_fact     —— 身份验证完成 / 业务工具返回的确定状态
      goal              —— 任务目标变化（Agent 明确声明）
      open_question     —— 记录待查证问题
      decision          —— Agent 已执行的决定（推荐卡、提交申请等）
      resolve_question  —— 问题已解决（从 open_questions 移除）
    """

    LIST_FIELDS = {
        "user_constraints": None,
        "verified_facts": None,
        "open_questions": None,
        "decisions_made": None,
    }
    SINGLE_FIELDS = ["current_goal"]
    FIELD_LIMITS = {
        "user_constraints": 10,
        "verified_facts": 15,
        "open_questions": 8,
        "decisions_made": 10,
    }

    def _actor(self) -> str:
        return "decision_agent"

    # -- 事件处理器 ---------------------------------------------------------
    def _on_user_constraint(self, event) -> dict:
        new = self._append_unique("user_constraints", event.get("constraint", ""))
        return {"user_constraints": new} if new else {}

    def _on_verified_fact(self, event) -> dict:
        new = self._append_unique("verified_facts", event.get("fact", ""))
        return {"verified_facts": new} if new else {}

    def _on_goal(self, event) -> dict:
        v = self._set_single("current_goal", event.get("goal"))
        return {"current_goal": [v]} if v else {}

    def _on_open_question(self, event) -> dict:
        new = self._append_unique("open_questions", event.get("question", ""))
        return {"open_questions": new} if new else {}

    def _on_decision(self, event) -> dict:
        new = self._append_unique("decisions_made", event.get("decision", ""))
        return {"decisions_made": new} if new else {}

    def _on_resolve_question(self, event) -> dict:
        q = _norm(event.get("question", ""))
        before = len(self._state["open_questions"])
        self._state["open_questions"] = [
            x for x in self._state["open_questions"] if _norm(x) != q
        ]
        removed = before - len(self._state["open_questions"])
        return {"open_questions_resolved": [removed]} if removed else {}

    # -- context 渲染 ---------------------------------------------------------
    def _render(self) -> str:
        s = self._state
        lines = ["[Working Memory]"]
        if s["user_constraints"]:
            lines.append("Known user constraints:")
            lines += [f"- {x}" for x in s["user_constraints"][-6:]]
        if s["verified_facts"]:
            lines.append("Known facts:")
            lines += [f"- {x}" for x in s["verified_facts"][-6:]]
        if s["current_goal"]:
            lines.append(f"Current goal: {s['current_goal']}")
        if s["open_questions"]:
            lines.append("Open questions:")
            lines += [f"- {x}" for x in s["open_questions"][-5:]]
        if len(lines) == 1:
            return ""
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Knowledge Agent Working Memory
# ---------------------------------------------------------------------------
class KnowledgeWorkingMemory(_WorkingMemory):
    """Knowledge Agent 的跨 handoff 检索工作状态。

    V1 的核心问题：每次 handoff 从零开始——重复 query、重复读文档。
    本 memory 让 KA 在 task 内"记得搜过什么、看过什么、确认过什么"。

    字段：
      queries_tried      —— 已尝试的检索 query（去重）
      documents_seen     —— 已见过的 doc_id（去重）
      facts_found        —— 已确认事实 "claim [doc_id]"（去重）
      open_questions     —— 尚未解决的子问题
      previous_requests  —— 之前收到的 request question 列表

    事件来源（全部确定性）：
      retrieval_query    —— 每次执行的 BM25 query
      documents          —— 每次检索返回的 doc_ids
      facts              —— Evidence Packet 中的 facts（claim+doc_id）
      open_question      —— packet 的 missing_information
      request            —— 新收到的 KnowledgeRequest question
      resolve_question   —— 问题已解决
    """

    LIST_FIELDS = {
        "queries_tried": None,
        "documents_seen": None,
        "facts_found": None,
        "open_questions": None,
        "previous_requests": None,
    }
    FIELD_LIMITS = {
        "queries_tried": 30,
        "documents_seen": 120,
        "facts_found": 40,
        "open_questions": 10,
        "previous_requests": 15,
    }

    def _actor(self) -> str:
        return "knowledge_agent"

    # -- 事件处理器 ---------------------------------------------------------
    def _on_retrieval_query(self, event) -> dict:
        new = self._append_unique("queries_tried", event.get("query", ""))
        return {"queries_tried": new} if new else {}

    def _on_documents(self, event) -> dict:
        docs = event.get("doc_ids") or []
        new = self._append_unique("documents_seen", docs)
        return {"documents_seen": new} if new else {}

    def _on_facts(self, event) -> dict:
        facts = []
        for f in event.get("facts") or []:
            if isinstance(f, dict):
                claim = f.get("claim", "")
                doc = f.get("source_doc_id", "")
                if claim:
                    facts.append(f"{claim} [{doc}]" if doc else claim)
        new = self._append_unique("facts_found", facts)
        return {"facts_found": new} if new else {}

    def _on_open_question(self, event) -> dict:
        new = self._append_unique("open_questions", event.get("question", ""))
        return {"open_questions": new} if new else {}

    def _on_request(self, event) -> dict:
        new = self._append_unique("previous_requests", event.get("question", ""))
        return {"previous_requests": new} if new else {}

    def _on_resolve_question(self, event) -> dict:
        q = _norm(event.get("question", ""))
        before = len(self._state["open_questions"])
        self._state["open_questions"] = [
            x for x in self._state["open_questions"] if _norm(x) != q
        ]
        removed = before - len(self._state["open_questions"])
        return {"open_questions_resolved": [removed]} if removed else {}

    # -- context 渲染 ---------------------------------------------------------
    def _render(self) -> str:
        s = self._state
        lines = ["[Knowledge Agent Working Memory — what you already know from earlier in this task]"]
        if s["facts_found"]:
            lines.append("Facts already established:")
            lines += [f"- {x}" for x in s["facts_found"][-10:]]
        if s["queries_tried"]:
            lines.append("Queries you already tried (do NOT repeat these):")
            lines += [f"- {x}" for x in s["queries_tried"][-10:]]
        if s["documents_seen"]:
            docs = s["documents_seen"]
            shown = docs[-20:]
            more = f" (+{len(docs)-20} more)" if len(docs) > 20 else ""
            lines.append(f"Documents already seen: {', '.join(shown)}{more}")
        if s["open_questions"]:
            lines.append("Still unresolved:")
            lines += [f"- {x}" for x in s["open_questions"][-5:]]
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

