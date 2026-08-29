#!/usr/bin/env python3
"""把一次 run 的 traces 导出为完整可读的 Markdown 文档。

用法:
    python export_trace_readme.py runs/<run_id>              # 导出到 run 目录下
    python export_trace_readme.py runs/<run_id> -o out.md    # 导出到指定文件

输出内容:
  - 每个任务的完整对话（user/assistant 全文不截断）
  - 每次工具调用的名称与参数
  - 检索调用（KB_search 等）的 query 与返回的文档 ID 列表
    （检索结果的文档全文太长，默认省略；加 --include-kb-content 才展开）

说明: 该文档用于人工阅读/失败分析，不进入 summary.json 指标计算。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 检索类工具：结果可能非常长（整篇文档全文）
RETRIEVAL_TOOLS = {"KB_search", "KB_search_bm25", "KB_search_dense", "grep"}
TOOL_RESULT_MAX = 500  # 非检索工具结果的截断长度


def doc_ids_from_content(content: str) -> list[str]:
    """从 KB_search 返回文本中提取文档 ID（格式: '   ID: doc_xxx'）。"""
    ids = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("ID:"):
            ids.append(line[3:].strip())
    return ids


def render_task(t: dict, include_kb_content: bool) -> list[str]:
    tid = t["task_id"]
    r = t["reward"]
    tag = "SUCCESS" if r >= 1.0 else "FAIL"
    retr = t.get("retrieval") or {}
    out = [
        f"\n{'#' * 72}",
        f"# {tid} | reward={r} {tag} | termination={t.get('termination_reason')}",
        f"# retrieval_calls={retr.get('total_calls', 0)}",
        f"{'#' * 72}",
    ]

    # 任务描述与 user_scenario（如果有）
    task = t.get("task") or {}
    if task.get("description"):
        out.append(f"\n## 任务描述\n```json\n{json.dumps(task['description'], ensure_ascii=False, indent=1)}\n```")
    if task.get("user_scenario"):
        us = task["user_scenario"]
        if us.get("instructions"):
            out.append(f"\n## 用户角色设定 (user_scenario.instructions)\n> {us['instructions']}")

    out.append("\n## 对话记录")
    for i, m in enumerate(t["conversation"]):
        role = m["role"]
        out.append(f"\n--- 消息#{i} [{role}] ---")
        if role == "user":
            out.append(m.get("content", "") or "(空)")
        elif role == "assistant":
            txt = m.get("content") or ""
            if txt:
                out.append(txt)
            for c in m.get("tool_calls") or []:
                name = c.get("name") or "(unknown)"
                args = c.get("arguments")
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args_str = str(args)
                out.append(f"  ▶ 调用工具: **{name}**")
                out.append(f"    参数: {args_str[:400]}")
        elif role == "tool":
            name = m.get("name") or "(unknown)"
            content = m.get("content", "") or ""
            if name in RETRIEVAL_TOOLS:
                ids = doc_ids_from_content(content)
                out.append(f"  (检索结果 {name}: 返回 {len(ids)} 个文档: {', '.join(ids[:10])}"
                           f"{' ...' if len(ids) > 10 else ''})")
                if include_kb_content:
                    out.append(f"  ```\n{content[:4000]}\n```")
            else:
                out.append(f"  {name} 返回: {content[:TOOL_RESULT_MAX]}")

    # 检索记录汇总
    calls = retr.get("calls") or []
    if calls:
        out.append("\n## 检索记录")
        for ci, c in enumerate(calls, 1):
            out.append(f"  #{ci} {c.get('name','KB_search')} query={c.get('query')!r} "
                       f"top_k={c.get('top_k')} -> {c.get('num_docs')} 个文档")
    out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="run 目录（含 traces/）")
    ap.add_argument("-o", "--output", help="输出文件（默认 <run_dir>/TRACES_README.md）")
    ap.add_argument("--include-kb-content", action="store_true",
                    help="展开检索返回的文档全文（默认只列文档 ID）")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    traces_dir = run_dir / "traces"
    if not traces_dir.is_dir():
        print(f"错误: 找不到 {traces_dir}", file=sys.stderr)
        return 1

    trace_files = sorted(
        traces_dir.glob("*.json"),
        key=lambda p: p.stem,  # task_001, task_002, ...
    )
    if not trace_files:
        print(f"错误: {traces_dir} 下没有 trace 文件", file=sys.stderr)
        return 1

    out = []
    out.append(f"# {run_dir.name} — 完整 Trace 阅读版")
    out.append(f"\n> 由 `export_trace_readme.py` 生成，共 {len(trace_files)} 个任务。"
               f"检索结果的文档全文默认省略，仅列文档 ID；"
               f"需要全文请加 `--include-kb-content` 重新生成。")

    for tp in trace_files:
        try:
            t = json.loads(tp.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"跳过 {tp.name}: {exc}", file=sys.stderr)
            continue
        out.extend(render_task(t, args.include_kb_content))

    output = Path(args.output) if args.output else run_dir / "TRACES_README.md"
    output.write_text("\n".join(out), encoding="utf-8")
    print(f"已生成: {output} ({output.stat().st_size / 1024:.0f} KB, {len(trace_files)} 个任务)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
