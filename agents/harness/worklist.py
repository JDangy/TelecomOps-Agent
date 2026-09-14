"""Worklist（V6.1 架构收紧）—— PlanStore 大步骤下的条目级展开。

动机（用户收紧指令 §2 + Frozen V5 Dev24 trace 归因 docs/v6_design.md §2-B）:
    task_026（6 笔 transaction 的 dispute→correction）:33 次工具调用
    交错重复;task_080（5 卡 freeze/unfreeze/close/order）:unfreeze 了
    3 张卡（gold 只 1 张）+ 漏 activate;task_077:close→unfreeze→close
    循环。V5 PlanStep 是"步骤意图",多实体任务里一个 step 没有逐实体
    粒度,agent 靠记忆在 N 个对象间往返。

正确关系（§2 原文）:
    PlanStore
    大步骤：处理所有异常交易
        ↓ 展开
    Worklist
    transaction A / transaction B / transaction C

    Worklist 只是"一个大步骤下面有哪些具体对象还没处理"。
    完成情况必须根据真实 Tool Result 更新。

收紧要点（与上一版 V6 的差异）:
    1. WorkItem 挂在 PlanStep 下（step_id 反向引用）——不是独立清单。
       条目的生命周期 = 步骤的生命周期:步骤 removed → 条目移除。
    2. 唯一来源是 plan 步骤的展开——上一版的 observe_entities
       （"runtime 观察"旁路补条目）已删:脱离 plan 的第二入口正是
       "第二套 Plan"的开端,不做。
    3. 实体绑定谓词复用 plan_store.args_reference_entity（单一实现,
       推进口径永远与 PlanStore 一致）。
    4. 渲染不在这里——ContextBuilder 是 context 的统一出口
       （本模块只提供数据结构 + 状态查询）。
    5. tool-result-driven,ok=False → failed,幂等,与 PlanStore 同语义。
"""

from __future__ import annotations

from typing import Optional

from agents.harness.plan_store import args_reference_entity

# 条目状态（与 PlanStore 对齐）
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"

ACTIVE = (PENDING, IN_PROGRESS, BLOCKED, FAILED)


class WorkItem:
    """一个具体对象的完成条目（挂在 PlanStep 下）。

    不再是独立 dataclass 状态机——就是步骤实体的进度载体。
    """

    __slots__ = ("step_id", "entity", "status", "note", "bound_tool")

    def __init__(self, step_id: int, entity: str, status: str = PENDING):
        self.step_id: int = step_id           # 父 PlanStep.step_id
        self.entity: str = str(entity)        # 具体对象（ID/对象名）
        self.status: str = status
        self.note: Optional[str] = None
        self.bound_tool: Optional[str] = None  # 实际推进它的工具（观察记录）

    def to_dict(self) -> dict:
        return {"step_id": self.step_id, "entity": self.entity,
                "status": self.status, "note": self.note}


