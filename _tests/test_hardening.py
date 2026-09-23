"""自检 / 自愈、plugin_set 门禁、同优先级顺序，以及两道兜底闸门（L2 / L3）的测试。"""

from __future__ import annotations

import asyncio
import functools
import sys
import unittest

from _harness import (  # noqa: F401
    GROUP,
    GROUP_KEY,
    MAX,
    PLUGIN_NAME,
    collect,
    make_event,
    make_plugin,
    run_gate,
    texts,
    web_module,
)
import _fake_astrbot  # noqa: F401
from _fake_astrbot import HandlerMeta, core_registry, run_handler_chain  # noqa: F401

class TestSelfCheck(unittest.TestCase):
    def test_handler_registered_first(self):
        plugin = make_plugin()
        md = core_registry().get_handler_by_full_name(plugin._handler_full_name())
        self.assertIsNotNone(md, "拦截器应该注册在 star_handlers_registry 里")
        self.assertEqual(md.extras_configs["priority"], MAX)
        self.assertTrue(md.enabled)
        self.assertEqual(plugin._handler_status["position"], 0, "拦截器必须是第一个")
        self.assertTrue(plugin._handler_status["registered"])

    def test_status_survives_low_priority_config(self):
        """把优先级调低、并且有别人排更前面时，自检要如实报告。"""
        plugin = make_plugin(priority=1)
        # 模拟一个优先级更高的其它插件 handler（内置插件就是 maxsize-1 这一档）
        other = _fake_astrbot.core_registry().get_handler_by_full_name(
            plugin._handler_full_name()
        ).__class__(
            event_type=_fake_astrbot.core_registry().get_handler_by_full_name(
                plugin._handler_full_name()
            ).event_type,
            handler_full_name="data.plugins.other.main_handle",
            handler_name="handle",
            handler_module_path="data.plugins.other.main",
            handler=lambda event: None,
            event_filters=[],
            extras_configs={"priority": 5},
        )
        core_registry().append(other)
        try:
            status = plugin._ensure_handler()
            self.assertEqual(status["priority"], 1)
            self.assertGreater(status["position"], 0)
            self.assertIn("handle", status["ahead"])
        finally:
            core_registry().remove(other)

    def test_self_heal_when_registry_cleared(self):
        """模拟热重载把注册表清空：插件实例化时必须把自己的 handler 补回去。"""
        plugin = make_plugin()
        core_registry().clear()
        self.assertIsNone(core_registry().get_handler_by_full_name(plugin._handler_full_name()))

        healed = make_plugin()
        self.assertTrue(healed._handler_status["self_healed"], "应该报告「已自动补注册」")
        md = core_registry().get_handler_by_full_name(healed._handler_full_name())
        self.assertIsNotNone(md)
        self.assertEqual(md.extras_configs["priority"], MAX)
        self.assertIsInstance(md.handler, functools.partial)
        self.assertEqual(md.handler.args, (healed,), "补注册时必须绑定到当前实例")
        self.assertTrue(md.event_filters, "补注册时必须带 EventMessageTypeFilter")

        # 补注册出来的 handler 必须真的能用
        event = make_event(user_id="10001", role="member", message="你好")
        result = asyncio.run(
            run_handler_chain(
                event,
                [
                    HandlerMeta(
                        name="gate_group_message",
                        plugin=PLUGIN_NAME,
                        handler=md.handler,
                        priority=md.extras_configs["priority"],
                    )
                ],
            )
        )
        self.assertTrue(result["stopped"])
        self.assertFalse(result["llm_called"])


