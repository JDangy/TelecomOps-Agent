"""
V1 2-Agent 架构：Decision Agent + Knowledge Agent（context 隔离）。

研究问题：把知识检索与证据整理从主决策 Agent 的上下文中隔离出来，
是否能降低 context inflation，并改善端到端 reliability / latency / token efficiency。

架构：
    User
    → Decision Agent（业务工具 + ask_knowledge_agent）
        → 需要知识时: ask_knowledge_agent(question)
            → Knowledge Agent 独立 context：
                自主 query formulation → BM25 检索 → 阅读结果 →
                判断证据是否充分 →（可多轮检索）→ Evidence Packet
        ← 只返回压缩后的 Evidence Packet（原始检索文本不进 Decision Agent context）
    → Decision Agent 依据 packet 决策 → business tools → User

设计约束（严格遵守）：
- Decision Agent 的工具列表里没有 KB_search / KB_search_bm25 / KB_search_dense /
  grep / shell——物理上无法直接检索
- Knowledge Agent 有独立 message state；只接收 question + 必要约束，
  不复制 Decision Agent 的历史对话
- Evidence Packet 里的 source_doc_id 必须来自真实检索结果，不允许编造
- Evidence 不足时明确返回 missing_information / uncertainty
- 不加 Query Rewriter / Reranker / Dense fallback（保持 BM25，实验可归因）
- 不在 prompt 里塞 policy hints / golden knowledge（实验变量只有
  context isolation + specialist decomposition）

Trace v2 集成：
- Knowledge Agent 的每次 LLM 调用带 call_name="knowledge_agent_response"，
  instrumentation 的 actor 映射会自动归类为 knowledge_agent
- handoff 通过 ask_knowledge_agent 工具的 tool_call_start/end 事件可见
- Evidence Packet 摘要记录在 tool_call_end 的额外字段（evidence_summary）
"""

from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, Field

from tau2.agent.base_agent import HalfDuplexAgent, ValidAgentInputMessage
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate


# ---------------------------------------------------------------------------
# 工具分流：哪些工具属于"知识检索"（Decision Agent 一律不给）
# ---------------------------------------------------------------------------
RETRIEVAL_TOOL_NAMES = {
    "KB_search", "KB_search_bm25", "KB_search_dense", "grep", "shell",
}


def split_tools(tools: List[Tool]) -> tuple[List[Tool], List[Tool]]:
    """把环境工具分成 (business_tools, knowledge_tools)。"""
    business, knowledge = [], []
    for t in tools:
        (knowledge if t.name in RETRIEVAL_TOOL_NAMES else business).append(t)
    return business, knowledge


# ---------------------------------------------------------------------------
# Evidence Packet
# ---------------------------------------------------------------------------
class EvidenceFact(BaseModel):
    claim: str = Field(description="A single factual claim supported by the knowledge base")
    source_doc_id: str = Field(description="The doc ID from the search result supporting this claim")


class EvidencePacket(BaseModel):
    """Knowledge Agent 返回给 Decision Agent 的压缩证据包。

    原则：结构化、可追溯、压缩。绝不包含大段 KB 原文。
    """
    answer: str = Field(
        description="Concise answer to the question asked, synthesized from the evidence"
    )
    facts: List[EvidenceFact] = Field(
        default_factory=list,
        description="Key factual claims with their source document IDs"
    )
    relevant_document_ids: List[str] = Field(
        default_factory=list,
        description="All document IDs that were consulted and deemed relevant"
    )
    missing_information: List[str] = Field(
        default_factory=list,
        description="Aspects of the question that could not be answered from the knowledge base"
    )
    confidence: str = Field(
        default="medium",
        description="high / medium / low — how confident the knowledge agent is in the evidence"
    )
    status: str = Field(
        default="partial",
        description=(
            "sufficient / partial / insufficient — whether THIS evidence is enough "
            "for the requesting agent to proceed. Note: confidence (certainty of "
            "individual facts) and status (sufficiency for the task) are different things."
        )
    )
    grounded_values: List[dict] = Field(
        default_factory=list,
        description=(
            "Precise machine-usable values extracted verbatim from the knowledge base: "
            "enum codes, exact amounts, thresholds, type/class names, reason codes. "
            "Each: {name, value, value_type(enum|number|string|boolean), source_doc_id, unit?}. "
            "Values MUST be preserved EXACTLY as they appear in source documents "
            "(no paraphrasing, no translation, no humanized rewriting)."
        )
    )
    constraints: List[dict] = Field(
        default_factory=list,
        description=(
            "V2.2: DEFINITE constraints from the KB — not case answers. Each: "
            "{tool_name?, parameter_name, constraint_type(enum|threshold|format), "
            "allowed_values?[], min?, max?, unit?, format?, source_doc_id}. "
            "Only include constraints the KB states explicitly (an enum set a tool "
            "accepts, a documented max transfer amount, a required date format). "
            "Never use this to pick WHICH legal value applies to the current case "
            "— that is the decision agent's job."
        )
    )


# ---------------------------------------------------------------------------
# 结构化 KnowledgeRequest（V1.1）：减少宽泛 handoff
# ---------------------------------------------------------------------------
class KnowledgeRequest(BaseModel):
    """Decision → Knowledge 的结构化请求。

    目的：让 Knowledge Agent 明确知道——用户要什么、已经知道什么、这次缺什么。
    替代 V1 的纯文本 question + context，避免"帮我找所有信用卡信息"式宽泛请求。
    不做 Planner——只是结构化传递既有信息。
    """
    question: str = Field(description="The specific question to answer")
    known_constraints: List[str] = Field(
        default_factory=list,
        description="User constraints relevant to this question, e.g. 'annual fee <= $100'",
    )
    known_facts: List[str] = Field(
        default_factory=list,
        description="Facts the decision agent already established",
    )
    needed_information: List[str] = Field(
        default_factory=list,
        description="What specific information is missing that this request should find",
    )


