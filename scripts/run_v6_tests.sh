#!/usr/bin/env bash
# V6.0 Level 0 — deterministic tests (no LLM / no network / no API)
# 用法: bash scripts/run_v6_tests.sh
set -e
cd "$(dirname "$0")/.."

echo "=================================================================="
echo "V6.0 Level 0: unit tests (deterministic) + integrity guard"
echo "=================================================================="

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "--- [1/3] tests/test_unit.py (V5 + V6 deterministic) ---"
"$PY" tests/test_unit.py

echo
echo "--- [2/3] tests/test_integrity.py (incl. V6 replay/runtime guards) ---"
"$PY" tests/test_integrity.py

echo
echo "--- [3/3] eval/replay.py --build (agent-visible-only replay cases) ---"
"$PY" -m eval.replay --build

echo
echo "Level 0 全部通过。"
echo "  - Level 1 (单轮决策 replay, 需 LLM) : 尚未获批 — 见 docs/v6_design.md §7"
echo "  - Level 2/3/4 (目标 task / regression / Dev24) : 尚未获批"