class Worklist:
    """PlanStore 之上的条目级进度（确定性,零 LLM,零独立来源）。

    数据流（单向,只有一个写入路径）:
        PlanStore.write_plan/update_plan
            → sync_from_plan 展开/同步（结构派生）
        Tool Result（真实执行）
            → on_tool_result 推进（与 PlanStore.on_tool_result 同口径）

    生命周期语义（与 PlanStore 一致,worklist 不自造第二套）:
        - 步骤 removed / 计划重写 → 条目随之消失/重建（step_id 变更
          → 新条目）。重写后条目回到 pending 是 PlanStore 本身的语义
          （重写 = 重新声明意图）,worklist 跟随,不加"完成缓存"。
        - 推进只认 (tool_hint, entity) + 真实 Tool Result,与 PlanStore
          的推进条件完全一致——两处进度永远同源。
    """

    def __init__(self):
        self._items: dict[tuple[int, str], WorkItem] = {}  # (step_id, entity) → item

    # ------------------------------------------------------------------
    # 从 PlanStore 同步（展开,不替代,幂等）
    # ------------------------------------------------------------------
    def sync_from_plan(self, plan_store) -> int:
        """把 active 计划中带 entities 的步骤展开成条目。

        规则（全确定性）:
        - step.entities 非空 → 每个实体一条 WorkItem（挂在 step_id 下）
        - step.entities 空 → 不生成条目（无实体粒度,归 PlanStep 自己管）
        - 步骤已真实完成的（completed/failed/blocked）→ 条目按步骤状态
          初始化（plan 重写防"抹掉"已完成——026 的二次 write 场景）
        - 步骤 removed → 条目移除（步骤没了,条目随之消失）
        - 幂等:已存在且非 pending 的条目不被重置
        返回净新增条数。
        """
        if plan_store is None or not plan_store.active:
            # 计划清空/未建 → 条目全部移除（Worklist 不能脱离 plan 存在）
            removed = len(self._items)
            self._items.clear()
            return 0
        created = 0
        # 1) 移除不存在/已 removed 步骤的条目（步骤没了,条目随之消失）
        live_steps = {s.step_id for s in plan_store.steps
                      if s.status != "removed"}
        for key in [k for k in self._items if k[0] not in live_steps]:
            self._emit("work_item_removed", self._items.pop(key))
        # 2) 展开带 entities 的步骤
        for step in plan_store.steps:
            if not getattr(step, "entities", None):
                continue
            if step.status == "removed":
                continue
            for ent in step.entities:
                key = (step.step_id, str(ent))
                if key in self._items:
                    continue
                status = PENDING
                if step.status == "completed":
                    status = COMPLETED
                elif step.status == "failed":
                    status = FAILED
                elif step.status == "blocked":
                    status = BLOCKED
                elif step.status == "in_progress":
                    status = IN_PROGRESS
                item = WorkItem(step_id=step.step_id, entity=ent,
                                status=status)
                self._items[key] = item
                created += 1
                self._emit("work_item_created", item,
                           goal=str(getattr(step, "description", ""))[:120])
        return created

    # ------------------------------------------------------------------
    # tool-result-driven 推进（与 PlanStore.on_tool_result 同口径）
    # ------------------------------------------------------------------
    def on_tool_result(self, plan_store, inner_tool: str, ok: bool,
                       arguments: dict, task_state=None) -> list:
        """一次内层工具执行结束 → 推进匹配条目。

        匹配（确定性,两层——与 PlanStore.on_tool_result 完全同口径,
        绑定谓词共用 plan_store.args_reference_entity）:
        1. 候选 = 条目的父步骤 tool_hint == inner_tool（或父步骤无 hint
           且是 current in_progress——与 PlanStore 无-hint 回退同条件）
        2. 候选中调用参数引用其 entity → 唯一命中才推进（多命中歧义
           → 不动,宁可不推进,不猜）
        ok=True → completed（记 bound_tool）
        ok=False → failed（note 带工具名）——绝不 completed
        已 completed 再次匹配成功 → 幂等 no-op（重复调用不重置）
        返回被推进/失败的条目列表（trace 用）。
        """
        progressed: list[WorkItem] = []
        if not self._items or not inner_tool or plan_store is None:
            return progressed
        # 无-hint 回退条件（与 PlanStore 一致:current step 且 in_progress）
        cur_step = None
        try:
            cur_step = plan_store._current_in_progress_step()
        except Exception:
            cur_step = None
        candidates = []
        for key, item in self._items.items():
            if item.status not in ACTIVE and item.status != COMPLETED:
                continue
            step = next((s for s in plan_store.steps
                         if s.step_id == item.step_id), None)
            if step is None or step.status == "removed":
                continue
            hint = getattr(step, "tool_hint", None)
            hint_hit = (hint == inner_tool) or (
                hint in (None, "") and step is cur_step)
            if not hint_hit:
                continue
            if args_reference_entity(arguments, [item.entity], task_state):
                candidates.append(item)
        if len(candidates) != 1:
            # 无候选/多候选歧义 → 不动（有明确依据才动）
            return progressed
        item = candidates[0]
        if item.status == COMPLETED:
            return progressed  # 幂等
        if ok:
            item.status = COMPLETED
            item.note = None
            item.bound_tool = inner_tool
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
    # 查询（ContextBuilder/ checkpoint 的数据来源）
    # ------------------------------------------------------------------
    def items_for_step(self, step_id: int) -> list:
        """某步骤下的全部条目（按 entity 插入序）。"""
        return [it for (sid, _), it in sorted(self._items.items())
                if sid == step_id]

    def unfinished(self) -> list:
        """未完成条目（pending/in_progress/failed/blocked）。

        多实体任务只要有一条未完成 → 非空（"不会只完成一个就整体
        结束"的可见性基础——guard 消费此查询）。
        """
        return [it for it in self._items.values() if it.status in ACTIVE]

    @property
    def has_unfinished(self) -> bool:
        return bool(self.unfinished())

    def completed(self) -> list:
        return [it for it in self._items.values() if it.status == COMPLETED]

    def __len__(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------------
    # trace（实时 emit;无 recorder 静默）
    # ------------------------------------------------------------------
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
                     step_id=item.step_id, entity=item.entity[:80],
                     status=item.status, **extra)
        except Exception:
            pass

    def reset(self) -> None:
        self._items.clear()