EVIDENCE_PACKET_SYSTEM_PROMPT = """You are a knowledge specialist for a bank's customer service system.

You have access to knowledge base search tools. Your job:
1. Read the question from the decision agent, plus any provided constraints and known facts.
2. If a block of "[Already established facts...]" is provided, REUSE those facts — do not search
   for information you already have. Only search for the specific missing pieces.
3. If recent searches stop returning new documents or new facts (the system will tell you),
   stop searching and produce your packet with what you have.
4. Each search should target a distinct piece of missing information. Do not keep reformulating
   variations of the same question hoping for different results.
5. When you have enough evidence, return your final answer AS A JSON EVIDENCE PACKET.

The evidence packet schema:
{
  "answer": "<concise synthesized answer>",
  "facts": [{"claim": "<factual claim>", "source_doc_id": "<doc id from search results>"}],
  "grounded_values": [{"tool_name": "<tool the value is used in>", "parameter_name": "<parameter of that tool>", "value": "<exact value>", "value_type": "enum|number|string|boolean", "source_doc_id": "<doc id>"}],
  "constraints": [{"tool_name": "<tool>", "parameter_name": "<parameter>", "constraint_type": "enum|threshold|format", "allowed_values": ["..."], "min": 0, "max": 5000, "unit": "USD", "format": "MM/DD/YYYY", "source_doc_id": "<doc id>"}],
  "relevant_document_ids": ["<doc ids consulted>"],
  "missing_information": ["<aspects you could not answer>"],
  "confidence": "high|medium|low",
  "status": "sufficient|partial|insufficient"
}

grounded_values — CRITICAL FOR DOWNSTREAM TOOL USE:
- Extract every machine-usable value the decision agent might need to fill tool
  parameters: enum codes, exact amounts/fees/limits/thresholds, type/class names,
  reason codes, option names.
- Each grounded value MUST name the TOOL it belongs to (tool_name) and the exact
  PARAMETER of that tool (parameter_name) — e.g. tool_name="close_bank_account_7392",
  parameter_name="reason", value="early_closure". Only do this when the document
  clearly indicates which tool and parameter the value is for.
- If you CANNOT reliably tell which tool/parameter a value belongs to, do NOT guess —
  put it in facts as a plain claim instead.
- Preserve values EXACTLY as written in the source document. If the document says
  "account_closure", write "account_closure" — NOT "closure", "account closing",
  or any paraphrase. Numbers keep their exact digits and units.
- If no precise tool-usable value is needed, an empty list is fine.

constraints — DEFINITE RULES ONLY (never case answers):
- Include a constraint ONLY when the knowledge base explicitly states it:
  - "enum": the complete set of legal values a parameter accepts
    (e.g. allowed_values: ["fraud", "customer_request", "account_closure"]).
  - "threshold": a documented numeric bound (max transfer amount, min balance).
  - "format": a required value format (e.g. MM/DD/YYYY for dates).
- Do NOT use constraints to say which legal value fits THIS case — deciding that
  is the decision agent's job. "enum" constrains the set; it does not pick one.
- If the KB does not state a constraint explicitly, leave the list empty.

- confidence: how certain you are about the individual facts.
- status: whether this evidence is SUFFICIENT for the requesting agent to act on, only PARTIAL, or clearly INSUFFICIENT. These are different: you can be highly confident in a fact yet the packet may be insufficient for the task.

STRICT RULES:
- source_doc_id values MUST be actual document IDs you saw in search results or in provided established facts. Never invent IDs.
- Do not copy large passages of raw text into the packet — distill facts.
- If evidence is insufficient, say so in missing_information, set confidence low, and status to "insufficient".
- You return ONLY the JSON packet as your final message — no other text around it.
"""

KNOWLEDGE_AGENT_MAX_ROUNDS = 6  # 防失控：一次 handoff 内最多 6 轮（检索+推理）


