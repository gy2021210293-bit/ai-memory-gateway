"""陈旧工具链请求侧自愈（repair_stale_tool_chains）纯函数测试。

规则（方案 v2 终稿）：
- 完全闭合链（每个 id 都有紧跟的匹配 tool 结果）→ 原样保留（含跨界闭合）
- 部分闭合（有匹配但未覆盖全部 id）→ 不碰，留给校验器 400
- 零结果 + 区起点在活跃块边界之前 → 剥 tool_calls（空内容删整条），跟随的无主 tool 一并删
- 零结果 + 区起点在活跃块内 → 不碰，留给校验器
- 无主 tool：陈旧区删；活跃尾内不碰
- 重复 id / 缺 id：闭合区内不碰（校验器 400）；陈旧零结果区内随链中和
- 幂等；纯函数（无 DB、无 IO、不修改输入）
"""

import unittest
from copy import deepcopy

from message_pipeline import (
    classify_request,
    reconcile_partition_block,
    repair_stale_tool_chains,
    validate_tool_sequence,
)


def assistant_call(*call_ids, content=""):
    message = {"role": "assistant", "content": content}
    if call_ids:
        message["tool_calls"] = [
            {"id": call_id, "type": "function"} for call_id in call_ids
        ]
    return message


def tool_result(call_id, content="result"):
    message = {"role": "tool", "tool_call_id": call_id, "content": content}
    return message


def user_msg(content="hi"):
    return {"role": "user", "content": content}


def system_msg(content="sys"):
    return {"role": "system", "content": content}


