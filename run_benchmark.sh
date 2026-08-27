#!/usr/bin/env bash
# ============================================================
# TelecomOps-Agent 标准评测命令
# 用法: bash run_benchmark.sh [domain] [repeats]
#   domain: telecom | banking (默认 banking)
#   repeats: 运行次数（默认 3，取平均）
# ============================================================
set -euo pipefail

DOMAIN="${1:-banking}"
REPEATS="${2:-3}"

case "$DOMAIN" in
  telecom)
    TASKS="configs/dev_tasks.json"
    TAG="telecom"
    ;;
  banking)
    TASKS="configs/banking_dev_tasks.json"
    TAG="banking"
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
echo ""

for i in $(seq 1 $REPEATS); do
  echo "--- Run $i/$REPEATS ---"
  python run_eval.py \
    --tasks "$TASKS" \
    --agent baseline \
    --model openai/deepseek-v4-flash \
    --tag "${TAG}_v0_run${i}" \
    --seed 42 \
    --max-steps 60
  echo ""
done

echo "=== Done: $REPEATS runs of $DOMAIN ==="