class KnowledgeAgent:
    """知识专员：独立 context，只拿知识工具，产出 Evidence Packet。

    不继承 HalfDuplexAgent——它不直接参与 user 对话轮，而是由
    DecisionAgent 通过 ask_knowledge_agent 工具同步调用。

    V1.1 memory 语义：
    - 持有 KnowledgeWorkingMemory（task 内跨 handoff 累积）
    - 每次 handoff 的 context = system + memory block + 本次 KnowledgeRequest
      ——不带上一次 handoff 的完整 messages（原始检索文档不无限累积，
         防 context explosion 从 DA 搬到 KA）
    - 检索 query / 返回 doc_ids / packet facts 全部程序化写入 memory
    - task 边界由 runner 调 start_task/end_task
    """

    def __init__(self, tools: List[Tool], llm: str, llm_args: Optional[dict] = None,
                 memory: Optional["KnowledgeWorkingMemory"] = None):
        self.tools = tools  # 只有 KB 检索工具
        self.llm = llm
        self.llm_args = dict(llm_args or {})
        self.memory = memory  # None 时不启用（向后兼容 V1 行为）

    def answer(self, request) -> dict:
        """回答一个 KnowledgeRequest，返回 EvidencePacket dict（V1.2 三段式）。

        Memory Hit   —— retrieve() 判定已有相似已回答问题：
                       直接复用 similar_packet 组装回答（零检索、零额外 LLM）。
        Partial Hit  —— 有相关事实但缺明确内容：把相关事实+缺口交给 KA，
                       只搜缺的部分（progress 检测防转圈）。
        Memory Miss  —— 正常检索流程。

        防膨胀三件套：
        - 不再全量注入 memory（relevant view 最多 6 facts + 8 docs）
        - prompt 不再写"勿重复 query"（V1.1 反向激励教训）
        - 每次检索后 note_retrieval_progress：连续 LOW_PROGRESS_LIMIT 次无
          新文档且无新事实 → 建议停止（KA 收到停止提示后收尾）
        """
        if isinstance(request, str):
            request = KnowledgeRequest(question=request)

        # ---- 三段式判定 ----
        view = None
        if self.memory is not None:
            view = self.memory.retrieve(request.model_dump())
            self._emit_memory_event_safe("memory_retrieve", request=request,
                                        verdict=view.get("verdict"),
                                        matched_facts=len(view.get("relevant_facts") or []),
                                        matched_packets=view.get("matched_packet_count") or 0)

        if view is not None and view.get("verdict") == "hit":
            # Memory Hit：零检索直接出 packet
            self._emit_memory_event_safe("memory_hit", request=request,
                                         matched_fact_count=len(view.get("relevant_facts") or []),
                                         matched_packet_count=view.get("matched_packet_count") or 0,
                                         retrieval_skipped=True)
            packet = self._reuse_packet(view, request)
            self._record_packet_memory(request, packet)
            return packet

        if view is not None and view.get("verdict") == "partial":
            self._emit_memory_event_safe("memory_partial_hit", request=request,
                                         matched_fact_count=len(view.get("relevant_facts") or []),
                                         matched_packet_count=0)
        elif view is not None:
            self._emit_memory_event_safe("memory_miss", request=request,
                                         matched_fact_count=0, matched_packet_count=0)

        # memory 记录本次 request
        if self.memory is not None:
            self.memory.update({"type": "request", "question": request.question})

        # ---- Partial / Miss：构建 context（relevant view，非全量）----
        user_lines = [f"Question from the decision agent:\n{request.question}"]
        if request.known_constraints:
            user_lines.append("Known user constraints:\n" +
                              "\n".join(f"- {x}" for x in request.known_constraints))
        if request.known_facts:
            user_lines.append("Facts the decision agent already established:\n" +
                              "\n".join(f"- {x}" for x in request.known_facts))
        if request.needed_information:
            user_lines.append("Specifically, find:\n" +
                              "\n".join(f"- {x}" for x in request.needed_information))
        # V1.2: 只注入 request 相关视图（partial 时），miss 时零注入
        if view is not None and view.get("verdict") == "partial":
            block = self._render_relevant_view(view)
            if block:
                user_lines.append(block)

        system = SystemMessage(role="system", content=EVIDENCE_PACKET_SYSTEM_PROMPT)
        user_msg = UserMessage(role="user", content="\n\n".join(user_lines))
        messages: List[Message] = [system, user_msg]

        forced_stop = False
        for _round in range(KNOWLEDGE_AGENT_MAX_ROUNDS):
            # 停止条件：连续 LOW_PROGRESS_LIMIT 次检索无新信息 → 强制收尾
            if (self.memory is not None and not forced_stop
                    and self.memory.should_stop_searching()):
                self._emit_memory_event_safe("retrieval_progress", request=request,
                                             progress="low", streak=None,
                                             stopped=True)
                messages.append(UserMessage(
                    role="user",
                    content=("Recent searches are not finding new documents or facts. "
                             "Stop searching and return your evidence packet JSON now "
                             "with what you have; use missing_information for gaps."),
                ))
                forced_stop = True
            assistant = generate(
                model=self.llm,
                messages=messages,
                tools=None if forced_stop else self.tools,
                call_name="knowledge_agent_response",
                **self.llm_args,
            )
            messages.append(assistant)
            if not assistant.is_tool_call():
                break  # 最终 packet 已产出
            # 执行它调用的每个知识工具（BM25 本地检索，微秒级）
            for tc in assistant.tool_calls:
                result = self._run_tool(tc)
                messages.append(result)
                # memory 记录 query + docs；progress 检测（确定性，无 LLM）
                if self.memory is not None and not result.error:
                    args = tc.arguments or {}
                    self.memory.update({"type": "retrieval_query",
                                         "query": args.get("query", "")})
                    import re as _re
                    doc_ids = [m.group(1).strip()
                               for m in _re.finditer(r"ID:\s*(\S+)", result.content or "")]
                    if doc_ids:
                        # progress: 本次返回的文档里有多少是 memory 没见过的
                        # （必须在 update memory 之前算，否则全算重复）
                        if hasattr(self.memory, "note_retrieval_progress"):
                            seen_before = set(self.memory.read().get("documents_seen", []))
                            new_docs = len(set(doc_ids) - seen_before)
                            prog = self.memory.note_retrieval_progress(new_docs, 0)
                            self._emit_memory_event_safe(
                                "retrieval_progress", request=request,
                                progress=prog["progress"],
                                new_docs=new_docs, streak=prog["streak"])
                        self.memory.update({"type": "documents", "doc_ids": doc_ids})
        else:
            # 轮次耗尽：强制收尾
            messages.append(UserMessage(
                role="user",
                content="Search round limit reached. Return your evidence packet JSON now with whatever you have. If evidence is insufficient, use missing_information.",
            ))
            assistant = generate(
                model=self.llm,
                messages=messages,
                tools=None,
                call_name="knowledge_agent_response",
                **self.llm_args,
            )
            messages.append(assistant)

        packet = self._parse_packet(assistant)
        self._record_packet_memory(request, packet)
        return packet

    def _record_packet_memory(self, request, packet) -> None:
        """packet 结果写 memory（facts/missing/answered 记录）。"""
        if self.memory is None:
            return
        if packet.get("facts"):
            self.memory.update({"type": "facts", "facts": packet["facts"]})
        for miss in packet.get("missing_information") or []:
            self.memory.update({"type": "open_question", "question": miss})
        if packet.get("status") in ("sufficient", "partial"):
            self.memory.update({"type": "resolve_question",
                                "question": request.question})
        # V1.2: 记录已回答问题（hit 复用依据）
        if hasattr(self.memory, "record_answered"):
            self.memory.record_answered(request.question, packet)

    def _reuse_packet(self, view, request) -> dict:
        """Memory Hit 路径：复用相似问题的 packet（零检索）。

        直接返回 similar_packet（其 facts/来源保留原样——事实本身不变），
        answer 加一行说明这是基于已确认信息，供 DA 理解上下文。
        不重新调用 LLM——纯程序复用。
        """
        packet = dict(view.get("similar_packet") or {})
        note = (f"(Reused verified evidence previously gathered for a similar "
                f"question: '{view.get('similar_question', '')[:120]}')")
        packet["answer"] = (packet.get("answer", "") + "\n" + note).strip()
        return packet

    def _render_relevant_view(self, view) -> str:
        """渲染 retrieve() 的相关视图（小而聚焦，替代 V1.1 全量注入）。"""
        lines = ["[Already established facts relevant to this question — reuse them, only search for what is missing:]"]
        for f in view.get("relevant_facts") or []:
            lines.append(f"- {f}")
        if view.get("known_missing"):
            lines.append("Known gaps from earlier: " + "; ".join(view["known_missing"][:3]))
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _emit_memory_event_safe(self, event_type, request=None, **fields) -> None:
        """发 memory 行为事件（无 recorder 静默跳过；不影响执行）。"""
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            return
        if rec is None:
            return
        payload = {k: v for k, v in fields.items()}
        if request is not None:
            payload["question"] = (getattr(request, "question", "") or "")[:200]
        try:
            rec.emit(event_type, "knowledge_agent",
                     parent_span_id=getattr(rec, "task_span_id", None),
                     **payload)
        except Exception:
            pass

    # -- 内部 -------------------------------------------------------------
    def _run_tool(self, tool_call) -> ToolMessage:
        """执行知识工具调用（与 tau2 Environment.get_response 语义一致）。

        额外职责：向 active TraceV2Recorder 发 tool_call_start/end 事件——
        Knowledge Agent 不经过 environment.get_response，所以 tool 层插桩
        看不到这里；由我们自己发（保持与主路径相同的事件结构）。

        找不到工具/参数错时返回 error ToolMessage——让 LLM 自己纠正，
        不抛异常打断 handoff。
        """
        import time as _time
        rec = None
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            rec = None

        sid = None
        t0 = _time.perf_counter()
        if rec is not None:
            try:
                sid = rec.emit(
                    "tool_call_start", "retrieval",
                    parent_span_id=getattr(rec, "task_span_id", None),
                    tool_name=tool_call.name,
                    query=(tool_call.arguments or {}).get("query"),
                    arguments={"query": (tool_call.arguments or {}).get("query")},
                    agent="knowledge_agent",  # 标记来源（主路径无此字段）
                )
            except Exception:
                sid = None

        def _finish(content: str, error: bool, doc_ids=None):
            if rec is not None and sid is not None:
                try:
                    rec.emit(
                        "tool_call_end", "retrieval",
                        span_id=sid,
                        parent_span_id=getattr(rec, "task_span_id", None),
                        tool_name=tool_call.name,
                        latency_ms=round((_time.perf_counter() - t0) * 1000, 1),
                        success=not error,
                        result_chars=len(content or ""),
                        doc_ids=doc_ids,
                        ranks=list(range(1, len(doc_ids) + 1)) if doc_ids else None,
                        scores=None,
                        agent="knowledge_agent",
                    )
                except Exception:
                    pass

        tool = next((t for t in self.tools if t.name == tool_call.name), None)
        if tool is None:
            msg = f"Error: unknown tool {tool_call.name!r}"
            _finish(msg, True)
            return ToolMessage(
                id=tool_call.id, role="tool", requestor="assistant",
                content=msg, error=True,
            )
        try:
            result = tool(**tool_call.arguments)
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            # 解析 doc_ids 供 trace（与 eval/trace.py 同格式）
            doc_ids = None
            try:
                import re as _re
                ids = [m.group(1).strip() for m in _re.finditer(r"ID:\s*(\S+)", content)]
                doc_ids = ids or None
            except Exception:
                doc_ids = None
            _finish(content, False, doc_ids)
            return ToolMessage(
                id=tool_call.id, role="tool", requestor="assistant",
                content=content, error=False,
            )
        except Exception as exc:
            msg = f"Error calling {tool_call.name}: {type(exc).__name__}: {exc}"
            _finish(msg, True)
            return ToolMessage(
                id=tool_call.id, role="tool", requestor="assistant",
                content=msg, error=True,
            )

    @staticmethod
    def _parse_packet(assistant: AssistantMessage) -> dict:
        """从 assistant 输出解析 JSON packet；解析失败返回降级 packet（不崩溃）。"""
        raw = (assistant.content or "").strip()
        # 剥 ```json fences
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        # 找第一个 { 到最后一个 }
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e > s:
            try:
                obj = json.loads(raw[s:e + 1])
                if isinstance(obj, dict) and "answer" in obj:
                    status = str(obj.get("status", "partial"))
                    if status not in ("sufficient", "partial", "insufficient"):
                        status = "partial"
                    # grounded_values：结构化精确值（V2.1 新格式
                    # tool_name+parameter_name；缺关键字段的条目丢弃，不伪造）
                    grounded = []
                    for gv in obj.get("grounded_values") or []:
                        if not isinstance(gv, dict):
                            continue
                        has_new = gv.get("tool_name") and gv.get("parameter_name")
                        has_old = gv.get("name") is not None
                        if (has_new or has_old) and "value" in gv:
                            entry = {
                                "value": gv["value"],
                                "value_type": str(gv.get("value_type", "string")),
                                "source_doc_id": gv.get("source_doc_id"),
                            }
                            if gv.get("unit"):
                                entry["unit"] = gv["unit"]
                            if has_new:
                                entry["tool_name"] = str(gv["tool_name"])
                                entry["parameter_name"] = str(gv["parameter_name"])
                            else:
                                entry["name"] = str(gv["name"])
                            grounded.append(entry)
                    # V2.2: KB 明确约束（enum 集合/阈值/格式——非案例值）。
                    # 只收 constraint_type 明确且带 parameter_name 的条目，
                    # 缺关键字段的丢弃（不猜）。
                    constraints_out = []
                    for c in obj.get("constraints") or []:
                        if not isinstance(c, dict) or not c.get("parameter_name"):
                            continue
                        ctype = str(c.get("constraint_type", "")).lower()
                        if ctype not in ("enum", "threshold", "format"):
                            continue
                        entry_c = {
                            "parameter_name": str(c["parameter_name"]),
                            "constraint_type": ctype,
                            "source_doc_id": c.get("source_doc_id"),
                        }
                        if c.get("tool_name"):
                            entry_c["tool_name"] = str(c["tool_name"])
                        if ctype == "enum":
                            av = [v for v in (c.get("allowed_values") or [])
                                  if v not in (None, "")]
                            if not av:
                                continue  # 空 enum 约束不可用——丢弃
                            entry_c["allowed_values"] = av
                        elif ctype == "threshold":
                            if c.get("min") is not None:
                                entry_c["min"] = c["min"]
                            if c.get("max") is not None:
                                entry_c["max"] = c["max"]
                            if c.get("unit"):
                                entry_c["unit"] = c["unit"]
                            if "min" not in entry_c and "max" not in entry_c:
                                continue
                        elif ctype == "format":
                            if not c.get("format"):
                                continue
                            entry_c["format"] = str(c["format"])
                        constraints_out.append(entry_c)
                    packet = EvidencePacket(
                        answer=str(obj.get("answer", "")),
                        facts=[EvidenceFact(**f) for f in obj.get("facts", []) if isinstance(f, dict) and "claim" in f],
                        relevant_document_ids=[str(d) for d in obj.get("relevant_document_ids", [])],
                        missing_information=[str(m) for m in obj.get("missing_information", [])],
                        confidence=str(obj.get("confidence", "medium")),
                        status=status,
                        grounded_values=grounded,
                        constraints=constraints_out,
                    )
                    return packet.model_dump()
            except Exception:
                pass
        # 降级：LLM 没按 schema 输出——原文进 answer，标记低置信
        return EvidencePacket(
            answer=raw[:2000] if raw else "(knowledge agent returned empty output)",
            missing_information=["knowledge agent failed to produce a structured evidence packet"],
            confidence="low",
            status="insufficient",
        ).model_dump()


