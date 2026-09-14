"""Structured Task State（V3）——对象级任务状态。

V2.2/V2.3 的 TaskState 是参数级（amount / card_id 裸键）——V3 痛点：
一个任务里同时有 transfer amount、transaction amount、多张卡的 id，
裸字段互相混淆（095 误拦：余额 96k 绑到 correction amount）。

V3 核心改变：状态有"属于谁"的概念——

    transfer_request.amount = 500     (source=user)
    refund_request.amount = 100       (source=tool)
    account_sav_lm83.balance = 96000  (source=tool)
    card_dbc_12345.status = ACTIVE    (source=tool)
    transfer.reason.allowed_values = [<option_a>, <option_b>, <option_c>]
                                      (source=knowledge)

数据模型（每条）：
    object    —— 业务对象（request 请求 / 实体 ID 对象 / 规则命名空间）
    field     —— 对象的字段（amount / id / status / allowed_values …）
    value     —— 值
    source    —— user / tool / knowledge（这条记录是怎么来的）
    fact_type —— 这条记录属于什么类型的事实（V6 收紧新增,见下）
    source_ref —— 具体引用（工具名 / doc_id / user 轮次）
    seq       —— 写入顺序

fact_type（事实类型——V6 架构收紧的核心字段）:
    system_fact      系统里的客观状态（余额、开户日期、卡状态、当前日期）
    user_statement   用户口头陈述（"大概四个月"、"我说过要转 500"）
    kb_rule          知识库业务规则（enum 集合、阈值、格式约束）
    user_preference  用户的意愿/偏好（要不要加急、想选哪个方案、怎么称呼）

    authority（权威来源）由 fact_type 决定,不做全局 source 排名:
        system_fact     → 系统工具最可信（用户说错以工具为准）
        kb_rule         → KB 最可信
        user_preference → 用户本人最可信（系统无权代答）
        user_statement  → 默认 unverified,待工具/KB 对齐
    这正是"账户开户时间问工具、业务规则问 KB、想不想加急问用户"的
    通用化——由【写入方】按提取规则声明事实类型,查询方按类型表判定,
    任何模块都不做全局 provenance 排名,也不做 task/domain 硬编码。

Supersede 语义（用户改口）：
    同一 (object, field) 重复写入 → 历史保留（trace 可查），
    判定只看最新条目——旧值不再拦 Agent。

查询分三类（关键设计——避免裸字段混淆又不过度复杂）：
    1) exact:      (object, field) 精确命中（最可信）
    2) by-entity:   field 在实参里是 ID 类（card_id）→ 先做实体解析
                   （proposed 值查实体索引），取该实体的同 field 状态
    3) bare:       唯一同名 bare 状态且无歧义才用（多对象同名→不命中，
                   放行——有明确依据才拦）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
# 事实类型（V6 收紧:authority 由事实类型决定,非全局 source 排名）
FT_SYSTEM_FACT = "system_fact"
FT_USER_STATEMENT = "user_statement"
FT_KB_RULE = "kb_rule"
FT_USER_PREFERENCE = "user_preference"

FACT_TYPES = (FT_SYSTEM_FACT, FT_USER_STATEMENT, FT_KB_RULE,
              FT_USER_PREFERENCE)

# 类型 → 权威来源（EvidenceView 据此给记录标 authority,零硬编码）
AUTHORITY_BY_TYPE = {
    FT_SYSTEM_FACT: "system",
    FT_KB_RULE: "knowledge_base",
    FT_USER_PREFERENCE: "user",
    FT_USER_STATEMENT: "unverified",   # 待工具/KB 对齐
}

# source → 默认 fact_type（写入方未显式声明时的通用映射）
_DEFAULT_FACT_TYPE = {
    "user": FT_USER_STATEMENT,
    "tool": FT_SYSTEM_FACT,
    "knowledge": FT_KB_RULE,
}


@dataclass
class StateEntry:
    object: str          # 业务对象（transfer_request / card_dbc_12345 / transfer_reason_rule）
    field: str            # 字段（amount / id / status / allowed_values…）
    value: Any
    source: str          # user / tool / knowledge
    source_ref: Optional[str] = None
    fact_type: str = ""  # 事实类型（空 = 取 _DEFAULT_FACT_TYPE[source]）
    seq: int = 0
    superseded_by: Optional[int] = None  # 被哪条 seq 取代（历史链）

    @property
    def effective_fact_type(self) -> str:
        """写入方未声明类型时按 source 通用映射。"""
        return self.fact_type or _DEFAULT_FACT_TYPE.get(self.source, FT_USER_STATEMENT)

    @property
    def authority(self) -> str:
        """该条记录的权威来源（按事实类型,不按 source 全局排名）。"""
        return AUTHORITY_BY_TYPE.get(self.effective_fact_type, "unverified")

    @property
    def key(self) -> str:
        return f"{self.object}.{self.field}"

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class TaskStateV3:
    """对象级任务状态（V3 主状态）。

    与 V2 TaskState（参数级）的关系：V3 是超集——V2 的写入自动
    归入 bare 命名空间（object=""），兼容存量调用；新的对象级
    写入用 set(object, field, …)。
    """

    # ---- 实体字段名（ID 类）→ 对象类型前缀 ----
    ENTITY_FIELDS = {
        "user_id": "user",
        "account_id": "account",
        "card_id": "card",
        "credit_card_account_id": "cc_account",
        "transaction_id": "transaction",
        "record_id": "record",
    }

    def __init__(self):
        self._entries: dict[str, list[StateEntry]] = {}  # key → 历史链
        self._entity_index: dict[str, set[str]] = {}     # 实体 ID → {keys}
        self._seq = 0
        self._pending_trace: list[dict] = []             # 未 flush 的 trace 事件

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def set(self, obj: str, field_name: str, value: Any, source: str,
            source_ref: Optional[str] = None,
            fact_type: str = "") -> Optional[StateEntry]:
        """写入一条状态（同 key 旧条目自动 supersede——历史保留）。

        fact_type: 事实类型（system_fact/user_statement/kb_rule/
        user_preference）。写入方按提取规则声明——如 UserStateExtractor
        的金额绑定是 user_statement,而"加急/称呼"类是 user_preference;
        ToolResultStateExtractor 一律 system_fact;KnowledgeStateExtractor
        一律 kb_rule。空 = 按 source 通用映射。
        返回 None = 空值被跳过；返回新条目。
        """
        if field_name in (None, "") or value in (None, ""):
            return None
        # 类型标记防御（构造层——任何来源都不可写入占位值）：
        # "string"/"number"/… 是文档/说明的占位，不是数据。
        # 修复：残块单处类型标记漏网导致 account_string.id='string'
        # 污染 → 后续真实 account_id 全部误拦（095/080 实测）。
        if isinstance(value, str) and value.strip().lower() in (
                "string", "number", "integer", "boolean", "float",
                "array", "object", "list", "dict", "null", "none", "n/a"):
            return None
        obj = (obj or "").strip().lower()
        field_name = (field_name or "").strip().lower()
        key = f"{obj}.{field_name}" if obj else field_name
        chain = self._entries.setdefault(key, [])
        # 幂等写入：同 key 当前值未变（宽松等价）→ no-op。
        # 修复 029 实测 602 次重复 constraints 重喂（每次 _do_ask 后
        # _update_kb_constraints 全量 packets 重放）——状态事件爆炸
        # （18 工具消息 → 3106 state 事件）。真值变化仍正常 supersede。
        if chain:
            cur = chain[-1]
            if cur.is_current and cur.source == source and _loose_eq(cur.value, value):
                return cur
        self._seq += 1
        entry = StateEntry(object=obj, field=field_name, value=value,
                           source=source, source_ref=source_ref,
                           fact_type=fact_type or "", seq=self._seq)
        # supersede：当前条目链上最后一条标记被取代
        if chain:
            chain[-1].superseded_by = self._seq
        old_value = chain[-1].value if chain else None
        chain.append(entry)
        # 实体索引：值本身是已知实体 ID 时登记（后续实体解析用）
        if field_name in ("id",) or field_name in self.ENTITY_FIELDS:
            self._entity_index.setdefault(str(value), set()).add(key)
        # trace（增量——只记新值+旧值+来源）
        self._pending_trace.append({
            "event": "state_update" if chain and old_value is not None else "state_write",
            "object": obj or None, "field": field_name,
            "old_value": old_value, "new_value": value,
            "source": source, "source_ref": source_ref,
        })
        return entry

    # V2 兼容写法：record(param, value, source, tool=…) → bare/tool 命名空间
    def record(self, param: str, value: Any, source: str,
               tool: str = "", source_ref: str = None) -> Optional[StateEntry]:
        return self.set(tool or "", param, value, source, source_ref)

    # ------------------------------------------------------------------
    # 读取（三类查询）
    # ------------------------------------------------------------------
    def latest(self, tool: str, param: str,
                proposed_value: Any = None) -> Optional[StateEntry]:
        """Harness 判定入口：给 (tool, param, proposed_value) 找当前约束。

        查找顺序：
        1. tool/param 精确（V2 兼容键 + "transfer/amount" 式对象键）
        2. param 是 ID 类字段 → proposed 值做实体解析（proposed ID 命中
           哪个对象 → 取该对象的同 field 值比较——target_card.id 案例）
        3. bare param：全库唯一才用（多对象同名 → None 放行）
        """
        p = (param or "").strip().lower()
        t = (tool or "").strip().lower()

        # 1) 精确键（对象级 + V2 参数级）
        for key in (f"{t}.{p}" if t else None, p):
            if key:
                chain = self._entries.get(key)
                if chain and chain[-1].is_current or chain:
                    cur = chain[-1]
                    if cur.is_current:
                        return cur

        # 2) ID 类字段的实体解析
        if p in self.ENTITY_FIELDS and proposed_value is not None:
            proposed = str(proposed_value)
            obj_prefix = self.ENTITY_FIELDS[p]
            # 找所有以该实体类型为对象的状态，看 proposed ID 是否
            # 作为其他 field 的值出现（即"agent 用了错误 ID"的检测基础：
            # 状态里记录过 card_dbc_12345.id=dbc_12345（source=tool），
            # proposed=dbc_54321 → 找 <prefix>_<other>.id 的当前值比较）
            # 按对象去重（同一实体的 card_id/id 是同一个 ID，只算一个候选）
            by_object: dict[str, StateEntry] = {}
            for key, chain in self._entries.items():
                cur = chain[-1]
                if cur.field in ("id", p) and cur.object.startswith(obj_prefix + "_") \
                        and cur.is_current:
                    by_object.setdefault(cur.object, cur)
            candidates = list(by_object.values())
            if len(candidates) == 1 and str(candidates[0].value) != proposed:
                # 唯一该类型实体且 agent 用了不同 ID → 用已知 ID 做约束
                return candidates[0]
            if len(candidates) > 1:
                # 多个实体（多卡任务）→ 只在 proposed 恰好等于其中之一时放行；
                # 全不匹配且唯一未确定 → 歧义，不拦（缺明确依据）
                return None

        # 3) bare：同 param 的 bare 状态唯一才用
        bare_chain = self._entries.get(p)
        if bare_chain and bare_chain[-1].is_current:
            # 但若同时存在对象级同名字段（多对象歧义）→ 放弃 bare
            obj_level_same = [k for k in self._entries
                              if k != p and k.endswith("." + p)]
            if not obj_level_same:
                return bare_chain[-1]
        return None

    # ------------------------------------------------------------------
    # 切片读取（Decision Agent 用——按需、不全量）
    # ------------------------------------------------------------------
    def relevant_slice(self, tool_name: str = "",
                       max_entries: int = 15) -> list[StateEntry]:
        """与当前动作相关的状态切片（渲染进 DA prompt 的来源）。

        规则：
        - tool_name 给定 → 该工具相关（工具命名空间 + 该工具参数涉及
          的实体）+ bare 全局
        - 全部只取当前有效条目（superseded 排除）
        - 控制条数（max_entries）防膨胀
        """
        out: list[StateEntry] = []
        t = (tool_name or "").strip().lower()
        seen_keys = set()
        for key, chain in self._entries.items():
            cur = chain[-1]
            if not cur.is_current:
                continue
            obj, f = cur.object, cur.field
            related = (obj == t) or (not obj) or (t and f in self.ENTITY_FIELDS)
            if related and key not in seen_keys:
                seen_keys.add(key)
                out.append(cur)
        return out[:max_entries]

    def current_snapshot(self) -> dict:
        """trace 用：全部当前有效状态（superseded 排除，无历史——防膨胀）。"""
        return {k: {"value": c.value, "source": c.source,
                    "ref": c.source_ref, "seq": c.seq,
                    "fact_type": c.effective_fact_type}
                for k, chain in self._entries.items()
                if (c := chain[-1]) and c.is_current}

    # ------------------------------------------------------------------
    # 证据视图查询（V6 收紧:EvidenceView 从这里现算,不自建存储）
    # ------------------------------------------------------------------
    def evidence_records(self, fact_types: Optional[tuple] = None,
                         max_entries: int = 30) -> list[StateEntry]:
        """按事实类型取当前有效记录（EvidenceView 的唯一数据入口）。

        fact_types: 过滤类型（None = 全部）。只返回 current 条目,
        按 seq 升序——视图层自己排序渲染。这是"TaskState → Evidence
        View"单向数据流的查询侧:视图无独立写入,每次现读现算。
        """
        out = []
        for chain in self._entries.values():
            cur = chain[-1] if chain else None
            if not cur or not cur.is_current:
                continue
            if fact_types is not None and cur.effective_fact_type not in fact_types:
                continue
            out.append(cur)
            if len(out) >= max_entries * 3:   # 采集上限防大状态扫描
                break
        return sorted(out, key=lambda e: e.seq)[:max_entries]

    def latest_system_records(self, max_entries: int = 20) -> list[StateEntry]:
        """每个 key 上【最近一次】tool/knowledge 写入的记录（视图用）。

        不要求 current——用户后来改口（user 写入把 tool 记录挤到历史）
        时,工具确认过的值仍然返回:系统事实不因用户口头改写而消失,
        由视图呈现为 conflict。这是"系统事实权威"在查询侧的表达,
        不改 set() 的 supersede 语义（V5 冻结行为零变更）。
        """
        out = []
        for chain in self._entries.values():
            rec = next((r for r in reversed(chain)
                        if r.source in ("tool", "knowledge")), None)
            if rec is not None:
                out.append(rec)
                if len(out) >= max_entries:
                    break
        return sorted(out, key=lambda e: e.seq)

    def user_claim_records(self, max_entries: int = 10) -> list[StateEntry]:
        """当前有效的用户陈述/偏好记录（视图的 unverified/偏好段来源）。"""
        return self.evidence_records(
            fact_types=(FT_USER_STATEMENT, FT_USER_PREFERENCE),
            max_entries=max_entries)

    def conflicting_statement_keys(self) -> list[str]:
        """同 key 上用户陈述与系统事实并存且值矛盾的 key 列表。

        "并存"的两种形态（TaskStateV3 每次写入 supersede 前一条,
        同 key 同时只有一条 current）:
        a) 系统值 current、用户旧陈述 superseded（tool 后到覆盖 claim）
        b) 用户陈述 current、系统旧值 superseded（claim 后到但无权
           抹掉系统事实——latest_system_records 仍可见系统值）
        两种都算 conflict——决策层必须看到矛盾,零独立存储。
        """
        out = []
        for key, chain in self._entries.items():
            if len(chain) < 2:
                continue
            sys_rec = next((r for r in reversed(chain)
                            if r.source in ("tool", "knowledge")), None)
            user_rec = next((r for r in reversed(chain)
                             if r.source == "user"), None)
            if (sys_rec and user_rec
                    and user_rec.effective_fact_type in (FT_USER_STATEMENT,
                                                         FT_USER_PREFERENCE)
                    and not _loose_eq(sys_rec.value, user_rec.value)):
                out.append(key)
        return out

    def drain_trace(self) -> list[dict]:
        """取出并清空 pending trace 事件（runner 每 task flush 到 v2 events）。"""
        out = self._pending_trace
        self._pending_trace = []
        return out

    def reset(self) -> None:
        self._entries.clear()
        self._entity_index.clear()
        self._seq = 0
        self._pending_trace = []

    def __len__(self) -> int:
        return sum(1 for chain in self._entries.values()
                   if chain and chain[-1].is_current)



def _loose_eq(a, b) -> bool:
    """宽松等价（幂等写入判断用——与 validator 同口径）。"""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return False

# ---------------------------------------------------------------------------
# V3 提取器：user / tool / knowledge → 对象级状态
# ---------------------------------------------------------------------------
_USER_NUM_RE = re.compile(r"(?:\$|usd\s*)(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)

# 用户动作动词 → 对象命名空间（transfer $500 → transfer_request.amount）
_USER_ACTIONS = [
    (re.compile(r"\btransfer\b", re.I), "transfer_request"),
    (re.compile(r"\b(refund|dispute|credit)\b", re.I), "refund_request"),
    (re.compile(r"\b(pay|payment)\b", re.I), "payment_request"),
    (re.compile(r"\b(deposit|withdraw)\b", re.I), "deposit_request"),
    (re.compile(r"\b(increase|limit)\b", re.I), "limit_request"),
    # 改口语境（延续前文对象）："make it $300" / "change to $250" / "instead"
    (re.compile(r"\b(make it|change( it| that)? to|instead|update|revise|adjust)\b", re.I), "@previous"),
]


def _last_object_ns(state) -> str:
    """最近一次 user 写入的【金额字段】对象命名空间（改口语境沿用）。

    只认 amount 类写入——"transfer $500 ... dispute refund $100 ...
    make it $300" 的 it 指最近的金额对象（refund_request）？口语上
    可能指 transfer——**歧义即弃**：返回最近写入者（保守），多对象
    歧义由 latest 的 bare 不命中兜底放行，不会错误强拦。
    """
    try:
        best_seq, best_obj = -1, ""
        for key, chain in state._entries.items():
            cur = chain[-1]
            if cur.source == "user" and cur.object and cur.field == "amount":
                if cur.seq > best_seq:
                    best_seq, best_obj = cur.seq, cur.object
        return best_obj
    except Exception:
        return ""


class UserStateExtractor:
    """用户消息 → 对象级状态（V3：动作金额绑到对应 request 命名空间）。

    延续 V2.3 的动作语境规则（余额描述不绑）。
    V6 收紧:
    - 通用 duration 自述提取（"about four months"/"2 days ago"…→
      user_statement 类的 user_statement.duration,无任何业务语境限制
      ——不猜它属于哪个业务,只记"用户这么说过"）
    - 用户偏好/意愿模式（expedited/称呼/想选哪个）→ user_preference
      ——这类事实用户本人是权威,系统工具不覆盖
    """

    STATE_MARKERS = re.compile(r"(balance|savings of|of about|worth|currently)", re.I)

    # 通用 duration 自述:前缀限定词(可选) + 数词/数字 + 时间单位
    _DURATION_RE = re.compile(
        r"\b(?:about|around|roughly|over|more than|almost|nearly|at least)?\s*"
        r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
        r"|\d{1,3})\s*"
        r"(months?|days?|years?|weeks?)\b", re.I)

    # 用户偏好/意愿模式（用户本人权威的事实——通用词形,无业务绑定）
    _PREFERENCE_RES = [
        (re.compile(r"\bexpedited\b", re.I), "expedited_shipping"),
        (re.compile(r"\brush\b|\burgent(ly)?\b", re.I), "priority"),
    ]

    @classmethod
    def feed(cls, state: TaskStateV3, user_text: str,
             turn_ref: str = "") -> None:
        if not user_text:
            return
        # 通用 duration 自述 → user_statement（待系统工具对齐;
        # 落 TaskState,不再有旁路 ledger）
        m = cls._DURATION_RE.search(user_text)
        if m:
            state.set("user_statement", "duration", m.group(0).strip().lower(),
                      "user", source_ref=turn_ref,
                      fact_type=FT_USER_STATEMENT)
        # 用户偏好 → user_preference（用户本人权威）
        for rex, fname in cls._PREFERENCE_RES:
            pm = rex.search(user_text)
            if pm:
                state.set("user_preference", fname, pm.group(0).strip().lower(),
                          "user", source_ref=turn_ref,
                          fact_type=FT_USER_PREFERENCE)
        for m in _USER_NUM_RE.finditer(user_text):
            start = m.start()
            window = user_text[max(0, start - 40):start]
            obj_ns = None
            for pattern, ns in _USER_ACTIONS:
                verb = pattern.search(window)
                if verb:
                    between = window[verb.end():]
                    if cls.STATE_MARKERS.search(between):
                        break  # 动词后的状态标记 → 描述
                    if ns == "@previous":
                        # 改口语境：沿用最近写入的对象命名空间（没有则不绑）
                        obj_ns = _last_object_ns(state) or None
                    else:
                        obj_ns = ns
                    break
            if obj_ns is None:
                continue  # 无动作语境 → 不绑（V2.3 规则）
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            state.set(obj_ns, "amount", value, "user", source_ref=turn_ref)


class ToolResultStateExtractor:
    """Tool Result → 对象级状态（V3：实体绑定）。

    banking 工具返回的记录格式（tau2）：
        "1. Record ID: dbc_xxx\n   user_id: u_xxx\n   status: ACTIVE"
    提取规则（保守——只提表结构化字段）：
    - ID 字段（card_id/account_id/…）→ 实体对象 <type>_<id8> 的 .id
    - 同一记录块内的 status/balance/amount 等字段 → 挂到该实体对象
    - 无实体锚点的散字段 → 不入（防止来源不明的值污染全局）

    V6 收紧:
    - get_current_time 类工具结果 → 全局 system_fact 命名空间
      "system.current_date"（时间锚点——决策视图的 P1 事实）。
      之前这个能力长在独立 ledger 里（旁路喂入）,现在回到
      TaskState 这个唯一事实源,与记录提取同一条数据流。
    """

    RECORD_ID_RE = re.compile(r"record\s+id\s*[:=]\s*(\S+)", re.I)
    # 当前时间类工具的确定性识别（工具名匹配 + 文本日期提取）
    CURRENT_TIME_TOOL_RE = re.compile(r"(get_current_time|current_time)", re.I)
    TIME_VALUE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

    FIELD_RES = {
        "user_id": re.compile(r"\buser_id\s*[:=]\s*(\S+)"),
        "account_id": re.compile(r"\baccount_id\s*[:=]\s*(\S+)"),
        "card_id": re.compile(r"\bcard_id\s*[:=]\s*(\S+)"),
        "credit_card_account_id": re.compile(r"\bcredit_card_account_id\s*[:=]\s*(\S+)"),
        "transaction_id": re.compile(r"\btransaction_id\s*[:=]\s*(\S+)"),
        "status": re.compile(r"\bstatus\s*[:=]\s*([A-Z_]+)"),
        "balance": re.compile(r"\bbalance\s*[:=]?\s*\$?([\d,]+(?:\.\d+)?)"),
        "current_balance": re.compile(r"\bcurrent_balance\s*[:=]?\s*\$?([\d,]+(?:\.\d+)?)"),
        "amount": re.compile(r"\bamount\s*[:=]\s*\$?([\d,]+(?:\.\d+)?)"),
        "issue_reason": re.compile(r"\bissue_reason\s*[:=]\s*(\S+)"),
    }

    # 类型标记词——schema 文档说明里的占位（"account_id: string"），
    # 出现在 ID 字段值位 = 文档不是数据，直接跳过该字段
    TYPE_MARKERS = {"string", "number", "integer", "boolean", "float",
                    "array", "object", "list", "dict"}

    @classmethod
    def feed(cls, state: TaskStateV3, tool_name: str,
             result_text: str) -> None:
        if not result_text:
            return
        # 时间类工具 → system.current_date（与记录提取同入口,零旁路）
        if cls.CURRENT_TIME_TOOL_RE.search(tool_name or ""):
            tm = cls.TIME_VALUE_RE.search(result_text)
            if tm:
                state.set("system", "current_date", tm.group(1), "tool",
                          source_ref=tool_name, fact_type=FT_SYSTEM_FACT)
        text = result_text[:4000]
        # 防文档说明混入：文本是 schema 文档（含 "id: string" 式说明）
        # 的典型特征——两个以上 ID 字段的值是类型标记 → 整块跳过
        type_marker_hits = 0
        for f, pat in cls.FIELD_RES.items():
            if (f.endswith("_id") or f == "card_id"):
                m = pat.search(text)
                if m and m.group(1).strip("',.;)").lower() in cls.TYPE_MARKERS:
                    type_marker_hits += 1
        if type_marker_hits >= 2:
            return  # schema 文档（参数说明）——不是工具数据记录
        # 按 "N. Record ID:" 分块——每块一个实体记录
        blocks = re.split(r"\n\s*\d+\.\s+record", text, flags=re.I)
        for block in blocks:
            # 找这个块的主实体（第一个出现的 ID 字段）
            anchor_type, anchor_id = None, None
            for f, pat in cls.FIELD_RES.items():
                if f.endswith("_id") or f == "card_id":
                    m = pat.search(block)
                    if m:
                        anchor_type = TaskStateV3.ENTITY_FIELDS.get(
                            f, f.replace("_id", ""))
                        anchor_id = m.group(1).strip("',.;)")
                        break
            if anchor_type is None:
                continue
            obj = f"{anchor_type}_{anchor_id[:16]}"
            # 块内全部字段挂到该实体
            for f, pat in cls.FIELD_RES.items():
                m = pat.search(block)
                if not m:
                    continue
                raw = m.group(1).strip("',.;)")
                if raw.lower() in cls.TYPE_MARKERS:
                    continue  # 类型标记占位（文档说明）——跳过
                if f.endswith("_id") and raw != anchor_id and f != "user_id":
                    continue  # 块内其他实体的 ID 不挂到本对象
                value: Any = raw
                if f in ("balance", "current_balance", "amount"):
                    try:
                        value = float(raw.replace(",", ""))
                    except ValueError:
                        continue
                field_name = f if f != "record_id" else "id"
                state.set(obj, field_name if field_name != anchor_type or True else "id"
                          if f.endswith("_id") else f,
                          value, "tool", source_ref=tool_name)
            # 主 ID 本身
            state.set(obj, "id", anchor_id, "tool", source_ref=tool_name)


class KnowledgeStateExtractor:
    """KB 明确规则 → 对象级状态（V3）。

    KA packet 的 constraints（V2.2 已有 enum/threshold/format）→
    规则命名空间："<tool>.<param>.allowed_values" / ".max" / ".min"。
    """

    @classmethod
    def feed_constraints(cls, state: TaskStateV3,
                         constraints: list) -> None:
        for c in constraints or []:
            if not isinstance(c, dict) or not c.get("parameter_name"):
                continue
            obj = (c.get("tool_name") or "rule").strip().lower()
            p = c["parameter_name"].strip().lower()
            ctype = str(c.get("constraint_type", "")).lower()
            ref = c.get("source_doc_id")
            if ctype == "enum" and c.get("allowed_values"):
                state.set(obj, f"{p}.allowed_values", list(c["allowed_values"]),
                          "knowledge", source_ref=ref)
            elif ctype == "threshold":
                if c.get("max") is not None:
                    state.set(obj, f"{p}.max", c["max"], "knowledge", ref)
                if c.get("min") is not None:
                    state.set(obj, f"{p}.min", c["min"], "knowledge", ref)
