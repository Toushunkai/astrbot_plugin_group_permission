"""v1.0.1 加固项的测试：自愈注册、自检、兜底闸门 L2 / L3。

背景（用户实测反馈）：群里一个非白名单成员 @ 机器人后，机器人仍然回复了，
而日志里能看到内置插件 `astrbot` 的 `on_message`（优先级 0）打了日志——
**说明主拦截器根本没有执行或没有拦下**。所以 v1.0.1 加了三件事：

1. 自检 + 自愈：handler 从注册表里消失时自动补注册，并在日志/插件页面报警；
2. L2 闸门：`OnLLMRequestEvent` 里取消未授权成员的 LLM 请求
   （不依赖「排在谁前面」）；
3. L3 闸门：`OnDecoratingResultEvent` 里拦下未授权成员的待发送内容。

跑法： python3 _tests/test_hardening.py
"""

from __future__ import annotations

import asyncio
import functools
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parents[2]))

import _fake_astrbot  # noqa: E402

_fake_astrbot.install()

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

from astrbot_plugin_group_permission.main import (  # noqa: E402
    GroupPermissionPlugin,
    PLUGIN_NAME,
)
from astrbot_plugin_group_permission.store import GroupStore  # noqa: E402

from _fake_astrbot import (  # noqa: E402
    HandlerMeta,
    core_registry,
    make_event,
    run_handler_chain,
)

MAX = sys.maxsize
GROUP = "123456"


def make_plugin(plugin_set=None, **overrides):
    cfg = {
        "enable": True,
        "default_group_enabled": True,
        "allow_owner_admin": True,
        "allow_global_admin": True,
        "mode": "command_only",
        "command_whitelist": ["help", "sid"],
        "whitelist": [],
        "reply_cooldown": 0,
        "auto_query_group_info": False,
    }
    cfg.update(overrides)
    config = AstrBotConfig(cfg)
    context = Context(
        config=AstrBotConfig(
            {"admins_id": [], "plugin_set": ["*"] if plugin_set is None else plugin_set}
        )
    )
    plugin = GroupPermissionPlugin(context, config)
    plugin.store = GroupStore(Path(tempfile.mkdtemp()) / "groups.json")
    return plugin


def run_gate(plugin, event):
    return asyncio.run(
        run_handler_chain(
            event,
            [
                HandlerMeta(
                    name="gate_group_message",
                    plugin=PLUGIN_NAME,
                    handler=plugin.gate_group_message,
                    priority=MAX,
                )
            ],
        )
    )


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


class TestPluginSetGuard(unittest.TestCase):
    """回归：plugin_set 不含本插件时，所有 handler 都会被跳过（用户实测踩到的坑）。"""

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


class TestPlatformKeys(unittest.TestCase):
    """平台维度（借鉴 astrbot_plugin_llmallowlist 的「按平台分别维护白名单」）。"""

    def test_same_group_id_on_two_platforms_is_isolated(self):
        plugin = make_plugin()
        qq = make_event(platform_name="aiocqhttp", user_id="10001", role="member", message="你好")
        tg = make_event(platform_name="telegram", user_id="10001", role="member", message="hi")

        self.assertEqual(plugin._group_key(qq), f"aiocqhttp:{GROUP}")
        self.assertEqual(plugin._group_key(tg), f"telegram:{GROUP}")

        plugin.store.set_group(f"aiocqhttp:{GROUP}", {"whitelist": ["10001"]})
        plugin.store.save()

        # QQ 群放行，Telegram 同群号不受影响
        self.assertTrue(run_gate(plugin, qq)["llm_called"])
        self.assertFalse(run_gate(plugin, tg)["llm_called"])

    def test_legacy_bare_group_id_still_works(self):
        """升级前写的裸群号配置必须还能被读到。"""
        plugin = make_plugin(mode="silent")
        plugin.store.set_group(GROUP, {"mode": "chat_only"})  # 旧格式
        event = make_event(user_id="10001", role="member", message="你好")
        settings = plugin._settings_for(plugin._group_key(event))
        self.assertEqual(settings.mode, "chat_only")
        self.assertEqual(settings.source, "group")

    def test_reset_clears_both_formats(self):
        plugin = make_plugin()
        plugin.store.set_group(GROUP, {"mode": "silent"})
        plugin.store.set_group(f"aiocqhttp:{GROUP}", {"mode": "reply"})
        plugin.store.reset_group(f"aiocqhttp:{GROUP}")
        self.assertEqual(plugin.store.groups(), {})


