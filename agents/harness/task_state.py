"""Task State / Parameter Provenance（V2.2）。

一句话：记录"当前任务中已明确确认的值"，每个值带来源（user / tool_result /
knowledge）与业务归属（属于哪个工具的哪个参数 / 哪个实体）。Harness 的
TaskStateValidator 用它发现"与明确事实冲突"的调用——如用户说转 $500
Agent 填 550、前一个工具返回 card_id=12345 后面用错 ID。

职责边界（对照总原则）：
- 只存"明确确认的值"，不复制 conversation、不做开放式推理
- 值的判定保守：user 显式说的数字/枚举、tool result 的 ID 字段、
  KB 的明确约束——三类才入库
- 同一参数出现多个冲突来源时（user 改口等）：以最新为准，旧值保留
  历史（不拦用户改主意的场景）
- 与 Working Memory 的关系：TaskState 是"参数级"视图（供 harness 用），
  Memory 的 facts/constraints 是"叙事级"（供 agent 读）——两者共存，
  TaskState 由确定性提取器维护，不用 LLM

三类来源（user 的第 2 节）：
    user          —— 用户消息中显式提供的值（"$500"、"platinum"）
    tool_result   —— 业务工具返回的 ID/状态（card_id、account_id、user_id）
    knowledge    —— KB 明确约束（enum 集合/阈值/格式/条件映射）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 来源标签
SOURCE_USER = "user"
SOURCE_TOOL = "tool_result"
SOURCE_KNOWLEDGE = "knowledge"


@dataclass
class ProvenanceEntry:
    """一个已确认值 + 它的来源与业务归属。"""

    value: Any
    source: str                     # user / tool_result / knowledge
    param_key: str                  # 规范化 "tool/param"（裸 param 可无 tool）
    source_ref: Optional[str] = None  # 具体引用（tool 名 / doc_id / user 第 N 轮）
    seq: int = 0                    # 写入顺序（冲突时最新优先）


def norm_key(tool: str, param: str) -> str:
    t = (tool or "").strip().lower().replace("-", "_").replace(" ", "_")
    p = (param or "").strip().lower().replace("-", "_").replace(" ", "_")
    return f"{t}/{p}" if t else p


class TaskState:
    """参数级任务状态（provenance registry）。

    key = 规范化 "tool/param"（如 "close_card/reason"）或裸 param
    （"amount"——工具未定时）。每个 key 存按写入顺序的条目列表；
    判定取最新，但历史保留（供"用户改口"场景与 trace 观察）。
    """

    def __init__(self):
        self._entries: dict[str, list[ProvenanceEntry]] = {}
        self._seq = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record(self, param: str, value: Any, source: str,
               tool: str = "", source_ref: str = None) -> Optional[ProvenanceEntry]:
        """记录一个已确认值。静默跳过空值；返回新条目。"""
        if param is None or param == "" or value is None or value == "":
            return None
        self._seq += 1
        entry = ProvenanceEntry(
            value=value, source=source, param_key=norm_key(tool, param),
            source_ref=source_ref, seq=self._seq,
        )
        self._entries.setdefault(entry.param_key, []).append(entry)
        return entry

    # ------------------------------------------------------------------
    # 读取（harness 判定用）
    # ------------------------------------------------------------------
    def latest(self, tool: str, param: str) -> Optional[ProvenanceEntry]:
        """取该工具该参数的最新确认值（tool/param 精确 → 裸 param 回退）。"""
        for key in (norm_key(tool, param), norm_key("", param)):
            lst = self._entries.get(key)
            if lst:
                return lst[-1]
        return None

    def all_with_tool(self, tool: str) -> list[ProvenanceEntry]:
        """某工具名下全部参数条目（最新一条/参数）。"""
        prefix = norm_key(tool, "").rstrip("/") + "/"
        out = {}
        for key, lst in self._entries.items():
            if key.startswith(prefix) and lst:
                out[key[len(prefix):]] = lst[-1]
        return [v for v in out.values()]

    def snapshot(self) -> dict:
        """trace 用：{key: [{value, source, ref, seq}...]}（只留每 key 最近 3 条）。"""
        return {k: [{"value": e.value, "source": e.source,
                     "ref": e.source_ref, "seq": e.seq}
                    for e in v[-3:]]
                for k, v in self._entries.items()}

    def reset(self) -> None:
        self._entries.clear()
        self._seq = 0

    def __len__(self) -> int:
        return sum(len(v) for v in self._entries.values())


# ---------------------------------------------------------------------------
# 三类确定性提取器（把 conversation/工具流里的"明确值"灌入 TaskState）
# ---------------------------------------------------------------------------

# 用户消息里显式的数字（金额/数量）与"裸枚举词"
_USER_NUM_RE = re.compile(
    r"(?:\$|usd\s*)(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)


class UserValueExtractor:
    """从用户消息提取显式金额值（V2.3 收紧：只提"动作金额"，不提"状态金额"）。

    V2.2 教训（095 误拦）：用户说 "savings of $96,000"（账户余额）被绑到
    裸 amount——后续 interest_correction amount=100（正确值）被误拦。
    歧义原则（V2.3 第 5 条）：只有明确知道两个值指同一业务含义才强约束。

    收紧规则：金额必须出现在**动作语境**（transfer/pay/deposit/withdraw/
    send/credit/charge/refund/wire…）——余额/描述（have/has/of/with/
    balance/savings of/about）不提取。
    """

    # 动作动词窗口（金额前 40 字内出现才算动作金额）
    ACTION_VERBS = re.compile(
        r"(transfer|send|pay|deposit|withdraw|wire|charge|credit|refund|"
        r"move|apply|submit|request|increase|decrease|close.*with|pay.*off)",
        re.IGNORECASE)
    # 状态语境（金额前 20 字内出现则视为描述，跳过）
    STATE_MARKERS = re.compile(
        r"(balance|savings of|of about|have of|has about|worth|currently)",
        re.IGNORECASE)

    @classmethod
    def feed(cls, state: TaskState, user_text: str, turn_ref: str = "") -> None:
        if not user_text:
            return
        for m in _USER_NUM_RE.finditer(user_text):
            start = m.start()
            window = user_text[max(0, start - 40):start]
            verb = cls.ACTION_VERBS.search(window)
            if not verb:
                continue  # 无动作语境——不绑（宁可少记，不误绑）
            # 状态标记只当它出现在"动词与金额之间"（动词修饰的是这个金额）
            # 才算描述："balance ... $12k" 无动词 → 上面已跳过；
            # "balance $12k and pay $200"：对 $200 而言 balance 在动词前 → 是动作金额
            between = window[verb.end():]
            if cls.STATE_MARKERS.search(between):
                continue  # "of about" 紧贴金额（如 "savings of about $96,000"）→ 描述
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            state.record("amount", value, SOURCE_USER, source_ref=turn_ref)


class ToolResultExtractor:
    """从业务工具返回提取 ID/状态类值。

    工具结果常见模式（tau2 banking）：
        "Record ID: xxx" / "user_id: xxx" / "card_id: xxx" /
        "Account ID: xxx" / "Account: chk_xxx"
    这些是后续调用的必填引用——Agent 用错 ID 是 task_080 类失败的
    直接原因。提取为 (param=ID 值, source=tool_result, tool=来源工具)。
    """

    # 常见 ID 字段 → 规范参数名
    ID_FIELDS = {
        "user_id": "user_id",
        "account_id": "account_id",
        "card_id": "card_id",
        "record id": "record_id",
        "account": "account_id",
        "transaction_id": "transaction_id",
        "credit_card_account_id": "credit_card_account_id",
    }
    _PATTERNS = [
        # "user_id: xxx" / "Account ID: xxx"（冒号形式，含值尾部的 \n 或空格截止）
        (re.compile(r"(user_id|account_id|card_id|transaction_id|"
                     r"credit_card_account_id|record id|account)\s*[:=]\s*(\S+)",
                    re.IGNORECASE), True),
        # "Record ID: 6680a37184"（前缀 Record ID 专用）
        (re.compile(r"record\s+id\s*[:=]\s*(\S+)", re.IGNORECASE), False),
    ]

    @classmethod
    def feed(cls, state: TaskState, tool_name: str, result_text: str) -> None:
        """工具结果提取——记录为裸参数（不带工具前缀）。

        为什么裸参数：工具返回的 ID 是跨工具引用（A 工具查到 card_id、
        B 工具消费 card_id）——它们是系统级业务实体标识，不属于任何
        单一工具的参数空间。source_ref 记录来源工具供 trace 观测。
        """
        if not result_text:
            return
        text = result_text if len(result_text) < 4000 else result_text[:4000]
        for pattern, with_name in cls._PATTERNS:
            for m in pattern.finditer(text):
                if with_name:
                    raw_name, raw_val = m.group(1), m.group(2)
                else:
                    raw_name, raw_val = "record_id", m.group(1)
                param = cls.ID_FIELDS.get(raw_name.lower())
                val = raw_val.strip("',.;)")
                if param and val and len(val) < 80:
                    state.record(param, val, SOURCE_TOOL,
                                 tool="", source_ref=tool_name)
