"""确定性单元测试（Frozen V5 runtime 的机制回归测试）。

覆盖范围（对应收口任务第七节）：
  1. Task State（V3 TaskStateV3 + V2 TaskState）
     - entity separation（多对象同名不互扰）
     - supersede（用户改口历史保留、判定看最新）
     - provenance（source/source_ref 随条目保存）
     - 类型标记防御（'string' 占位值拒入库）+ 幂等写入
  2. Harness
     - valid call 放行（三源全过）
     - grounded conflict 拒绝（task_state_conflict + correction）
     - unlock 边界（未 unlock 的 wrapper 不带 inner_schema）
     - recovery 预算（同 field 超限放行——DecisionAgent 侧常量核对）
  3. PlanStore（V5）
     - write/update 基本操作
     - current step 推进（set_current）
     - tool result 进度（tool_hint + 实体绑定）
     - entity ambiguity（多对象同工具不误完成）
     - bounded guard（completion guard 上限 2）
     - block/unblock/remove
  4. Context Builder（V4）
     - 短历史不 compact
     - 长历史旧 ToolResult 存根化
     - error ToolMessage 保留
     - Task State 未 externalize 时不 stub

运行约束：无网络 / 无 LLM / 无 API key，秒级完成。
不修改任何 runtime 行为——只测既有行为。

运行: python tests/test_unit.py
"""

import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = 0, 0


def check(name):
    """简单测试收集器。"""
    global PASS, FAIL
    PASS += 1
    print(f"  PASS {name}")


def run(name, fn):
    global FAIL
    try:
        fn()
        check(name)
    except AssertionError as e:
        FAIL += 1
        print(f"  FAIL {name}: {e}")


# ===========================================================================
# 1. Task State
# ===========================================================================
def test_task_state_v3_entity_separation():
    """多对象同名 field 互不干扰——V3 的核心动机（095 误拦根治）。"""
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    # 两个对象各有自己的 amount（transfer vs refund）——工具命名空间
    ts.set("transfer_money", "amount", 500, "user")
    ts.set("get_refund", "amount", 100, "tool")
    # 各自精确查询（validator 的第一优先键）
    e1 = ts.latest("transfer_money", "amount")
    assert e1 is not None and float(e1.value) == 500.0, \
        f"transfer amount 应为 500, got {e1 and e1.value}"
    e2 = ts.latest("get_refund", "amount")
    assert e2 is not None and float(e2.value) == 100.0, "refund amount 应为 100"
    # 裸 amount 查询（无对象）：存在对象级同名 field → 不命中（多对象歧义放行）
    e3 = ts.latest("", "amount")
    assert e3 is None, "多对象同名 bare field 应返回 None（放行）"


def test_task_state_v3_supersede():
    """用户改口：同 (object, field) 重复写入 → 旧条目保留历史、判定只看最新。"""
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    ts.set("transfer_request", "amount", 500, "user")
    ts.set("transfer_request", "amount", 300, "user", "turn 3")
    chain = ts._entries["transfer_request.amount"]
    assert len(chain) == 2, "历史链应保留两条"
    assert chain[0].superseded_by is not None, "旧条目应标记 superseded_by"
    assert not chain[0].is_current, "旧条目不再是 current"
    assert chain[1].is_current and float(chain[1].value) == 300.0
    # harness 判定入口拿到的是最新值
    e = ts.latest("transfer_request", "amount")
    assert float(e.value) == 300.0, "latest 应返回改口后的 300"


def test_task_state_v3_provenance():
    """每条状态带来源（user/tool/knowledge）与具体引用。"""
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    ts.set("user_abc", "id", "abc123", "tool", "get_user_information_by_name")
    ts.set("transfer_reason", "allowed_values", ["a", "b"], "knowledge", "doc_x_001")
    e1 = ts._entries["user_abc.id"][-1]
    assert e1.source == "tool" and e1.source_ref == "get_user_information_by_name"
    e2 = ts._entries["transfer_reason.allowed_values"][-1]
    assert e2.source == "knowledge" and e2.source_ref == "doc_x_001"


def test_task_state_v3_type_marker_defense_and_idempotent():
    """构造层防御：类型占位值拒入库；同值幂等写入 no-op。"""
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    # 'string'/'number' 等文档占位值不入库（095/080 实测 bug 的防御）
    assert ts.set("account_x", "id", "string", "tool") is None
    assert ts.set("account_x", "balance", "n/a", "tool") is None
    assert "account_x.id" not in ts._entries
    # 幂等：同 source 同值重写 → 返回当前条目，不新增历史
    e1 = ts.set("card_1", "status", "active", "tool", "t1")
    e2 = ts.set("card_1", "status", "active", "tool", "t1")
    assert e1 is e2, "幂等写入应返回同一条目"
    assert len(ts._entries["card_1.status"]) == 1, "幂等写入不应新增历史链"


def test_task_state_v3_entity_resolution():
    """ID 类字段实体解析：唯一实体 + agent 用错 ID → 用已知 ID 做约束。"""
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    ts.set("card_dbc_12345", "id", "dbc_12345", "tool", "get_cards")
    # 提议 card_id=dbc_54321（错误 ID）→ 唯一已知实体 → 返回已知约束
    e = ts.latest("close_card", "card_id", proposed_value="dbc_54321")
    assert e is not None and str(e.value) == "dbc_12345", "应返回已知 ID 作为约束"
    # 提议正确 ID → 无冲突
    e2 = ts.latest("close_card", "card_id", proposed_value="dbc_12345")
    assert e2 is None or str(e2.value) == "dbc_12345"
    # 多实体（多卡任务）→ 歧义不拦
    ts.set("card_dbc_67890", "id", "dbc_67890", "tool", "get_cards")
    e3 = ts.latest("close_card", "card_id", proposed_value="dbc_11111")
    assert e3 is None, "多实体歧义应返回 None（放行——有明确依据才拦）"


def test_task_state_v2_latest_and_record():
    """V2 参数级 TaskState：record/latest 的 key 归一化与最新优先。"""
    from agents.harness.task_state import TaskState, SOURCE_USER, SOURCE_TOOL
    ts = TaskState()
    ts.record("amount", 500, SOURCE_USER, tool="transfer_money")
    ts.record("amount", 300, SOURCE_USER, tool="transfer_money")
    e = ts.latest("transfer_money", "amount")
    assert e is not None and float(e.value) == 300.0, "V2 latest 应取最新"
    # 裸参数名回退
    ts2 = TaskState()
    ts2.record("card_id", "abc", SOURCE_TOOL, source_ref="get_cards")
    assert ts2.latest("", "card_id") is not None


# ===========================================================================
# 2. Harness
# ===========================================================================
def _make_harness_with_state():
    """构造 wire 好三源的 harness（无 LLM / 无环境）。"""
    from agents.harness.action_harness import ActionHarness
    from agents.harness.task_state_v3 import TaskStateV3
    from agents.harness.task_state_validator import TaskStateValidator
    ts = TaskStateV3()
    h = ActionHarness()
    h.wire(ts)
    # 把占位 validator 换成持有 V3 实例（wire 已做；这里再确认类型）
    for p in h.policies:
        if isinstance(p, TaskStateValidator):
            p.task_state = ts
    return h, ts


def _mk_call(name, args):
    tc = types.SimpleNamespace()
    tc.name = name
    tc.arguments = args
    tc.id = "call_test_1"
    return tc


def test_harness_valid_call_allowed():
    """三源全过 → 放行且执行。"""
    h, ts = _make_harness_with_state()
    ts.set("do_transfer", "amount", 500, "user")
    calls = _mk_call("do_transfer", {"amount": 500, "account_id": "acc_1"})
    called = {}

    def execute(args):
        called.update(args)
        return "ok"
    from agents.harness.base import HarnessContext
    passed, content, meta = h.process(calls, execute, HarnessContext())
    assert passed, f"合法调用应放行, verdicts: {[v.verdict for v in meta.get('verdicts', [])]}"
    assert called.get("amount") == 500, "放行后应执行原参数"