# ---------------------------------------------------------------------------
# Decision Agent
# ---------------------------------------------------------------------------
DECISION_AGENT_INSTRUCTION = """
You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

You have business tools for account actions. For KNOWLEDGE QUESTIONS (product details,
fees, rates, eligibility, policies, procedures), do NOT guess — call
ask_knowledge_agent with a SPECIFIC question. It returns a structured
evidence packet with verified facts, source document IDs, a confidence level,
and a status field.

Calling ask_knowledge_agent effectively:
- question: one focused question (not "tell me everything about X").
- known_constraints: pass the user's relevant constraints (e.g. annual fee limits)
  so the knowledge agent searches accordingly.
- needed_information: list what specifically you still need to know.

Reading the packet:
- facts are verified claims with source document IDs.
- status says whether the evidence is sufficient for you to act:
  "sufficient" = proceed; "partial" = usable but gaps remain;
  "insufficient" = ask a refined question or tell the user what is unclear.
- Do not treat high confidence on individual facts as proof the whole task is solved.

If a [Working Memory] block appears in your system prompt, it summarizes what you
already know in this task — use it to avoid re-asking questions already answered.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


# ---------------------------------------------------------------------------
# V1.3: procedural efficiency instruction（失败分析驱动的最小改进，可回退）
#
# 数据依据（12 个顽固失败任务，V0 winter trace 实测）：
#   - 动作数 >8 的任务成功率 0/8；成功任务平均 2 个动作、失败任务平均 11.7
#   - max_steps 死亡任务停在 31 轮 assistant 响应：
#     纯文本轮占 23%~58%（task_088 一半是解说）
#     单调用轮约一半（能并行的没并行）
#     unlock 与 call 分离成两轮
# 三条指令全部针对"长动作序列执行持久力"：
#   1. 并行批处理（多对象同流程 → 一轮发出所有调用）
#   2. unlock+call 同轮合并
#   3. 减少解说轮（先讲完整计划，然后连续执行）
# 不改变：检索/KM/memory/评分/工具 schema——变量只有 DA 的执行风格。
# ---------------------------------------------------------------------------
DECISION_AGENT_INSTRUCTION_EFFICIENT = """
You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

