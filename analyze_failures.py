#!/usr/bin/env python3
"""
失败分析工具：读取一次 eval run 的结果 + 人工标注（failure_labels.json），
输出失败数 / 类型分布 / 百分比 / failed task IDs，并可选导出 CSV。

用法：
  python analyze_failures.py runs/v0_20260825_160700
  python analyze_failures.py runs/v0_20260825_160700 --labels failure_labels.json
  python analyze_failures.py runs/v0_20260825_160700 --labels failure_labels.json --csv failures.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from eval.taxonomy import FAILURE_TYPES, validate_failure_type


def load_results(run_dir: Path) -> tuple[list[dict], dict | None]:
    """读取 run 目录下的 results.json / summary.json。"""
    results_path = run_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"未找到 {results_path}（该目录不是一次 eval run 的输出？）")
    results = json.loads(results_path.read_text(encoding="utf-8"))
    summary = None
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return results, summary


def load_labels(labels_path: Path) -> dict:
    """读取人工标注，返回 {task_id: (failure_type, notes)}。

    支持两种格式：
      - {"run_id": ..., "labels": {task_id: type | {failure_type, notes}}}
      - 直接 {task_id: type | {failure_type, notes}}
    """
    if not labels_path.exists():
        return {}
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "labels" in data:
        data = data["labels"]
    if not isinstance(data, dict):
        raise ValueError(f"failure_labels.json 格式不正确: {labels_path}")
    out = {}
    for tid, val in data.items():
        if isinstance(val, str):
            out[tid] = (val, "")
        elif isinstance(val, dict):
            ftype = val.get("failure_type")
            if not ftype:
                raise ValueError(f"task {tid} 的标注缺少 failure_type")
            out[tid] = (ftype, val.get("notes", ""))
        else:
            raise ValueError(f"task {tid} 的标注格式不正确")
    return out


def analyze(run_dir: Path, labels_path: Path | None, csv_path: Path | None) -> int:
    results, summary = load_results(run_dir)
    labels = load_labels(labels_path) if labels_path else {}

    failed = [r for r in results if not r["success"]]
    total_failed = len(failed)

    # 为每个失败任务分配类型
    distribution = {t: 0 for t in FAILURE_TYPES}
    for r in failed:
        tid = r["task_id"]
        ftype, notes = labels.get(tid, ("unknown", ""))
        validate_failure_type(ftype)
        r["failure_type"] = ftype
        r["notes"] = notes
        distribution[ftype] += 1

    # ---- 输出 ----
    print(f"Run:          {run_dir}")
    if summary:
        print(f"Run ID:       {summary.get('run_id')}")
        print(f"Agent:        {summary.get('agent')}")
        print(f"Total tasks:  {summary.get('total_tasks')}")
    print(f"Failed tasks: {total_failed}")
    print()

    print("Failure distribution:")
    print(f"  {'type':<22} {'count':>5} {'pct':>7}   bar")
    print("  " + "-" * 46)
    for t in FAILURE_TYPES:
        count = distribution[t]
        pct = count / total_failed * 100 if total_failed else 0.0
        bar = "#" * int(pct / 2)
        print(f"  {t:<22} {count:>5} {pct:>6.1f}%  {bar}")

    print()
    print("Failed task IDs:")
    if failed:
        for r in failed:
            print(f"  [{r['failure_type']:<18}] {r['task_id']}")
    else:
        print("  (无失败任务)")

    # ---- CSV ----
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "task_id", "failure_type", "notes", "reward",
                    "termination_reason", "turns", "tool_calls",
                ],
            )
            writer.writeheader()
            for r in failed:
                writer.writerow(
                    {
                        "task_id": r["task_id"],
                        "failure_type": r.get("failure_type", "unknown"),
                        "notes": r.get("notes", ""),
                        "reward": r.get("reward"),
                        "termination_reason": r.get("termination_reason"),
                        "turns": r.get("turns"),
                        "tool_calls": r.get("tool_calls"),
                    }
                )
        print(f"\nCSV 已导出: {csv_path}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="分析一次 eval run 的失败分布")
    parser.add_argument("run_dir", help="run 目录，如 runs/v0_20260825_160700")
    parser.add_argument(
        "--labels",
        default="failure_labels.json",
        help="人工标注文件（默认: failure_labels.json）",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="导出失败明细到 CSV（默认不导出）",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        print(f"错误: run 目录不存在: {run_dir}", file=sys.stderr)
        return 1

    labels_path = Path(args.labels) if args.labels else None
    csv_path = Path(args.csv) if args.csv else None

    try:
        return analyze(run_dir, labels_path, csv_path)
    except (ValueError, FileNotFoundError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
