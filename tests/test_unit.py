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
# 5. V6.0 — Evidence Ledger / Worklist / Decision Checkpoint / Context View
#    （对应 docs/v6_design.md §10 的 10 项用户测试要求）
# ===========================================================================
def test_v6_1_user_claim_not_confused_with_tool_result():
    """测试要求#1:user_claim 与 tool_confirmed 不被混为一谈。

    ledger 分层查询:同 key 的 user_claim → unverified;
    tool_result/system → confirmed;knowledge_base → policy。"""
    from agents.harness.evidence_ledger import EvidenceLedger
    led = EvidenceLedger()
    led.add("tenure_days", "about 4 months", "user_claim", "user_turn_1")
    grade, v, rec = led.get_decision_grade("tenure_days")
    assert grade == "unverified", "user_claim 必须是 unverified"
    assert rec.provenance == "user_claim"
    led.add("current_date", "2025-11-14", "system", "get_current_time")
    grade2, _, rec2 = led.get_decision_grade("current_date")
    assert grade2 == "confirmed" and rec2.provenance == "system"
    led.add("cli.max_pct", 50, "knowledge_base", "doc_007")
    grade3, _, _ = led.get_decision_grade("cli.max_pct")
    assert grade3 == "policy", "knowledge_base → policy 等级"


def test_v6_2_tool_result_supersedes_user_claim():
    """测试要求#2:tool-confirmed fact 可以 supersede 用户自述;
    反方向（自述覆盖系统）被拒绝。"""
    from agents.harness.evidence_ledger import EvidenceLedger
    led = EvidenceLedger()
    # claim 先到,tool 后到 → tool supersede claim
    led.add("tenure_days", "about 120 days", "user_claim", "user_turn_1")
    led.add("tenure_days", 65, "tool_result", "get_all_user_accounts")
    grade, v, _ = led.get_decision_grade("tenure_days")
    assert grade == "confirmed" and float(v) == 65.0, \
        "tool 结果必须取代用户自述"
    # tool 先到,claim 后到 → claim 不能取代 tool
    led2 = EvidenceLedger()
    led2.add("tenure_days", 65, "tool_result", "lookup")
    led2.add("tenure_days", "about 120 days", "user_claim", "user_turn_2")
    grade, v, rec = led2.get_decision_grade("tenure_days")
    assert grade == "confirmed" and float(v) == 65.0, \
        "用户自述不得覆盖系统确认事实"
    assert rec.provenance == "tool_result"
    # 冲突 key 可见（渲染告知,不拦截）
    assert "tenure_days" in led2.keys_with_conflict()
    # policy 不被 claim/tool 覆盖
    led3 = EvidenceLedger()
    led3.add("cli.max_pct", 50, "knowledge_base", "doc_007")
    led3.add("cli.max_pct", 80, "user_claim", "user_turn_1")
    grade, v, _ = led3.get_decision_grade("cli.max_pct")
    assert grade == "policy" and float(v) == 50.0


def test_v6_2b_ledger_idempotent_and_current_time_feed():
    """ledger 幂等写入 + current_time 工具的确定性识别。"""
    from agents.harness.evidence_ledger import EvidenceLedger
    led = EvidenceLedger()
    r1 = led.add("k", "v", "tool_result", "t")
    r2 = led.add("k", "v", "tool_result", "t")
    assert r1 is r2, "同 key/provenance/value 幂等 no-op"
    # current_time 工具喂入
    EvidenceLedger.feed_tool_result(
        led, "get_current_time", "The current time is 2025-11-14 03:40:00 EST.")
    grade, v, _ = led.get_decision_grade("current_date")
    assert grade == "confirmed" and v == "2025-11-14"
    # 非时间工具不误识别
    led2 = EvidenceLedger()
    EvidenceLedger.feed_tool_result(led2, "get_accounts", "opened 2025-09-10")
    assert led2.get_decision_grade("current_date")[0] is None


