"""
失败分类法（failure taxonomy）。

每个失败的 task 由人工依据 trace 标注一个失败类型，写入 failure_labels.json；
analyze_failures.py 读取标注并输出分布统计。

12 类与 tau-bench 论文的失败类别对齐（见项目 README 的 "失败分类" 一节）。
"""

FAILURE_TYPES = [
    "planning",             # 规划错误：错误地分解 / 安排任务步骤，顺序不当
    "missing_information",  # 信息缺失：未获取 / 未利用关键客户或系统信息就下结论
    "wrong_tool",           # 选错工具：调用了一个与该步骤无关的工具
    "wrong_tool_args",      # 工具参数错误：工具本身正确，但参数用错（客户、ID、值域等）
    "policy_violation",     # 违反策略：作出了违反公司 / 产品策略的承诺或操作
    "observation_error",    # 观察错误：错误理解了工具返回结果（误读 / 漏读）
    "premature_stop",       # 过早停止：任务尚未完成就结束对话
    "loop",                 # 陷入循环：反复执行同一动作或来回推翻自己
    "bad_communication",    # 沟通不佳：措辞不清、未理解用户意图、未解释操作
    "wrong_transfer",       # 错误转移：把本不该转走的用户错误转给了其他部门 / 代理
    "environment_error",    # 环境错误：模拟器 / 工具 / 网络等环境问题（非 agent 自身错误）
    "unknown",              # 未分类：标注人暂时无法判断，留待复查
]

FAILURE_TYPES_SET = frozenset(FAILURE_TYPES)


def validate_failure_type(t: str) -> None:
    """校验失败类型是否合法，非法时抛 ValueError。"""
    if t not in FAILURE_TYPES_SET:
        raise ValueError(
            f"未知失败类型: {t!r}。合法类型: {FAILURE_TYPES}"
        )
