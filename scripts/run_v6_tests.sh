#!/usr/bin/env bash
# V6.1 Level 0 — deterministic tests only.
# 本阶段（架构收紧）不构建/运行 replay（用户指令:只做架构与确定性
# 测试,未获批前不跑 replay / API / Dev24 / Holdout）。
# 用法: bash scripts/run_v6_tests.sh
set -e
cd "$(dirname "$0")/.."

echo "=================================================================="
echo "V6.1 Level 0: unit tests (deterministic) + integrity guard"
echo "  架构收紧: TaskState 唯一事实源 / PlanStore→Worklist /"
echo "  ContextBuilder 统一出口 / checkpoint 6问零LLM"
echo "=================================================================="

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "--- [1/2] tests/test_unit.py (V5 + V6.1 deterministic, 37 tests) ---"
"$PY" tests/test_unit.py

echo
echo "--- [2/2] tests/test_integrity.py (runtime guards + 硬编码禁令) ---"
"$PY" tests/test_integrity.py

echo
echo "Level 0 全部通过（replay 构建本阶段移除——待用户审阅后再定）"
echo "  - Level 1 (单轮决策 replay, 需 LLM) : 未获批"
echo "  - Level 2/3/4 (目标 task / regression / Dev24) : 未获批"
