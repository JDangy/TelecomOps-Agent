"""Long-term Memory —— 仅接口预留，不实现（V1.1 明确范围外）。

未来的扩展链路（现在不做）：
    task trajectory → experience extraction → long-term memory → future task retrieval

明确不做的：
- 不创建 vector database
- 不做 embedding
- 不做跨任务 retrieval
- 不做 Skill
- 不把历史 task 注入当前 context
- 默认关闭，不影响 V1.1 实验

接口形状参考（实现时再细化）：
    retrieve(query) -> list[experience]   # 未来：检索相关历史经验
    store(experience) -> None             # 未来：task end 提炼经验入库
"""

from __future__ import annotations

from typing import Any


class LongTermMemory:
    """长期记忆占位：所有真实功能未实现。

    默认 disabled；调用任何方法抛 NotImplementedError（显式失败优于静默
    返回空——防止上层以为有长期记忆在生效）。
    """

    enabled: bool = False  # 永久关闭直到真正实现

    def retrieve(self, query: str) -> list[Any]:
        raise NotImplementedError(
            "LongTermMemory 尚未实现（V1.1 只预留接口）："
            "不做跨任务检索/向量库/embedding。"
        )

    def store(self, experience: Any) -> None:
        raise NotImplementedError(
            "LongTermMemory 尚未实现（V1.1 只预留接口）。"
        )
