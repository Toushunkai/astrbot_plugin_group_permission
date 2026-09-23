"""gate.py 纯逻辑单测（不需要 AstrBot）。

跑法： python3 _tests/test_gate.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 插件目录

from gate import (  # noqa: E402
    ACTION_ALLOW,
    ACTION_ALLOW_COMMAND,
    ACTION_BLOCK_LLM_ONLY,
    ACTION_BLOCK_REPLY,
    ACTION_BLOCK_SILENT,
    MODE_CHAT_ONLY,
    MODE_COMMAND_ONLY,
    MODE_REPLY,
    MODE_SILENT,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    as_str_list,
    decide,
    match_command,
    merge_settings,
    normalize_command_text,
    normalize_group_role,
    normalize_user_id,
    render_reply,
    role_from_group_object,
    role_from_raw_message,
)


def settings(**kwargs):
    base = {
        "enabled": True,
        "allow_owner_admin": True,
        "allow_global_admin": True,
        "mode": MODE_COMMAND_ONLY,
        "reply_template": "没有权限哦 {user}",
        "whitelist": [],
        "command_whitelist": ["help", "sid"],
    }
    base.update(kwargs)
    return merge_settings(base, None)


class TestNormalize(unittest.TestCase):
    def test_user_id(self):
        self.assertEqual(normalize_user_id("qq:12345"), "12345")
        self.assertEqual(normalize_user_id(" @12345 "), "12345")
        self.assertEqual(normalize_user_id(12345), "12345")
        self.assertEqual(normalize_user_id(None), "")

    def test_group_role(self):
        self.assertEqual(normalize_group_role("owner"), ROLE_OWNER)
        self.assertEqual(normalize_group_role("群主"), ROLE_OWNER)
        self.assertEqual(normalize_group_role("ADMIN"), ROLE_ADMIN)
        self.assertEqual(normalize_group_role("member"), ROLE_MEMBER)
        self.assertIsNone(normalize_group_role("whatever"))
        self.assertIsNone(normalize_group_role(None))

    def test_role_from_raw_message_dict_and_object(self):
        raw = {"sender": {"user_id": "1", "role": "owner"}}
        self.assertEqual(role_from_raw_message(raw), ROLE_OWNER)

        class Sender:
            role = "admin"

        class Raw:
            sender = Sender()

        self.assertEqual(role_from_raw_message(Raw()), ROLE_ADMIN)
        self.assertIsNone(role_from_raw_message(None))


class TestGroupObjectRole(unittest.TestCase):
    class Group:
        def __init__(self, owner=None, admins=None):
            self.group_owner = owner
            self.group_admins = admins

    def test_owner_and_admin(self):
        g = self.Group(owner="1", admins=["2", "3"])
        self.assertEqual(role_from_group_object(g, "1"), ROLE_OWNER)
        self.assertEqual(role_from_group_object(g, "2"), ROLE_ADMIN)
        self.assertIsNone(role_from_group_object(g, "9"))
        self.assertIsNone(role_from_group_object(None, "1"))


class TestCommandMatch(unittest.TestCase):
    def test_exact_and_args(self):
        self.assertEqual(match_command("/help", ["help"]), "help")
        self.assertEqual(match_command("help 参数", ["help"]), "help")
        self.assertEqual(match_command("／help", ["help"]), "help")
        self.assertEqual(match_command("help\u30001", ["help"]), "help")
        self.assertIsNone(match_command("helper", ["help"]))
        self.assertIsNone(match_command("", ["help"]))

    def test_wildcard_and_case(self):
        self.assertEqual(match_command("任意命令 x", ["*"]), "*")
        self.assertEqual(match_command("HELP", ["help"]), "help")

    def test_normalize_command_text(self):
        self.assertEqual(normalize_command_text("/gperm add 1"), "gperm add 1")
        self.assertEqual(normalize_command_text("##x"), "x")


class TestMergeSettings(unittest.TestCase):
    def test_global_only(self):
        s = merge_settings({"enabled": True, "mode": MODE_SILENT}, None)
        self.assertTrue(s.enabled)
        self.assertEqual(s.mode, MODE_SILENT)
        self.assertEqual(s.source, "global")

    def test_override_wins_and_none_follows_global(self):
        s = merge_settings(
            {"enabled": True, "mode": MODE_SILENT, "whitelist": ["1"]},
            {"enabled": False, "mode": None, "whitelist": ["2", "3"]},
        )
        self.assertFalse(s.enabled)  # 覆盖生效
        self.assertEqual(s.mode, MODE_SILENT)  # None = 跟随全局
        self.assertEqual(s.whitelist, ["2", "3"])
        self.assertEqual(s.source, "group")

    def test_bad_mode_falls_back(self):
        self.assertEqual(merge_settings({"mode": "不存在的模式"}, None).mode, MODE_COMMAND_ONLY)

    def test_as_str_list(self):
        self.assertEqual(as_str_list("1,2\n3"), ["1", "2", "3"])
        self.assertEqual(as_str_list(["1", "1", " 2 "]), ["1", "2"])
        self.assertEqual(as_str_list(None), [])


class TestDecide(unittest.TestCase):
    def _decide(self, **kwargs):
        params = {
            "is_group": True,
            "user_id": "10001",
            "group_role": ROLE_MEMBER,
            "is_global_admin": False,
            "message_text": "你好",
        }
        s = kwargs.pop("settings", settings())
        params.update(kwargs)
        return decide(settings=s, **params)

    def test_private_chat_never_touched(self):
        d = self._decide(is_group=False, group_role=None)
        self.assertEqual(d.action, ACTION_ALLOW)

    def test_disabled_group_allows(self):
        d = self._decide(settings=settings(enabled=False))
        self.assertEqual(d.action, ACTION_ALLOW)

    def test_owner_and_admin_allowed_by_switch(self):
        self.assertEqual(self._decide(group_role=ROLE_OWNER).action, ACTION_ALLOW)
        self.assertEqual(self._decide(group_role=ROLE_ADMIN).action, ACTION_ALLOW)

    def test_owner_switch_off_blocks_owner(self):
        d = self._decide(group_role=ROLE_OWNER, settings=settings(allow_owner_admin=False))
        self.assertEqual(d.action, ACTION_BLOCK_SILENT)
        self.assertIn("放行群主与群管理", d.reason)
        self.assertIn("开关已关闭", d.reason)

    def test_whitelist_allows_member(self):
        d = self._decide(settings=settings(whitelist=["10001"]))
        self.assertEqual(d.action, ACTION_ALLOW)
        self.assertTrue(d.blocks_llm is False)

    def test_global_admin_allowed(self):
        d = self._decide(is_global_admin=True, settings=settings(allow_owner_admin=False))
        self.assertEqual(d.action, ACTION_ALLOW)

    def test_command_only_mode(self):
        allowed = self._decide(message_text="/help")
        self.assertEqual(allowed.action, ACTION_ALLOW_COMMAND)
        self.assertTrue(allowed.blocks_llm)
        self.assertFalse(allowed.halts_event)

        blocked = self._decide(message_text="你好呀")
        self.assertEqual(blocked.action, ACTION_BLOCK_SILENT)
        self.assertTrue(blocked.blocks_llm)
        self.assertTrue(blocked.halts_event)

    def test_silent_mode(self):
        d = self._decide(settings=settings(mode=MODE_SILENT), message_text="help")
        self.assertEqual(d.action, ACTION_BLOCK_SILENT)

    def test_reply_mode(self):
        d = self._decide(settings=settings(mode=MODE_REPLY))
        self.assertEqual(d.action, ACTION_BLOCK_REPLY)
        self.assertTrue(d.should_reply)

    def test_chat_only_mode(self):
        d = self._decide(settings=settings(mode=MODE_CHAT_ONLY))
        self.assertEqual(d.action, ACTION_BLOCK_LLM_ONLY)
        self.assertTrue(d.blocks_llm)  # 禁止 AI
        self.assertFalse(d.halts_event)  # 但不拦其它插件

    def test_every_block_decision_blocks_llm(self):
        """核心保证：只要是未授权，就一定禁止默认 AI（唤醒词也不响应）。"""
        for mode in (MODE_CHAT_ONLY, MODE_COMMAND_ONLY, MODE_SILENT, MODE_REPLY):
            d = self._decide(settings=settings(mode=mode), message_text="随便说说")
            self.assertNotEqual(d.action, ACTION_ALLOW)
            self.assertTrue(d.blocks_llm, f"{mode} 模式没有禁止 AI")


class TestRenderReply(unittest.TestCase):
    def test_placeholders(self):
        text = render_reply(
            "{user}({user_id}) 在 {group} 只能玩 {commands}，模式 {mode}",
            user_name="小明",
            user_id="10001",
            group_id="123456",
            commands=["help", "sid"],
            mode=MODE_COMMAND_ONLY,
        )
        self.assertIn("小明(10001)", text)
        self.assertIn("123456", text)
        self.assertIn("help、sid", text)
        self.assertIn("仅放行指定命令", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
