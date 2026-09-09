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
# main
# ===========================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("确定性单元测试 — Frozen V5 runtime（无 LLM / 无网络）")
    print("=" * 70)
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        run(name, fn)
    print(f"\n{'✅ 全部通过' if FAIL == 0 else f'❌ {FAIL} 失败'} ({PASS}/{len(tests)})")
    sys.exit(1 if FAIL else 0)
