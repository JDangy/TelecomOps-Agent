#!/usr/bin/env bash
# ============================================================
# TelecomOps-Agent 标准评测命令
# 用法: bash run_benchmark.sh [domain] [repeats]
#   domain: telecom | banking (默认 banking)
#   repeats: 运行次数（默认 3，取平均）
# ============================================================
set -euo pipefail

# 定位项目根目录（脚本所在目录），优先用 .venv 的 python（tau2 装在 venv 里，
# 系统 python 通常没有 tau2，直接用 `python` 会 ModuleNotFoundError）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
elif python3 -c "import tau2" 2>/dev/null; then
  PYTHON="python3"
else
  echo "错误: 找不到可用的 python（需安装 tau2）。请先执行:" >&2
  echo "  uv venv .venv && uv pip install -r requirements.txt" >&2
  exit 1
fi

DOMAIN="${1:-banking}"
REPEATS="${2:-3}"

case "$DOMAIN" in
  telecom)
    TASKS="configs/dev_tasks.json"
    TAG="telecom"
    RETRIEVAL_ARGS=()
    ;;
  banking)
    TASKS="configs/banking_dev_tasks.json"
    TAG="banking"
    # banking_knowledge 必须指定 retrieval 变体；bm25 为纯本地检索（不调 embedding API）
    RETRIEVAL_ARGS=(--retrieval-config bm25)
    ;;
  *)
    echo "Usage: $0 [telecom|banking] [repeats]"
    exit 1
    ;;
esac

echo "=== TelecomOps-Agent Benchmark ==="
echo "  domain:  $DOMAIN"
echo "  tasks:   $TASKS"
echo "  repeats: $REPEATS"
echo "  model:   openai/deepseek-v4-flash"
echo "  seed:    42"
echo "  max_steps: 60"
echo "  retrieval: ${RETRIEVAL_ARGS[*]:-(无)}"
echo ""

for i in $(seq 1 $REPEATS); do
  echo "--- Run $i/$REPEATS ---"
  "$PYTHON" run_eval.py \
    --tasks "$TASKS" \
    --agent baseline \
    --model openai/deepseek-v4-flash \
    --tag "${TAG}_v0_run${i}" \
    --seed 42 \
    --max-steps 60 \
    "${RETRIEVAL_ARGS[@]}"
  echo ""
done

echo "=== Done: $REPEATS runs of $DOMAIN ==="
