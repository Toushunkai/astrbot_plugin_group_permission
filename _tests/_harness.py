"""测试公共脚手架：造插件实例、跑拦截器、收集输出。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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

from _fake_astrbot import HandlerMeta, make_event, run_handler_chain  # noqa: E402

MAX = sys.maxsize
GROUP = "123456"
GROUP_KEY = f"aiocqhttp:{GROUP}"


def make_plugin(plugin_set=None, **overrides):
    """造一个插件实例：配置用默认值，数据文件落在临时目录。"""
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
    """把主拦截器跑一遍（模拟 AstrBot 的 handler 调用与 yield 语义）。"""
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


def web_module():
    """拿桩里的 astrbot.api.web（用于给页面 API 塞请求体）。"""
    return sys.modules["astrbot.api.web"]