def test_harness_grounded_conflict_rejected():
    """与 Task State 已确认值冲突 → 拒绝且带 correction 行。"""
    h, ts = _make_harness_with_state()
    # 用户在 do_transfer 语境下确认过 amount=500（工具命名空间——validator 精确键）
    ts.set("do_transfer", "amount", 500, "user")
    calls = _mk_call("do_transfer", {"amount": 550})
    from agents.harness.base import HarnessContext
    passed, content, meta = h.process(calls, lambda a: "ok", HarnessContext())
    assert not passed, "amount=550 与用户确认的 500 冲突应拦截"
    assert "correction" in content and "500" in content, \
        f"拒绝信息应含 correction 指令, got: {content[:200]}"
    verdicts = [v.verdict for v in meta["verdicts"]]
    assert "task_state_conflict" in verdicts


def test_harness_kb_enum_rejected_with_correction():
    """KB enum 约束（KA packet 入库）→ 非法值拒绝且 correction 给出合法集。"""
    from agents.harness.task_state_v3 import KnowledgeStateExtractor
    h, ts = _make_harness_with_state()
    KnowledgeStateExtractor.feed_constraints(ts, [
        {"tool_name": "close_card", "parameter_name": "reason",
         "constraint_type": "enum",
         "allowed_values": ["fraud_or_security_concern", "account_closure_request"],
         "source_doc_id": "doc_x_001"},
    ])
    calls = _mk_call("close_card", {"reason": "wrong_reason_value"})
    from agents.harness.base import HarnessContext
    passed, content, meta = h.process(calls, lambda a: "ok", HarnessContext())
    assert not passed, "KB enum 之外的值应拦截"
    verdicts = [v.verdict for v in meta["verdicts"]]
    assert "kb_enum_violation" in verdicts
    assert "correction" in content and "account_closure_request" in content
    # 合法值放行
    calls2 = _mk_call("close_card", {"reason": "fraud_or_security_concern"})
    passed2, _, meta2 = h.process(calls2, lambda a: "ok", HarnessContext())
    assert passed2, "KB enum 内的合法值应放行"


def test_harness_unknown_value_passes():
    """not_in_task_state / no_kb_constraint → 放行（有明确依据才拦——核心原则）。"""
    h, ts = _make_harness_with_state()
    calls = _mk_call("do_transfer", {"reason": "vacation", "amount": 99})
    from agents.harness.base import HarnessContext
    passed, content, meta = h.process(calls, lambda a: "ok", HarnessContext())
    assert passed, "无 task state / KB 记录的参数应放行"
    verdicts = [v.verdict for v in meta["verdicts"]]
    blocking = [v for v in verdicts if v not in
                ("not_in_task_state", "no_kb_constraint", "matched")]
    assert not blocking, f"应无 blocking verdict, got {verdicts}"


def test_resolver_unlock_boundary():
    """未 unlock 的 wrapper 调用：resolve 出 inner 名但不带 schema。"""
    from agents.harness.resolver import ActionResolver, ResolvedAction
    # 不带 toolkit 的 resolver：一切 inner 均视为未 unlock
    r = ActionResolver()
    tc = _mk_call("call_discoverable_agent_tool", {
        "agent_tool_name": "submit_cash_back_dispute_0589",
        "arguments": "{\"user_id\": \"abc\"}",
    })
    ra = r.resolve(tc)
    assert isinstance(ra, ResolvedAction)
    assert ra.is_wrapper, "wrapper 调用应识别为 wrapper"
    assert ra.tool_name == "submit_cash_back_dispute_0589", "应解析出 inner 工具名"
    assert ra.inner_schema is None, "未 unlock 的工具不得携带 inner schema"
    assert ra.resolve_error == "inner_tool_not_unlocked"
    # inner 参数仍被解析（供 trace/观察）
    assert ra.arguments == {"user_id": "abc"}
    # 非 wrapper 调用原样通过
    tc2 = _mk_call("get_current_time", {})
    ra2 = r.resolve(tc2)
    assert not ra2.is_wrapper and ra2.tool_name == "get_current_time"


def test_recovery_budget_constant_and_semantics():
    """V2.3 recovery 预算：MAX_SAME_FIELD_REJECTIONS=2 常量存在（超限放行语义在 DA 拦截循环）。"""
    # 不实例化 DecisionAgent（需要 LLM 环境）——直接核对源码中的常量与逻辑标记
    import inspect
    import agents.two_agent as ta
    src = inspect.getsource(ta)
    assert "MAX_SAME_FIELD_REJECTIONS" in src
    m = [l for l in src.split("\n") if "MAX_SAME_FIELD_REJECTIONS = " in l]
    assert m and "2" in m[0], f"预算常量应为 2, got: {m}"
    # 预算逻辑存在：超限字段跳过拦截（continue 放行）
    assert "over_budget" in src and src.count("over_budget") >= 2, \
        "recovery 预算放行逻辑应存在"


# ===========================================================================
# 3. PlanStore（V5）
# ===========================================================================
def test_plan_store_write_and_update():
    """write_plan 覆盖式写入；update_plan 各 op 语义正确。"""
    from agents.harness.plan_store import (
        PlanStore, PENDING, IN_PROGRESS, COMPLETED, BLOCKED, REMOVED,
    )
    ps = PlanStore()
    r = ps.write_plan("close cards", [
        {"description": "verify identity"},
        {"description": "close card A", "tool_hint": "close_card",
         "entities": ["card_A"]},
    ])
    assert r["ok"] and r["n_steps"] == 2
    assert ps.active
    # set_current：pending → in_progress 且聚焦
    r2 = ps.update_plan("set_current", step_id=1)
    assert r2["ok"] and ps.current_step().status == IN_PROGRESS
    # add_step
    r3 = ps.update_plan("add_step", description="confirm", step_id=None)
    assert r3["ok"] and r3["added"] == 3
    # remove_step：置 removed；current 被移除则清空焦点
    ps.update_plan("set_current", step_id=3)
    r4 = ps.update_plan("remove_step", step_id=3)
    assert r4["ok"] and ps.steps[2].status == REMOVED
    assert ps.current_step() is None or ps.current_step().step_id != 3
    # block/unblock
    ps.update_plan("block_step", step_id=1, note="waiting user")
    assert ps.steps[0].status == BLOCKED and ps.steps[0].note == "waiting user"
    ps.update_plan("unblock_step", step_id=1)
    assert ps.steps[0].status == PENDING and ps.steps[0].note is None
    # 未知 op / 未知 step
    assert not ps.update_plan("bogus_op")["ok"]
    assert not ps.update_plan("set_current", step_id=99)["ok"]


def test_plan_store_tool_result_progress():
    """步骤完成由真实 tool result + tool_hint 驱动（不信口头）。"""
    from agents.harness.plan_store import PlanStore, IN_PROGRESS, COMPLETED, FAILED
    ps = PlanStore()
    ps.write_plan("goal", [
        {"description": "close card A", "tool_hint": "close_card",
         "entities": ["card_A"]},
        {"description": "notify user", "tool_hint": "send_message"},
    ])
    ps.update_plan("set_current", step_id=1)
    # 正确工具成功 → 步骤 completed
    st = ps.on_tool_result("close_card", True, {"card_id": "card_A"})
    assert st is not None and st.status == COMPLETED
    # 无关工具 → 不推进
    st2 = ps.on_tool_result("get_balance", True, {})
    assert st2 is None


