"""Action Resolver（V2.1）——把 wrapper 调用解析成真实业务动作。

背景（V2 targeted 的结构性发现）：
    banking 域关键参数藏在 call_discoverable_agent_tool(tool_name, arguments)
    的内层 JSON 字符串里——V2 harness 只能看到 wrapper 的
    {agent_tool_name: string, arguments: string}，抓不到 reason/amount/
    account_type 等真实错误。

职责分离（严格遵守）：
    Resolver 负责"看懂调用"——解析 wrapper、找到 inner tool 与 schema。
    Harness 负责"检查调用"——对 ResolvedAction 应用校验。
    不把解析逻辑硬编码进 validator；Harness 不自动改参数。

inner tool schema 来源（不改 tau2）：
    wrapper Tool._func 是绑定到 toolkit 实例的 bound method；
    toolkit.get_discoverable_tools() 返回 {name: method}（44 个）；
    每个 method 的 inspect.signature + docstring 提供 inner 参数 schema
    （docstring 记录 enum 合法值："Must be one of: 'x' | 'y'"）。
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
    真实工具名 + 参数 dict + 从 toolkit 方法 signature/docstring 构建的
    inner schema。
    """

    outer_tool_name: str
    outer_arguments: dict
    tool_name: str                     # inner（真实业务工具名）
    arguments: dict                    # inner 参数
    inner_schema: Optional[dict] = None  # {properties: {...}, required: [...], enum_from_doc: {...}}
    is_wrapper: bool = False
    resolve_error: Optional[str] = None  # 解析失败原因（inner 参数坏 JSON 等）


class ActionResolver:
    """看懂 Agent 的工具调用（wrapper 穿透）。"""

    WRAPPER_NAMES = {
        "call_discoverable_agent_tool",
        "call_discoverable_user_tool",  # 用户侧同样可解析（一致性预留）
    }
    # wrapper 里指向 inner 工具名的参数名
    INNER_NAME_KEYS = ("agent_tool_name", "discoverable_tool_name", "tool_name")

    def __init__(self, wrapper_tool=None):
        """wrapper_tool: 环境的 call_discoverable_agent_tool Tool 对象——
        通过 ._func.__self__ 拿 toolkit → inner 方法注册表。"""
        self._inner_registry: dict = {}
        if wrapper_tool is not None:
            try:
                f = getattr(wrapper_tool, "_func", None)
                tk = getattr(f, "__self__", None)
                if tk is not None and hasattr(tk, "get_discoverable_tools"):
                    self._inner_registry = dict(tk.get_discoverable_tools())
            except Exception:
                self._inner_registry = {}

    # ------------------------------------------------------------------
    def resolve(self, tool_call) -> ResolvedAction:
        """解析一个 proposed tool call → ResolvedAction。

        永不抛异常：解析失败时 resolve_error 记录原因、is_wrapper 保持
        False、arguments 原样——harness 对解析失败按 outer 校验（回退
        V2 行为），不因解析器问题拦截正确调用。
        """
        outer_name = getattr(tool_call, "name", "(unknown)")
        outer_args = dict(getattr(tool_call, "arguments", None) or {})

        # wrapper 工具持有（如 call_discoverable_agent_tool）
        wrapper_tool = getattr(tool_call, "_harness_tool", None)
        if wrapper_tool is not None:
            # 惰性补充注册表（wrapper 已知时）
            if not self._inner_registry:
                self.__init__(wrapper_tool)

        if outer_name not in self.WRAPPER_NAMES:
            # 普通工具：outer == inner，schema 用 wrapper 自身 openai_schema
            schema = None
            if wrapper_tool is not None:
                try:
                    ps = wrapper_tool.openai_schema.get("function", {}).get("parameters", {})
                    schema = ps
                except Exception:
                    schema = None
            return ResolvedAction(
                outer_tool_name=outer_name,
                outer_arguments=outer_args,
                tool_name=outer_name,
                arguments=outer_args,
                inner_schema=schema,
            )

        # ---- wrapper 穿透 ----
        inner_name = None
        for k in self.INNER_NAME_KEYS:
            if k in outer_args:
                inner_name = outer_args.get(k)
                break
        raw_args = outer_args.get("arguments")
        if inner_name is None:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=outer_name, arguments=outer_args,
                is_wrapper=True, resolve_error="missing_inner_tool_name",
            )
        # 内层参数：dict（已解析）或 JSON 字符串
        if isinstance(raw_args, dict):
            inner_args = dict(raw_args)
        elif isinstance(raw_args, str):
            try:
                inner_args = json.loads(raw_args, parse_int=float)
            except Exception as exc:
                return ResolvedAction(
                    outer_tool_name=outer_name, outer_arguments=outer_args,
                    tool_name=inner_name, arguments={},
                    is_wrapper=True, resolve_error=f"inner_arguments_json_error: {exc}",
                )
        elif raw_args is None:
            inner_args = {}
        else:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=inner_name, arguments={},
                is_wrapper=True, resolve_error=f"inner_arguments_type:{type(raw_args).__name__}",
            )

        method = self._inner_registry.get(inner_name)
        if method is None:
            return ResolvedAction(
                outer_tool_name=outer_name, outer_arguments=outer_args,
                tool_name=inner_name, arguments=inner_args,
                is_wrapper=True, resolve_error=f"unknown_inner_tool:{inner_name}",
            )
        return ResolvedAction(
            outer_tool_name=outer_name,
            outer_arguments=outer_args,
            tool_name=inner_name,
            arguments=inner_args,
            inner_schema=self._schema_from_method(method),
            is_wrapper=True,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _schema_from_method(method) -> dict:
        """从 toolkit 方法构建 inner 参数 schema（不依赖 tau2 Tool 构造）。

        - required: signature 无默认值的参数
        - properties.<p>.type: annotation 粗映射（str/float/int/bool）
        - enum_from_doc: docstring 的 "Must be one of: 'x' | 'y' 或 'x', 'y'"
          模式提取（合法值文档在 docstring，不在 schema——诚实记录，
          提取不到就不填，不猜）
        """
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            return {}
        props: dict = {}
        required: list = []
        for pname, p in sig.parameters.items():
            ann = p.annotation
            t = "string"
            if ann is float or ann is int:
                t = "number"
            elif ann is bool:
                t = "boolean"
            elif str(ann).startswith("typing.Optional[float]") or "float" in str(ann):
                t = "number"
            props[pname] = {"type": t}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        # docstring enum 提取："Must be one of: 'keep_active' (card remains ...)", 格式
        enum_from_doc: dict = {}
        doc = inspect.getdoc(method) or ""
        for m in re.finditer(
            r"(\w+)\s*(?:\([^)]*\))?\s*:\s*.*?one of:\s*([^\n]+)", doc, re.IGNORECASE
        ):
            field_name, rest = m.group(1), m.group(2)
            # 提取引号内的值 / 管道分隔
            vals = re.findall(r"'([^']+)'", rest)
            if not vals:
                vals = [v.strip(" '\"") for v in rest.split("|")]
                vals = [v for v in vals if v and len(v) < 40]
            if len(vals) >= 2:
                enum_from_doc[field_name] = vals
        return {"properties": props, "required": required,
                "enum_from_doc": enum_from_doc}