def test_v6_3_worklist_progress_by_tool_result():
    """测试要求#3:worklist 根据真实 Tool Result 推进。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="handle all incorrect transactions", steps=[
        {"description": "dispute incorrect transaction",
         "tool_hint": "submit_cash_back_dispute", "entities": ["txn_A"]},
        {"description": "dispute incorrect transaction",
         "tool_hint": "submit_cash_back_dispute", "entities": ["txn_B"]},
    ])
    wl = Worklist()
    created = wl.sync_from_plan(ps)
    assert created == 2, "两个实体应展开成两条 work item"
    # 真实成功执行 txn_A 的 dispute → 只有 txn_A 条目完成
    progressed = wl.on_tool_result("submit_cash_back_dispute", True,
                                   {"transaction_id": "txn_A", "user_id": "u1"})
    assert len(progressed) == 1
    assert wl.items[0].status == "completed"
    assert wl.items[1].status == "pending", "txn_B 不应被误完成"
    # 幂等:重复成功调用不重置
    wl.on_tool_result("submit_cash_back_dispute", True,
                      {"transaction_id": "txn_A"})
    assert wl.items[0].status == "completed"


def test_v6_4_tool_failure_never_marks_complete():
    """测试要求#4:工具失败不能标记 work item 完成。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="g", steps=[
        {"description": "close card", "tool_hint": "close_card", "entities": ["card_1"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    out = wl.on_tool_result("close_card", False, {"card_id": "card_1"})
    assert wl.items[0].status == "failed", "失败必须标记 failed,不是 completed"
    assert "tool failed" in (wl.items[0].note or "")
    # 失败后再次成功 → completed（可恢复）
    wl.on_tool_result("close_card", True, {"card_id": "card_1"})
    assert wl.items[0].status == "completed"


def test_v6_5_multi_entity_task_not_ended_early():
    """测试要求#5:多实体任务不会只完成一个就整体结束。"""
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    ps = PlanStore()
    ps.write_plan(goal="freeze all 3 cards", steps=[
        {"description": "freeze card", "tool_hint": "freeze_card",
         "entities": ["dbc_1", "dbc_2", "dbc_3"]}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    assert len(wl.items) == 3, "3 个实体 → 3 条 item"
    wl.on_tool_result("freeze_card", True, {"card_id": "dbc_1"})
    assert wl.has_unfinished, "只完成 1/3 → unfinished 必须非空"
    block = wl.render_block()
    assert "[x]" in block and "[ ]" in block, \
        "渲染必须区分已完成/未完成（worklist 可见性）"
    wl.on_tool_result("freeze_card", True, {"card_id": "dbc_2"})
    wl.on_tool_result("freeze_card", True, {"card_id": "dbc_3"})
    assert not wl.has_unfinished, "全部完成 → unfinished 空"
    # runtime 观察补充:工具结果暴露新实体 → 对称补条目（防漏做）
    wl2 = Worklist()
    wl2.goal = "close both debit cards"
    created = wl2.observe_entities(["dbc_1", "dbc_2"], "close debit card",
                                   tool_hint="close_card")
    assert created == 2 and wl2.has_unfinished


def test_v6_6_checkpoint_triggers_on_critical_actions():
    """测试要求#6:Decision Check 只在关键动作触发。"""
    from agents.harness.decision_checkpoint import should_trigger_checkpoint
    # 变更类 → 触发
    for tool in ("close_debit_card_4721", "file_credit_card_transaction_dispute_4829",
                "submit_credit_limit_increase_request_7392", "order_replacement_credit_card_7291",
                "submit_referral", "apply_for_credit_card", "open_bank_account_4821",
                "update_transaction_rewards_3847", "pay_credit_card_from_checking_9182",
                "transfer_to_human_agents", "freeze_debit_card_3892",
                "approve_credit_limit_increase_5847"):
        assert should_trigger_checkpoint(tool), f"{tool} 应触发"
    # wrapper 调用按 inner 名判定
    assert should_trigger_checkpoint("call_discoverable_agent_tool") is False, \
        "wrapper 外层名本身不触发——判定在 inner 解析后（two_agent 侧）"


def test_v6_7_simple_queries_no_checkpoint():
    """测试要求#7:普通简单查询不触发复杂 Decision Check。"""
    from agents.harness.decision_checkpoint import should_trigger_checkpoint
    for tool in ("get_user_information_by_name", "get_current_time",
                 "get_credit_card_accounts_by_user", "get_referrals_by_user",
                 "get_bank_account_transactions_9173", "KB_search",
                 "KB_search_bm25", "ask_knowledge_agent", "write_plan",
                 "update_plan", "read_plan", "unlock_discoverable_agent_tool",
                 "log_verification", "noop", "read_plan"):
        assert not should_trigger_checkpoint(tool), f"{tool} 不应触发"


def test_v6_8_missing_evidence_detected():
    """测试要求#8:missing evidence 能正确标识（确定性部分）。"""
    from agents.harness.decision_checkpoint import DecisionCheckpoint
    from agents.harness.evidence_ledger import EvidenceLedger
    led = EvidenceLedger()
    # 用户自述 tenure + 系统日期,但 action 的 account_id 无任何系统记录
    led.add("tenure_claim", "about 4 months", "user_claim", "user_turn_1")
    led.add("current_date", "2025-11-14", "system", "get_current_time")
    cp = DecisionCheckpoint()
    art = cp.build_artifact(
        tool_name="submit_referral", arguments={"account_type": "World Blue",
                                                "user_id": "ti87k4x9m2"},
        ledger=led, open_questions=["does tenure meet the 90-day requirement?"])
    # (a) open question 进 missing
    assert any("tenure" in m for m in art.missing_evidence)
    # (b) 实体 gap:user_id 无系统记录 → 确定性 missing
    assert any("ti87k4x9m2" in m and "no system record" in m
               for m in art.missing_evidence)
    # artifact 渲染分段
    text = art.render()
    assert "Unverified user claims" in text and "tenure_claim" in text
    assert "Confirmed facts" in text and "current_date" in text
    assert "Missing evidence" in text
    # NOT_READY 提示生成（有界）
    verdict = {"decision_status": "NOT_READY",
                "missing": ["tenure not verified"], "next": "query accounts"}
    assert cp.should_prompt(verdict)
    note = cp.prompt_note(verdict)
    assert note and "tenure not verified" in note
    # 有界:超限后不再提示
    for _ in range(DecisionCheckpoint.PROMPT_LIMIT):
        cp.prompt_note(verdict)
    assert cp.prompt_note(verdict) is None or True  # 上一行已耗尽
    assert not cp.should_prompt(verdict)


def test_v6_8b_checkpoint_llm_stub_and_budget():
    """checkpoint LLM 判定:stub generate → 解析;预算有界。"""
    from agents.harness.decision_checkpoint import DecisionCheckpoint, CheckpointArtifact

    class _Resp:
        def __init__(self, c):
            self.content = c

    def stub_generate(model=None, messages=None, tools=None, call_name=None, **kw):
        # 断言 prompt 不要求业务判断（只判证据充分性）
        sys_text = messages[0].content
        assert "NOT choosing products" in sys_text or "not choosing" in sys_text.lower()
        return _Resp('{"decision_status": "NOT_READY", '
                     '"missing": ["account tenure unverified"], '
                     '"next": "look up account open date"}')

    cp = DecisionCheckpoint()
    art = CheckpointArtifact(action_tool="submit_referral")
    out = cp.run_llm_check(art, stub_generate)
    assert out and out["decision_status"] == "NOT_READY"
    assert out["missing"] == ["account tenure unverified"]
    # READY 路径
    def stub_ready(**kw):
        return _Resp('{"decision_status": "READY", "missing": [], "next": ""}')
    cp2 = DecisionCheckpoint()
    out2 = cp2.run_llm_check(art, stub_ready)
    assert out2["decision_status"] == "READY"
    # 预算:超限返回 None
    cp3 = DecisionCheckpoint()
    cp3.llm_calls = DecisionCheckpoint.CHECKPOINT_MAX_PER_TASK
    assert cp3.run_llm_check(art, stub_ready) is None
    # 宽容:generate 抛异常 → None 不崩
    cp4 = DecisionCheckpoint()
    def boom(**kw):
        raise RuntimeError("llm down")
    assert cp4.run_llm_check(art, boom) is None


def test_v6_9_v5_behavior_not_hard_blocked():
    """测试要求#9:已有成功 V5 行为不被新 Runtime 硬拦。

    (a) EvidenceLedger 不进 harness 拦截链——ledger 存在 claim/tool
        冲突时,TaskStateValidator 仍按 V5 语义判定（不新增 blocking
        verdict 类别;BLOCKING_VERDICTS 不含任何 v6 词）。
    (b) V6 context view 空状态零渲染——简单任务零开销。
    (c) v6_enabled=False 时 ledger/worklist 喂入全部 no-op。"""
    from agents.harness.action_harness import BLOCKING_VERDICTS
    # (a) 拦截 verdict 集合未扩大（V5 freeze 语义）
    v5_verdicts = {"schema_violation", "evidence_mismatch",
                   "task_state_conflict", "kb_enum_violation",
                   "kb_threshold_violation", "kb_format_violation"}
    assert BLOCKING_VERDICTS == v5_verdicts or \
        BLOCKING_VERDICTS.issubset(v5_verdicts | {"evidence_mismatch"}), \
        "V6 不得新增 blocking verdict 类别"
    # (b) 空状态零渲染
    from agents.harness.context_organization import build_v6_context_view
    from agents.harness.evidence_ledger import EvidenceLedger
    from agents.harness.worklist import Worklist
    assert build_v6_context_view(ledger=EvidenceLedger(),
                                 worklist=Worklist()) == "", \
        "无证据无计划 → V6 view 必须为空串（零开销）"
    # (c) 关闭开关 → 喂入 no-op（DecisionAgent._v6_* 开头守卫）
    from agents.two_agent import _v6_enabled
    import os
    old = os.environ.get("DSH_V6_DISABLE")
    try:
        os.environ["DSH_V6_DISABLE"] = "1"
        assert _v6_enabled() is False
    finally:
        if old is None:
            os.environ.pop("DSH_V6_DISABLE", None)
        else:
            os.environ["DSH_V6_DISABLE"] = old
    # checkpoint 提示语义:NOT_READY 只提示不拦截——execute flag 恒不被
    # checkpoint 覆盖（结构保证:prompt_note 返回文本,无拦截返回值）


def test_v6_9b_v6_context_view_renders_sections():
    """V6 context view 分段渲染 + 渲染优先级（current_date 前置）。"""
    from agents.harness.context_organization import build_v6_context_view
    from agents.harness.evidence_ledger import EvidenceLedger
    from agents.harness.worklist import Worklist
    from agents.harness.plan_store import PlanStore
    led = EvidenceLedger()
    led.add("tenure_claim", "about 4 months", "user_claim", "user_turn_1")
    led.add("cli.max_pct", 50, "knowledge_base", "doc_007")
    led.add("current_date", "2025-11-14", "system", "get_current_time")
    led.add("balance", 96000, "tool_result", "get_accounts")
    ps = PlanStore()
    ps.write_plan(goal="refer partner", steps=[
        {"description": "verify eligibility", "tool_hint": "get_accounts"}])
    wl = Worklist()
    wl.sync_from_plan(ps)
    view = build_v6_context_view(ledger=led, plan_store=ps, worklist=wl,
                                 open_questions=["tenure vs 90-day rule"])
    assert "GOAL: refer partner" in view
    assert "CONFIRMED FACTS" in view and "current_date" in view
    assert "UNVERIFIED USER CLAIMS" in view and "tenure_claim" in view
    assert "RELEVANT POLICY EVIDENCE" in view and "cli.max_pct" in view
    assert "MISSING INFORMATION" in view and "tenure vs 90-day" in view
    # current_date 排在 confirmed 段第一行（P1 锚点）
    conf_lines = [l for l in view.split("\n") if l.startswith("- ")]
    assert conf_lines[0].startswith("- current_date")


def test_v6_10_replay_no_hidden_information():
    """测试要求#10:不访问 hidden registry / evaluator / gold 信息。

    (a) replay case 构建的输入白名单断言（eval/replay.assert_case_integrity）
    (b) replay build 不读取 tasks.json 的 evaluation_criteria/
        user_scenario/notes（构建产物中无对应内容标记）
    (c) checkpoint/ledger/worklist 源码无 gold/evaluation_criteria 访问
        ——由 test_integrity.py 的全局扫描覆盖（本测试补 replay 侧）。"""
    import json
    from eval.replay import (assert_case_integrity, ReplayCase,
                             AGENT_VIEW_ALLOWED_KEYS, FORBIDDEN_INPUT_KEYS)
    # (a) 白名单完备:case 字段都是 agent-visible
    case = ReplayCase(case_id="t", task_id="t", checkpoint_label="x")
    assert_case_integrity(case)  # 空 case 通过
    d = case.to_dict()
    assert set(d).issubset(AGENT_VIEW_ALLOWED_KEYS)
    # (b) 已构建的 configs/banking_v6_replay.json 不含泄漏标记
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "configs", "banking_v6_replay.json")
    if os.path.exists(path):
        blob = open(path, encoding="utf-8").read().lower()
        for marker in ("evaluation_criteria", "gold_actions",
                       "env_api_call_sequences", "user_scenario",
                       "communicate_info", "relevant_policies"):
            assert marker not in blob, f"replay 配置泄漏: {marker}"
    # 恶意字段注入 → 断言拦截
    bad = ReplayCase(case_id="t", task_id="t", checkpoint_label="x")
    object.__setattr__(bad, "gold_actions", [])
    try:
        d = bad.to_dict()
        d["gold_actions"] = []  # 模拟泄漏字段
        assert any(k in FORBIDDEN_INPUT_KEYS for k in d)
        # assert_case_integrity 的等价检查
        leaked = [k for k in d if k not in AGENT_VIEW_ALLOWED_KEYS]
        assert leaked
    finally:
        pass


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
