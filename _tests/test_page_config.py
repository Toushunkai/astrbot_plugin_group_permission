"""平台维度配置、固定文案发送方式、群成员信息获取与配置删除的测试。"""

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
from _fake_astrbot import core_registry, run_handler_chain, HandlerMeta  # noqa: F401

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
    """固定文案的发送方式：引用 → @ → 普通，发送失败逐级降级。"""

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