class TestPluginSetGuard(unittest.TestCase):
    """回归：plugin_set 不含本插件时，所有 handler 都会被跳过。"""

    def test_detects_missing_plugin_set(self):
        plugin = make_plugin(plugin_set=["astrbot", "builtin_commands"])
        status = plugin._plugin_set_status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["fixable"])
        self.assertIn(PLUGIN_NAME, status["error"])

    def test_ok_when_wildcard_or_included(self):
        for value in (["*"], [PLUGIN_NAME], None):
            plugin = make_plugin(plugin_set=value)
            self.assertTrue(plugin._plugin_set_status()["ok"], value)

    def test_one_click_fix_appends_without_touching_others(self):
        plugin = make_plugin(plugin_set=["astrbot", "builtin_commands"])
        web = sys.modules["astrbot.api.web"]
        web.request.set_json({})
        res = asyncio.run(plugin.page_fix_plugin_set())["payload"]
        self.assertTrue(res["ok"])
        self.assertEqual(
            plugin.context.get_config()["plugin_set"],
            ["astrbot", "builtin_commands", PLUGIN_NAME],
        )
        self.assertTrue(plugin._plugin_set_status()["ok"])
        self.assertEqual(plugin.context.get_config().saved, 1, "必须落盘")

    def test_fix_is_idempotent(self):
        plugin = make_plugin(plugin_set=[PLUGIN_NAME])
        web = sys.modules["astrbot.api.web"]
        web.request.set_json({})
        res = asyncio.run(plugin.page_fix_plugin_set())["payload"]
        self.assertEqual(plugin.context.get_config()["plugin_set"], [PLUGIN_NAME])
        self.assertIn("本来就包含", res["message"])


class TestTieBreakOrdering(unittest.TestCase):
    """回归：同优先级是稳定排序，插件不能因为自检而把自己挪到同级最后面。

    用户日志里出现过
    「拦截器前面还有 1 个 handler：handle_session_control_agent」——
    就是无条件 remove+append 造成的，本该排在它前面。
    """

    def test_no_reorder_when_priority_unchanged(self):
        plugin = make_plugin()  # 装饰器已把我们的 handler 放进注册表（用户插件先加载）
        md = core_registry().get_handler_by_full_name(plugin._handler_full_name())
        builtin = md.__class__(
            event_type=md.event_type,
            handler_full_name="astrbot.builtin_stars.astrbot.main_handle_session_control_agent",
            handler_name="handle_session_control_agent",
            handler_module_path="astrbot.builtin_stars.astrbot.main",
            handler=lambda event: None,
            event_filters=[],
            extras_configs={"priority": MAX},  # 内置插件同样是 maxsize
        )
        core_registry().append(builtin)  # 内置插件后加载 → 排在我们后面
        try:
            status = plugin._ensure_handler()
            self.assertEqual(status["position"], 0, "自检不应该把自己排到内置 handler 后面")
            self.assertEqual(
                [h.handler_name for h in core_registry()][:2],
                ["gate_group_message", "handle_session_control_agent"],
            )
        finally:
            core_registry().remove(builtin)

    def test_priority_change_still_reorders(self):
        plugin = make_plugin(priority=3)
        status = plugin._ensure_handler()
        self.assertEqual(status["priority"], 3)
        md = core_registry().get_handler_by_full_name(plugin._handler_full_name())
        self.assertEqual(md.extras_configs["priority"], 3)
        self.assertTrue(status["registered"])


class TestLlmGateL2(unittest.TestCase):
    def test_marks_then_blocks_llm(self):
        plugin = make_plugin()
        event = make_event(user_id="10001", role="member", message="证明一下黎曼猜想")
        run_gate(plugin, event)
        self.assertTrue(event.get_extra("_gpm_unauthorized"))

        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertTrue(event.is_stopped(), "未授权成员的 LLM 请求必须被取消")

    def test_allows_authorized_user(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="你好")
        run_gate(plugin, event)
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertFalse(event.is_stopped(), "群主的 LLM 请求不应该被取消")

    def test_allows_command_whitelist(self):
        plugin = make_plugin()
        event = make_event(user_id="10001", role="member", message="/help")
        run_gate(plugin, event)
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertFalse(event.is_stopped(), "白名单命令自己发起的 LLM 请求要放行")

    def test_fallback_when_interceptor_never_ran(self):
        """主拦截器没跑（没有标记）时，L2 也要能自己判定并拦下。"""
        plugin = make_plugin()
        event = make_event(user_id="10001", role="member", message="证明一下黎曼猜想")
        self.assertIsNone(event.get_extra("_gpm_action"))
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertTrue(event.is_stopped(), "兜底判定必须拦住未授权成员")

    def test_fallback_allows_owner(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="你好")
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertFalse(event.is_stopped())

    def test_fallback_skips_cron_events(self):
        """定时任务的 LLM 请求绝对不能被误伤。"""
        plugin = make_plugin()
        event = make_event(
            user_id=GROUP, role=None, message="早报内容", platform_name="cron"
        )
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertFalse(event.is_stopped(), "cron 事件必须放行")

    def test_fallback_skips_bot_self_and_session_sender(self):
        """sender 等于 self_id 或等于群号（定时任务的形态）时放行。"""
        plugin = make_plugin()
        for uid in ("9999", GROUP):
            event = make_event(user_id=uid, role=None, message="推送")
            asyncio.run(plugin.gate_llm_request(event, req=None))
            self.assertFalse(event.is_stopped(), f"sender={uid} 不应该被拦")

    def test_fallback_skips_disabled_group(self):
        plugin = make_plugin()
        plugin.store.set_group(GROUP, {"enabled": False})
        event = make_event(user_id="10001", role="member", message="你好")
        asyncio.run(plugin.gate_llm_request(event, req=None))
        self.assertFalse(event.is_stopped())