You have business tools for account actions. For KNOWLEDGE QUESTIONS (product details,
fees, rates, eligibility, policies, procedures), do NOT guess — call
ask_knowledge_agent with a SPECIFIC question. It returns a structured
evidence packet with verified facts, source document IDs, a confidence level,
and a status field.

Calling ask_knowledge_agent effectively:
- question: one focused question (not "tell me everything about X").
- known_constraints: pass the user's relevant constraints (e.g. annual fee limits)
  so the knowledge agent searches accordingly.
- needed_information: list what specifically you still need to know.

Reading the packet:
- facts are verified claims with source document IDs.
- status says whether the evidence is sufficient for you to act:
  "sufficient" = proceed; "partial" = usable but gaps remain;
  "insufficient" = ask a refined question or tell the user what is unclear.
- Do not treat high confidence on individual facts as proof the whole task is solved.

If a [Working Memory] block appears in your system prompt, it summarizes what you
already know in this task — use it to avoid re-asking questions already answered.

WORKING EFFICIENTLY ON MULTI-STEP PROCEDURES (important — your turn budget is limited):
- When a procedure requires the same steps for MULTIPLE cards/accounts (e.g. freeze
  three cards, or close two accounts), do them ALL in one response: issue every
  independent tool call together in parallel instead of one per turn.
- When a procedure requires an unlock tool before calling the actual tool, put the
  unlock call AND the follow-up call in the SAME response — do not split them across
  turns waiting for the unlock result when the follow-up arguments are already known.
- Before starting a multi-step procedure, briefly tell the user the full plan in ONE
  message. Then execute the steps continuously without pausing to explain each one.
  A short progress note every few completed steps is enough — do not spend a whole
  turn narrating each individual action.
- Only pause for user input when the policy or a missing fact genuinely requires it.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


# V2 harness 变体的增量提示：告诉 DA 校验拒绝长什么样、如何修正。
# 轻量（第六节要求）——不是自我检查 prompt，只是对结构化错误的响应方式。
HARNESS_INSTRUCTION_APPENDIX = """

Tool calls may be validated before execution. If a tool call is rejected with a
"Harness validation failed" message, recover like this:
- The message lists the exact field, your proposed value, the constraint source,
  and the confirmed or allowed value. Change ONLY the listed field(s) to the
  listed value and immediately call the SAME tool again with the corrected
  arguments.
- Do not retry with the same values. Do not re-plan the task or switch tools —
  the rest of your arguments were fine; only the listed field was wrong.
- If the source is "user" (user_message), the confirmed_value is what the
  customer explicitly asked for — use it exactly.
- If allowed_values is listed, pick from it; if confirmed_value is listed, use it.
""".strip()

INSTRUCTION_VARIANTS = {
    "two_agent": DECISION_AGENT_INSTRUCTION,
    "two_agent_efficient": DECISION_AGENT_INSTRUCTION_EFFICIENT,
    # V2: V1.2 instruction + harness 拒绝响应方式（架构唯一变量是 harness 本身）
    "two_agent_harness": DECISION_AGENT_INSTRUCTION + "\n" + HARNESS_INSTRUCTION_APPENDIX,
}


