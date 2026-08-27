#!/usr/bin/env python3
"""
版本对比工具：对比两次 eval run（如 V0 baseline 与 V1），
输出 success rate / average reward / average turns / average tool calls 的差异，
并列出逐任务的转好 / 转坏。

用法：
  python compare_runs.py runs/v0_20260825_160700 runs/v1_20260825_180000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_run(run_dir: Path) -> dict:
    """读取 run 目录下的 summary.json + results.json。"""
    summary_path = run_dir / "summary.json"
    results_path = run_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"未找到 {results_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    results = json.loads(results_path.read_text(encoding="utf-8"))
    return {"summary": summary, "results": results}


def get(run: dict, key: str):
    """从 summary 安全取值（兼容缺失字段）。"""
    return run["summary"].get(key) if run["summary"] else None


def fmt(v, nd=4):
    if v is None:
        return "N/A"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}"
    return str(v)


def compare(old_dir: Path, new_dir: Path) -> int:
    old = load_run(old_dir)
    new = load_run(new_dir)

    old_by_id = {r["task_id"]: r for r in old["results"]}
    new_by_id = {r["task_id"]: r for r in new["results"]}

    # 汇总指标对比（优先用各自 summary 的聚合值，保证口径一致）
    # 若两次 run 用了不同 retrieval config，追加对比 retrieval 指标
    old_rc = get(old, "retrieval_config")
    new_rc = get(new, "retrieval_config")
    metrics = [
        ("success_rate", "Success rate"),
        ("average_reward", "Avg reward"),
        ("average_turns", "Avg turns"),
        ("average_tool_calls", "Avg tool calls"),
        ("estimated_cost", "Est. cost ($)"),
    ]
    # 只要有一个 run 有 retrieval 数据，就对比 retrieval 指标
    if old_rc is not None or new_rc is not None or get(old, "average_retrieval_calls") is not None:
        metrics += [
            ("average_retrieval_calls", "Avg retrieval calls"),
            ("average_documents_retrieved", "Avg docs retrieved"),
            ("average_documents_per_call", "Avg docs/call"),
            ("average_required_document_recall", "Avg req-doc recall"),
        ]
        for k in (1, 3, 5, 10):
            metrics.append((f"average_hit_at_{k}", f"Avg hit@{k}"))

    print(f"Compare: {old_dir}  vs  {new_dir}")
    print(f"  {old_dir}  (old): run_id={get(old, 'run_id')}  domain={get(old, 'domain')}  "
          f"retrieval={old_rc}  tasks={get(old, 'total_tasks')}")
    print(f"  {new_dir}  (new): run_id={get(new, 'run_id')}  domain={get(new, 'domain')}  "
          f"retrieval={new_rc}  tasks={get(new, 'total_tasks')}")
    print()

    # 指标方向：success/reward/hit@k/recall 上升好；turns/tool_calls/cost/retrieval_calls 下降好
    lower_is_better = {
        "average_turns", "average_tool_calls", "estimated_cost",
        "average_retrieval_calls", "average_documents_retrieved", "average_documents_per_call",
    }
    print("  {:<26} {:>12} {:>12} {:>14}".format("metric", "old", "new", "delta"))
    print("  " + "-" * 68)
    for key, label in metrics:
        ov = get(old, key)
        nv = get(new, key)
        delta = ""
        if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            d = nv - ov
            sign = "+" if d > 0 else ""
            good = (d < 0) if key in lower_is_better else (d > 0)
            bad = (d > 0) if key in lower_is_better else (d < 0)
            mark = "  ✓" if good else ("  ✗" if bad else "")
            delta = f"{sign}{d:.4f}{mark}"
        print(f"  {label:<26} {fmt(ov):>12} {fmt(nv):>12} {delta:>14}")

    print()

    # 逐任务转好 / 转坏
    improved = []   # old fail -> new success
    regressed = []  # old success -> new fail
    changed_reward = []

    for tid, new_r in new_by_id.items():
        old_r = old_by_id.get(tid)
        if old_r is None:
            continue
        if not old_r["success"] and new_r["success"]:
            improved.append(tid)
        if old_r["success"] and not new_r["success"]:
            regressed.append(tid)
        if old_r["reward"] is not None and new_r["reward"] is not None and old_r["reward"] != new_r["reward"]:
            changed_reward.append((tid, old_r["reward"], new_r["reward"]))

    print(f"old fail → new success (转好): {len(improved)}")
    for tid in improved:
        print(f"    ✓ {tid}")
    if not improved:
        print("    (无)")

    print()
    print(f"old success → new fail (转坏): {len(regressed)}")
    for tid in regressed:
        print(f"    ✗ {tid}")
    if not regressed:
        print("    (无)")

    if changed_reward:
        print()
        print("Reward 变化但成功状态未变的 task:")
        for tid, ov, nv in changed_reward:
            print(f"    {tid:<60} {ov:.2f} -> {nv:.2f}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="对比两次 eval run")
    parser.add_argument("old_run", help="旧 run 目录（如 V0），如 runs/v0_xxx")
    parser.add_argument("new_run", help="新 run 目录（如 V1），如 runs/v1_xxx")
    args = parser.parse_args()

    old_dir = Path(args.old_run)
    new_dir = Path(args.new_run)
    for d in (old_dir, new_dir):
        if not d.exists():
            print(f"错误: 目录不存在: {d}", file=sys.stderr)
            return 1

    try:
        return compare(old_dir, new_dir)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
