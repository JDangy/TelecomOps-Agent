"""Action Resolver（V2.1；integrity 收口后）——把 wrapper 调用解析成真实业务动作。

背景（V2 targeted 的结构性发现）：
    banking 域关键参数藏在 call_discoverable_agent_tool(tool_name, arguments)
    的内层 JSON 字符串里——V2 harness 只能看到 wrapper 的
    {agent_tool_name: string, arguments: string}，抓不到 reason/amount/
    account_type 等真实错误。

职责分离（严格遵守）：
    Resolver 负责"看懂调用"——解析 wrapper、找到 inner tool 与 schema。
    Harness 负责"检查调用"——对 ResolvedAction 应用校验。
    不把解析逻辑硬编码进 validator；Harness 不自动改参数。

inner tool schema 来源（Benchmark Integrity Cleanup 后的合法边界）：
    官方流程：KB 检索 → unlock_discoverable_agent_tool → 环境向 Agent
    暴露工具定义 → Agent 才能调用。Harness 不得拥有比 Agent 更强的
    "上帝视角"——因此：

    - **已 unlock 的工具**：schema 取自 toolkit 的
      _agent_discoverable_tools_state[name]["tool_info"]——这是 Agent
      通过官方 unlock 流程**已经合法看到**的工具定义（同一份解析，
      无新信息）。
    - **未 unlock 的工具**：不读其 signature/docstring/enum（那属于
      benchmark 内部实现），inner_schema 置空、
      resolve_error="inner_tool_not_unlocked"——Harness 对其降级为
      wrapper 层校验（不 inner-validate，更不会把 hidden enum 泄漏进
      rejection message）。

    即：Harness 只能使用 Agent 在当前 task 中通过合法运行路径获得的
    信息；宁可少校验，不偷看内部实现。
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ResolvedAction:
    """统一的业务动作表示（resolver 输出 / harness 校验输入）。

    普通工具：outer == inner（tool_name/arguments 原样，schema 取自 wrapper）。
    wrapper 工具：outer 是 call_discoverable_agent_tool；inner 是解析出的
    真实工具名 + 参数 dict + **仅当该工具已被 Agent 合法 unlock 时**的
    inner schema（来自 unlock 时环境暴露给 Agent 的同一份 tool_info）。
    """

    outer_tool_name: str
    outer_arguments: dict
    tool_name: str                     # inner（真实业务工具名）
    arguments: dict                    # inner 参数
    inner_schema: Optional[dict] = None  # {properties, required, enum_from_doc}（仅已 unlock）
    is_wrapper: bool = False
    resolve_error: Optional[str] = None  # inner_tool_not_unlocked / json_error 等


class ActionResolver:
    """看懂 Agent 的工具调用（wrapper 穿透——只对已 unlock 的工具）。"""

    WRAPPER_NAMES = {
        "call_discoverable_agent_tool",
        "call_discoverable_user_tool",  # 用户侧同样可解析（一致性预留）
    }
    # wrapper 里指向 inner 工具名的参数名
    INNER_NAME_KEYS = ("agent_tool_name", "discoverable_tool_name", "tool_name")

    def __init__(self, wrapper_tool=None):
        """wrapper_tool: 环境的 call_discoverable_agent_tool Tool 对象。

        只持有 toolkit 引用（不解开全量注册表）——每次 resolve 时读取
        当前的 **unlock state**（_agent_discoverable_tools_state），即
        Agent 已合法获得定义的工具集合。
        """
        self._toolkit = None
        self._inner_registry: dict = {}   # 兼容字段（不再预填全量——恒空）
        if wrapper_tool is not None:
            try:
                f = getattr(wrapper_tool, "_func", None)
                tk = getattr(f, "__self__", None)
                if tk is not None and hasattr(tk, "_agent_discoverable_tools_state"):
                    self._toolkit = tk
            except Exception:
                self._toolkit = None

    # ------------------------------------------------------------------
    def _unlocked_tool_info(self, inner_name: str) -> Optional[dict]:
        """取该工具的 **unlock 后暴露给 Agent 的** tool_info（合法信息）。

        未 unlock（不在 state）→ None——不查 get_discoverable_tools()、
        不 inspect signature/docstring（benchmark 内部实现，禁止访问）。
        """
        if self._toolkit is None:
            return None
        try:
            state = self._toolkit._agent_discoverable_tools_state or {}
            entry = state.get(inner_name)
            if entry and isinstance(entry, dict):
                return entry.get("tool_info")
        except Exception:
            pass
        return None

    def _is_unlocked(self, inner_name: str) -> bool:
        if self._toolkit is None:
            return False
        try:
            state = self._toolkit._agent_discoverable_tools_state or {}
            return inner_name in state
        except Exception:
            return False

    # ------------------------------------------------------------------
    def resolve(self, tool_call) -> ResolvedAction:
        """解析一个 proposed tool call → ResolvedAction。

        永不抛异常：解析失败时 resolve_error 记录原因、arguments 原样——
        harness 对解析失败按 outer 校验（回退 V2 行为），不因解析器
        问题拦截正确调用。
        """
        outer_name = getattr(tool_call, "name", "(unknown)")
        outer_args = dict(getattr(tool_call, "arguments", None) or {})

        if outer_name not in self.WRAPPER_NAMES:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=outer_name, arguments=outer_args,
                inner_schema=None, is_wrapper=False,
            )

        inner_name = None
        for k in self.INNER_NAME_KEYS:
            if k in outer_args and isinstance(outer_args[k], str):
                inner_name = outer_args[k]
                break
        if inner_name is None:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=outer_name, arguments=outer_args,
                is_wrapper=True, resolve_error="no_inner_tool_name",
            )

        raw_args = outer_args.get("arguments", "{}")
        inner_args: dict = {}
        if isinstance(raw_args, str):
            try:
                inner_args = json.loads(raw_args, parse_int=float) if raw_args.strip() else {}
                if not isinstance(inner_args, dict):
                    inner_args = {}
            except json.JSONDecodeError as exc:
                return ResolvedAction(
                    outer_tool_name=outer_name, outer_arguments=outer_args,
                    tool_name=inner_name, arguments={},
                    is_wrapper=True, resolve_error=f"inner_arguments_json_error: {exc}",
                )
        elif isinstance(raw_args, dict):
            inner_args = dict(raw_args)
        elif raw_args is None:
            inner_args = {}
        else:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=inner_name, arguments={},
                is_wrapper=True, resolve_error=f"inner_arguments_type:{type(raw_args).__name__}",
            )

        # ---- Integrity 边界：只对 Agent 已 unlock 的工具取 schema ----
        tool_info = self._unlocked_tool_info(inner_name)
        if tool_info is None:
            # 未 unlock（或 toolkit 不可用）——inner 参数原样解析（供
            # trace/TaskState 观察用），但 **不带 inner_schema**：
            # SchemaValidation 对无 schema 的 ResolvedAction 只做 wrapper
            # 层校验；绝不 introspect hidden 实现。
            return ResolvedAction(
                outer_tool_name=outer_name,
                outer_arguments=outer_args,
                tool_name=inner_name,
                arguments=inner_args,
                inner_schema=None,
                is_wrapper=True,
                resolve_error="inner_tool_not_unlocked",
            )
        return ResolvedAction(
            outer_tool_name=outer_name,
            outer_arguments=outer_args,
            tool_name=inner_name,
            arguments=inner_args,
            inner_schema=self._schema_from_tool_info(tool_info),
            is_wrapper=True,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _schema_from_tool_info(tool_info: dict) -> dict:
        """从 unlock 时暴露的 tool_info 构建 inner 参数 schema。

        tool_info 是官方 parse_discoverable_tool_docstring 的产物——
        即 Agent 在 unlock 响应里**已经看到**的参数与合法值定义。
        （合法值在 tool_info['parameters'] 的描述文本里，格式与
        docstring 一致：enum 信息对 Agent 已知，对 Harness 同样合法。）
        """
        schema: dict = {"properties": {}, "required": [], "enum_from_doc": {}}
        try:
            params = tool_info.get("parameters") or {}
            if isinstance(params, list):  # 兼容列表形态
                params = {str(p.get("name") or f"p{i}"): p
                          for i, p in enumerate(params) if isinstance(p, dict)}
            tmap = {"string": "string", "str": "string",
                    "number": "number", "float": "number",
                    "integer": "number", "int": "number",
                    "boolean": "boolean", "bool": "boolean"}
            for pname, p in params.items():
                pname = str(pname).strip()
                if not pname or not isinstance(p, dict):
                    continue
                pdesc = str(p.get("description") or "").strip()
                ptype = str(p.get("type") or "").strip().lower()
                schema["properties"][pname] = {"type": tmap.get(ptype, "string")}
                if p.get("required"):
                    schema["required"].append(pname)
                # enum 提取（与 Agent 在 unlock 响应中看到的同一描述文本）
                m = re.search(r"one of:\s*([^\n]+)", pdesc, re.IGNORECASE)
                if m:
                    vals = re.findall(r"'([^']+)'", m.group(1))
                    if len(vals) >= 2:
                        schema["enum_from_doc"][pname] = vals
        except Exception:
            return {}
        return schema