class TestOutgoingGateL3(unittest.TestCase):
    def test_clears_reply_for_blocked_sender(self):
        plugin = make_plugin(mode="silent")
        event = make_event(user_id="10001", role="member", message="你好")
        run_gate(plugin, event)

        async def late_plugin(event):
            return
            yield  # pragma: no cover - 让它成为异步生成器

        # 模拟「事件已经被拦，但某个插件仍然往事件里塞了结果」
        event.set_result(event.plain_result("偷偷发出去的内容"))
        asyncio.run(plugin.gate_outgoing(event))
        self.assertIsNone(event.get_result(), "未授权成员的回复必须被清掉")

    def test_lets_our_own_fallback_through(self):
        plugin = make_plugin(mode="reply")
        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertEqual(len(result["replies"]), 1)
        self.assertTrue(event.get_extra("_gpm_fallback"))
        event.set_result(event.plain_result("权限提示"))
        asyncio.run(plugin.gate_outgoing(event))
        self.assertIsNotNone(event.get_result(), "我们自己发的权限提示不能拦")

    def test_allows_authorized_and_commands(self):
        plugin = make_plugin()
        allowed = make_event(user_id="20001", role="owner", message="你好")
        run_gate(plugin, allowed)
        allowed.set_result(allowed.plain_result("正常回复"))
        asyncio.run(plugin.gate_outgoing(allowed))
        self.assertIsNotNone(allowed.get_result())

        cmd = make_event(user_id="10001", role="member", message="/help")
        run_gate(plugin, cmd)
        cmd.set_result(cmd.plain_result("帮助内容"))
        asyncio.run(plugin.gate_outgoing(cmd))
        self.assertIsNotNone(cmd.get_result(), "白名单命令的输出要放行")

    def test_chat_only_mode_only_clears_model_results(self):
        plugin = make_plugin(mode="chat_only")
        event = make_event(user_id="10001", role="member", message="你好")
        run_gate(plugin, event)

        other = event.plain_result("其它插件的普通消息")
        event.set_result(other)
        asyncio.run(plugin.gate_outgoing(event))
        self.assertIsNotNone(event.get_result(), "chat_only 不该挡其它插件")

        model = event.plain_result("AI 的回复")
        model.result_content_type = "llm_result"
        event.set_result(model)
        asyncio.run(plugin.gate_outgoing(event))
        self.assertIsNone(event.get_result(), "chat_only 必须挡下 AI 回复")


class TestDebugCommand(unittest.TestCase):
    def test_debug_reports_status(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="gperm debug")

        async def collect(agen):
            return [item async for item in agen]

        out = asyncio.run(collect(plugin.gperm_debug(event)))
        text = "".join(getattr(item, "get_plain_text", lambda: "")() for item in out)
        self.assertIn("拦截器已注册：是", text)
        self.assertIn("消息分发顺序：第 1/", text)
        self.assertIn("plugin_set：全部启用", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
