"""
TelecomOps-Agent 的 agents 包。

负责把我们自己的 agent 命名（CLI 的 --agent）映射到具体的 agent 实现。
V0 baseline 使用 tau2 官方自带的 llm_agent；后续 V1/V2 在这里注册自定义 agent。
"""

from agents.registry import AGENT_REGISTRY, resolve_agent

__all__ = ["AGENT_REGISTRY", "resolve_agent"]
