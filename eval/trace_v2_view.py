#!/usr/bin/env python3
"""Trace v2 查看工具：execution tree + timing breakdown。

用法:
    python eval/trace_v2_view.py runs/<run_id>/traces/task_xxx.v2.json
    python eval/trace_v2_view.py runs/<run_id>            # 查看整个 run 的汇总
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def view_task(v2: dict) -> dict:
    """打印单个任务的 execution tree 与 timing breakdown，返回 timing 摘要。"""
    events = v2["events"]
    print(f"═══ {v2['task_id']} | {len(events)} events | trace v{v2['trace_version']} ═══")
    print()

    # start/end 配对：query 等调用参数在 start 事件，latency 在 end 事件
    starts = {e["span_id"]: e for e in events if e["event_type"].endswith("_start")}
    llm_ms = user_ms = retr_ms = tool_ms = 0.0
    n_agent = n_user = n_retr = n_tool = 0
    for e in events:
        t = e["event_type"]
        if t == "llm_call_end":
            ms = e.get("latency_ms") or 0
            if e["actor"] == "agent":
                llm_ms += ms; n_agent += 1
            else:
                user_ms += ms; n_user += 1
            print(f"  LLM[{e['actor']:<14}] {ms:>9.1f}ms tokens={e.get('prompt_tokens')}+{e.get('completion_tokens')} finish={e.get('finish_reason')} tcalls={e.get('n_tool_calls')}")
        elif t == "llm_call_error":
            print(f"  LLM_ERR[{e['actor']}] {e.get('latency_ms')}ms {e.get('error_type')} rate_limit={e.get('rate_limit')}")
        elif t == "tool_call_end":
            ms = e.get("latency_ms") or 0
            st = starts.get(e["span_id"], {})
            if e["actor"] == "retrieval":
                retr_ms += ms; n_retr += 1
                q = (st.get("query") or "")[:45]
                print(f"  RETRIEVAL        {ms:>9.1f}ms {e['tool_name']} docs={len(e.get('doc_ids') or [])} q='{q}'")
            else:
                tool_ms += ms; n_tool += 1
                print(f"  TOOL             {ms:>9.1f}ms {e['tool_name']}")
        elif t == "rate_limit_wait":
            print(f"  RL_WAIT          {e.get('wait_seconds')}s (attempt {e.get('attempt')})")

    s = v2.get("summary") or {}
    wall = s.get("task_wall_seconds")
    print()
    if wall:
        rl = s.get("rate_limit_wait_seconds") or 0
        other = wall - (llm_ms + user_ms + retr_ms + tool_ms) / 1000 - rl
        print(f"Task wall time: {wall:.1f}s")
        print(f"  Agent LLM:       {llm_ms/1000:>7.1f}s ({n_agent} calls)")
        print(f"  User Sim LLM:    {user_ms/1000:>7.1f}s ({n_user} calls)")
        print(f"  Retrieval:       {retr_ms/1000:>7.1f}s ({n_retr} calls)")
        print(f"  Business tools:  {tool_ms/1000:>7.1f}s ({n_tool} calls)")
        print(f"  Rate-limit wait: {rl:>7.1f}s")
        print(f"  Other (env/eval):{other:>7.1f}s")
    print()
    return {
        "task_id": v2["task_id"],
        "wall": wall,
        "agent_llm_s": round(llm_ms / 1000, 1),
        "user_llm_s": round(user_ms / 1000, 1),
        "retrieval_s": round(retr_ms / 1000, 2),
        "tools_s": round(tool_ms / 1000, 2),
        "rl_wait_s": rl if wall else None,
        "n_llm": n_agent + n_user,
    }


def view_run(run_dir: str) -> None:
    """查看整个 run 的 v2 traces 汇总。"""
    p = Path(run_dir) / "traces"
    files = sorted(p.glob("*.v2.json"))
    if not files:
        print(f"{p} 下没有 .v2.json 文件")
        return
    walls, rows = [], []
    for f in files:
        v2 = json.loads(f.read_text(encoding="utf-8"))
        rows.append(view_task(v2))
        walls.append(v2.get("summary", {}).get("task_wall_seconds") or 0)
    print("═══ Run 汇总 ═══")
    for r in rows:
        print(f"  {r['task_id']:<12} wall={r['wall']:>6.1f}s agent_llm={r['agent_llm_s']:>6.1f}s user_llm={r['user_llm_s']:>6.1f}s retr={r['retrieval_s']:>5.2f}s llm_calls={r['n_llm']}")
    if walls:
        print(f"\n  平均 wall: {sum(walls)/len(walls):.1f}s | 总: {sum(walls):.1f}s")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    target = Path(sys.argv[1])
    if target.is_dir():
        view_run(str(target))
    elif target.suffix == ".json":
        view_task(json.loads(target.read_text(encoding="utf-8")))
    else:
        print(f"不支持的参数: {target}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