class DecisionAgent(LLMAgent):
    """LLMAgent 子类：换 instruction + 拦截 ask_knowledge_agent 调用。

    拦截原因：orchestrator 把 agent 的工具调用统一交给
    environment.get_response 执行，而 ask_knowledge_agent 不是环境工具
    （它要调 Knowledge Agent）。因此在 agent 侧拦截：LLM 产出 ask 调用时，
    就地运行 KnowledgeAgent，把 Evidence Packet 作为 ToolMessage 回填进
    agent state，再让 LLM 继续——语义与 orchestrator 执行普通工具完全一致
    （半双工：agent 连续行动直到发文本消息）。

    Evidence Packet 以 ToolMessage 形式进入 Decision Agent context——
    这正是设计意图（压缩证据替代原始检索 dump）。其余消息循环完全复用
    官方 LLMAgent（保证与 V0 可比）。
    """

    MAX_INTERCEPT_ROUNDS = 12  # 单轮内拦截上限：防 ask 无限循环（远超合理用量）

    def __init__(self, tools, domain_policy, knowledge_agent: KnowledgeAgent,
                 memory: Optional["DecisionWorkingMemory"] = None,
                 instruction_variant: str = "two_agent",
                 harness_enabled: bool = False, **kwargs):
        super().__init__(tools=tools, domain_policy=domain_policy, **kwargs)
        self._knowledge_agent = knowledge_agent
        self.memory = memory  # None 时不启用（向后兼容 V1 行为）
        # V1.3: instruction 变体（two_agent = V1.2 原样；two_agent_efficient = 执行效率版）
        self._instruction_variant = instruction_variant
        # V2: Action Harness（None = 关闭，V1.2 行为；registry two_agent_harness 启用）
        from agents.harness import ActionHarness
        from agents.harness.task_state_v3 import TaskStateV3
        self.task_state = TaskStateV3()  # V3: 对象级任务状态（主状态）
        self.harness = ActionHarness() if harness_enabled else None
        if self.harness is not None:
            # 三源接线：TaskState 由本 agent 喂（user/tool result），
            # KB 约束由 packets 累积（_do_ask 里 update）
            self.harness.wire(task_state=self.task_state, kb_constraints=[])
        self._packets: list = []  # 本任务收到的 Evidence Packets
        self._tool_by_name = {t.name: t for t in tools if t.name != "ask_knowledge_agent"}
        # V2.3 recovery 预算：同一 (inner_tool, field) 最多被 harness 拒 N 次
        # ——超过后该字段不再拦（防死循环；运行时工具自身报错兜底）。
        self._rejection_counts: dict = {}
        self.MAX_SAME_FIELD_REJECTIONS = 2

    # -- V2.2: TaskState 三源喂入 ------------------------------------------------
    def _feed_task_state(self, message) -> None:
        """把进入 agent 的消息中的明确值灌入 TaskStateV3（确定性，无 LLM）。

        V3：user → 对象命名空间（transfer_request.amount）；
        tool result → 实体绑定（card_dbc_123.status）——注意 tau2 单结果
        是裸 ToolMessage、多结果才包 MultiToolMessage（两种都处理）；
        knowledge → 规则命名空间（_do_ask 后 constraints 刷新）。
        """
        if self.task_state is None:
            return
        try:
            from agents.harness.task_state_v3 import (
                UserStateExtractor, ToolResultStateExtractor,
            )
            from tau2.data_model.message import MultiToolMessage, ToolMessage
            if isinstance(message, UserMessage):
                UserStateExtractor.feed(
                    self.task_state, message.content or "",
                    turn_ref="user_message")
            elif isinstance(message, MultiToolMessage):
                for tm in message.tool_messages:
                    ToolResultStateExtractor.feed(
                        self.task_state, "", tm.content or "")
            elif isinstance(message, ToolMessage):
                # tau2 _wrap_tool_results：单结果是裸 ToolMessage
                # （ToolMessage 无 name 字段——source_ref 用通用标记）
                ToolResultStateExtractor.feed(
                    self.task_state, "tool_result", message.content or "")
        except Exception:
            pass  # 状态喂入失败不影响对话流

    def _update_kb_constraints(self) -> None:
        """KB 约束 → TaskStateV3（V3 统一事实源）。

        规则只进 Task State 一份；kb_validator 从状态读（wire 已接），
        不再单独维护约束列表。
        """
        try:
            from agents.harness import constraints_from_packets
            cons = constraints_from_packets(self._packets)
            if self.task_state is not None and cons:
                from agents.harness.task_state_v3 import KnowledgeStateExtractor
                KnowledgeStateExtractor.feed_constraints(self.task_state, cons)
        except Exception:
            pass

    def policies_of_harness(self):
        return getattr(self.harness, "policies", [])

    def _harness_context(self, arguments: dict):
        """构建校验上下文（V2.1 provenance 修复）。

        evidence：本任务 packets 的 grounded_values（tool/param 索引）。
        user_context：只提取【可可靠确定参数归属】的用户值——
        Decision memory verified_facts 里形如 "<param> = <value>" 的显式
        赋值（如 "amount = 500"）。匹配不上的（自然语言约束）不构造键
        ——缺 provenance 保持 not_grounded 放行（不猜）。
        """
        import re as _re
        from agents.harness import context_from_packets
        user_values = {}
        if self.memory is not None:
            mem = self.memory.read()
            # 保守提取：verified_facts 中 "param = value" 显式模式
            for fact in mem.get("verified_facts", []):
                m = _re.match(r"^\s*([a-z_][a-z0-9_]*)\s*=\s*(\S+)\s*$",
                              str(fact), _re.IGNORECASE)
                if m:
                    user_values[m.group(1).lower()] = m.group(2)
        return context_from_packets(self._packets, user_values=user_values or None)

    @property
    def system_prompt(self) -> str:
        from tau2.agent.llm_agent import SYSTEM_PROMPT
        # 注意：memory block 不在这里注入——system_prompt 只在 agent init 时
        # 渲染一次，那时 memory 为空。动态注入见 _messages_with_memory()
        # （每次 generate 前把最新 memory block 拼到 system 消息尾部）。
        instruction = INSTRUCTION_VARIANTS.get(
            self._instruction_variant, DECISION_AGENT_INSTRUCTION
        )
        return SYSTEM_PROMPT.format(
            domain_policy=self.domain_policy,
            agent_instruction=instruction,
        )

    def generate_next_message(self, message, state):
        """覆盖：拦截 ask_knowledge_agent，其余行为与 LLMAgent 一致。"""
        # V2.2: 进入消息的明确值 → TaskState（user 金额 / tool ID）
        self._feed_task_state(message)
        # 与官方实现相同的入队逻辑（UserMessage/MultiToolMessage）
        if isinstance(message, UserMessage) and message.is_audio:
            raise ValueError("User message cannot be audio.")
        if hasattr(message, "tool_messages"):  # MultiToolMessage
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        assistant_message = None
        for _ in range(self.MAX_INTERCEPT_ROUNDS):
            # V1.1: 每次生成前注入最新 memory block——system_prompt property
            # 只在 init 时渲染一次，动态 memory 必须在这里按需拼装。
            # base_prompt 缓存无 memory 版本，memory block 追加其上（不改 state）。
            full = self._messages_with_memory(state)
            assistant_message = generate(
                model=self.llm,
                tools=self.tools,
                messages=full,
                call_name="agent_response",
                **self.llm_args,
            )
            if not (assistant_message.is_tool_call()
                    and (assistant_message.tool_calls or [])):
                break  # 纯文本 → 与官方行为一致

            calls = assistant_message.tool_calls or []
            ask_calls = [tc for tc in calls if tc.name == "ask_knowledge_agent"]
            biz_calls = [tc for tc in calls if tc.name != "ask_knowledge_agent"]

            # ---- V2 Harness：业务工具执行前校验（只拦 rejected；通过的原样
            # 留给 orchestrator 执行，保持消息结构与 V1.2 完全兼容）----
            rejected = {}
            if self.harness is not None and biz_calls:
                ctx = self._harness_context({})
                for tc in biz_calls:
                    tool = self._tool_by_name.get(tc.name)
                    if tool is None:
                        continue
                    # 临时挂 tool 供 harness 读 schema
                    tc._harness_tool = tool  # noqa: SLF001
                    passed, content, meta = self.harness.process(
                        tc, execute=None, context=ctx, validate_only=True)
                    if not passed:
                        # V2.3 recovery 预算：超限的 (tool, field) 不再拦
                        # ——放行原始调用让运行时工具报错兜底（不烧循环 token）
                        res = (meta or {}).get("resolved")
                        inner_name = getattr(res, "tool_name", tc.name)
                        blocking_fields = [
                            v.field for v in (meta.get("verdicts") or [])
                            if getattr(v, "verdict", "") in (
                                "schema_violation", "evidence_mismatch",
                                "task_state_conflict", "kb_enum_violation",
                                "kb_threshold_violation", "kb_format_violation")
                        ]
                        over_budget = [
                            f for f in blocking_fields
                            if self._rejection_counts.get((inner_name, f), 0) >= self.MAX_SAME_FIELD_REJECTIONS
                        ]
                        if over_budget:
                            # 该调用已有过 N 次同类拒绝——本次放行（自然失败兜底）
                            continue
                        for f in blocking_fields:
                            self._rejection_counts[(inner_name, f)] = \
                                self._rejection_counts.get((inner_name, f), 0) + 1
                        rejected[tc.id] = content
                # 清理临时挂载
                for tc in biz_calls:
                    if hasattr(tc, "_harness_tool"):
                        del tc._harness_tool

            if not ask_calls and not rejected:
                # 全部通过（或纯业务工具）→ 原样交给 orchestrator（V1.2 路径）
                break

            # ---- 拦截处理：ask 执行 + rejected 回填错误，通过的剔除 ----
            state.messages.append(assistant_message)
            for tc in ask_calls:
                packet_json = self._do_ask(tc)
                state.messages.append(ToolMessage(
                    id=tc.id, role="tool", requestor="assistant",
                    content=packet_json, error=False,
                ))
                try:
                    self._packets.append(json.loads(packet_json))
                except Exception:
                    pass
                self._update_kb_constraints()  # V2.2: KB 明确约束刷新进 harness
            for tc, err in rejected.items():
                state.messages.append(ToolMessage(
                    id=tc, role="tool", requestor="assistant",
                    content=err, error=True,
                ))
            # 从 assistant 消息剔除已就地处理的调用；剩下的（通过的 biz 调用）
            remaining = [
                tc for tc in (assistant_message.tool_calls or [])
                if tc.name != "ask_knowledge_agent" and tc.id not in rejected
            ]
            if remaining and not ask_calls:
                # 只剩通过的 biz 调用且无 ask → 剔除后若为空则继续循环；
                # 这里 remaining 非空但 rejected 也在同一条消息里——把 rejected
                # 已回填、通过的留下给 orchestrator
                assistant_message.tool_calls = remaining
                return assistant_message, state
            if remaining:
                assistant_message.tool_calls = remaining
                return assistant_message, state
            assistant_message = None  # 全部就地处理完 → 循环继续生成下一步
        # 兜底：不应到达（MAX 轮内必有非 ask 输出），防御性生成纯文本
        if assistant_message is None:
            full = self._messages_with_memory(state)
            assistant_message = generate(
                model=self.llm,
                tools=[t for t in self.tools if t.name != "ask_knowledge_agent"],
                messages=full,
                call_name="agent_response",
                **self.llm_args,
            )
        state.messages.append(assistant_message)
        return assistant_message, state

    def _messages_with_memory(self, state):
        """返回 system(含 memory block + V3 task-state slice) + 历史消息。"""
        blocks = []
        mem_block = self.memory.context_block() if self.memory is not None else ""
        if mem_block:
            blocks.append(mem_block)
        # V3: 对象级任务状态切片（当前有效，非全量）
        state_block = self._task_state_block(
            tool_name=self._recent_biz_tool(state))
        if state_block:
            blocks.append(state_block)
        if not blocks or not state.system_messages:
            return state.system_messages + state.messages
        sys_msg = state.system_messages[0].model_copy(deep=True)
        sys_msg.content = (sys_msg.content or "") + "\n\n" + "\n\n".join(blocks)
        return [sys_msg] + list(state.system_messages[1:]) + state.messages

    def _recent_biz_tool(self, state) -> str:
        """最近一次业务工具名（动作相关状态选择的提示，确定性）。

        取 state.messages 里最后一条 assistant 消息的最后一个非-ask
        工具调用——DA 上一个动作即当前意图锚点（转账中 → transfer 工具
        命名空间的规则/实体优先展示）。
        """
        try:
            from tau2.data_model.message import AssistantMessage
            for m in reversed(state.messages):
                if isinstance(m, AssistantMessage) and m.tool_calls:
                    for tc in reversed(m.tool_calls):
                        if tc.name != "ask_knowledge_agent":
                            # wrapper 调用 → 解 inner 工具名（与动作相关的
                            # 真正业务工具）
                            args = getattr(tc, "arguments", None) or {}
                            inner = args.get("agent_tool_name") if isinstance(args, dict) else None
                            return inner or tc.name
            return ""
        except Exception:
            return ""

    def _task_state_block(self, max_chars: int = 900,
                          tool_name: str = "") -> str:
        """渲染 TaskStateV3 的当前有效状态——V3 收口：动作相关优先。

        选择逻辑（确定性，无 LLM）：
        P0 该工具命名空间的 knowledge 规则（tool.param.allowed_values…）
           + 该工具直接涉及的实体（proposed 值命中 entity 索引）
        P1 user 的请求对象（活跃意图：transfer_request.* 等——DA 正在
           执行用户意愿）
        P2 其他 user/tool 实体状态（背景）
        每层内按写入时间（seq）排序；预算内放不下则截断 P2（保 P0/P1）。
        tool_name: DA 即将调用的工具（generate 循环里最近一次 biz 调用；
        为空时退化为全量当前状态（无动作上下文）。
        """
        if self.task_state is None:
            return ""
        try:
            p0, p1, p2 = [], [], []
            t = (tool_name or "").strip().lower()
            for key, chain in getattr(self.task_state, "_entries", {}).items():
                cur = chain[-1]
                if not cur.is_current:
                    continue
                item = (key, cur)
                if t and (cur.object == t):
                    p0.append(item)            # 该工具的 knowledge 规则
                elif cur.source == "user":
                    p1.append(item)            # 用户请求对象（活跃意图）
                elif cur.source == "tool":
                    p2.append(item)            # 实体背景
                else:
                    p2.append(item)            # knowledge 其他工具规则（背景）
            def _fmt(key, cur):
                ref = f" (from {cur.source_ref})" if cur.source_ref else ""
                return f"- {key} = {cur.value} [source: {cur.source}{ref}]"
            lines = []
            for layer in (p0, p1, p2):
                for key, cur in sorted(layer, key=lambda x: x[1].seq):
                    lines.append(_fmt(key, cur))
            if not lines:
                return ""
            text = ("[Confirmed task state — use these exact values for matching fields; "
                    "do not confuse similar fields of different objects:]\n" + "\n".join(lines))
            if len(text) > max_chars:
                # 预算溢出：先保 P0/P1，P2 逐条舍（不机械中间截断）
                keep = []
                used = 200  # 头部说明占位
                for layer in (p0, p1, p2):
                    for key, cur in sorted(layer, key=lambda x: x[1].seq):
                        ln = _fmt(key, cur)
                        if used + len(ln) <= max_chars or layer is p0:
                            keep.append(ln)
                            used += len(ln) + 1
                text = ("[Confirmed task state — use these exact values for matching fields; "
                        "do not confuse similar fields of different objects:]\n" + "\n".join(keep))
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n... (state truncated)"
            return text
        except Exception:
            return ""
            text = "[Confirmed task state — use these exact values for matching fields; do not confuse similar fields of different objects:]\n" + "\n".join(entries)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... (state truncated)"
            return text
        except Exception:
            return ""

    def _do_ask(self, tool_call) -> str:
        """执行 ask_knowledge_agent：handoff 到 Knowledge Agent，返回 packet JSON。

        V1.1：构建结构化 KnowledgeRequest；packet 的 facts/missing_information
        程序化写入 Decision memory（无 LLM）。trace 记 handoff 事件。
        """
        import time as _time
        args = tool_call.arguments or {}

        # 结构化 KnowledgeRequest（从 ask 参数直接映射）
        request = KnowledgeRequest(
            question=args.get("question", ""),
            known_constraints=args.get("known_constraints") or [],
            known_facts=args.get("known_facts") or [],
            needed_information=args.get("needed_information") or [],
        )
        # 若 DA 有 memory：把已知约束/事实自动补充进 request（省 LLM 复述）
        if self.memory is not None:
            mem = self.memory.read()
            known = set(request.known_constraints)
            for c in mem.get("user_constraints", []):
                if c not in known:
                    request.known_constraints.append(c)
            # DA memory 的 verified_facts 作为 known_facts 补充（去重）
            kf = {f for f in request.known_facts}
            for f in mem.get("verified_facts", []):
                if f not in kf:
                    request.known_facts.append(f)

        rec = None
        try:
            from eval.instrumentation import get_active_recorder
            rec = get_active_recorder()
        except Exception:
            rec = None
        if rec is not None:
            try:
                rec.emit("handoff", "decision_agent",
                         parent_span_id=getattr(rec, "task_span_id", None),
                         to_agent="knowledge_agent",
                         question=request.question[:500],
                         known_constraints=request.known_constraints,
                         needed_information=request.needed_information)
            except Exception:
                pass

        t0 = _time.perf_counter()
        packet = self._knowledge_agent.answer(request)
        packet_json = json.dumps(packet, ensure_ascii=False)

        # packet → DA memory（确定性提取，无 LLM）：
        # facts 作为已确认事实；missing 作为待决问题
        if self.memory is not None:
            for f in packet.get("facts") or []:
                claim = f.get("claim")
                doc = f.get("source_doc_id")
                if claim:
                    self.memory.update({
                        "type": "verified_fact",
                        "fact": f"{claim} [source: {doc}]" if doc else claim,
                    })
            for miss in packet.get("missing_information") or []:
                self.memory.update({"type": "open_question", "question": miss})

        if rec is not None:
            try:
                rec.emit("handoff_result", "knowledge_agent",
                         parent_span_id=getattr(rec, "task_span_id", None),
                         latency_ms=round((_time.perf_counter() - t0) * 1000, 1),
                         evidence_packet_chars=len(packet_json),
                         evidence_doc_ids=packet.get("relevant_document_ids", []),
                         evidence_fact_count=len(packet.get("facts", [])),
                         evidence_confidence=packet.get("confidence"),
                         evidence_status=packet.get("status"),
                         missing_information_count=len(packet.get("missing_information", [])),
                         )
            except Exception:
                pass
        return packet_json


