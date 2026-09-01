# V2.1 Inner-Parameter Harness Validation — Targeted Diagnostic 报告

## 交付清单（对应 13 项要求）

**1. Action Resolver 设计**：`agents/harness/resolver.py` ——
`ActionResolver.resolve(tool_call) → ResolvedAction(outer_tool_name, outer_arguments,
tool_name, arguments, inner_schema, is_wrapper, resolve_error)`。职责与校验分离：
resolver 只"看懂"，harness 只"检查"。

**2. wrapper 如何解析**：`call_discoverable_agent_tool` 的
`arguments` 是 JSON 字符串 → `json.loads(raw_args, parse_int=float)`（与
tau2 执行侧同口径）→ inner 工具名取 `agent_tool_name`。永不抛异常：
解析失败记 `resolve_error` 回退 outer 校验放行（不误拦）。

**3. inner schema 来源**：wrapper `Tool._func.__self__`（toolkit 实例）
→ `get_discoverable_tools()` 44 个方法 → 每个方法 `inspect.signature`
（required=无默认值参数，类型映射 str/float/bool）+ docstring enum 提取
（`"Must be one of: 'x' | 'y'"` 正则——提取不到不填，不猜）。不改 tau2。

**4. grounded_values 新 schema**：`{tool_name, parameter_name, value,
value_type, source_doc_id, unit?}`——evidence 明确说"属于哪个工具的
哪个参数"；KA prompt 要求：不能可靠判断归属 → 留在 facts，不猜。

**5. user provenance 修复**：废 `_uc_N` 错误键；只提取 verified_facts
中 `param = value` 显式模式（正则）为 user_values（tool/param 或裸名
双键匹配）；不可靠来源不构造键——缺 provenance 保持 not_grounded 放行。

**6. trace 串联**：`action_proposed → action_resolved（inner_tool_name+
inner_arguments）→ action_validation → action_rejected / action_executed`。
执行仍在 orchestrator（validate_only 放行）——未为 trace 改执行逻辑。

## 版本迭代（本阶段真实开发轨迹）

| 版本 | 问题 | 修复 |
|---|---|---|
| V2.1 | 037 对照组 1.0→0.0（14 拒绝 loop）| 发现三类根因 |
| V2.1.1 | 假枚举/叙述文本 evidence | evidence 健康过滤（违反 inner enum/含空格→降级放行）|
| V2.1.2 | 伪值说明 `"true_or_false_boolean_from_customer"`、跨 packet 矛盾（doc_014 vs doc_015 相反 boolean）| 描述性伪值模式 + 冲突键检测 |
| V2.1.3 | 格式占位符 `"MM/DD/YYYY"`、字符串化 boolean（False vs "false"）| 格式占位符识别 + 布尔等价 |
| V2.1.4 | inner required 全拦（signature 推导非运行时约束，一次拦 10 字段）| inner missing_required 降级非拦截 |

## 最终结果（V2.1.4 targeted，V1.2 对照）

| task | V1.2 | V2.1.4 | 拒绝 | loop | 备注 |
|---|---|---|---|---|---|
| 054 | 0.0 | 0.0 | 0 | 0 | 干净放行 |
| 053 | 0.0 | 0.0 | 1 | 1 | 1 次拦截 |
| 095 | 0.0 | 0.0 | 6 | — | evidence 打架 |
| 070 | 0.0 | 0.0 | 0 | 0 | 干净放行 |
| 080 | 0.0 | 0.0 | **51** | **58** | evidence 大面积错位 |
| **037（对照）** | **1.0** | **1.0** | **0** | **0** | **对照组修复 ✅ 零误拦** |

**核心机制指标**：
- wrapper 解析 110 次，resolve_errors 0（穿透 100% 稳定）
- verdicts：matched 66 / not_grounded 217 / evidence_mismatch 63
- **evidence_mismatch 从 V2 的 0 → 63**：harness 确实进入了业务参数层 ✅
- schema enum 拦截能力保持（单元验证：违反 inner enum 仍拦）
- 最大连续拒绝 58（task_080）——**validation loop 出现** ⚠️
- corrected_after_rejection = 0：被拒后 DA 未能产出通过重试 ⚠️

**7. 抓到多少真实参数错误**：schema 层 2 次（V2 的 wrapper 类型错已修）；
enum 层真实拦截存在（如 037 系列的 resolution_requested）；但 63 次
evidence_mismatch 绝大多数是 **evidence 错而非 proposed 错**（见下）。

**8-10**：修正 0；false rejection 经四轮修复后对照组归零，但 080 的 51 次
拒绝是"合法但案例错误"的 evidence（非 health rule 可枚举的形态——
KA 把文档参数名当值提取：`value="user_id"`/`"card_id"`）。

**11. success**：1/6 → 1/6（对照组不伤，顽固任务未救活）
**12. 效率**：LLM 249→371（+49%）、wall 1904→4500s（+136%）——
loop 成本沉重
**13. 是否值得更大评测**：**不值得（stubborn6/24-task 暂缓）**

## 最终诚实结论

**V2.1 的工程目标全部达成**：wrapper 穿透稳定（110/110）、inner 校验
生效（63 次参数级 mismatch）、对照组零误拦（037 恢复 1.0）、错误回传
修正链路保持、trace 五事件全链路。

**但研究结论是负的：证据侧不可靠是比执行侧更深的瓶颈。**
四轮迭代修复了所有可枚举的 evidence 病理（假枚举/叙述文本/伪值说明/
矛盾/格式占位符/字符串化布尔/过度 required），第五种形态（**KA 把文档
参数名本身当值**：value="user_id"）证明了根本问题：**LLM 生成的
grounded_values 无法被规则层穷举地清洗**——错误形态是开放集。

更本质：KB 文档记录的是**参数的合法域**（哪个枚举集合、什么格式），
而 case-specific 的正确值（用户的卡 ID、本次的 dispute reason）来自
**用户/环境上下文，不是 KB**——"evidence-grounded 校验"的前提在这些
参数上不成立。

## 建议下一步方向（供决策，未实施）

1. **Harness 保留但只做 schema 层**（enum/required/类型）——这部分纯
   确定性、零 false rejection、真实有用（037 修的就是这些）；
   evidence 层关闭（配置开关保留）
2. 若要 evidence 校验可行：需要 packet 质量门槛（KA 侧输出校验：
   value ≠ 参数名、布尔/数值类型检查）而非消费侧穷举清洗——
   但这开始接近"用复杂度对冲复杂度"
3. 顽固任务（080 等）的失败不在参数校验可达范围——回到 V1.3 报告
   的结论：它们的错误值来源是 DA 自身的推断，evidence 里根本没有
   对照材料（not_grounded 217 次佐证）

## 回退状态

默认 agent 仍为 `two_agent`（V1.2）。`two_agent_harness` 保留；
harness 代码/六层健康过滤/trace/指标全部保留（`69c348e`）。

## 复现

```bash
python run_eval.py --tasks configs/banking_paramfail6.json --agent two_agent_harness \
  --model openai/deepseek-v4-flash --retrieval-config bm25 --tag v214_param6 --seed 42
```
