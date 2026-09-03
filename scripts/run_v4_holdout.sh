#!/bin/bash
# V4 Sealed Holdout 启动脚本（V4 freeze 泛化验证）
#
# 用途：等 API 余额恢复后由本人或 agent 执行（一次）。
# 代码已 git worktree 隔离在 /tmp/v4_holdout_run（commit 5899be0），
# 主仓库的 V5 改动不会影响本次 sealed 运行。
#
# 运行方式:
#   bash scripts/run_v4_holdout.sh
# 进度:
#   ls /tmp/v4_holdout_run/runs/v4_holdout_*/traces/*.v2.json | wc -l   （/22）
# 结果:
#   /tmp/v4_holdout_run/runs/v4_holdout_*/results.json + summary.json
#   （跑完后建议把 runs/v4_holdout_* 目录拷回主仓库 runs/ 归档）

set -e
cd /tmp/v4_holdout_run
export $(grep -v '^#' .env | xargs)
unset ANTHROPIC_BASE_URL

# 健康检查——余额不足直接退出（不产生半途结果）
.venv/bin/python - <<'PYEOF' 2>/dev/null
import os, sys
import litellm
try:
    r = litellm.completion(model=os.environ.get("HOLDOUT_MODEL", "openai/deepseek-v4-flash"),
        messages=[{"role": "user", "content": "ok"}], max_tokens=3)
    print("API OK")
except Exception as e:
    print(f"API FAIL: {str(e)[:150]}")
    sys.exit(1)
PYEOF

TAG=v4_holdout
if [ -d "runs/${TAG}_$(date +%Y%m%d)_*" ] 2>/dev/null; then
    echo "holdout 已运行过——sealed 协议禁止重跑"; exit 1
fi

setsid nohup .venv/bin/python run_eval.py \
  --tasks configs/banking_holdout_sealed.json \
  --agent two_agent_harness \
  --model openai/deepseek-v4-flash \
  --retrieval-config bm25 \
  --tag ${TAG} \
  --seed 42 --max-steps 60 \
  > /tmp/v4_holdout.log 2>&1 < /dev/null &
echo "holdout PID=$! — 日志 /tmp/v4_holdout.log"
echo "sealed set: 22 tasks（banking_holdout_sealed.json，seed=20260903 抽样）"
