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


EVIDENCE_PACKET_SYSTEM_PROMPT = """You are a knowledge specialist for a bank's customer service system.

You have access to knowledge base search tools. Your job:
1. Read the question from the decision agent (and any user constraints provided).
2. Formulate search queries and search the knowledge base as many times as needed.
3. Read the search results carefully.
4. Decide: do you have enough evidence to answer? If not, search again with better queries.
5. When you have enough evidence, return your final answer AS A JSON EVIDENCE PACKET.

The evidence packet schema:
{
  "answer": "<concise synthesized answer>",
  "facts": [{"claim": "<factual claim>", "source_doc_id": "<doc id from search results>"}],
  "relevant_document_ids": ["<doc ids consulted>"],
  "missing_information": ["<aspects you could not answer>"],
  "confidence": "high|medium|low"
}

STRICT RULES:
- source_doc_id values MUST be actual document IDs you saw in search results. Never invent IDs.
- Do not copy large passages of raw text into the packet — distill facts.
- If evidence is insufficient, say so in missing_information and set confidence to low.
- You return ONLY the JSON packet as your final message — no other text around it.
"""

KNOWLEDGE_AGENT_MAX_ROUNDS = 6  # 防失控：一次 handoff 内最多 6 轮（检索+推理）


class KnowledgeAgent:
    """知识专员：独立 context，只拿知识工具，产出 Evidence Packet。

    不继承 HalfDuplexAgent——它不直接参与 user 对话轮，而是由
    DecisionAgent 通过 ask_knowledge_agent 工具同步调用。
    每次 handoff 是一个全新 context（不跨 handoff 记忆，V1 保持最小）。
    """

    def __init__(self, tools: List[Tool], llm: str, llm_args: Optional[dict] = None):
        self.tools = tools  # 只有 KB 检索工具
        self.llm = llm
        self.llm_args = dict(llm_args or {})

    def answer(self, question: str, context_note: str = "") -> dict:
        """回答一个知识问题，返回 EvidencePacket dict。

        流程：构建 system + user 消息 → 循环（LLM → 若调工具则执行 → 回填）→
        LLM 输出 JSON packet。失败时返回带 missing_information 的降级 packet。
        """
        user_content = f"Question from the decision agent:\n{question}"
        if context_note:
            user_content += f"\n\nUser constraints / task context (for query formulation only):\n{context_note}"

        system = SystemMessage(role="system", content=EVIDENCE_PACKET_SYSTEM_PROMPT)
        user_msg = UserMessage(role="user", content=user_content)
        messages: List[Message] = [system, user_msg]

        tools_schema = [t.openai_schema for t in self.tools] or None

        for _round in range(KNOWLEDGE_AGENT_MAX_ROUNDS):
            assistant = generate(
                model=self.llm,
                messages=messages,
                tools=self.tools,
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

        return self._parse_packet(assistant)

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
                    packet = EvidencePacket(
                        answer=str(obj.get("answer", "")),
                        facts=[EvidenceFact(**f) for f in obj.get("facts", []) if isinstance(f, dict) and "claim" in f],
                        relevant_document_ids=[str(d) for d in obj.get("relevant_document_ids", [])],
                        missing_information=[str(m) for m in obj.get("missing_information", [])],
                        confidence=str(obj.get("confidence", "medium")),
                    )
                    return packet.model_dump()
            except Exception:
                pass
        # 降级：LLM 没按 schema 输出——原文进 answer，标记低置信
        return EvidencePacket(
            answer=raw[:2000] if raw else "(knowledge agent returned empty output)",
            missing_information=["knowledge agent failed to produce a structured evidence packet"],
            confidence="low",
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
ask_knowledge_agent with a clear, specific question. It returns a structured
evidence packet with verified facts and source document IDs.

Use the evidence packet to answer the user or inform your decisions. If the packet
reports missing_information or low confidence, you may ask the knowledge agent
a refined question, or tell the user honestly what is unclear.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


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

    def __init__(self, tools, domain_policy, knowledge_agent: KnowledgeAgent, **kwargs):
        super().__init__(tools=tools, domain_policy=domain_policy, **kwargs)
        self._knowledge_agent = knowledge_agent

    @property
    def system_prompt(self) -> str:
        from tau2.agent.llm_agent import SYSTEM_PROMPT
        return SYSTEM_PROMPT.format(
            domain_policy=self.domain_policy,
            agent_instruction=DECISION_AGENT_INSTRUCTION,
        )

    def generate_next_message(self, message, state):
        """覆盖：拦截 ask_knowledge_agent，其余行为与 LLMAgent 一致。"""
        # 与官方实现相同的入队逻辑（UserMessage/MultiToolMessage）
        if isinstance(message, UserMessage) and message.is_audio:
            raise ValueError("User message cannot be audio.")
        if hasattr(message, "tool_messages"):  # MultiToolMessage
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        assistant_message = None
        for _ in range(self.MAX_INTERCEPT_ROUNDS):
            full = state.system_messages + state.messages
            assistant_message = generate(
                model=self.llm,
                tools=self.tools,
                messages=full,
                call_name="agent_response",
                **self.llm_args,
            )
            # 没有 ask 调用（纯文本或纯业务工具）→ 与官方行为一致，直接返回
            ask_calls = [
                tc for tc in (assistant_message.tool_calls or [])
                if tc.name == "ask_knowledge_agent"
            ] if assistant_message.is_tool_call() else []
            if not ask_calls:
                break
            # 拦截：先入队 assistant 消息，再执行每个 ask 并回填 packet
            state.messages.append(assistant_message)
            for tc in ask_calls:
                packet_json = self._do_ask(tc)
                state.messages.append(ToolMessage(
                    id=tc.id, role="tool", requestor="assistant",
                    content=packet_json, error=False,
                ))
            # 非混合调用：ask 单独出现时已处理完，循环让 LLM 看到 packet 继续
            other_calls = [
                tc for tc in (assistant_message.tool_calls or [])
                if tc.name != "ask_knowledge_agent"
            ]
            if other_calls:
                # 混合调用：让 orchestrator 执行业务工具——但 assistant 消息
                # 已含 ask 调用，orchestrator 会因未知工具报错。
                # 处理：把 ask 的 tool_calls 从消息里剔除（结果已回填），
                # 只把业务工具留给 orchestrator。若剔除后为空，继续循环。
                remaining = [tc for tc in assistant_message.tool_calls if tc.name != "ask_knowledge_agent"]
                if remaining:
                    assistant_message.tool_calls = remaining
                    return assistant_message, state
                continue
            assistant_message = None  # 纯 ask：吃掉本条，循环继续生成
        # 兜底：不应到达（MAX 轮内必有非 ask 输出），防御性生成纯文本
        if assistant_message is None:
            full = state.system_messages + state.messages
            assistant_message = generate(
                model=self.llm,
                tools=[t for t in self.tools if t.name != "ask_knowledge_agent"],
                messages=full,
                call_name="agent_response",
                **self.llm_args,
            )
        state.messages.append(assistant_message)
        return assistant_message, state

    def _do_ask(self, tool_call) -> str:
        """执行 ask_knowledge_agent：handoff 到 Knowledge Agent，返回 packet JSON。

        向 trace v2 发 handoff 事件（记录 question + packet 摘要）。
        """
        import time as _time
        args = tool_call.arguments or {}
        question = args.get("question", "")
        context = args.get("context", "")

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
                         question=question[:500])
            except Exception:
                pass

        t0 = _time.perf_counter()
        packet = self._knowledge_agent.answer(question, context_note=context)
        packet_json = json.dumps(packet, ensure_ascii=False)

        if rec is not None:
            try:
                rec.emit("handoff_result", "knowledge_agent",
                         parent_span_id=getattr(rec, "task_span_id", None),
                         latency_ms=round((_time.perf_counter() - t0) * 1000, 1),
                         evidence_packet_chars=len(packet_json),
                         evidence_doc_ids=packet.get("relevant_document_ids", []),
                         evidence_fact_count=len(packet.get("facts", [])),
                         evidence_confidence=packet.get("confidence"),
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

    def ask_knowledge_agent(question: str, context: str = "") -> str:
        """Ask the knowledge specialist agent to look up verified facts from the
        knowledge base. Use for product details, fees, rates, eligibility rules,
        procedures, or policy questions. Returns a structured evidence packet
        with an answer, verified facts with source document IDs, and confidence.

        Args:
            question: The specific knowledge question to answer, e.g. 'What is
                the annual fee and cash back rate of the Platinum Rewards Card?'
            context: Optional user constraints or task context the knowledge
                agent should account for, e.g. 'user wants no annual fee'.
        """
        # 实际不会走到这里（DecisionAgent 拦截执行）；保底实现防御性调用
        return json.dumps(
            EvidencePacket(
                answer="(executed via agent-side interception path)",
                confidence="low",
            ).model_dump()
        )

    return as_tool(ask_knowledge_agent)


# ---------------------------------------------------------------------------
# Factory：注册进 agents/registry.py
# ---------------------------------------------------------------------------
def create_two_agent(tools, domain_policy, **kwargs) -> DecisionAgent:
    """V1 factory：分流工具 → 构造 KnowledgeAgent → 注入 ask 工具 → DecisionAgent。

    与 create_llm_agent 同签名（build.py 调用约定）。
    """
    llm = kwargs.get("llm")
    llm_args = kwargs.get("llm_args") or {}

    business_tools, knowledge_tools = split_tools(tools)
    if not knowledge_tools:
        raise ValueError(
            "two_agent 需要至少一个知识检索工具（KB_search 等），"
            "但 environment.get_tools() 里没有找到。请确认 domain/retrieval config。"
        )

    knowledge_agent = KnowledgeAgent(
        tools=knowledge_tools, llm=llm, llm_args=llm_args,
    )
    ask_tool = make_ask_knowledge_tool()
    return DecisionAgent(
        tools=business_tools + [ask_tool],
        domain_policy=domain_policy,
        knowledge_agent=knowledge_agent,
        llm=llm,
        llm_args=llm_args,
    )