# ---------------------------------------------------------------------------
# ask_knowledge_agent 工具（schema only）
# ---------------------------------------------------------------------------
def make_ask_knowledge_tool() -> Tool:
    """构造 ask_knowledge_agent 工具 schema（供 LLM tool_choice）。

    注意1：tau2 Tool 的 name 派生自函数 __name__（构造参数 name 无效），
    因此内层函数必须命名为 ask_knowledge_agent。
    注意2：这个工具的执行不经过 orchestrator/environment——DecisionAgent
    在 generate_next_message 里拦截并就地 handoff 到 KnowledgeAgent
    （orchestrator 只认环境注册的工具，直接交给它会报 unknown tool）。
    这里只需要 schema 让 LLM 知道"可以问知识专员"。
    """
    from tau2.environment.tool import as_tool

    def ask_knowledge_agent(question: str,
                            known_constraints: List[str] = None,
                            known_facts: List[str] = None,
                            needed_information: List[str] = None) -> str:
        """Ask the knowledge specialist agent to look up verified facts from the
        knowledge base. Use for product details, fees, rates, eligibility rules,
        procedures, or policy questions. Returns a structured evidence packet:
        answer, verified facts with source document IDs, confidence, and a
        status field (sufficient/partial/insufficient) telling you whether the
        evidence is enough to proceed.

        Args:
            question: One focused question, e.g. 'What is the annual fee and
                cash back rate of the Platinum Rewards Card?' — do NOT ask
                broad open-ended questions like 'tell me about all cards'.
            known_constraints: User constraints relevant to this question,
                e.g. ['annual fee <= $100', 'user travels monthly'].
            known_facts: Facts you already established, e.g.
                ['user has a Gold Rewards Card'].
            needed_information: What specifically you still need to find,
                e.g. ['fee waiver conditions'].
        """
        # 实际不会走到这里（DecisionAgent 拦截执行）；保底实现防御性调用
        return json.dumps(
            EvidencePacket(
                answer="(executed via agent-side interception path)",
                confidence="low",
                status="insufficient",
            ).model_dump()
        )

    return as_tool(ask_knowledge_agent)