class RepairStaleToolChainsTests(unittest.TestCase):
    def _repair(self, messages, active_tail_len=0):
        return repair_stale_tool_chains(messages, active_tail_len)

    # ---------- 闭合链 ----------

    def test_closed_chain_is_untouched(self):
        messages = [user_msg(), assistant_call("c1"), tool_result("c1")]

        out, result = self._repair(messages)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)

    def test_cross_boundary_closure_is_untouched(self):
        # DB 里有 assistant(tool_calls)，结果正在本次请求的 delta 里回传（跨界闭合）
        database = [user_msg("run"), assistant_call("t1")]
        delta = [tool_result("t1")]
        messages = database + delta

        out, result = self._repair(messages, active_tail_len=len(delta))

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)
        self.assertTrue(validate_tool_sequence(out).valid)

    def test_multiple_closed_zones_untouched(self):
        messages = [
            user_msg(),
            assistant_call("c1"), tool_result("c1"),
            assistant_call("c2"), tool_result("c2"),
        ]

        out, result = self._repair(messages)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)

    # ---------- 零结果陈旧链（中断场景） ----------

    def test_interrupt_chain_with_empty_content_is_dropped(self):
        # 用户打断工具调用：assistant(tool_calls) 后直接跟新的 user
        messages = [user_msg("run"), assistant_call("t0"), user_msg("别管了")]

        out, result = self._repair(messages, active_tail_len=1)

        self.assertEqual(out, [user_msg("run"), user_msg("别管了")])
        self.assertEqual(result.dropped_assistants, 1)
        self.assertTrue(result.changed)
        self.assertTrue(validate_tool_sequence(out).valid)

    def test_interrupt_chain_with_text_is_stripped_not_dropped(self):
        messages = [
            user_msg("run"),
            assistant_call("t0", content="让我查一下"),
            user_msg("别管了"),
        ]

        out, result = self._repair(messages, active_tail_len=1)

        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["role"], "assistant")
        self.assertNotIn("tool_calls", out[1])
        self.assertEqual(out[1]["content"], "让我查一下")
        self.assertEqual(result.stripped_assistants, 1)
        self.assertTrue(result.changed)

    def test_stale_chain_repaired_even_without_trailing_user(self):
        # 纯函数层面：无活跃尾（active_tail_len=0）时整条历史都视为陈旧
        messages = [user_msg("run"), assistant_call("t0")]

        out, result = self._repair(messages)

        self.assertEqual(out, [user_msg("run")])
        self.assertEqual(result.dropped_assistants, 1)

    # ---------- 无主 tool ----------

    def test_stale_orphan_tool_is_dropped(self):
        messages = [user_msg("old"), tool_result("c1")]

        out, result = self._repair(messages)

        self.assertEqual(out, [user_msg("old")])
        self.assertEqual(result.dropped_orphan_tools, 1)
        self.assertTrue(result.changed)

    def test_orphan_tool_in_active_tail_is_kept(self):
        # 活跃尾内的无主 tool 不修（留给校验器 400，红线）
        messages = [user_msg("old"), tool_result("c1")]

        out, result = self._repair(messages, active_tail_len=1)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)
        self.assertFalse(validate_tool_sequence(out).valid)

    # ---------- 部分闭合 / 错配 / 重复：一律不碰 ----------

    def test_partial_closure_is_left_for_validator(self):
        # 并行 [c1,c2] 只回了 c1：部分闭合，绝不中和
        messages = [assistant_call("c1", "c2"), tool_result("c1")]

        out, result = self._repair(messages)

        self.assertEqual(out, messages)
        self.assertEqual(result.left_for_validator, 1)
        self.assertFalse(result.changed)
        self.assertFalse(validate_tool_sequence(out).valid)

    def test_mismatched_id_in_stale_region_is_repaired(self):
        messages = [user_msg(), assistant_call("c1"), tool_result("x")]

        out, result = self._repair(messages)

        self.assertEqual(out, [user_msg()])
        self.assertEqual(result.dropped_assistants, 1)
        self.assertEqual(result.dropped_orphan_tools, 1)

    def test_mismatched_id_in_active_tail_is_kept(self):
        messages = [assistant_call("c1"), tool_result("x")]

        out, result = self._repair(messages, active_tail_len=2)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)
        self.assertFalse(validate_tool_sequence(out).valid)

    def test_duplicate_result_in_closed_zone_is_kept(self):
        # 闭合区内重复 id：不修（校验器 duplicate_tool_result 400）
        messages = [assistant_call("c1"), tool_result("c1"), tool_result("c1")]

        out, result = self._repair(messages)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)
        self.assertFalse(validate_tool_sequence(out).valid)

    def test_missing_tool_call_id_stale_is_repaired(self):
        messages = [user_msg(), assistant_call("c1"), {"role": "tool", "content": "r"}]

        out, result = self._repair(messages)

        self.assertEqual(out, [user_msg()])
        self.assertTrue(result.changed)

    def test_missing_tool_call_id_active_is_kept(self):
        messages = [assistant_call("c1"), {"role": "tool", "content": "r"}]

        out, result = self._repair(messages, active_tail_len=2)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)
        self.assertFalse(validate_tool_sequence(out).valid)

    def test_missing_call_ids_on_assistant_stale_is_repaired(self):
        # assistant 的 tool_calls 全部缺 id：陈旧区随链中和
        messages = [
            user_msg(),
            {"role": "assistant", "content": "", "tool_calls": [{"type": "function"}]},
        ]

        out, result = self._repair(messages)

        self.assertEqual(out, [user_msg()])
        self.assertTrue(result.changed)

    # ---------- 边界 / 混合 ----------

    def test_stale_then_closed_zones(self):
        # 陈旧 t0 之后是完整闭合的 t1 链：纯函数正确中和 t0（v2 门控放宽的用例）
        messages = [
            assistant_call("t0"),
            user_msg("w"),
            assistant_call("t1"),
            tool_result("t1"),
        ]

        out, result = self._repair(messages, active_tail_len=2)

        self.assertEqual(
            out, [user_msg("w"), assistant_call("t1"), tool_result("t1")]
        )
        self.assertEqual(result.dropped_assistants, 1)
        self.assertTrue(validate_tool_sequence(out).valid)

    def test_zone_starting_exactly_at_boundary_is_active(self):
        messages = [user_msg(), assistant_call("c1")]

        out, result = self._repair(messages, active_tail_len=1)

        self.assertEqual(out, messages)
        self.assertFalse(result.changed)

    def test_system_messages_pass_through(self):
        messages = [system_msg(), user_msg(), assistant_call("t0"), user_msg()]

        out, result = self._repair(messages, active_tail_len=1)

        self.assertEqual(out[0], system_msg())
        self.assertEqual([m["role"] for m in out], ["system", "user", "user"])
        self.assertEqual(result.dropped_assistants, 1)

    def test_idempotent(self):
        messages = [
            user_msg(),
            assistant_call("t0"),
            user_msg("别管了"),
            assistant_call("t1"),
            tool_result("t1"),
        ]

        out1, result1 = self._repair(messages, active_tail_len=1)
        out2, result2 = self._repair(out1, active_tail_len=1)

        self.assertEqual(out1, out2)
        self.assertFalse(result2.changed)

    def test_input_is_not_mutated(self):
        messages = [
            user_msg(),
            assistant_call("t0"),
            user_msg("别管了"),
            assistant_call("t1"),
            tool_result("t1"),
        ]
        snapshot = deepcopy(messages)

        self._repair(messages, active_tail_len=1)

        self.assertEqual(messages, snapshot)

    # ---------- 门控组合（纯函数层验证 main.py 调用逻辑） ----------

    def test_partition_interrupt_flow_heals_history(self):
        # 分区模式中断场景：DB 有陈旧 t0，本次请求是普通 user 块 → 门控通过 → 修复
        database = [user_msg("run"), assistant_call("t0")]
        client = database + [user_msg("别管了")]
        reconciled = reconcile_partition_block(database, client)

        gate = bool(reconciled.provider_messages)
        self.assertTrue(gate)
        self.assertEqual(
            [m["role"] for m in reconciled.provider_messages], ["user"]
        )

        all_msgs = database + list(reconciled.provider_messages)
        out, result = repair_stale_tool_chains(
            all_msgs, len(reconciled.provider_messages)
        )

        self.assertEqual([m["role"] for m in out], ["user", "user"])
        self.assertTrue(result.changed)
        self.assertTrue(validate_tool_sequence(out).valid)

    def test_tool_result_flow_closed_zone_untouched(self):
        # 工具结果回传请求（跨界闭合）：门控通过，但正在闭合的链在组装列表中
        # 闭合 → zone 逻辑分毫不动 → 校验通过
        database = [user_msg("run"), assistant_call("t1")]
        client = database + [tool_result("t1")]
        reconciled = reconcile_partition_block(database, client)

        gate = bool(reconciled.provider_messages)
        self.assertTrue(gate)
        self.assertTrue(reconciled.is_tool_chain)

        all_msgs = database + list(reconciled.provider_messages)
        out, result = repair_stale_tool_chains(
            all_msgs, len(reconciled.provider_messages)
        )

        self.assertFalse(result.changed)
        self.assertTrue(validate_tool_sequence(out).valid)

    def test_tool_result_flow_repairs_accumulated_stale_history(self):
        # 回归：DB 里积累的旧悬挂链 t0 + 本次回传 t1 结果 —— 修复必须中和 t0，
        # 否则组装列表校验失败（此前的"一用工具就报错"1675）
        database = [
            user_msg("run"),
            assistant_call("t0"),      # 旧悬挂链（之前失败的工具调用遗留）
            user_msg("再试一次"),
            assistant_call("t1"),      # 当前链
        ]
        client = [
            user_msg("run"),
            user_msg("再试一次"),
            tool_result("t1"),         # 客户端丢弃 assistant 消息
        ]
        reconciled = reconcile_partition_block(database, client)

        self.assertEqual(reconciled.reason, "db_assistant_supplied")
        self.assertTrue(bool(reconciled.provider_messages))

        all_msgs = database + list(reconciled.provider_messages)
        self.assertFalse(validate_tool_sequence(all_msgs).valid)  # 修复前 1675

        out, result = repair_stale_tool_chains(
            all_msgs, len(reconciled.provider_messages)
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.dropped_assistants, 1)
        self.assertTrue(validate_tool_sequence(out).valid)
        # 正在闭合的 t1 链必须原样保留，旧悬挂链 t0 被中和
        self.assertEqual(
            [m["role"] for m in out],
            ["user", "user", "assistant", "tool"],
        )

    def test_partial_parallel_closure_still_rejected(self):
        # 并行 [t1a,t1b] 只回 t1a：部分闭合 → 对齐拒绝（1498 路径），
        # 门控不通过、修复不运行，红线不变
        database = [user_msg("run"), assistant_call("t1a", "t1b")]
        client = [
            user_msg("run"),
            assistant_call("t1a", "t1b"),
            tool_result("t1a"),
        ]
        reconciled = reconcile_partition_block(database, client)

        self.assertNotEqual(reconciled.reason, "aligned")
        self.assertFalse(reconciled.provider_messages)
        self.assertFalse(bool(reconciled.provider_messages))

    def test_empty_current_block_gate_blocks_repair(self):
        # 当前块无法识别（无主 tool 尾）：门控不通过 → 修复不会被调用
        # → 原消息原样进入校验器，维持 400（回归：行为与修复前逐字节一致）
        classified = classify_request([user_msg("old"), tool_result("x")])

        gate = bool(classified.current_block)
        self.assertFalse(gate)
        self.assertEqual(classified.current_block, ())

        self.assertFalse(
            validate_tool_sequence(list(classified.ordinary_messages)).valid
        )


if __name__ == "__main__":
    unittest.main()