def test_plan_store_entity_ambiguity():
    """多对象同工具不误完成：两个 card 的 close 步骤，一次调用只推进绑定的那个。"""
    from agents.harness.plan_store import PlanStore, IN_PROGRESS, COMPLETED, PENDING
    ps = PlanStore()
    ps.write_plan("close two cards", [
        {"description": "close card A", "tool_hint": "close_card", "entities": ["card_A"]},
        {"description": "close card B", "tool_hint": "close_card", "entities": ["card_B"]},
    ])
    ps.update_plan("set_current", step_id=1)
    # 调用参数引用 card_B → 只推进步骤 2（实体绑定消歧）
    st = ps.on_tool_result("close_card", True, {"card_id": "card_B"})
    assert st is not None and st.step_id == 2 and st.status == COMPLETED
    # 步骤 1 仍 pending（未绑定到本次调用）
    assert ps.steps[0].status in (PENDING, IN_PROGRESS)
    # 之后 card_A 的调用推进步骤 1
    st2 = ps.on_tool_result("close_card", True, {"card_id": "card_A"})
    assert st2 is not None and st2.step_id == 1 and st2.status == COMPLETED


def test_plan_store_tool_failure_marks_failed():
    """工具执行失败 → 匹配步骤标 failed 并附 note。"""
    from agents.harness.plan_store import PlanStore, FAILED
    ps = PlanStore()
    ps.write_plan("g", [{"description": "close A", "tool_hint": "close_card",
                         "entities": ["card_A"]}])
    ps.update_plan("set_current", step_id=1)
    st = ps.on_tool_result("close_card", False, {"card_id": "card_A"})
    assert st is not None and st.status == FAILED
    assert st.note == "tool failed: close_card"


def test_plan_store_bounded_guard():
    """completion guard 有界：最多提醒 GUARD_LIMIT 次后不再触发。"""
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan("g", [{"description": "step 1"}])
    assert ps.guard_should_remind(), "有 pending 步骤应触发 guard"
    ps.guard_reminders = ps.GUARD_LIMIT  # 模拟已提醒到上限
    assert not ps.guard_should_remind(), "超限后不再提醒（有界）"
    # 全部完成后不触发
    ps2 = PlanStore()
    ps2.write_plan("g", [{"description": "s1", "tool_hint": "t"}])
    ps2.update_plan("set_current", step_id=1)
    ps2.on_tool_result("t", True, {})
    assert not ps2.guard_should_remind(), "无 pending 步骤不应触发 guard"


def test_plan_store_scope_bound_no_hint_fallback():
    """V5.1 无 tool_hint 的克制回退：current+in_progress+实体作用域三条件全满足才推进。"""
    from agents.harness.plan_store import PlanStore, COMPLETED
    from agents.harness.task_state_v3 import TaskStateV3
    ps = PlanStore()
    ts = TaskStateV3()
    # Task State 已确认 user 实体（generic step entities → 参数须命中已知实体）
    ts.set("user_890", "id", "890389b165", "tool", "get_user_information")
    ps.write_plan("verify and lookup", [
        {"description": "verify the customer's identity", "entities": ["customer"]},
    ])
    ps.update_plan("set_current", step_id=1)
    # 无 hint 的步骤 + 调用参数命中已确认 user_id → 推进
    st = ps.on_tool_result("get_credit_card_accounts_by_user", True,
                           {"user_id": "890389b165"}, task_state=ts)
    assert st is not None and st.status == COMPLETED, \
        "V5.1 实体作用域绑定应推进无 hint 的 current step"
    # 不带实体参数的辅助调用（如 get_current_time）不推进
    ps2 = PlanStore()
    ps2.write_plan("g", [{"description": "verify", "entities": ["customer"]}])
    ps2.update_plan("set_current", step_id=1)
    st2 = ps2.on_tool_result("get_current_time", True, {}, task_state=ts)
    assert st2 is None, "无实体参数的辅助调用不应推进步骤"


def test_plan_store_plan_block_render():
    """plan_block 渲染包含 goal/步骤状态/current 标记，且 active=False 时为空。"""
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    assert ps.plan_block() == "", "无计划时应返回空串"
    ps.write_plan("close cards", [
        {"description": "step one", "tool_hint": "t1"},
    ])
    ps.update_plan("set_current", step_id=1)
    block = ps.plan_block()
    assert "GOAL: close cards" in block and "CURRENT" in block


# ===========================================================================
# 4. Context Builder
# ===========================================================================
def _tau2_tool_message(content, error=False, idx=0):
    """构造真实 tau2 ToolMessage（venv 内 editable 安装可用）。"""
    from tau2.data_model.message import ToolMessage
    return ToolMessage(id=f"tm_{idx}", role="tool", requestor="assistant",
                       content=content, error=error)


def _tau2_user_message(content):
    from tau2.data_model.message import UserMessage
    return UserMessage(role="user", content=content)


def _tau2_system():
    from tau2.data_model.message import SystemMessage
    return SystemMessage(role="system", content="SYSTEM PROMPT")


def _mk_state(n_msgs, tool_idx_with_record=None, error_idx=None):
    """构造 tau2 AgentState 替身：system + n 条消息。"""
    state = types.SimpleNamespace()
    state.system_messages = [_tau2_system()]
    msgs = []
    for i in range(n_msgs):
        if i == tool_idx_with_record:
            msgs.append(_tau2_tool_message(
                "Found 1 record(s):\n Record ID: rec_123\n user_id: usr_001", idx=i))
        elif i == error_idx:
            msgs.append(_tau2_tool_message(
                "Harness validation failed for amount", error=True, idx=i))
        else:
            msgs.append(_tau2_user_message(f"message {i}"))
    state.messages = msgs
    return state


def _mk_plan_tracker():
    """构造已激活 Plan Mode 的 PlanTracker（存根化只在 Plan Mode 下发生）。

    直接构造激活态——绕过 maybe_upgrade 的行为触发阈值（单元测试要确定性）。
    """
    from agents.harness.execution_plan import PlanTracker, ExecutionPlan
    pt = PlanTracker()
    pt.plan = ExecutionPlan(goal="test goal", active=True)
    return pt


def test_context_builder_short_history_no_compact():
    """短历史（≤COMPACT_TRIGGER）Plan Mode 下也不触发存根化。"""
    from agents.harness.context_builder import build_context, COMPACT_TRIGGER
    from agents.harness.task_state_v3 import TaskStateV3
    n = COMPACT_TRIGGER  # 恰好在下限（> 才 compact）
    state = _mk_state(n, tool_idx_with_record=0)
    out = build_context(state, TaskStateV3(), plan_tracker=_mk_plan_tracker(),
                        memory_block="MEM", state_block="STATE")
    assert len(out) == n + 1, "短历史应原样输出（+1 system）"
    assert "Record ID" in out[1].content, "未 compact 的 ToolMessage 应保留全文"


def test_context_builder_long_history_stubs_old_toolresult():
    """Plan Mode + 长历史 + RECENT_WINDOW 之外的已入库 ToolMessage → 存根替换。"""
    from agents.harness.context_builder import (
        build_context, COMPACT_TRIGGER, RECENT_WINDOW,
    )
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    ts.set("user_usr_001", "id", "usr_001", "tool", "get_user")  # 已外部化
    n = COMPACT_TRIGGER + 10
    # 旧消息区（窗口外）放一条含 Record 的 ToolMessage
    old_idx = 1
    state = _mk_state(n, tool_idx_with_record=old_idx)
    out = build_context(state, ts, plan_tracker=_mk_plan_tracker())
    assert len(out) == n + 1
    stubbed = out[old_idx + 1]  # +1 system
    assert "Record ID" not in stubbed.content, "旧 ToolResult 应被替换为存根"
    assert "archived" in stubbed.content.lower(), \
        f"存根应含 archived 标记, got: {stubbed.content[:120]}"
    # 存根应提到已入库实体
    assert "usr_001" in stubbed.content


