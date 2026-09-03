"""Benchmark Integrity Guard（轻量回归测试）。

防两类回归（integrity cleanup 约定）：
1. **Prompt 泄漏**：agents/ 的字符串常量（prompt/instruction/示例）
   不得出现 tau2 banking_knowledge 真实 discoverable tool 名——那是
   Agent 本应通过 KB → unlock 才能发现的信息。
2. **Runtime 泄漏**：DecisionAgent/Harness/Resolver/TaskState/Context
   Builder 的运行时路径不得访问 evaluator 专属字段
   （evaluation_criteria / required_documents / gold actions）。

评测侧（eval/runner 的 metrics、required_documents recall 计算）
不在此限——那是 task 结束后的合法分析。

运行: .venv/bin/python tests/test_integrity.py（无网络/无 LLM）
"""

import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REAL_TOOL_NAMES_FILE = "third_party/tau2-bench/data/tau2/domains/banking_knowledge/tasks.json"


def _real_discoverable_tool_names():
    """从 tau2 toolkit 提取全部真实 discoverable 工具名（含 44 个）。"""
    try:
        from tau2.domains.banking_knowledge.environment import get_environment
        env = get_environment(retrieval_variant="bm25", retrieval_kwargs={})
        for t in env.get_tools():
            if t.name == "call_discoverable_agent_tool":
                tk = t._func.__self__
                return set(tk.get_discoverable_tools().keys())
    except Exception:
        pass
    return set()


def test_no_real_tool_names_in_agent_prompts():
    """agents/ 下任何 .py 的字符串中不得出现真实 discoverable 工具名。"""
    real = _real_discoverable_tool_names()
    assert real, "无法获取真实工具名列表（tau2 环境异常）"
    violations = []
    for f in glob.glob(os.path.join(ROOT, "agents", "**", "*.py"), recursive=True):
        content = open(f, encoding="utf-8").read()
        for name in real:
            if name in content:
                for i, line in enumerate(content.split("\n"), 1):
                    if name in line:
                        violations.append(f"{os.path.relpath(f, ROOT)}:{i}: {name}")
    assert not violations, (
        f"Prompt/代码中出现真实 benchmark 工具名 {len(violations)} 处: {violations[:5]}")


def test_no_evaluator_field_access_in_runtime():
    """runtime（agents/）不得访问 evaluator 专属字段。"""
    # 读取合法豁免（注释/docstring 里讨论这些词是允许的——只查真实访问模式）
    access_patterns = [
        re.compile(r"task\s*\[\s*['\"]evaluation_criteria['\"]\s*\]"),
        re.compile(r"task\s*\.\s*evaluation_criteria"),
        re.compile(r"task\s*\[\s*['\"]required_documents['\"]\s*\]"),
        re.compile(r"\.get\s*\(\s*['\"]evaluation_criteria['\"]"),
        re.compile(r"\.get\s*\(\s*['\"]gold"),
        re.compile(r"['\"]gold_action"),
    ]
    violations = []
    for f in glob.glob(os.path.join(ROOT, "agents", "**", "*.py"), recursive=True):
        content = open(f, encoding="utf-8").read()
        for pat in access_patterns:
            for m in pat.finditer(content):
                line_no = content[:m.start()].count("\n") + 1
                line = content.split("\n")[line_no - 1].strip()
                if line.startswith("#") or '"""' in line[:4]:
                    continue  # 注释行豁免
                violations.append(f"{os.path.relpath(f, ROOT)}:{line_no}: {line[:60]}")
    assert not violations, f"Runtime 疑似访问 evaluator 字段: {violations[:5]}"


def test_resolver_uses_unlock_state_only():
    """Resolver 的 inner schema 只来自 unlock state（合法路径）。"""
    src = open(os.path.join(ROOT, "agents/harness/resolver.py"), encoding="utf-8").read()
    # 禁止实际调用 get_discoverable_tools（全量上帝视角）。
    # 只查可执行调用（排除 docstring/注释里的文字说明）。
    code_lines = []
    in_doc = False
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.endswith('"""') and len(stripped) > 3:
            in_doc = not in_doc if stripped.count('"""') == 1 else False
            continue
        if not in_doc and not stripped.startswith("#"):
            code_lines.append(line)
    code = "\n".join(code_lines)
    assert ".get_discoverable_tools()" not in code, (
        "resolver 不得调用 get_discoverable_tools()（全量 introspection 泄漏）")
    # unlock state 是唯一 inner schema 来源
    assert "_agent_discoverable_tools_state" in src


def test_holdout_sealed():
    """Holdout 配置存在且只创建不运行（无对应 runs/ 目录）。"""
    cfg = json.load(open(os.path.join(ROOT, "configs/banking_holdout_sealed.json")))
    assert cfg["holdout_protocol"]["sample_size"] == len(cfg["task_ids"])
    assert cfg["holdout_protocol"]["sampling_seed"] == 20260903
    # 从未运行过：runs/ 下不应有 holdout tag 的 run 目录
    ran = [d for d in glob.glob(os.path.join(ROOT, "runs", "*"))
           if "holdout" in os.path.basename(d).lower()]
    assert not ran, f"Holdout 竟有运行记录: {ran}"


def test_dev_set_marked():
    """Dev config 已正式标记为 Development Set。"""
    dev = json.load(open(os.path.join(ROOT, "configs/banking_dev_tasks.json")))
    assert dev.get("set_role") == "DEVELOPMENT SET（Dev）"
    assert "holdout" in json.dumps(dev.get("complement", "")).lower()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{'✅ 全部通过' if failed == 0 else f'❌ {failed} 失败'}")
    sys.exit(1 if failed else 0)
