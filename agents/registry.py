"""
Agent 注册表。

把 CLI 中的逻辑 agent 名（--agent baseline）解析为 tau2 可识别的 agent 实现名。

V0 baseline = tau2 官方 ``llm_agent``（文本半双工，工具调用 + 对话）。

后续增加自己的 agent 时，在这里加一行映射即可：
  - 若新 agent 仍是 tau2 registry 里的实现（如 llm_agent_gt），直接指向名字；
  - 若是自定义实现，将 value 改为一个 factory 函数
    ``factory(tools, domain_policy, **kwargs) -> HalfDuplexAgent``，
    runner 会对 value 进行 callable 判断。

注意：不要在这里注册会读到 evaluation_criteria 的 agent —— 那是作弊。
"""

AGENT_REGISTRY = {
    # 逻辑名 -> tau2 registry agent 名
    "baseline": "llm_agent",
}


def resolve_agent(name: str):
    """把逻辑 agent 名解析为 (tau2_agent_name 或 factory, is_factory)。

    Args:
        name: CLI 传入的 --agent 值，如 "baseline"。

    Returns:
        (impl, is_callable)：impl 为 tau2 agent 名字符串，或一个可调用 factory。
    """
    if name not in AGENT_REGISTRY:
        raise ValueError(
            f"未知 agent: {name!r}。可用: {sorted(AGENT_REGISTRY)}。"
            f"在 agents/registry.py 中注册新 agent。"
        )
    impl = AGENT_REGISTRY[name]
    return impl, callable(impl)