# ---------------------------------------------------------------------------
# Factory：注册进 agents/registry.py
# ---------------------------------------------------------------------------
def create_two_agent(tools, domain_policy, instruction_variant="two_agent",
                     harness_enabled: bool = False, **kwargs) -> DecisionAgent:
    """V1.1+ factory：分流工具 → 双 memory → KnowledgeAgent → DecisionAgent。

    与 create_llm_agent 同签名（build.py 调用约定）。
    memory 由 runner 在 task 边界调 start_task/end_task（见 eval/runner.py）。

    Args:
        instruction_variant: "two_agent" = V1.2 原样 instruction；
            "two_agent_efficient" = V1.3 执行效率 instruction
            （唯一变量：长流程批处理/合并/少解说——完全可回退）。
        harness_enabled: V2 Action Harness（业务工具执行前 schema+evidence
            校验；False = V1.2 行为原样）。
    """
    llm = kwargs.get("llm")
    llm_args = kwargs.get("llm_args") or {}

    business_tools, knowledge_tools = split_tools(tools)
    if not knowledge_tools:
        raise ValueError(
            "two_agent 需要至少一个知识检索工具（KB_search 等），"
            "但 environment.get_tools() 里没有找到。请确认 domain/retrieval config。"
        )

    # V1.1: per-agent working memory（默认启用；disable_memory=True 回退 V1 行为）
    from agents.memory import DecisionWorkingMemory, KnowledgeWorkingMemory
    da_memory = DecisionWorkingMemory()
    ka_memory = KnowledgeWorkingMemory()

    knowledge_agent = KnowledgeAgent(
        tools=knowledge_tools, llm=llm, llm_args=llm_args,
        memory=ka_memory,
    )
    ask_tool = make_ask_knowledge_tool()
    return DecisionAgent(
        tools=business_tools + [ask_tool],
        domain_policy=domain_policy,
        knowledge_agent=knowledge_agent,
        memory=da_memory,
        llm=llm,
        llm_args=llm_args,
        instruction_variant=instruction_variant,
        harness_enabled=harness_enabled,
    )


def create_two_agent_efficient(tools, domain_policy, **kwargs) -> DecisionAgent:
    """V1.3 factory：two_agent_efficient 逻辑名入口。

    与 create_two_agent 完全同构，唯一差异 instruction_variant
    （V1.2 行为用 --agent two_agent 即可完整回退）。
    """
    return create_two_agent(
        tools, domain_policy, instruction_variant="two_agent_efficient", **kwargs
    )


def create_two_agent_harness(tools, domain_policy, **kwargs) -> DecisionAgent:
    """V2 factory：two_agent_harness = V1.2 架构 + Action Harness。

    与 V1.2 唯一差异 harness_enabled=True（业务工具执行前经过
    schema + evidence-grounded 校验；rejection 回传结构化错误让 DA 自行修正）。
    回退方式：--agent two_agent 即 V1.2 原样（harness 代码保留不删）。
    """
    return create_two_agent(
        tools, domain_policy, instruction_variant="two_agent_harness",
        harness_enabled=True, **kwargs,
    )
