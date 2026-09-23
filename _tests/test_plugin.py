"""拦截器集成测试：用 AstrBot 桩把插件真的跑起来。

验证的重点（都是需求里明确要求的）：
1. 非群主/非管理/不在白名单的群友，@ 和唤醒词都不会唤起 AI；
2. 群主 / 群管理（有单独开关）/ 白名单成员 / AstrBot 全局管理员可以正常对话；
3. 三种（四种）处理模式各自的行为，包括「先 yield 回复再 stop」的文案真的发得出去；
4. 按群配置与全局配置的合并。

跑法： python3 _tests/test_plugin.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))  # _tests（为了 import _fake_astrbot）
sys.path.insert(0, str(_HERE.parents[2]))  # 工程根目录（为了 import 插件包）

import _fake_astrbot  # noqa: E402

_fake_astrbot.install()

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

from astrbot_plugin_group_permission.main import GroupPermissionPlugin  # noqa: E402
from astrbot_plugin_group_permission.store import GroupStore  # noqa: E402

from _fake_astrbot import (  # noqa: E402
    HandlerMeta,
    make_event,
    run_handler_chain,
)

MAX = sys.maxsize
GROUP = "123456"


def make_plugin(**overrides):
    cfg = {
        "enable": True,
        "default_group_enabled": True,
        "allow_owner_admin": True,
        "allow_global_admin": True,
        "mode": "command_only",
        "command_whitelist": ["help", "sid"],
        "whitelist": [],
        "reply_cooldown": 60,
        "auto_query_group_info": False,
    }
    cfg.update(overrides)
    config = AstrBotConfig(cfg)
    context = Context(config=AstrBotConfig({"admins_id": []}))
    plugin = GroupPermissionPlugin(context, config)
    # 数据文件放到临时目录，别碰工作区
    plugin.store = GroupStore(Path(tempfile.mkdtemp()) / "groups.json")
    return plugin


def run_gate(plugin, event, priority=MAX, others=()):
    handlers = [
        HandlerMeta(
            name="gate_group_message",
            plugin="astrbot_plugin_group_permission",
            handler=plugin.gate_group_message,
            priority=priority,
        ),
        *others,
    ]
    return asyncio.run(run_handler_chain(event, handlers))


async def collect(agen):
    return [item async for item in agen]


def texts(results):
    out = []
    for item in results:
        if hasattr(item, "get_plain_text"):
            out.append(item.get_plain_text())
        elif hasattr(item, "chain"):
            out.append("".join(getattr(c, "text", "") for c in item.chain))
    return out


class TestGateInterception(unittest.TestCase):
    def test_plain_member_is_blocked_and_ai_disabled(self):
        plugin = make_plugin()
        event = make_event(user_id="10001", role="member", message="你好呀")
        result = run_gate(plugin, event)
        self.assertEqual(result["executed"], ["astrbot_plugin_group_permission"])
        self.assertTrue(result["stopped"])
        self.assertFalse(result["llm_called"], "未授权成员的普通消息不应该触发 AI")
        self.assertEqual(result["replies"], [])

    def test_plain_member_wake_word_and_at_also_disabled(self):
        """需求重点：唤醒词 / @ 也不能唤起 AI。"""
        plugin = make_plugin()
        for message in ("@机器人", "你好", "/", ""):
            event = make_event(user_id="10001", role="member", message=message)
            result = run_gate(plugin, event)
            self.assertFalse(result["llm_called"], f"消息 {message!r} 竟然触发了 AI")
            self.assertTrue(result["stopped"], f"消息 {message!r} 没有被拦下")

    def test_builtin_empty_mention_handler_never_runs(self):
        """内置插件的 handle_empty_mention（priority=maxsize-1）必须排在我们后面。"""
        plugin = make_plugin()

        async def builtin_empty_mention(event):
            event._has_send_oper = True
            yield event.plain_result("想要问什么呢？😄")

        event = make_event(user_id="10001", role="member", message="@机器人")
        result = run_gate(
            plugin,
            event,
            others=[
                HandlerMeta(
                    name="handle_empty_mention",
                    plugin="astrbot",  # 内置插件
                    handler=builtin_empty_mention,
                    priority=MAX - 1,
                )
            ],
        )
        self.assertEqual(result["executed"], ["astrbot_plugin_group_permission"])
        self.assertEqual(result["replies"], [], "内置插件的空 @ 回复不应该发出去")

    def test_whitelisted_command_still_works(self):
        plugin = make_plugin()

        async def help_command(event):
            yield event.plain_result("这是帮助")

        event = make_event(user_id="10001", role="member", message="/help")
        result = run_gate(
            plugin,
            event,
            others=[HandlerMeta(name="help", plugin="builtin_commands", handler=help_command)],
        )
        self.assertEqual(result["replies"], ["这是帮助"])
        self.assertFalse(result["stopped"], "命中命令白名单的命令应该被放行")
        self.assertFalse(result["llm_called"], "未授权成员即使跑命令也不能触发默认 AI")

    def test_command_whitelist_with_wildcard(self):
        plugin = make_plugin(command_whitelist=["*"])
        event = make_event(user_id="10001", role="member", message="/随便什么")
        result = run_gate(plugin, event)
        self.assertFalse(result["stopped"])

    def test_owner_allowed_and_ai_runs(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="你好")
        result = run_gate(plugin, event)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["llm_called"], "群主应该能正常和 AI 对话")

    def test_group_admin_allowed_and_switch_off_blocks(self):
        plugin = make_plugin()
        event = make_event(user_id="20002", role="admin", message="你好")
        self.assertTrue(run_gate(plugin, event)["llm_called"])

        plugin2 = make_plugin(allow_owner_admin=False)
        event2 = make_event(user_id="20002", role="admin", message="你好")
        result2 = run_gate(plugin2, event2)
        self.assertFalse(result2["llm_called"])
        self.assertTrue(result2["stopped"])

    def test_whitelist_member_allowed(self):
        plugin = make_plugin(whitelist=["10001"])
        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"])

    def test_global_admin_allowed_even_with_owner_switch_off(self):
        plugin = make_plugin(allow_owner_admin=False)
        event = make_event(user_id="1", role="member", message="你好")
        event.role = "admin"  # WakingCheckStage 对 admins_id 里的用户会这样标记
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"])

    def test_master_switch_off_allows_everyone(self):
        plugin = make_plugin(enable=False)
        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"])

    def test_per_group_disable_allows_everyone(self):
        plugin = make_plugin()
        plugin.store.set_group(GROUP, {"enabled": False})
        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"])
        self.assertFalse(result["stopped"])

    def test_per_group_whitelist_override(self):
        plugin = make_plugin(whitelist=["1"])
        plugin.store.set_group(GROUP, {"whitelist": ["10001"]})
        allowed = make_event(user_id="10001", role="member", message="你好")
        self.assertTrue(run_gate(plugin, allowed)["llm_called"])
        # 全局白名单里的人在“覆盖了白名单”的群里反而不在白名单内
        blocked = make_event(user_id="1", role="member", message="你好")
        self.assertFalse(run_gate(plugin, blocked)["llm_called"])


class TestModes(unittest.TestCase):
    def test_reply_mode_really_sends_the_text(self):
        """「先 yield 回复、再 stop_event」的顺序必须真的能把文案发出去。"""
        plugin = make_plugin(mode="reply", reply_cooldown=0)
        event = make_event(user_id="10001", nickname="小明", role="member", message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["stopped"])
        self.assertEqual(len(result["replies"]), 1)
        self.assertIn("小明", result["replies"][0])
        self.assertFalse(result["llm_called"])

    def test_reply_cooldown_silences_repeats(self):
        plugin = make_plugin(mode="reply", reply_cooldown=60)
        first = run_gate(plugin, make_event(user_id="10001", role="member", message="你好"))
        second = run_gate(plugin, make_event(user_id="10001", role="member", message="又来"))
        self.assertEqual(len(first["replies"]), 1)
        self.assertEqual(second["replies"], [], "冷却期内不应该重复发提示")
        self.assertTrue(second["stopped"], "冷却期内仍然要拦截")

    def test_silent_mode_sends_nothing(self):
        plugin = make_plugin(mode="silent")
        result = run_gate(plugin, make_event(user_id="10001", role="member", message="你好"))
        self.assertEqual(result["replies"], [])
        self.assertTrue(result["stopped"])

    def test_chat_only_mode_only_blocks_ai(self):
        plugin = make_plugin(mode="chat_only")

        async def other_plugin(event):
            yield event.plain_result("其它插件照常工作")

        event = make_event(user_id="10001", role="member", message="你好")
        result = run_gate(
            plugin,
            event,
            others=[HandlerMeta(name="other", plugin="签到插件", handler=other_plugin)],
        )
        self.assertFalse(result["stopped"], "chat_only 模式不应该中断事件")
        self.assertFalse(result["llm_called"], "chat_only 模式必须禁止默认 AI")
        self.assertEqual(result["replies"], ["其它插件照常工作"])

    def test_chat_only_mode_does_not_reply(self):
        plugin = make_plugin(mode="chat_only")
        result = run_gate(plugin, make_event(user_id="10001", role="member", message="你好"))
        self.assertEqual(result["replies"], [])


class TestUnknownRole(unittest.TestCase):
    def test_unknown_role_blocked_by_default(self):
        plugin = make_plugin()  # auto_query_group_info=False → 角色识别不出来
        event = make_event(user_id="10001", role=None, message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["llm_called"])

    def test_unknown_role_can_be_allowed(self):
        plugin = make_plugin(unknown_role_action="allow")
        event = make_event(user_id="10001", role=None, message="你好")
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"])

    def test_role_from_group_object_is_used_when_raw_missing(self):
        plugin = make_plugin()
        event = make_event(
            user_id="10001", role=None, group_owner="10001", group_admins=["2"], message="你好"
        )
        result = run_gate(plugin, event)
        self.assertTrue(result["llm_called"], "群对象里带了群主信息时应该识别为群主")


class TestCommands(unittest.TestCase):
    def test_member_cannot_edit_whitelist(self):
        plugin = make_plugin()
        event = make_event(user_id="10001", role="member", message="gperm add 10002")
        out = texts(asyncio.run(collect(plugin.gperm_add(event))))
        self.assertTrue(any("只有群主" in t for t in out), out)
        self.assertEqual(plugin.store.get_group(GROUP), {})

    def test_owner_can_edit_whitelist(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="gperm add 10002")
        out = texts(asyncio.run(collect(plugin.gperm_add(event))))
        self.assertTrue(any("白名单已添加" in t for t in out), out)
        self.assertEqual(plugin.store.get_group(GROUP).get("whitelist"), ["10002"])

    def test_owner_can_toggle_and_set_mode(self):
        plugin = make_plugin()
        owner = dict(user_id="20001", role="owner")
        asyncio.run(collect(plugin.gperm_off(make_event(**owner, message="gperm off"))))
        self.assertFalse(plugin.store.get_group(GROUP)["enabled"])
        asyncio.run(collect(plugin.gperm_mode(make_event(**owner, message="gperm mode silent"))))
        self.assertEqual(plugin.store.get_group(GROUP)["mode"], "silent")
        asyncio.run(collect(plugin.gperm_reset(make_event(**owner, message="gperm reset"))))
        self.assertEqual(plugin.store.get_group(GROUP), {})

    def test_command_whitelist_edit(self):
        plugin = make_plugin()
        event = make_event(user_id="20001", role="owner", message="gperm cmd add 签到")
        asyncio.run(collect(plugin.gperm_cmd(event)))
        self.assertEqual(plugin.store.get_group(GROUP)["command_whitelist"], ["help", "sid", "签到"])

    def test_at_self_is_not_added_to_whitelist(self):
        """@机器人 是唤醒用的，不能把自己加进白名单。"""
        plugin = make_plugin()
        At = sys.modules["astrbot.api.message_components"].At
        event = make_event(user_id="20001", role="owner", message="gperm add")
        event.message_obj.message = [At(qq="9999"), At(qq="10002")]
        asyncio.run(collect(plugin.gperm_add(event)))
        self.assertEqual(plugin.store.get_group(GROUP)["whitelist"], ["10002"])


class TestPageApis(unittest.TestCase):
    def test_overview_and_simulate(self):
        plugin = make_plugin(mode="command_only")
        web = sys.modules["astrbot.api.web"]

        overview = asyncio.run(plugin.page_overview())["payload"]
        self.assertTrue(overview["ok"])
        self.assertEqual(overview["global"]["mode"], "command_only")
        self.assertEqual(len(overview["modes"]), 4)

        web.request.set_json(
            {
                "group_id": GROUP,
                "user_id": "10001",
                "role": "member",
                "message": "你好",
            }
        )
        sim = asyncio.run(plugin.page_simulate())["payload"]
        self.assertEqual(sim["action"], "block_silent")
        self.assertTrue(sim["blocks_llm"])
        self.assertTrue(sim["halts_event"])

        web.request.set_json(
            {"group_id": GROUP, "user_id": "20001", "role": "owner", "message": "你好"}
        )
        sim2 = asyncio.run(plugin.page_simulate())["payload"]
        self.assertEqual(sim2["action"], "allow")

    def test_save_group_and_global(self):
        plugin = make_plugin()
        web = sys.modules["astrbot.api.web"]
        web.request.set_json(
            {
                "group_id": GROUP,
                "note": "主群",
                "values": {
                    "enabled": None,
                    "mode": "silent",
                    "whitelist": ["1", "2"],
                    "command_whitelist": None,
                },
            }
        )
        res = asyncio.run(plugin.page_save_group())["payload"]
        self.assertTrue(res["ok"])
        override = plugin.store.get_group(GROUP)
        self.assertEqual(override["mode"], "silent")
        self.assertEqual(override["whitelist"], ["1", "2"])
        self.assertNotIn("enabled", override)  # None = 跟随全局，不写入
        self.assertEqual(override["note"], "主群")

        web.request.set_json({"mode": "reply", "whitelist": "7,8", "priority": MAX})
        res2 = asyncio.run(plugin.page_save_global())["payload"]
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["global"]["mode"], "reply")
        self.assertEqual(res2["global"]["whitelist"], ["7", "8"])
        self.assertEqual(plugin.config.saved, 1)

    def test_page_apis_registered(self):
        plugin = make_plugin()
        routes = [r[0] for r in plugin.context.web_apis]
        self.assertIn("/astrbot_plugin_group_permission/page/overview", routes)
        self.assertIn("/astrbot_plugin_group_permission/page/simulate", routes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
