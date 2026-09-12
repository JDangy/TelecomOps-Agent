"""Worklist（V6.0）—— Goal/未完成事项的条目级表达。

动机（Frozen V5 Dev24 trace 归因,docs/v6_design.md §2-B）:
    task_026（6 笔 transaction 的 dispute→correction）:33 次工具调用
    交错重复,Phase 2 的 update 反复穿插重新查询,max_steps。
    task_080（5 张卡 freeze/unfreeze/close/order + 3 dispute）:
    42 次调用,unfreeze 了 3 张卡（gold 只 1 张）,漏查 pending
    transactions,最后 activate 缺席 → max_steps。
    task_077:47 次调用,close→unfreeze→close 循环。

    V5 PlanStore 的 PlanStep 是"步骤意图",多实体任务里一个 step
    （"dispute all incorrect transactions"）没有 per-entity 粒度,
    完成推进在多实体下歧义不推进（宁 pending 不猜——正确但导致
    步骤状态长期 pending,agent 无法从 plan 视图知道"哪笔已完成"）。

设计（严格对照用户 V6 指令 §三-2）:
    - 不删除/不重写 PlanStore —— Worklist 是 PlanStore 之上的轻量层:
      从带 entities 的 PlanStep **展开** per-entity 条目。
    - tool-result-driven（V5 已验证原则）:on_tool_result 与
      PlanStore.on_tool_result 同口径（tool_hint + 实体绑定）,
      绝不由 LLM 声明"完成"推进。
    - 失败 → failed,不误标完成（用户测试要求 #4）。
    - 通用结构:（goal 语义, entities, tool_hint）,无任何
      task/domain 硬编码——适用于多 dispute/多卡/多账户/多流程。

与 PlanStore 的分工:
    PlanStep = "还要做什么"的步骤意图（含无实体的顺序步骤）
    WorkItem = "每个实体的完成条目"（只从带 entities 的步骤派生,
    外加 runtime 观察（工具结果里的实体集）补充——见 sync_from_state）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 条目状态（与 PlanStore 对齐）
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"

ACTIVE = (PENDING, IN_PROGRESS, BLOCKED, FAILED)


@dataclass
class WorkItem:
    item_id: int
    goal: str                 # 业务语义（"dispute transaction A"）
    tool_hint: Optional[str] = None
    entities: list = field(default_factory=list)   # 实体 ID/对象名
    status: str = PENDING
    source: str = "plan"      # plan（步骤展开）/ runtime（观察派生）
    note: Optional[str] = None
    _bound_tool: Optional[str] = None   # 实际推进它的工具（观察记录）

    def to_dict(self) -> dict:
        return {"id": self.item_id, "goal": self.goal,
                "status": self.status, "tool_hint": self.tool_hint,
                "entities": self.entities, "source": self.source,
                "note": self.note}


class Worklist:
    """条目级未完成事项（确定性,零 LLM）。

    trace 事件实时 emit（work_item_*）——与 PlanStore 的 plan_* 事件
    同模式,由调用方或本类直接发（recorder 缺失静默）。
    """

    def __init__(self):
        self.goal: Optional[str] = None      # 总目标（从 PlanStore.goal 同步）
        self.items: list[WorkItem] = []
        self._next_id = 1

    # ------------------------------------------------------------------
    # 从 PlanStore 同步（展开,不替代）
    # ------------------------------------------------------------------
    def sync_from_plan(self, plan_store) -> int:
        """把 PlanStore 当前 active 计划的带-entities 步骤展开成条目。

        规则（全确定性）:
        - 只处理 status ∈ ACTIVE 的步骤（completed/removed 的跳过——
          其实体若已有条目则按步骤完成状态初始化条目状态）。
        - step.entities 非空 → 每个实体一条 WorkItem
          （goal = step description,tool_hint 同步骤）。
        - step.entities 空 → 不生成条目（无实体粒度,归 PlanStep 管）。
        - 幂等:同一（goal 文本, entity）已存在 → 不重复创建;
          已 COMPLETED 的条目不被重新同步为 pending（防 plan 重写
          把已完成事项"抹掉"——026 的 plan 二次 write 场景）。
        返回新建条数。
        """
        if plan_store is None or not plan_store.active:
            return 0
        self.goal = plan_store.goal
        created = 0
        for step in plan_store.steps:
            if not getattr(step, "entities", None):
                continue
            if step.status == "removed":
                continue
            for ent in step.entities:
                key = (str(step.description), str(ent))
                if self._find_by_key(key) is not None:
                    continue
                status = PENDING
                if step.status == "completed":
                    status = COMPLETED   # 步骤已真实完成 → 条目已完成
                elif step.status == "failed":
                    status = FAILED
                elif step.status == "blocked":
                    status = BLOCKED
                elif step.status == "in_progress":
                    status = IN_PROGRESS
                item = WorkItem(
                    item_id=self._next_id, goal=str(step.description)[:200],
                    tool_hint=step.tool_hint, entities=[str(ent)],
                    status=status, source="plan")
                self.items.append(item)
                self._next_id += 1
                created += 1
                self._emit("work_item_created", item)
        return created

    # ------------------------------------------------------------------
    # tool-result-driven 推进（与 PlanStore.on_tool_result 同口径）
    # ------------------------------------------------------------------
    def on_tool_result(self, inner_tool: str, ok: bool,
                       arguments: dict, task_state=None) -> list[WorkItem]:
        """一次内层工具执行结束 → 推进匹配条目。

        匹配（确定性,两层——沿用 PlanStore 已验证语义）:
        1. tool_hint == inner_tool 的 ACTIVE 条目集合 M
        2. M 中条目:调用参数引用其 entity（字符串命中或 Task State
           实体 ID 二次确认）→ 候选;唯一候选才推进（多候选歧义保持
           现状——宁可不推进,不猜）。
        3. hint 缺失/不匹配的条目:仅在工具结果**携带新实体**时由
           runtime 观察补充（见 observe_entities）,不在这里猜。

        ok=True → completed（记 _bound_tool）
        ok=False → failed（note 带工具名）——**绝不 completed**（测试 #4）
        已 completed 的条目再次匹配成功 → 幂等 no-op（防重复调用重置,
        026 形态）。
        返回被推进（或失败）的条目列表（trace 用）。
        """
        progressed: list[WorkItem] = []
        if not self.items or not inner_tool:
            return progressed
        matches = [it for it in self.items
                   if it.tool_hint == inner_tool and it.status in ACTIVE]
        candidates = []
        for it in matches:
            hit = self._args_reference_entity(arguments, it.entities, task_state)
            if hit:
                candidates.append((it, hit))
        if len(candidates) != 1:
            # 无候选/多候选歧义 → 不动（有明确依据才动）
            return progressed
        item = candidates[0][0]
        if item.status == COMPLETED:
            return progressed  # 幂等
        if ok:
            item.status = COMPLETED
            item.note = None
            item._bound_tool = inner_tool
            progressed.append(item)
            self._emit("work_item_completed", item, tool=inner_tool)
        else:
            item.status = FAILED
            item.note = f"tool failed: {inner_tool}"
            progressed.append(item)
            self._emit("work_item_failed", item, tool=inner_tool,
                       note=item.note)
        return progressed

    # ------------------------------------------------------------------
    # runtime 观察补充（多实体任务的"漏掉的对象"可见性——080 形态）
    # ------------------------------------------------------------------
    def observe_entities(self, entities: list, goal_hint: str = "",
                         tool_hint: str = None) -> int:
        """工具结果里发现的新实体集 → 对称补条目（防"只做一半"）。

        场景（通用,非 task 特定）:用户目标涉及 N 个实体（3 张卡、
        6 笔交易）,plan 里只有部分实体有条目。当工具结果暴露完整
        实体集（如 get_debit_cards 返回 3 张卡）且 goal_hint 非空时,
        为"同 goal_hint、无条目"的实体补 PENDING 条目。
        幂等:（goal_hint, entity）已存在任何条目 → 跳过。
        返回补充条数。**保守**:goal_hint 为空不补（无语义锚不猜）。
        """
        if not entities or not goal_hint:
            return 0
        existing = {(it.goal, e) for it in self.items for e in it.entities}
        created = 0
        for ent in entities:
            key = (goal_hint, str(ent))
            if key in existing:
                continue
            item = WorkItem(item_id=self._next_id,
                            goal=str(goal_hint)[:200],
                            tool_hint=tool_hint, entities=[str(ent)],
                            status=PENDING, source="runtime")
            self.items.append(item)
            self._next_id += 1
            created += 1
            self._emit("work_item_created", item)
        return created

    # ------------------------------------------------------------------
    # 查询/渲染
    # ------------------------------------------------------------------
    def unfinished(self) -> list[WorkItem]:
        """未完成条目（pending/in_progress/failed/blocked）。
        多实体任务只要有一条未完成 → 非空（测试 #5:不会只完成
        一个就整体结束的可见性基础）。"""
        return [it for it in self.items if it.status in ACTIVE]

    @property
    def has_unfinished(self) -> bool:
        return bool(self.unfinished())

    def completed(self) -> list[WorkItem]:
        return [it for it in self.items if it.status == COMPLETED]

    def render_block(self, max_items: int = 10, max_chars: int = 700) -> str:
        """渲染注入 context 的 worklist 视图。

        组织（对照用户 V6 指令 §三-2 的 [x]/[ ] 形态）:
            GOAL 行 + 当前条目优先（in_progress→pending→failed）+
            已完成摘要（压缩一行计数 + 实体列表）。
        无条目时返回空串（简单任务零开销——保护 001/004 路径）。
        """
        if not self.items:
            return ""
        icons = {COMPLETED: "[x]", IN_PROGRESS: "[~]", PENDING: "[ ]",
                 FAILED: "[!]", BLOCKED: "[=]"}
        lines = []
        if self.goal:
            lines.append(f"GOAL: {self.goal}")
        # 当前优先
        order = {IN_PROGRESS: 0, PENDING: 1, FAILED: 2, BLOCKED: 3}
        active_items = sorted(self.unfinished(),
                              key=lambda it: order.get(it.status, 9))
        for it in active_items[:max_items]:
            icon = icons.get(it.status, "[ ]")
            ent = it.entities[0] if len(it.entities) == 1 else str(it.entities)
            note = f"  note: {it.note}" if it.note else ""
            lines.append(f"{icon} {it.goal} (object: {ent}){note}")
        done = self.completed()
        if done:
            ents = ", ".join(it.entities[0] for it in done[:6])
            more = f" (+{len(done) - 6} more)" if len(done) > 6 else ""
            lines.append(f"[x] {len(done)} item(s) completed: {ents}{more}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (worklist truncated)"
        return text

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _find_by_key(self, key: tuple) -> Optional[WorkItem]:
        goal, ent = key
        for it in self.items:
            if it.goal == goal and ent in it.entities:
                return it
        return None

    @staticmethod
    def _args_reference_entity(arguments: dict, entities: list,
                               task_state) -> Optional[str]:
        """调用参数是否引用条目实体（与 PlanStore._args_reference_entity
        同口径:字符串命中 + Task State 实体 ID 二次确认）。"""
        vals = [str(v) for v in (arguments or {}).values()
                if v is not None and not isinstance(v, (dict, list))]
        for e in entities:
            e = str(e)
            for v in vals:
                if v == e or (e in v) or (v in e and len(v) >= 4):
                    return e
        if task_state is not None:
            try:
                for key, chain in getattr(task_state, "_entries", {}).items():
                    cur = chain[-1] if chain else None
                    if not cur or not cur.is_current:
                        continue
                    if cur.field in ("id", "account_id", "card_id", "user_id",
                                     "transaction_id"):
                        for e in entities:
                            if cur.object.endswith(e) or e in cur.object:
                                if str(cur.value) in vals:
                                    return e
            except Exception:
                pass
        return None

    def _emit(self, event_type: str, item: WorkItem, **extra) -> None:
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            return
        if rec is None:
            return
        try:
            rec.emit(event_type, "decision_agent",
                     parent_span_id=getattr(rec, "task_span_id", None),
                     item_id=item.item_id, goal=item.goal[:120],
                     entities=item.entities, status=item.status,
                     source=item.source, **extra)
        except Exception:
            pass

    def reset(self) -> None:
        self.goal = None
        self.items = []
        self._next_id = 1