class TestReplyStyle(unittest.TestCase):
    """固定文案的发送方式：引用 → @ → 普通（借鉴参考实现的三级降级）。"""

    def test_quote_style_builds_reply_component(self):
        plugin = make_plugin(mode="reply", reply_style="quote", reply_cooldown=0)
        event = make_event(user_id="10001", role="member", message="你好")
        chain = plugin._reply_chain(event, "没有权限")
        names = [type(c).__name__ for c in chain]
        self.assertEqual(names[0], "Reply")
        self.assertIn("At", names)
        self.assertEqual(chain[-1].text, "没有权限")

    def test_at_style_has_no_reply_component(self):
        plugin = make_plugin(mode="reply", reply_style="at", reply_cooldown=0)
        event = make_event(user_id="10001", role="member", message="你好")
        names = [type(c).__name__ for c in plugin._reply_chain(event, "x")]
        self.assertNotIn("Reply", names)
        self.assertIn("At", names)

    def test_plain_style_sends_plain_result(self):
        plugin = make_plugin(
            mode="reply", reply_style="plain", reply_cooldown=0, reply="没有权限哦 {user}"
        )
        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertEqual(result["replies"], ["没有权限哦 测试用户"])

    def test_legacy_reply_with_at_maps_to_style(self):
        plugin = make_plugin(reply_with_at=False)
        self.assertEqual(plugin._global_cfg()["reply_style"], "plain")
        plugin2 = make_plugin(reply_with_at=True)
        self.assertEqual(plugin2._global_cfg()["reply_style"], "at")

    def test_send_fallback_when_quote_fails(self):
        """引用发送失败时要降级到 @，再失败降级到普通消息。"""
        plugin = make_plugin(reply_style="quote")
        event = make_event(user_id="10001", role="member", message="你好")

        attempts = []

        async def flaky_send(message):
            attempts.append([type(c).__name__ for c in message.chain])
            if len(attempts) < 3:
                raise RuntimeError("平台不支持这个组件")
            event.sent.append(message)

        event.send = flaky_send
        ok = asyncio.run(plugin._send_with_fallback(event, "提示"))
        self.assertTrue(ok)
        self.assertEqual(attempts[0][0], "Reply")
        self.assertEqual(attempts[1][0], "At")
        self.assertEqual(attempts[2][0], "Plain")


