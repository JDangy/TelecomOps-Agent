"""
Agent Memory 抽象（V1.1）。

每个 Agent 拥有自己独立的 memory instance；memory 是 Agent 私有状态：
- Agent 之间不能直接读取对方 memory
- 跨 Agent 信息只能通过 KnowledgeRequest / EvidencePacket 等正式接口传递
- working memory 生命周期 = 单个 task：start_task 创建、end_task 快照后清空
- 不允许上一个 task 的内容泄漏到下一个 task

两层结构：
  Agent Memory
  ├── WorkingMemory   —— 当前实现：task 内工作记忆（压缩的任务状态）
  └── LongTermMemory  —— 仅预留接口，不实现（不做向量库/embedding/跨任务检索）

设计原则：
- memory 保存"压缩后的任务状态"（query/doc_id/fact/未决问题），不是第二份聊天记录
- 更新走确定性程序逻辑（从事件直接提取），不加 LLM summarizer
- 注入 Agent context 时有明确预算（字符上限 + 去重），不能无限增长
"""

from agents.memory.base import AgentMemory, MemoryError
from agents.memory.working_memory import (
    DecisionWorkingMemory,
    KnowledgeWorkingMemory,
)
from agents.memory.long_term import LongTermMemory

__all__ = [
    "AgentMemory",
    "MemoryError",
    "DecisionWorkingMemory",
    "KnowledgeWorkingMemory",
    "LongTermMemory",
]
