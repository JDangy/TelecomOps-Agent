#!/usr/bin/env python3
"""把一次 run 的 traces 导出为可读的 Markdown 文档。

用法:
    python export_trace_readme.py runs/<run_id>                    # 机器版（默认）
    python export_trace_readme.py runs/<run_id> --human            # 人类阅读版（推荐）
    python export_trace_readme.py runs/<run_id> -o out.md          # 指定输出文件
    python export_trace_readme.py runs/<run_id> --include-kb-content  # 展开检索文档全文

两种输出模式:
  - 默认（机器版）: 消息编号 + 工具调用 JSON 参数 + 检索返回的文档 ID 列表，
    适合程序化处理/逐条核对。
  - --human（人类版）: 聊天流式排版（👤用户/🤖客服），工具调用转为自然语言描述，
    检索返回展示文档标题而非 ID，末尾附客观"观察"区。适合人工通读/失败分析。

说明: 该文档用于人工阅读/失败分析，不进入 summary.json 指标计算。
"""
from __future__ import annotations

import argparse
import json
import re
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


def doc_titles_from_content(content: str) -> list[str]:
    """从 KB_search 返回文本中提取文档标题。

    检索返回格式为 'N. Title\\n   ID: doc_xxx\\n   Score: ...'——标题行是
    紧跟在 ID: 行前面的那一行。按行扫描，找到 ID: 行则取其上一行作为标题，
    避免把文档正文里的 '2. xxx' 列表误判为标题。
    """
    lines = content.split("\n")
    titles = []
    for i, line in enumerate(lines):
        if line.strip().startswith("ID:"):
            # 标题在 ID 行的上一行（通常形如 '1. Title'）
            prev = lines[i - 1].strip() if i > 0 else ""
            m = re.match(r"^\d+\.\s+(.+)$", prev)
            if m:
                titles.append(m.group(1).strip())
    return titles


# ---------------------------------------------------------------------------
# 机器版渲染
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 人类阅读版渲染
# ---------------------------------------------------------------------------
def render_task_human(t: dict) -> list[str]:
    tid = t["task_id"]
    r = t["reward"]
    tag = "SUCCESS" if r >= 1.0 else "FAIL"
    ok = r >= 1.0
    retr = t.get("retrieval") or {}
    task = t.get("task") or {}

    out = [f"\n{'─' * 60}"]
    result_icon = "✅" if ok else "❌"
    out.append(f"## {tid} ｜ {result_icon} {'成功' if ok else '失败'} ｜ reward={r}")
    out.append(f"{'─' * 60}")

    # 任务背景（客观事实：用户角色设定）
    us = task.get("user_scenario") or {}
    instructions = (us.get("instructions") or "").strip()
    if instructions:
        out.append(f"\n**任务背景（用户角色设定）**")
        out.append(f"> {instructions}")
    elif task.get("description"):
        out.append(f"\n**任务背景**")
        out.append(f"> {json.dumps(task['description'], ensure_ascii=False)}")

    # 聊天流
    out.append(f"\n**对话过程**")
    n_assistant_msgs = 0
    n_user_msgs = 0
    last_user_txt = ""
    for m in t["conversation"]:
        role = m["role"]
        if role == "user":
            txt = m.get("content", "") or ""
            last_user_txt = txt
            n_user_msgs += 1
            out.append(f"\n👤 **用户**:")
            for line in txt.split("\n"):
                out.append(f"> {line}" if line.strip() else ">")
        elif role == "assistant":
            txt = m.get("content") or ""
            tc = m.get("tool_calls") or []
            if tc:
                n_assistant_msgs += 1
                # 自然语言描述工具动作
                for c in tc:
                    name = c.get("name") or "(unknown)"
                    args = c.get("arguments") or {}
                    if not isinstance(args, dict):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if name in ("KB_search", "KB_search_bm25", "KB_search_dense"):
                        q = args.get("query", "")
                        out.append(f"\n🤖 **客服** 在知识库中检索了：\n\n  🔍 「{q}」")
                    elif name == "grep":
                        pat = args.get("pattern", args.get("query", ""))
                        out.append(f"\n🤖 **客服** 用 grep 搜索了：\n\n  🔍 模式 `{pat}`")
                    elif name in ("get_user_information_by_name", "get_user_information_by_id",
                                  "get_user_information_by_email"):
                        out.append(f"\n🤖 **客服** 查找了用户资料：`{name}({args})`")
                    elif name in ("unlock_discoverable_agent_tool", "call_discoverable_agent_tool",
                                  "give_discoverable_user_tool"):
                        out.append(f"\n🤖 **客服** 操作了内部工具：`{name}({args})`")
                    else:
                        out.append(f"\n🤖 **客服** 调用了 `{name}({args})`")
                if txt:
                    out.append(f"\n🤖 **客服**:")
                    for line in txt.split("\n"):
                        out.append(f"> {line}" if line.strip() else ">")
        elif role == "tool":
            name = m.get("name") or "(unknown)"
            content = m.get("content", "") or ""
            if name in RETRIEVAL_TOOLS:
                titles = doc_titles_from_content(content)
                if titles:
                    shown = titles[:6]
                    more = f"（共 {len(titles)} 篇，显示前 {len(shown)} 篇）" if len(titles) > len(shown) else ""
                    out.append(f"\n   ↳ 检索返回 {len(titles)} 篇文档{more}：\n"
                               + "\n".join(f"     · {x}" for x in shown))
            else:
                out.append(f"\n   ↳ `{name}` 返回：{content[:TOOL_RESULT_MAX]}")

    # 客观观察区
    out.append(f"\n**观察**")
    out.append(f"- 对话轮数：用户 {n_user_msgs} 次发言 / 客服 {n_assistant_msgs} 次动作")
    out.append(f"- 检索调用：{retr.get('total_calls', 0)} 次")
    if not ok and last_user_txt:
        final = last_user_txt.strip().replace("\n", " ")[:120]
        out.append(f"- 最后一条用户消息：{final}")
    out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="run 目录（含 traces/）")
    ap.add_argument("-o", "--output", help="输出文件（默认 <run_dir>/TRACES_README.md）")
    ap.add_argument("--human", action="store_true",
                    help="输出人类阅读版（聊天流排版，推荐人工通读）")
    ap.add_argument("--include-kb-content", action="store_true",
                    help="展开检索返回的文档全文（默认只列文档 ID/标题）")
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
    out.append(f"# {run_dir.name} — {'人类阅读版' if args.human else '完整 Trace 阅读版'} Trace")
    if args.human:
        out.append(f"\n> 共 {len(trace_files)} 个任务。由 `export_trace_readme.py --human` 生成；"
                   f"对话按轮次排版，检索结果展示文档标题。")
    else:
        out.append(f"\n> 共 {len(trace_files)} 个任务。由 `export_trace_readme.py` 生成；"
                   f"检索结果的文档全文默认省略，仅列文档 ID；"
                   f"需要全文请加 `--include-kb-content` 重新生成。")

    for tp in trace_files:
        try:
            t = json.loads(tp.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"跳过 {tp.name}: {exc}", file=sys.stderr)
            continue
        if args.human:
            out.extend(render_task_human(t))
        else:
            out.extend(render_task(t, args.include_kb_content))

    suffix = "_human.md" if args.human else "_README.md"
    output = Path(args.output) if args.output else run_dir / f"TRACES{suffix}"
    output.write_text("\n".join(out), encoding="utf-8")
    print(f"已生成: {output} ({output.stat().st_size / 1024:.0f} KB, {len(trace_files)} 个任务)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