class TestMemberFetchFastPath(unittest.TestCase):
    """优先用 OneBot 的 get_group_member_list（一次调用），失败再退回 get_group()。"""

    class _Api:
        def __init__(self, members=None, boom=False):
            self.members = members or []
            self.boom = boom
            self.calls = []

        async def call_action(self, action, **kwargs):
            self.calls.append(action)
            if self.boom:
                raise RuntimeError("api 不支持")
            return self.members

    def test_uses_member_list_api(self):
        plugin = make_plugin(auto_query_group_info=True)
        api = self._Api(
            [
                {"user_id": 20001, "role": "owner"},
                {"user_id": 20002, "role": "admin"},
                {"user_id": 10001, "role": "member"},
            ]
        )
        event = make_event(user_id="20002", role=None, bot=type("B", (), {"api": api})())
        role, source = asyncio.run(plugin._resolve_group_role(event, "20002"))
        self.assertEqual(role, "admin")
        self.assertEqual(source, "platform_api")
        self.assertEqual(api.calls, ["get_group_member_list"])

    def test_falls_back_to_event_get_group(self):
        """OneBot 接口不可用时，退回跨平台的 event.get_group()。"""
        plugin = make_plugin(auto_query_group_info=True)
        api = self._Api(boom=True)
        event = make_event(
            user_id="20001", role=None, group_owner="20001", bot=type("B", (), {"api": api})()
        )
        data = asyncio.run(plugin._fetch_group_roles(event, GROUP, plugin._global_cfg()))
        self.assertEqual(data["owner"], "20001")
        self.assertEqual(api.calls, ["get_group_member_list"])

    def test_cheap_source_wins_over_api(self):
        """消息里已经带了群主信息时，不应该再去调接口。"""
        plugin = make_plugin(auto_query_group_info=True)
        api = self._Api(boom=True)
        event = make_event(
            user_id="20001", role=None, group_owner="20001", bot=type("B", (), {"api": api})()
        )
        role, source = asyncio.run(plugin._resolve_group_role(event, "20001"))
        self.assertEqual(role, "owner")
        self.assertEqual(source, "group_object")
        self.assertEqual(api.calls, [])


class TestForgetGroup(unittest.TestCase):
    """回归：页面/指令加进去的群配置必须能真正删掉（含「见过的群」记录）。"""

    def _plugin(self):
        return make_plugin()

    def test_forget_removes_override_and_seen(self):
        plugin = self._plugin()
        key = f"aiocqhttp:{GROUP}"
        plugin.store.set_group(key, {"enabled": True, "mode": "silent"})
        plugin.store.touch_group(key, "测试群")  # 机器人发过消息就会记下来
        self.assertEqual(len(plugin.store.known_groups()), 1)

        web = sys.modules["astrbot.api.web"]
        web.request.set_json({"key": key})
        res = asyncio.run(plugin.page_forget_group())["payload"]
        self.assertTrue(res["ok"])
        self.assertEqual(plugin.store.known_groups(), [], "这一行必须彻底消失")
        self.assertEqual(plugin.store.groups(), {})

    def test_forget_accepts_group_id_and_platform(self):
        plugin = self._plugin()
        key = f"telegram:{GROUP}"
        plugin.store.set_group(key, {"enabled": True})
        plugin.store.touch_group(key, "")
        web = sys.modules["astrbot.api.web"]
        web.request.set_json({"group_id": GROUP, "platform": "telegram"})
        asyncio.run(plugin.page_forget_group())
        self.assertEqual(plugin.store.known_groups(), [])

    def test_reset_keeps_row_but_can_be_forgotten(self):
        """「恢复全局」只清配置，行还在——这正是用户觉得「删不掉」的原因。"""
        plugin = self._plugin()
        key = f"aiocqhttp:{GROUP}"
        plugin.store.set_group(key, {"enabled": True})
        plugin.store.touch_group(key, "")
        web = sys.modules["astrbot.api.web"]

        web.request.set_json({"key": key})
        asyncio.run(plugin.page_reset_group())
        rows = plugin.store.known_groups()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["configured"])

        web.request.set_json({"key": key})
        asyncio.run(plugin.page_forget_group())
        self.assertEqual(plugin.store.known_groups(), [])

    def test_prune_only_removes_unconfigured(self):
        plugin = self._plugin()
        plugin.store.touch_group("aiocqhttp:111", "见过的")
        plugin.store.touch_group("aiocqhttp:222", "见过的")
        plugin.store.set_group("aiocqhttp:222", {"enabled": True})
        web = sys.modules["astrbot.api.web"]
        web.request.set_json({})
        res = asyncio.run(plugin.page_prune_groups())["payload"]
        self.assertEqual(res["removed"], 1)
        rows = plugin.store.known_groups()
        self.assertEqual([r["group_id"] for r in rows], ["222"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