def test_context_builder_error_toolmessage_preserved():
    """Plan Mode 下 error ToolMessage（harness 拒绝的修正依据）不存根化——保留全文。"""
    from agents.harness.context_builder import build_context, COMPACT_TRIGGER
    from agents.harness.task_state_v3 import TaskStateV3
    ts = TaskStateV3()
    ts.set("user_usr_001", "id", "usr_001", "tool", "get_user")
    n = COMPACT_TRIGGER + 10
    # 窗口外放一条 error ToolMessage（error=True 不替换）
    state = _mk_state(n, tool_idx_with_record=1)
    state.messages.insert(2, _tau2_tool_message(
        "Error: Record ID: rec_999 validation failed for amount",
        error=True, idx=999))
    out = build_context(state, ts, plan_tracker=_mk_plan_tracker())
    found = [m for m in out if getattr(m, "error", False)]
    assert found, "error ToolMessage 应保留在输出中"
    assert "Record ID" in found[0].content, "error 消息不应被存根化"


def test_context_builder_not_externalized_no_stub():
    """Plan Mode + Task State 未收录该实体 → 即使长历史也不替换。"""
    from agents.harness.context_builder import build_context, COMPACT_TRIGGER
    from agents.harness.task_state_v3 import TaskStateV3
    empty_ts = TaskStateV3()  # 空状态：没有任何实体被外部化
    n = COMPACT_TRIGGER + 10
    state = _mk_state(n, tool_idx_with_record=1)
    out = build_context(state, empty_ts, plan_tracker=_mk_plan_tracker())
    assert "Record ID" in out[2].content, "未外部化的 ToolResult 不应被存根化"


def test_context_builder_system_blocks_injected():
    """memory/state block 注入 system 尾部；无 block 时 system 原样。"""
    from agents.harness.context_builder import build_context
    from agents.harness.task_state_v3 import TaskStateV3
    state = _mk_state(4)
    out = build_context(state, TaskStateV3(), plan_tracker=None,
                        memory_block="MEM-BLOCK", state_block="STATE-BLOCK")
    assert "MEM-BLOCK" in out[0].content and "STATE-BLOCK" in out[0].content
    assert out[0].content.startswith("SYSTEM PROMPT"), "原 system 内容保留在前"


# ===========================================================================
# 5. V6.1（架构收紧）— Evidence View / Worklist / Checkpoint / Context
#    对应用户收紧指令 §1-§7 的 10 项要求;全部确定性,无 LLM/网络
# ===========================================================================
def test_v6_1_user_claim_not_confused_with_system_fact():
    """要求#1:用户说的 ≠ 工具确认的——TaskState 是唯一事实源,
    EvidenceView 分层呈现（不混同）。"""
    from agents.harness.task_state_v3 import (
        TaskStateV3, UserStateExtractor, ToolResultStateExtractor)
    from agents.harness.evidence_view import EvidenceView
    ts = TaskStateV3()
    UserStateExtractor.feed(ts, "I have been a customer for about four months")
    ToolResultStateExtractor.feed(ts, "get_current_time",
                                  "The current time is 2025-11-14 03:40:00 EST.")
    ev = EvidenceView(ts)
    # user_statement → unverified 段
    claims = ev.unverified_user_claims()
    assert any(r.field == "duration" for r in claims), \
        "duration 自述必须出现在 unverified 段"
    # system_fact → confirmed 段（current_date 前置）
    confirmed = ev.confirmed_system_facts()
    assert confirmed and confirmed[0].key == "system.current_date"
    # 分段永不混同:confirmed 段里没有 user 来源,unverified 段没有 tool 来源
    assert all(r.source in ("tool", "knowledge") for r in confirmed)
    assert all(r.source == "user" for r in ev.unverified_user_claims())


def test_v6_2_no_second_fact_store_single_feed_path():
    """要求#1(架构):EvidenceView 零存储——从 TaskState 现算,
    不存在第二条事实写入路径。"""
    from agents.harness.task_state_v3 import TaskStateV3, FT_SYSTEM_FACT
    from agents.harness.evidence_view import EvidenceView
    ts = TaskStateV3()
    ev = EvidenceView(ts)
    assert not hasattr(ev, "_entries"), "视图不得有独立事实存储"
    # 最初无 confirmed
    assert ev.confirmed_system_facts() == []
    # TaskState 更新 → 同一视图对象立即读到新值（现算,非快照）
    ts.set("account_x", "balance", 96000, "tool",
           source_ref="get_accounts", fact_type=FT_SYSTEM_FACT)
    recs = ev.confirmed_system_facts()
    assert any(r.key == "account_x.balance" for r in recs), \
        "视图必须从 TaskState 现算,不是快照"
    # grade 查询口径
    assert ev.grade("account_x.balance") == "confirmed"


def test_v6_3_tool_result_supersedes_user_claim():
    """要求#2:工具确认可取代用户自述;用户改口不能抹掉系统事实。
    冲突 key 由 TaskState 历史链现算暴露（零独立存储）。"""
    from agents.harness.task_state_v3 import (
        TaskStateV3, FT_USER_STATEMENT, FT_SYSTEM_FACT)
    from agents.harness.evidence_view import EvidenceView
    # claim 先到,tool 后到 → current 是 tool 值,conflict 仍可见
    ts = TaskStateV3()
    ts.set("user_statement", "duration", "about four months", "user",
           fact_type=FT_USER_STATEMENT)
    ts.set("user_statement", "duration", 65, "tool",
           fact_type=FT_SYSTEM_FACT)
    ev = EvidenceView(ts)
    assert ev.grade("user_statement.duration") == "confirmed"
    assert "user_statement.duration" in ts.conflicting_statement_keys()
    assert not ev.unverified_user_claims(), \
        "被工具取代的陈述不再是 unverified"
    # tool 先到,claim 后到 → claim 记录在案但系统值不被覆盖
    ts2 = TaskStateV3()
    ts2.set("user_statement", "duration", 65, "tool",
            fact_type=FT_SYSTEM_FACT)
    ts2.set("user_statement", "duration", "about four months", "user",
            fact_type=FT_USER_STATEMENT)
    ev2 = EvidenceView(ts2)
    # 系统事实在 latest_system_records 里仍然可见（用户改口不抹事实）
    sys_recs = ev2.confirmed_system_facts()
    assert any(r.value == 65 for r in sys_recs), \
        "用户后到的自述不得抹掉工具确认值"
    assert "user_statement.duration" in ts2.conflicting_statement_keys()


def test_v6_4_authority_by_fact_type_not_global_rank():
    """要求#5:权威按事实类型,不做 tool>user 全局排名。

    - 账户开户时间（system_fact）→ 系统工具可信,用户说错以工具为准
    - 业务规则（kb_rule）→ KB 可信
    - 用户偏好（user_preference）→ 用户本人可信,不需要系统验证"""
    from agents.harness.task_state_v3 import (
        TaskStateV3, FT_USER_PREFERENCE, FT_KB_RULE)
    from agents.harness.evidence_view import EvidenceView
    ts = TaskStateV3()
    # 用户偏好:出现在 preferences 段,不在 unverified 段（用户权威）
    ts.set("user_preference", "expedited_shipping", "yes", "user",
           fact_type=FT_USER_PREFERENCE)
    # KB 规则
    ts.set("close_card", "reason.allowed_values", ["a", "b"], "knowledge",
           source_ref="doc_001", fact_type=FT_KB_RULE)
    ev = EvidenceView(ts)
    prefs = ev.user_preferences()
    assert prefs and all(r.effective_fact_type == "user_preference"
                         for r in prefs)
    assert not any(r.effective_fact_type == "user_preference"
                   for r in ev.unverified_user_claims()), \
        "用户偏好不是 unverified——用户本人是权威"
    policy = ev.policy_rules()
    assert any(r.field == "reason.allowed_values" for r in policy)
    # grade:偏好/规则/系统三类各自定级
    assert ev.grade("user_preference.expedited_shipping") == "preference"
    assert ev.grade("close_card.reason.allowed_values") == "policy"


