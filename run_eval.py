#!/usr/bin/env python3
"""
TelecomOps-Agent 的 evaluation CLI 入口。

用法：
  python run_eval.py --tasks configs/dev_tasks.json --agent baseline
  python run_eval.py --tasks configs/dev_tasks.json --agent baseline --num-tasks 5
  python run_eval.py --tasks configs/dev_tasks.json --agent baseline --tag v0

  python run_eval.py --domain banking_knowledge --retrieval-config bm25 \
    --tasks configs/banking_dev_tasks.json --agent baseline
  python run_eval.py --domain banking_knowledge --retrieval-config bm25 \
    --retrieval-config-kwargs '{"top_k": 5}' \
    --tasks configs/banking_dev_tasks.json --agent baseline --num-tasks 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv()  # 从 .env 加载 API key（不覆盖已有环境变量）

    parser = argparse.ArgumentParser(description="TelecomOps-Agent evaluation runner")
    parser.add_argument(
        "--tasks",
        default="configs/dev_tasks.json",
        help="task 列表配置文件（默认: configs/dev_tasks.json）",
    )
    parser.add_argument(
        "--agent",
        default="baseline",
        help="agent 逻辑名（默认: baseline，见 agents/registry.py）",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="domain 名（覆盖 tasks 文件中的 domain 字段，如 telecom / banking_knowledge）",
    )
    parser.add_argument(
        "--retrieval-config",
        default=None,
        help="retrieval 变体名（banking_knowledge 用，如 bm25 / bm25_grep / no_knowledge）",
    )
    parser.add_argument(
        "--retrieval-config-kwargs",
        default=None,
        help="retrieval 配置覆盖参数，JSON 格式（如 '{\"top_k\": 10}'）",
    )
    parser.add_argument(
        "--num-tasks",
        type=int,
        default=None,
        help="只运行前 N 个 task（用于快速 smoke test）",
    )
    parser.add_argument(
        "--tag",
        default="v0",
        help="run tag（用于生成 run_id，如 v0 -> v0_20260825_160700）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="agent 模型（LiteLLM 格式，覆盖 env TELECOMOPS_AGENT_LLM）",
    )
    parser.add_argument(
        "--user-model",
        default=None,
        help="user simulator 模型（覆盖 env TELECOMOPS_USER_LLM）",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="每个 task 最大对话轮数（默认 60）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子（默认 42）",
    )
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="输出目录（默认: runs/）",
    )

    args = parser.parse_args()

    # 兼容：若只设置了 OPENAI_API_KEY 而没有 DEEPSEEK_API_KEY，且模型走 openai/ 前缀，
    # 则允许继续（key 校验下移，交给 litellm 按模型前缀决定）。
    # 若两个 key 都没有，报错。
    # key 校验：DEEPSEEK / OPENAI / ANTHROPIC 三选一（anthropic/ 前缀模型走 ANTHROPIC_API_KEY）
    has_key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not has_key:
        print(
            "错误: 未设置 DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY。"
            "请将 env.example 复制为 .env 并填写，或 export 对应 key=... 后重试。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 默认模型解析。可通过 CLI --model / env TELECOMOPS_AGENT_LLM 覆盖。
    # 优先级：
    #   1. 显式 --model
    #   2. env TELECOMOPS_AGENT_LLM
    #   3. 若设置了 ANTHROPIC_BASE_URL（如 api.b.ai）→ anthropic/deepseek-v4-flash
    #   4. 若设置了 OPENAI_API_KEY（OpenAI 兼容端点）→ openai/deepseek-v4-flash
    #   5. 兜底 deepseek/deepseek-chat
    default_agent_model = "deepseek/deepseek-chat"
    if (
        not args.model
        and not os.environ.get("TELECOMOPS_AGENT_LLM")
        and os.environ.get("ANTHROPIC_BASE_URL")
    ):
        default_agent_model = "anthropic/deepseek-v4-flash"
    elif (
        not args.model
        and not os.environ.get("TELECOMOPS_AGENT_LLM")
        and os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("DEEPSEEK_API_KEY")
    ):
        default_agent_model = "openai/deepseek-v4-flash"
    llm_agent = args.model or os.environ.get("TELECOMOPS_AGENT_LLM") or default_agent_model
    llm_user = args.user_model or os.environ.get("TELECOMOPS_USER_LLM") or llm_agent
    max_steps = args.max_steps or int(os.environ.get("TELECOMOPS_MAX_STEPS", "60"))
    seed = args.seed or int(os.environ.get("TELECOMOPS_SEED", "42"))

    tasks_file = Path(args.tasks)
    if not tasks_file.exists():
        print(f"错误: tasks 文件不存在: {tasks_file}", file=sys.stderr)
        sys.exit(1)

    # 解析 retrieval_config_kwargs（JSON 字符串 -> dict）
    retrieval_config_kwargs = None
    if args.retrieval_config_kwargs:
        try:
            retrieval_config_kwargs = json.loads(args.retrieval_config_kwargs)
        except json.JSONDecodeError as e:
            print(f"错误: --retrieval-config-kwargs 不是合法 JSON: {e}", file=sys.stderr)
            sys.exit(1)

    sys.path.insert(0, str(Path(__file__).parent))

    from eval.runner import run_eval

    summary = run_eval(
        tasks_file=tasks_file,
        agent_name=args.agent,
        domain=args.domain,
        retrieval_config=args.retrieval_config,
        retrieval_config_kwargs=retrieval_config_kwargs,
        llm_agent_model=llm_agent,
        llm_user_model=llm_user,
        max_steps=max_steps,
        seed=seed,
        run_tag=args.tag,
        runs_dir=Path(args.runs_dir),
        num_tasks=args.num_tasks,
    )

    print()
    print("=" * 60)
    print(f"Run complete: {summary['run_id']}")
    print(f"  Directory:  {summary['run_dir']}")
    print(f"  Domain:     {summary.get('domain')}")
    print(f"  Retrieval:  {summary.get('retrieval_config')}")
    print(f"  Total:      {summary['total_tasks']}")
    print(f"  Success:    {summary['success_count']}/{summary['total_tasks']} "
          f"({summary['success_rate']*100:.1f}%)")
    print(f"  Avg reward: {summary['average_reward']}")
    print(f"  Avg turns:  {summary['average_turns']}")
    print(f"  Avg tool:   {summary['average_tool_calls']}")
    if summary.get("average_retrieval_calls") is not None:
        print(f"  Avg retrieval calls: {summary['average_retrieval_calls']}")
        print(f"  Avg docs retrieved:  {summary['average_documents_retrieved']}")
        print(f"  Avg required doc recall: {summary.get('average_required_document_recall')}")
    print(f"  Total cost: ${summary['estimated_cost']:.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()