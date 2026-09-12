"""Replay Harness（V6.0）—— 关键决策点的单轮决策重放。

动机（用户 V6 指令 §六）:
    改掉"每做一点改动就跑 24 个 task"的开发流程。利用已上传的
    Frozen V5 trace,截取"关键决策前"的 agent-visible state,只让
    模型做下一步决策——Level 1 评测（单轮决策 replay）。

Integrity 边界（用户指令 §六 的硬约束,单测断言）:
    - replay 输入只能使用当时 Agent 实际可见的信息:
        v1 trace 的 system_prompt + conversation(即 agent 可见记录)
        + v2 trace 的 agent 侧事件(工具结果/state)
    - 不泄漏 gold answer / evaluation_criteria / task notes /
      user_scenario instructions(用户侧剧本,agent 不可见)
    - 不读取 hidden tools(不调用 get_discoverable_tools)
    - expected_properties 只用于人审报告,不进 replay prompt

用法:
    # Level 0(本文件的单测覆盖):构建 + 不泄漏断言
    .venv/bin/python -m eval.replay --list
    .venv/bin/python -m eval.replay --build          # 构建 agent_view JSON
    .venv/bin/python -m eval.replay --run --llm MODEL  # Level 1(需用户批准)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# Frozen V5 Dev24 正式基线的两段 run(设计文档 §1)
V5_DEV_RUNS = [
    "runs/v5_dev24_20260904_173828",
    "runs/v5_dev24r_20260904_195336",
]

# replay 输入字段的**白名单**(integrity 断言的依据)
AGENT_VIEW_ALLOWED_KEYS = {
    "case_id", "task_id", "checkpoint_label", "cut_seq",
    "system_prompt", "messages", "tool_results",
    "v2_agent_events",
}

# 明确禁止出现在 replay 输入中的字段名(integrity 断言)
FORBIDDEN_INPUT_KEYS = {
    "evaluation_criteria", "gold", "gold_actions", "actions_gold",
    "notes", "user_scenario", "instructions", "reward", "env_api_call_sequences",
    "communicate_info", "annotations", "expected_properties",
}


@dataclass
class ReplayCase:
    """一个单轮决策重放点。"""
    case_id: str
    task_id: str
    checkpoint_label: str          # pre_recommendation / pre_mutation / ...
    cut_seq: Optional[int] = None  # v2 trace 里的截断 seq(最后一个 llm_call_end)
    run_dir: str = ""
    # 构建产物(agent-visible only)
    system_prompt: str = ""
    messages: list = field(default_factory=list)   # role/content 列表(脱壳)
    tool_results: list = field(default_factory=list)  # (tool_name, result) 摘要
    v2_agent_events: list = field(default_factory=list)  # agent 侧事件过滤

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "task_id": self.task_id,
            "checkpoint_label": self.checkpoint_label,
            "cut_seq": self.cut_seq,
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "tool_results": self.tool_results,
            "v2_agent_events": self.v2_agent_events,
        }


# ---------------------------------------------------------------------------
# case 定义(关键决策点选择,依据 docs/v6_design.md §7)
# ---------------------------------------------------------------------------
# label → (task_id, 定位策略)
# 定位策略(确定性,不读 gold):
#   pre_last_biz_mutation: 最后一次变更类工具调用(action_proposed 且
#       工具名匹配 mutation)前的最后一个 llm_call_end
#   pre_transfer: transfer_to_human_agents 调用前
#   mid_max_steps: max_steps 任务的中点(50% llm calls)
CASES = [
    # 失败任务的关键决策点(taxonomy 各类)
    ("054_pre_dispute", "task_054", "pre_mutation"),
    ("070_pre_open", "task_070", "pre_mutation"),
    ("100_pre_referral", "task_100", "pre_mutation"),
    ("026_phase2_mid", "task_026", "mid_max_steps"),
    ("077_pre_close", "task_077", "pre_mutation"),
    ("080_pre_dispute", "task_080", "pre_mutation"),
    # V5 成功任务(regression——V6 view 不得引入误导)
    ("010_first_query", "task_010", "pre_mutation"),
    ("021_pre_dispute", "task_021", "pre_mutation"),
    ("024_pre_recommend", "task_024", "pre_mutation"),
    ("037_pre_replace", "task_037", "pre_mutation"),
]


def _find_trace(task_id: str) -> Optional[dict]:
    """在两个 Frozen V5 run 里找 task 的 (v1, v2) trace 路径。

    优先选 v2 事件数多的（第一段 run 是环境故障中断的续跑前半段,
    其 v2 trace 可能只有头几个事件——选事件完整的那个）。
    """
    best = None
    for run in V5_DEV_RUNS:
        v1 = Path(run) / "traces" / f"{task_id}.json"
        v2 = Path(run) / "traces" / f"{task_id}.v2.json"
        if not v2.exists():
            continue
        try:
            v2d = json.loads(v2.read_text())
        except Exception:
            continue
        n_ev = len(v2d.get("events") or [])
        if n_ev < 20:   # 明显不完整(task_start 后中断)——跳过
            continue
        v1d = json.loads(v1.read_text()) if v1.exists() else {}
        cand = {"run": run, "v1": v1d, "v2": v2d, "n_events": n_ev}
        if best is None or n_ev > best["n_events"]:
            best = cand
    if best is None:
        return None
    return best


# 变更类工具(与 decision_checkpoint.should_trigger 同口径;独立实现避免
# 循环 import——replay 是 eval 侧工具)
import re as _re

_MUTATION_RE = _re.compile(
    r"^(order|close|file|submit|approve|deny|pay|apply|open|freeze|"
    r"unfreeze|activate|replace|transfer)", _re.I)


def _is_mutation_tool(name: str) -> bool:
    n = (name or "").lower()
    if n.startswith(("get_", "kb_", "read_", "write_plan", "update_plan",
                      "read_plan", "unlock_", "log_verification", "ask_")):
        return False
    return bool(_MUTATION_RE.search(n)) or n == "transfer_to_human_agents"


def _resolve_inner(event_args: dict, outer_name: str) -> str:
    """从 v2 事件参数解析 inner 工具名(wrapper 穿透)。"""
    if outer_name == "call_discoverable_agent_tool":
        a = event_args or {}
        for k in ("agent_tool_name", "tool_name"):
            if isinstance(a.get(k), str):
                return a[k]
    if outer_name == "give_discoverable_user_tool":
        a = event_args or {}
        if isinstance(a.get("discoverable_tool_name"), str):
            return "give:" + a["discoverable_tool_name"]
    return outer_name


def _locate_cut(v2: dict, strategy: str) -> Optional[int]:
    """按策略定位截断 seq(v2 事件流上的 llm_call_end)。"""
    evs = v2.get("events") or []
    llm_ends = [e for e in evs
                if e.get("event_type") == "llm_call_end"
                and e.get("actor") in ("agent", "decision_agent")]
    if not llm_ends:
        return None
    if strategy == "mid_max_steps":
        return llm_ends[len(llm_ends) // 2].get("seq")
    # pre_mutation / pre_transfer:最后一次变更类 tool_call_start 前
    # 的最后一个 agent llm_call_end
    target_seq = None
    for e in evs:
        if e.get("event_type") != "tool_call_start":
            continue
        if e.get("actor") not in ("environment", "retrieval"):
            continue
        inner = _resolve_inner(e.get("arguments") or {}, e.get("tool_name") or "")
        if _is_mutation_tool(inner):
            target_seq = e.get("seq")
            break
    if target_seq is None:
        # 无变更类调用 → 退化取中点(仍然 agent-visible,只是决策点不同)
        return llm_ends[len(llm_ends) // 2].get("seq")
    before = [e for e in llm_ends if (e.get("seq") or 0) < target_seq]
    return (before[-1].get("seq") if before
            else llm_ends[0].get("seq"))


def build_case(case_id: str, task_id: str, label: str) -> Optional[ReplayCase]:
    """从 Frozen V5 trace 构建一个 replay case。

    agent-visible 原则:v1 conversation 本身就是 agent 视角的记录
    (user/assistant/tool 消息),system_prompt 是当时 prompt——全部
    合法。cut_seq 之前的对话进 messages;工具结果摘要进 tool_results。
    **不含**:v1 trace 里的 task 字段(notes/instructions)、reward。
    """
    tr = _find_trace(task_id)
    if tr is None:
        return None
    v1, v2 = tr["v1"], tr["v2"]
    cut_seq = _locate_cut(v2, label)
    if cut_seq is None:
        return None
    case = ReplayCase(case_id=case_id, task_id=task_id,
                     checkpoint_label=label, cut_seq=cut_seq,
                     run_dir=tr["run"])
    case.system_prompt = v1.get("system_prompt") or ""
    # v1 conversation → messages(到 cut 为止:按 v2 事件的
    # 对应关系截断——统计 cut 前已开始的工具调用数 n_tools,
    # conversation 里前 n_tools 条 tool 消息对应它们,取到该点)
    evs = v2.get("events") or []
    n_tools = sum(1 for e in evs
                  if e.get("event_type") == "tool_call_start"
                  and (e.get("seq") or 0) <= cut_seq
                  and e.get("actor") == "environment")
    conv = v1.get("conversation") or []
    kept, tool_seen = [], 0
    for m in conv:
        role = m.get("role")
        if role == "tool":
            tool_seen += 1
            if tool_seen > n_tools:
                break
            kept.append({"role": "tool",
                         "name": m.get("name") or "",
                         "content": (m.get("content") or "")[:600]})
        elif role in ("user", "assistant"):
            kept.append({"role": role,
                         "content": (m.get("content") or "")[:800]})
    case.messages = kept
    # tool results 摘要(cut 前的 DecisionAgent 循环内工具调用——
    # 仅 environment actor;KA 检索在独立 context,不进 DA 视图)
    for e in evs:
        if (e.get("event_type") == "tool_call_end"
                and (e.get("seq") or 0) <= cut_seq
                and e.get("actor") == "environment"):
            name = e.get("tool_name") or ""
            case.tool_results.append({"tool": name,
                                       "success": e.get("success")})
        if len(case.tool_results) >= 40:
            break
    # v2 agent 侧事件(plan/state——V6 状态重建的素材,不进 prompt 本体)
    for e in evs:
        if (e.get("seq") or 0) > cut_seq:
            break
        if e.get("event_type") in ("plan_written", "plan_updated",
                                   "plan_step_progressed",
                                   "state_write", "state_update",
                                   "action_proposed", "action_rejected"):
            case.v2_agent_events.append(
                {k: e.get(k) for k in ("seq", "event_type", "tool_name",
                                       "object", "field", "new_value",
                                       "result")})
        if len(case.v2_agent_events) >= 120:
            case.v2_agent_events = case.v2_agent_events[-120:]
    return case


# ---------------------------------------------------------------------------
# Integrity 断言(Level 0 单测调用)
# ---------------------------------------------------------------------------
def assert_case_integrity(case: ReplayCase) -> None:
    """replay case 的输入白名单/禁词断言(不满足直接 raise)。

    断言:
    1. to_dict() 的键 ⊆ AGENT_VIEW_ALLOWED_KEYS
    2. 任何禁词字段不存在
    3. messages/system_prompt/tool_results 内不含 gold/evaluation
       标记文本(v1 task 字段本来就没复制进来——这是结构保证,
       断言是防回归)
    """
    d = case.to_dict()
    for k in d:
        assert k in AGENT_VIEW_ALLOWED_KEYS, \
            f"replay case 泄漏非白名单字段: {k}"
    for k in FORBIDDEN_INPUT_KEYS:
        assert k not in d, f"replay case 含禁止字段: {k}"
    blob = json.dumps(d, ensure_ascii=False).lower()
    for marker in ("evaluation_criteria", "gold_actions",
                   "env_api_call_sequences", "user_scenario"):
        assert marker not in blob, f"replay case 输入含泄漏标记: {marker}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_case_defs() -> list:
    return [(cid, tid, label) for cid, tid, label in CASES]


def main(argv=None):
    ap = argparse.ArgumentParser("eval.replay")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--build", action="store_true",
                    help="构建 replay cases 并落盘 configs/banking_v6_replay.json")
    ap.add_argument("--run", action="store_true",
                    help="Level 1: 单轮决策 replay(需 --llm;会调用付费 API)")
    ap.add_argument("--llm", default=None)
    ap.add_argument("--repeat", type=int, default=3,
                    help="每个 case 的重复次数(观察随机性)")
    ap.add_argument("--out", default="runs/v6_replay",
                    help="Level 1 输出目录")
    args = ap.parse_args(argv)

    if args.list:
        for cid, tid, label in load_case_defs():
            print(f"{cid:24s} {tid:12s} {label}")
        return 0

    if args.build:
        cases = []
        skipped = []
        for cid, tid, label in load_case_defs():
            case = build_case(cid, tid, label)
            if case is None:
                skipped.append(cid)
                continue
            assert_case_integrity(case)
            cases.append(case.to_dict())
        out = {"source_runs": V5_DEV_RUNS, "protocol":
               "agent-visible-only; no gold/notes/scenario leakage",
               "cases": cases}
        Path("configs").mkdir(exist_ok=True)
        Path("configs/banking_v6_replay.json").write_text(
            json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"built {len(cases)} cases ({len(skipped)} skipped: {skipped})")
        print("-> configs/banking_v6_replay.json")
        return 0

    if args.run:
        if not args.llm:
            print("Level 1 replay 需要显式 --llm <model>(付费 API,需用户批准)")
            return 1
        print("Level 1 replay 尚未获批准执行(用户指令 §十一:在我明确允许之前"
              "不要调用付费 API)。构建产物已就绪,请先审阅 "
              "configs/banking_v6_replay.json。")
        return 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