def test_v6_5_worklist_expanded_from_plan_step():
    """要求#2(Worklist):大步骤 → 每个具体对象一条目;
    WorkItem 挂在 PlanStep 下（step_id 反向引用）。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="handle all incorrect transactions", steps=[
        {"description": "dispute incorrect transaction",
         "tool_hint": "submit_cash_back_dispute",
         "entities": ["txn_A", "txn_B", "txn_C"]},
        {"description": "notify user"},   # 无实体 → 不展开条目
    ])
    wl = Worklist()
    created = wl.sync_from_plan(ps)
    assert created == 3, "3 个实体 → 3 条 item;无实体步骤不展开"
    items = wl.items_for_step(1)
    assert [i.entity for i in items] == ["txn_A", "txn_B", "txn_C"]
    assert all(i.step_id == 1 for i in items), "条目必须挂在步骤 1 下"


def test_v6_6_worklist_progress_by_real_tool_result():
    """要求#3:完成情况根据真实 Tool Result 更新;只推进绑定的实体。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="g", steps=[
        {"description": "dispute txn", "tool_hint": "submit_dispute",
         "entities": ["txn_A", "txn_B"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    prog = wl.on_tool_result(ps, "submit_dispute", True,
                             {"transaction_id": "txn_A"})
    assert len(prog) == 1 and prog[0].entity == "txn_A"
    st = {i.entity: i.status for i in wl.items_for_step(1)}
    assert st["txn_A"] == "completed" and st["txn_B"] == "pending", \
        "txn_B 不应被误完成"
    # 幂等:重复成功调用不重置
    wl.on_tool_result(ps, "submit_dispute", True, {"transaction_id": "txn_A"})
    assert wl.items_for_step(1)[0].status == "completed"


def test_v6_7_tool_failure_never_marks_complete():
    """要求#4:工具失败不能标记 work item 完成。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="g", steps=[
        {"description": "close card", "tool_hint": "close_card",
         "entities": ["card_1"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    wl.on_tool_result(ps, "close_card", False, {"card_id": "card_1"})
    it = wl.items_for_step(1)[0]
    assert it.status == "failed" and "tool failed" in (it.note or "")
    # 失败后再次成功 → completed（可恢复）
    wl.on_tool_result(ps, "close_card", True, {"card_id": "card_1"})
    assert it.status == "completed"


def test_v6_8_multi_entity_not_ended_early():
    """要求#5:多实体任务不会只完成一个对象就整体结束——
    unfinished 非空 + ContextBuilder 渲染区分 [x]/[ ]。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    from agents.harness.context_builder import worklist_block
    ps = PlanStore()
    ps.write_plan(goal="freeze all 3 cards", steps=[
        {"description": "freeze card", "tool_hint": "freeze_card",
         "entities": ["dbc_1", "dbc_2", "dbc_3"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    wl.on_tool_result(ps, "freeze_card", True, {"card_id": "dbc_1"})
    assert wl.has_unfinished, "只完成 1/3 → unfinished 必须非空"
    block = worklist_block(ps, wl)
    assert "[x]" in block and "[ ]" in block and "dbc_2" in block
    wl.on_tool_result(ps, "freeze_card", True, {"card_id": "dbc_2"})
    wl.on_tool_result(ps, "freeze_card", True, {"card_id": "dbc_3"})
    assert not wl.has_unfinished
    # 步骤 removed → 条目移除（Worklist 不能脱离 PlanStore 存在）
    ps.update_plan("remove_step", step_id=1)
    wl.sync_from_plan(ps)
    assert len(wl) == 0, "步骤移除后条目必须随之移除"


def test_v6_9_checkpoint_triggers_only_on_critical_actions():
    """要求#6:Decision Check 只在关键动作触发;普通工具零触发。"""
    from agents.harness.decision_checkpoint import should_trigger_checkpoint
    # 变更类/不可逆/transfer → 触发
    for tool in ("close_debit_card_4721",
                 "file_credit_card_transaction_dispute_4829",
                 "submit_credit_limit_increase_request_7392",
                 "submit_referral", "apply_for_credit_card",
                 "open_bank_account_4821", "pay_credit_card_from_checking",
                 "update_transaction_rewards_3847", "freeze_debit_card",
                 "approve_credit_limit_increase_5847",
                 "transfer_to_human_agents"):
        assert should_trigger_checkpoint(tool), f"{tool} 应触发"
    # 普通查询/检索/规划/审计 → 零触发
    for tool in ("get_user_information_by_name", "get_current_time",
                 "get_credit_card_accounts_by_user", "get_referrals_by_user",
                 "get_bank_account_transactions_9173", "KB_search",
                 "KB_search_bm25", "ask_knowledge_agent", "write_plan",
                 "update_plan", "read_plan", "unlock_discoverable_agent_tool",
                 "log_verification", "noop"):
        assert not should_trigger_checkpoint(tool), f"{tool} 不应触发"


def test_v6_10_checkpoint_artifact_six_questions_no_extra_llm():
    """要求#4:checkpoint 是 6 问短 artifact,不是"再问一次确定吗"
    ——Runtime 零额外 LLM 调用（源码级断言）。

    artifact 含:目标/确认事实/用户说的/硬规则/缺失证据/矛盾;
    render 短且无 chain-of-thought;一次性消费;预算超限只记 trace。"""
    import inspect
    from agents.harness.decision_checkpoint import DecisionCheckpoint
    from agents.harness.task_state_v3 import (
        TaskStateV3, UserStateExtractor, ToolResultStateExtractor)
    from agents.harness.evidence_view import EvidenceView
    from agents.harness.plan_store import PlanStore
    # (a) 零 LLM:类源码无第二次判定调用
    src = inspect.getsource(DecisionCheckpoint)
    assert "run_llm_check" not in src and "NOT_READY" not in src, \
        "checkpoint 不得有第二次 LLM 判定调用（收紧指令 §4）"
    # (b) 6 问 artifact 构建
    ts = TaskStateV3()
    UserStateExtractor.feed(ts, "a customer for about four months")
    ToolResultStateExtractor.feed(ts, "get_current_time",
                                  "The current time is 2025-11-14 03:40 EST.")
    ps = PlanStore()
    ps.write_plan(goal="refer the user's partner", steps=[
        {"description": "submit referral"}])
    cp = DecisionCheckpoint()
    note_text = cp.note_for_next_turn(
        "submit_referral", {"account_type": "World Blue",
                            "user_id": "ti87k4x9m2"},
        evidence_view=EvidenceView(ts), plan_store=ps,
        open_questions=["tenure vs 90-day requirement"])
    assert note_text is not None
    # (c) 一次性消费:取回 artifact;注入文本与其 render 一致
    art = cp.consume_pending()
    assert art is not None
    text = art.render()
    assert text == note_text, "注入文本必须就是 artifact 渲染"
    assert "Current goal" in text and "refer" in text
    assert "NOT verified by system" in text and "four months" in text
    assert "Confirmed by system" in text and "current_date" in text
    assert "Missing key evidence" in text
    assert "tenure vs 90-day" in text            # open question 进 missing
    assert "no system record" in text            # 实体 gap 进 missing
    assert len(text) < 2000, "artifact 必须短（无 chain-of-thought）"
    assert cp.consume_pending() is None
    # (d) 预算超限 → 只记 trace 不注入
    cp2 = DecisionCheckpoint()
    cp2.triggered = DecisionCheckpoint.MAX_PER_TASK
    assert cp2.note_for_next_turn("submit_referral", {},
                                  evidence_view=EvidenceView(ts)) is None


def test_v6_11_context_builder_single_outlet():
    """要求#3:ContextBuilder 是最终 context 的统一出口——
    证据块/条目块全部经 build_context 组装,无平行 context 模块。"""
    import os
    from agents.harness.context_builder import (
        build_context, evidence_block, worklist_block)
    from agents.harness.task_state_v3 import (
        TaskStateV3, UserStateExtractor, ToolResultStateExtractor)
    from agents.harness.evidence_view import EvidenceView
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    # 平行模块已删除（收紧指令 §3:不做第二套 context 系统）
    assert not os.path.exists(os.path.join(
        ROOT, "agents", "harness", "context_organization.py"))
    assert not os.path.exists(os.path.join(
        ROOT, "agents", "harness", "evidence_ledger.py"))
    # 组装:V5 blocks 与 V6.1 blocks 同一出口
    ts = TaskStateV3()
    UserStateExtractor.feed(ts, "a customer for about four months")
    ToolResultStateExtractor.feed(ts, "get_current_time",
                                  "The current time is 2025-11-14 03:40 EST.")
    ps = PlanStore()
    ps.write_plan(goal="g", steps=[
        {"description": "close card", "tool_hint": "close_card",
         "entities": ["card_1", "card_2"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    state = _mk_state(4)
    out = build_context(state, ts, plan_tracker=None,
                        memory_block="MEM-BLOCK", state_block="",
                        evidence_view=EvidenceView(ts),
                        plan_store=ps, worklist=wl)
    blob = out[0].content
    assert "MEM-BLOCK" in blob
    assert "confirmed by system" in blob and "current_date" in blob
    assert "NOT yet verified" in blob and "four months" in blob
    assert "Worklist" in blob and "[ ]" in blob and "card_2" in blob
    # 空状态零渲染（简单任务零开销——V5 成功路径保护）
    out2 = build_context(_mk_state(4), TaskStateV3(), plan_tracker=None,
                         evidence_view=EvidenceView(TaskStateV3()),
                         plan_store=PlanStore(), worklist=Worklist())
    assert out2[0].content == "SYSTEM PROMPT", \
        "空状态必须零渲染（V5 成功路径保护）"


def test_v6_12_task_state_current_date_single_feed():
    """要求#1(数据流):get_current_time → TaskState 的 system.current_date
    ——与记录提取同一条数据流,无旁路喂入。"""
    from agents.harness.task_state_v3 import (
        TaskStateV3, ToolResultStateExtractor)
    from agents.harness.evidence_view import EvidenceView
    ts = TaskStateV3()
    ToolResultStateExtractor.feed(ts, "get_current_time",
                                  "The current time is 2025-11-14 03:40:00 EST.")
    ev = EvidenceView(ts)
    recs = ev.confirmed_system_facts()
    assert recs and recs[0].key == "system.current_date" \
        and recs[0].value == "2025-11-14"
    # 非时间工具不误识别
    ts2 = TaskStateV3()
    ToolResultStateExtractor.feed(ts2, "get_accounts", "opened 2025-09-10")
    assert all(r.key != "system.current_date"
               for r in EvidenceView(ts2).confirmed_system_facts())


def test_v6_13_v5_behavior_not_hard_blocked():
    """要求#9:已有 V5 成功行为不被硬拦。

    (a) EvidenceView/Worklist/Checkpoint 不进 harness 拦截链
        ——BLOCKING_VERDICTS 未扩大（V5 freeze 语义）。
    (b) DSH_V6_DISABLE=1 一键回退纯 V5 行为。
    (c) checkpoint 注入式零 LLM（agent 侧源码断言）。"""
    import inspect
    from agents.harness.action_harness import BLOCKING_VERDICTS
    v5_verdicts = {"schema_violation", "evidence_mismatch",
                   "task_state_conflict", "kb_enum_violation",
                   "kb_threshold_violation", "kb_format_violation"}
    assert BLOCKING_VERDICTS == v5_verdicts or \
        BLOCKING_VERDICTS.issubset(v5_verdicts | {"evidence_mismatch"}), \
        "V6 不得新增 blocking verdict 类别"
    # (b) 环境开关
    import importlib
    import agents.two_agent as ta
    old = os.environ.get("DSH_V6_DISABLE")
    try:
        os.environ["DSH_V6_DISABLE"] = "1"
        importlib.reload(ta)
        assert ta._v6_enabled() is False, "DSH_V6_DISABLE=1 必须回退纯 V5"
    finally:
        if old is None:
            os.environ.pop("DSH_V6_DISABLE", None)
        else:
            os.environ["DSH_V6_DISABLE"] = old
        importlib.reload(ta)   # 恢复默认(enabled)
    assert ta._v6_enabled() is True
    # (c) checkpoint 零 LLM:agent 侧注入函数不调用 generate/LLM
    src = inspect.getsource(ta.DecisionAgent._v6_checkpoint_for_call)
    code_lines = [l for l in src.split("\n")
                  if l.strip() and not l.strip().startswith("#")]
    assert not any(re.search(r"\bgenerate\s*\(", l) for l in code_lines), \
        "checkpoint 不得调用 LLM（generate 调用）"


# ===========================================================================
# 6. V6.1 修复回归（三处已确认实现问题;全部确定性,无 LLM/网络）
#    #1 PlanStore/Worklist 父子进度冲突
#    #2 DecisionCheckpoint tool-call 消息时序
#    #3 纯文本最终推荐 Decision Check
# ===========================================================================
def test_v6_14_multi_entity_parent_step_waits_for_all_items():
    """修复#1:带多 entities 且已展开 Worklist 的 PlanStep——
    父步骤 completed 必须由子条目整体状态决定;
    单实体成功绝不整体完成;子失败不误标 completed;重复成功幂等。"""
    from agents.harness.plan_store import (
        PlanStore, COMPLETED, IN_PROGRESS)
    from agents.harness.worklist import Worklist
    ps = PlanStore()
    ps.write_plan("handle all incorrect transactions", [
        {"description": "dispute incorrect transaction",
         "tool_hint": "submit_dispute",
         "entities": ["txn_A", "txn_B", "txn_C"]},
    ])
    ps.update_plan("set_current", step_id=1)
    wl = Worklist()
    wl.sync_from_plan(ps)
    step = ps.steps[0]
    assert step.child_managed, "展开后步骤应标记为条目化"

    def _apply(txn, ok=True):
        ps.on_tool_result("submit_dispute", ok, {"transaction_id": txn})
        wl.on_tool_result(ps, "submit_dispute", ok, {"transaction_id": txn})

    # (a) 3 个实体完成 1 个 → 父 step 不 completed
    _apply("txn_A")
    assert step.status != COMPLETED, \
        "只有一个实体成功时父步骤不得 completed"
    assert step.status == IN_PROGRESS, "父步骤应保持 in_progress（已开始未完成）"
    assert wl.unfinished(), "仍有 2 个子条目未完成"

    # (b) 完成 3/3 → 父 step completed
    _apply("txn_B")
    assert step.status != COMPLETED, "2/3 完成父步骤仍不得 completed"
    _apply("txn_C")
    assert step.status == COMPLETED, "全部子条目完成后父步骤才 completed"
    assert not wl.unfinished(), "全部完成后不应再有未完成子条目"

    # (d) 重复成功调用保持幂等（不重置状态）
    _apply("txn_A")
    assert step.status == COMPLETED, "幂等重复调用不得回退父步骤状态"
    assert all(i.status == "completed" for i in wl.items_for_step(1)), \
        "幂等重复调用不得重置已完成的子条目"

    # (c) 其中一个失败 → 父 step 不 completed
    ps2 = PlanStore()
    ps2.write_plan("g", [{"description": "close cards",
                          "tool_hint": "close_card",
                          "entities": ["c1", "c2", "c3"]}])
    ps2.update_plan("set_current", step_id=1)
    wl2 = Worklist()
    wl2.sync_from_plan(ps2)
    step2 = ps2.steps[0]

    def _apply2(card, ok=True):
        ps2.on_tool_result("close_card", ok, {"card_id": card})
        wl2.on_tool_result(ps2, "close_card", ok, {"card_id": card})

    _apply2("c1", True)
    _apply2("c2", False)
    assert step2.status != COMPLETED, \
        "存在 failed 子条目时父步骤不得 completed"
    assert wl2.has_unfinished, "c2 失败 → 仍有未完成子条目（父不整体结束）"
    # 失败条目可恢复;全部完成后父步骤才 completed（失败不永久阻塞）
    _apply2("c2", True)
    _apply2("c3", True)
    assert step2.status == COMPLETED

    # V5 回退形态:未展开 Worklist（child_managed=False）时行为不变
    ps3 = PlanStore()
    ps3.write_plan("g", [{"description": "x", "tool_hint": "close_card",
                          "entities": ["c1", "c2"]}])
    ps3.update_plan("set_current", step_id=1)
    st3 = ps3.on_tool_result("close_card", True, {"card_id": "c1"})
    assert st3 is not None and st3.status == COMPLETED, \
        "无 Worklist（V5 回退）时单次匹配仍按 V5 语义完成"


def _mk_naked_decision_agent(seed_evidence=True):
    """构造可测的 DecisionAgent（object.__new__ 绕过需要 LLM 的 __init__）。

    只装配 generate_next_message 路径所需的运行时属性——无网络 / 无 LLM。
    """
    from agents.harness.task_state_v3 import (
        TaskStateV3, UserStateExtractor, ToolResultStateExtractor)
    from agents.harness.plan_store import PlanStore
    from agents.harness.worklist import Worklist
    from agents.harness.evidence_view import EvidenceView
    from agents.harness.decision_checkpoint import DecisionCheckpoint
    from agents.harness.execution_plan import PlanTracker
    from agents.two_agent import DecisionAgent
    a = object.__new__(DecisionAgent)
    a.llm = "stub"
    a.tools = []
    a.llm_args = {}
    a.domain_policy = ""
    a._knowledge_agent = None
    a.memory = None
    a._instruction_variant = "two_agent_harness"
    a.task_state = TaskStateV3()
    a.harness = None
    a._packets = []
    a._tool_by_name = {}
    a._rejection_counts = {}
    a.MAX_SAME_FIELD_REJECTIONS = 2
    a.plan_tracker = PlanTracker()
    a._toolcall_inner_by_id = {}
    a._toolcall_args_by_id = {}
    a._state_messages_cache = []
    a.plan_store = PlanStore()
    a._planning_prompted = False
    a._planning_fallback_pending = False
    a.v6_enabled = True
    a.evidence_view = EvidenceView(a.task_state)
    a.worklist = Worklist()
    a.checkpoint = DecisionCheckpoint()
    a._v6_note_pending = None
    a._v6_checkpointed_signatures = set()
    if seed_evidence:
        UserStateExtractor.feed(
            a.task_state, "I have been a customer for about four months")
        ToolResultStateExtractor.feed(
            a.task_state, "get_current_time",
            "The current time is 2025-11-14 03:40:00 EST.")
    return a


def _mk_agent_state():
    state = types.SimpleNamespace()
    state.system_messages = [_tau2_system()]
    state.messages = []
    return state


def _install_scripted_generate(script):
    """把 agents.two_agent.generate 换成按脚本返回 AssistantMessage 的桩。

    同时记录每次 generate 收到的 messages（本轮 LLM context）——
    用于断言 checkpoint 只出现在 generate 的临时视图里,不在
    state.messages（正式 conversation state）里。
    返回 (box, restore)。box["messages"] = [每轮的 messages 列表]。
    用完必须 restore（避免污染其他测试）。
    """
    import agents.two_agent as ta
    box = {"i": 0, "messages": []}
    original = ta.generate

    def fake_generate(model=None, tools=None, messages=None,
                      call_name=None, **kwargs):
        box["messages"].append(messages)
        i = box["i"]
        box["i"] += 1
        return script[min(i, len(script) - 1)]()

    ta.generate = fake_generate
    return box, (lambda: setattr(ta, "generate", original))


def _context_has_checkpoint(messages):
    """本轮传给 generate 的 messages 副本中是否含 checkpoint 6 问。"""
    from tau2.data_model.message import SystemMessage
    return any(isinstance(m, SystemMessage)
               and "DECISION CHECKPOINT" in (m.content or "")
               for m in (messages or []))


def _state_has_checkpoint(state):
    """正式 state.messages 中是否含 checkpoint（正确实现下应恒为 False）。"""
    return _context_has_checkpoint(state.messages)


def _has_dangling_tool_call(messages, accepted):
    """历史里是否存在未被接受的、无对应 ToolResult 的旧 Assistant tool_call。

    被接受的那条（返回给 orchestrator、即将执行）不算——它的 ToolResult
    由 orchestrator 下一轮回填。
    """
    from tau2.data_model.message import AssistantMessage
    for m in messages:
        if isinstance(m, AssistantMessage) and m.tool_calls and m is not accepted:
            return True
    return False


def test_v6_15_checkpoint_hold_leaves_no_dangling_tool_call():
    """修复#2:关键动作首次触发 checkpoint 时,不把未执行的 Assistant
    tool-call 写入正式历史（否则产生 Assistant→System→Assistant 的
    无 ToolResult 脏序列）。同签名第二次正常放行。"""
    from tau2.data_model.message import (
        AssistantMessage, ToolCall, UserMessage)
    a = _mk_naked_decision_agent(seed_evidence=True)
    state = _mk_agent_state()

    def _proposal():
        return AssistantMessage.text("", tool_calls=[ToolCall(
            id="c1", name="submit_referral",
            arguments={"account_type": "World Blue"})])

    box, restore = _install_scripted_generate([_proposal, _proposal])
    try:
        ret, state = a.generate_next_message(
            UserMessage(role="user", content="please refer my partner"),
            state)
    finally:
        restore()

    # 恰好多一轮:首次 checkpoint,第二次放行
    assert box["i"] == 2, f"应恰好两轮 generate（一次 checkpoint 重生成）, got {box['i']}"
    assert ret is not None and ret.tool_calls, "第二次同签名动作应正常放行"
    assert ret.tool_calls[0].name == "submit_referral"
    # 历史中不存在无 ToolResult 的旧 Assistant tool-call（脏序列根治）
    assert not _has_dangling_tool_call(state.messages, ret), \
        "历史里不得残留被 checkpoint 丢弃的旧 assistant tool-call"
    atc = [m for m in state.messages
           if isinstance(m, AssistantMessage) and m.tool_calls]
    assert len(atc) == 1 and atc[0] is ret, \
        "只应保留被放行的那条 tool-call"
    # V6.1 修复 #2:checkpoint 是 ephemeral context——只在触发后的
    # 下一轮 generate 视图里出现一次,**绝不进入正式 state.messages**。
    assert not _context_has_checkpoint(box["messages"][0]), \
        "触发轮(round1)生成时 checkpoint 尚未产生"
    assert _context_has_checkpoint(box["messages"][1]), \
        "触发后的下一轮 generate 视图必须含 checkpoint 6 问"
    assert not _state_has_checkpoint(state), \
        "checkpoint 不得写入正式 state.messages"
    # pending 一次性消费（注入后即清空）
    assert a._v6_note_pending is None, "checkpoint 尾注应已被消费"
    assert a.checkpoint.pending is None, "checkpoint artifact pending 应已消费"
    assert len(a._v6_checkpointed_signatures) == 1, "同签名只 checkpoint 一次"


def test_v6_16_final_recommendation_checkpoint_and_restraint():
    """修复#3:纯文本最终推荐触发证据检查点（通用意图,非产品词）;
    普通文本不触发;空证据零触发;同任务只检查一次。"""
    from agents.harness.decision_checkpoint import is_final_recommendation
    from tau2.data_model.message import AssistantMessage, UserMessage

    # (a) 通用 recommendation/selection 意图 → 触发
    for text in ("I recommend the Sky Blue account.",
                 "The best option is the savings account.",
                 "You should choose the premium card.",
                 "The checking account is the most suitable account."):
        assert is_final_recommendation(text), f"应识别为最终推荐: {text!r}"
    # (b) 普通进展/状态/追问 → 不触发（避免大面积误触发）
    for text in ("I have already checked your account.",
                 "Your card is currently active.",
                 "I need to confirm one more piece of information.",
                 "I will now look up your transactions.",
                 "The Emerald account works for you."):
        assert not is_final_recommendation(text), \
            f"普通文本不得触发推荐检查: {text!r}"

    # (c) 集成:有证据 → 触发一次（多一轮 generate）,文本不落地为最终回复
    a = _mk_naked_decision_agent(seed_evidence=True)
    state = _mk_agent_state()
    rec = lambda: AssistantMessage.text(
        "After review, I recommend the premium account.")
    box, restore = _install_scripted_generate([rec, rec])
    try:
        ret, state = a.generate_next_message(
            UserMessage(role="user", content="which one fits me?"), state)
    finally:
        restore()
    assert box["i"] == 2, "最终推荐应触发一次 checkpoint（多一轮 generate）"
    assert ret is not None and "recommend" in (ret.content or "").lower()
    assert not _context_has_checkpoint(box["messages"][0]), \
        "含推荐的首次生成尚未注入 checkpoint"
    assert _context_has_checkpoint(box["messages"][1]), \
        "推荐触发后的下一轮 generate 视图应含证据 6 问"
    assert not _state_has_checkpoint(state), \
        "推荐 checkpoint 同样不得写入 state.messages（ephemeral context）"
    assert a._v6_note_pending is None and a.checkpoint.pending is None, \
        "推荐 checkpoint 的 pending 应一次性消费"
    assert a.checkpoint.triggered == 1, "同一任务推荐检查只触发一次"
    assert ("__final_recommendation__",) in a._v6_checkpointed_signatures

    # (d) 克制:空证据视图零触发（简单任务零开销,保护 V5 成功路径）
    a2 = _mk_naked_decision_agent(seed_evidence=False)
    state2 = _mk_agent_state()
    box2, restore2 = _install_scripted_generate([rec, rec])
    try:
        ret2, state2 = a2.generate_next_message(
            UserMessage(role="user", content="which one fits me?"), state2)
    finally:
        restore2()
    assert box2["i"] == 1, "无任何证据时推荐不得触发 checkpoint"
    assert a2.checkpoint.triggered == 0
    assert not _context_has_checkpoint(box2["messages"][0]), \
        "空证据时 checkpoint 不得出现"
    assert not _state_has_checkpoint(state2)
    assert ret2 is not None and "recommend" in (ret2.content or "").lower()


def test_v6_17_plan_step_entities_cap_all_entries():
    """修复#1:PlanStep 实体上限统一提高（MAX_STEP_ENTITIES）——
    6 个实体全部保存、Worklist 全部展开、4/6 父不完成、6/6 父完成。
    add_step 入口与 write_plan 使用同一常量。"""
    from agents.harness.plan_store import (
        PlanStore, COMPLETED, MAX_STEP_ENTITIES)
    from agents.harness.worklist import Worklist
    import inspect
    import agents.harness.plan_store as ps_mod
    # 统一常量:两个入口都引用 MAX_STEP_ENTITIES,无散落的字面量截断
    assert MAX_STEP_ENTITIES >= 6, "上限必须覆盖当前 benchmark 长任务"
    src = inspect.getsource(ps_mod)
    assert src.count("MAX_STEP_ENTITIES") >= 3, \
        "两个实体入口都应使用同一常量（含定义处）"
    assert "[:4]" not in src, "不得残留旧的 4 实体截断"

    ents6 = ["txn_%d" % i for i in range(1, 7)]  # 6 个对象
    ps = PlanStore()
    ps.write_plan("handle all incorrect transactions", [
        {"description": "dispute incorrect transaction",
         "tool_hint": "submit_dispute", "entities": list(ents6)},
    ])
    # write_plan 实际保存 6 个（不再截断到 4）
    assert ps.steps[0].entities == ents6, \
        f"write_plan 应保存全部 6 个实体, got {ps.steps[0].entities}"
    # add_step 走同一常量（独立 store 验证,避免同名实体产生歧义）
    ps_b = PlanStore()
    ps_b.write_plan("g", [{"description": "seed"}])
    ps_b.update_plan("add_step", description="extra", entities=list(ents6))
    assert ps_b.steps[1].entities == ents6, "add_step 应保存全部 6 个实体"

    ps.update_plan("set_current", step_id=1)
    wl = Worklist()
    wl.sync_from_plan(ps)
    items = wl.items_for_step(1)
    assert len(items) == 6, f"Worklist 应展开 6 个条目, got {len(items)}"
    assert [i.entity for i in items] == ents6

    def _apply(txn):
        ps.on_tool_result("submit_dispute", True, {"transaction_id": txn})
        wl.on_tool_result(ps, "submit_dispute", True, {"transaction_id": txn})

    for t in ents6[:4]:
        _apply(t)
    assert ps.steps[0].status != COMPLETED, \
        "完成 4/6 时父步骤仍不得 completed（后 2 个任务不能丢）"
    assert len(wl.unfinished()) == 2, "应还剩 2 个待处理对象"
    _apply(ents6[4])
    assert ps.steps[0].status != COMPLETED, "5/6 仍不得 completed"
    _apply(ents6[5])
    assert ps.steps[0].status == COMPLETED, "6/6 完成后父步骤才 completed"
    assert not wl.unfinished()


def test_v6_18_checkpoint_is_ephemeral_context_not_history():
    """修复#2:连续三轮验证 checkpoint 真正是"一次性 context"。

    round1:关键动作 → trigger（本轮 generate 视图尚无 checkpoint）
    round2:generate 视图含 checkpoint（DA 重新决策并放行同签名动作）
    round3:无新 trigger → generate 视图不再含旧 checkpoint

    全程 state.messages 不得出现 checkpoint;pending 消费后不复活。
    """
    from tau2.data_model.message import (
        AssistantMessage, ToolCall, ToolMessage, UserMessage)
    a = _mk_naked_decision_agent(seed_evidence=True)
    state = _mk_agent_state()

    def _proposal():
        return AssistantMessage.text("", tool_calls=[ToolCall(
            id="c1", name="submit_referral",
            arguments={"account_type": "World Blue"})])

    def _plain():
        return AssistantMessage.text(
            "Your referral has been submitted.", tool_calls=None)

    box, restore = _install_scripted_generate([_proposal, _proposal, _plain])
    try:
        # 第 1 次进入:round1 触发 + round2 放行 → 返回被接受的 tool-call
        ret, state = a.generate_next_message(
            UserMessage(role="user", content="please refer my partner"),
            state)
        assert ret.tool_calls and ret.tool_calls[0].name == "submit_referral"
        # 模拟 orchestrator 回填 ToolResult,进入下一轮（由 agent 入队）
        ret3, state = a.generate_next_message(
            ToolMessage(id=ret.tool_calls[0].id, role="tool",
                        requestor="assistant",
                        content="referral submitted", error=False), state)
    finally:
        restore()

    assert box["i"] == 3, f"应三轮 generate, got {box['i']}"
    # round1 尚未注入;round2 注入;round3 旧 checkpoint 不再出现
    assert not _context_has_checkpoint(box["messages"][0]), \
        "round1 generate 视图不应含 checkpoint"
    assert _context_has_checkpoint(box["messages"][1]), \
        "round2 generate 视图应含 checkpoint（一次性注入）"
    assert not _context_has_checkpoint(box["messages"][2]), \
        "round3 无新 trigger 时旧 checkpoint 必须消失（ephemeral）"
    # 正式历史全程无 checkpoint
    assert not _state_has_checkpoint(state), \
        "checkpoint 绝不进入正式 state.messages"
    assert a.checkpoint.triggered == 1, "只触发一次"
    assert a.checkpoint.pending is None and a._v6_note_pending is None


# ===========================================================================
# main
# ===========================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("确定性单元测试 — Frozen V5 runtime + V6.0 runtime（无 LLM / 无网络）")
    print("=" * 70)
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        run(name, fn)
    print(f"\n{'✅ 全部通过' if FAIL == 0 else f'❌ {FAIL} 失败'} ({PASS}/{len(tests)})")
    sys.exit(1 if FAIL else 0)
