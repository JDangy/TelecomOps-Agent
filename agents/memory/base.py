"""Memory 统一接口（base）。

所有 memory 实现遵循同一生命周期：

    start_task(task_id)   —— task 开始：创建/重置
    read()                —— 读取当前状态（注入 Agent context 的来源）
    update(event)         —— 事件驱动更新（只允许程序可提取的字段）
    snapshot()            —— 导出完整快照（task end 写入 trace 用）
    end_task()            —— task 结束：快照 + 清空，防跨 task 泄漏
    reset()               —— 紧急清空（异常路径也必须干净）

trace v2 事件契约（由实现方负责发，见 instrumentation 的 memory_* 事件）：
- update 事件只记录本次增量（changed fields），避免 trace 爆炸
- task end 由 runner 触发 snapshot 事件记录完整状态一次
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class MemoryError(Exception):
    """memory 生命周期/接口误用（如未 start_task 就 update）。"""


class AgentMemory(ABC):
    """Agent 私有 memory 的统一接口。

    实现约束：
    - 每个 Agent 实例持有一个 memory 实例（不共享）
    - working memory 必须在 task 边界重置（end_task 后回到空状态）
    - snapshot() 返回纯 JSON-serializable dict（写 trace 用）
    - context_block() 返回注入 Agent prompt 的固定格式文本（有预算上限）
    """

    # 子类定义：注入 context 的字符预算上限
    context_budget_chars: int = 1500

    @abstractmethod
    def start_task(self, task_id: str) -> None:
        """task 开始：初始化本 task 的 memory 状态。"""

    @abstractmethod
    def read(self) -> dict:
        """读取当前 memory 状态（结构化 dict）。"""

    @abstractmethod
    def update(self, event: dict) -> dict:
        """事件驱动更新。

        Args:
            event: 形如 {"type": "...", ...} 的确定性事件。

        Returns:
            本次实际发生的增量 {"field": [新增项...]}——空 dict 表示无变化。
            增量用于 trace v2 的 memory_update 事件（不写全量）。
        """

    @abstractmethod
    def snapshot(self) -> dict:
        """导出完整快照（JSON-serializable；task end 写 trace 用）。"""

    @abstractmethod
    def end_task(self) -> dict:
        """task 结束：返回快照并清空内部状态（防跨 task 泄漏）。"""

    @abstractmethod
    def reset(self) -> None:
        """无条件清空（异常恢复路径使用）。"""

    # -- context 注入（具体格式由子类实现，基类只约束预算）----------------
    def context_block(self) -> str:
        """渲染成 Agent 可见的固定格式文本（≤ context_budget_chars）。

        空状态时返回空字符串（不注入任何内容，零污染）。
        """
        if not self.read():
            return ""
        text = self._render()
        if len(text) > self.context_budget_chars:
            text = text[: self.context_budget_chars] + "\n... (memory truncated)"
        return text

    def _render(self) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError

    # -- trace 事件辅助 ---------------------------------------------------
    def _emit_memory_event(self, event_type: str, **fields: Any) -> None:
        """向 active TraceV2Recorder 发 memory 事件（无 recorder 时静默跳过）。

        事件 actor 由调用方（Agent）告知——memory 自己不知道宿主是谁。
        """
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            return
        # 子类在调用时传入 actor；此处统一缺省 system（不应出现）
        actor = fields.pop("_actor", "system")
        if rec is None:
            return
        try:
            rec.emit(event_type, actor, **fields)
        except Exception:
            pass  # trace 失败绝不影响 Agent 执行
