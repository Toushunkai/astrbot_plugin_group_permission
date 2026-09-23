"""AstrBot 桩 · 入口（把替身装配进 sys.modules + 再导出）。

``install()`` 把 ``_stub_core`` / ``_stub_platform`` 里的替身类装配成 ``astrbot.*``
模块塞进 ``sys.modules``，这样插件就能在没安装 AstrBot 的环境里被真的 import 起来。
测试用的管线仿真与事件工厂在 ``_sim_pipeline``，这里一并再导出。

⚠️ 这里复刻的是 AstrBot v4.28.1 的行为，关键处标注了源码位置。桩不是 AstrBot 本身。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import _stub_core
import _stub_platform
from _sim_pipeline import (  # noqa: F401  （再导出给测试用）
    FakeGroup,
    FakeMessageObj,
    FakeSender,
    HandlerMeta,
    HandlerRegistry,
    make_event,
    now,
    partial_instance,
    run_handler_chain,
)


def install() -> None:
    """把 astrbot 相关的模块塞进 sys.modules（重复调用无副作用）。"""
    if "astrbot" in sys.modules and getattr(
        sys.modules["astrbot"], "__is_group_permission_stub__", False
    ):
        return

    core = _stub_core
    platform = _stub_platform

    astrbot = types.ModuleType("astrbot")
    astrbot.__is_group_permission_stub__ = True
    api = types.ModuleType("astrbot.api")
    api_event = types.ModuleType("astrbot.api.event")
    api_star = types.ModuleType("astrbot.api.star")
    api_web = types.ModuleType("astrbot.api.web")
    msg_components = types.ModuleType("astrbot.api.message_components")
    core_mod = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_star_handler = types.ModuleType("astrbot.core.star.star_handler")
    core_filter_emt = types.ModuleType("astrbot.core.star.filter.event_message_type")
    core_filter = types.ModuleType("astrbot.core.star.filter")

    # 真实的 `from astrbot.api.event import filter` 拿到的是 astrbot/api/event/filter/
    # 这个**子包**，它同时导出装饰器与 EventMessageType。
    filter_ns = types.SimpleNamespace(
        event_message_type=platform._Filter.event_message_type,
        command=platform._Filter.command,
        command_group=platform._Filter.command_group,
        on_llm_request=platform._Filter.on_llm_request,
        on_decorating_result=platform._Filter.on_decorating_result,
        permission_type=platform._Filter.permission_type,
        platform_adapter_type=platform._Filter.platform_adapter_type,
        regex=platform._Filter.regex,
        EventMessageType=core.EventMessageType,
        PermissionType=platform.PermissionType,
    )

    core_star_handler.EventType = core.EventType
    core_star_handler.StarHandlerMetadata = core.StarHandlerMetadata
    core_star_handler.star_handlers_registry = core.star_handlers_registry
    core_filter_emt.EventMessageType = core.EventMessageType
    core_filter_emt.EventMessageTypeFilter = core.EventMessageTypeFilter
    core_filter.event_message_type = core_filter_emt

    msg_components.Plain = platform.Plain
    msg_components.At = platform.At
    msg_components.Reply = platform.Reply
    api.AstrBotConfig = platform.AstrBotConfig
    api_event.AstrMessageEvent = platform.AstrMessageEvent
    api_event.MessageChain = platform.MessageChain
    api_event.MessageEventResult = platform.MessageEventResult
    api_event.filter = filter_ns
    api_event.EventMessageType = core.EventMessageType
    api_event.AstrBotConfig = platform.AstrBotConfig
    api_star.Context = platform.Context
    api_star.Star = platform.Star
    api_star.register = platform.register
    api_web.request = platform.request
    api_web.json_response = platform.json_response
    api_web.error_response = platform.error_response

    core_star.star_handler = core_star_handler
    core_star.filter = core_filter
    core_star.star_map = platform.star_map
    core_mod.star = core_star
    astrbot.api = api
    astrbot.core = core_mod
    api.event = api_event
    api.star = api_star
    api.web = api_web
    api.message_components = msg_components

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = api_event
    sys.modules["astrbot.api.event.filter"] = types.ModuleType("astrbot.api.event.filter")
    sys.modules["astrbot.api.event.filter"].__dict__.update(filter_ns.__dict__)
    sys.modules["astrbot.api.star"] = api_star
    sys.modules["astrbot.api.web"] = api_web
    sys.modules["astrbot.api.message_components"] = msg_components
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.star"] = core_star
    sys.modules["astrbot.core.star.star_handler"] = core_star_handler
    sys.modules["astrbot.core.star.filter"] = core_filter
    sys.modules["astrbot.core.star.filter.event_message_type"] = core_filter_emt


def core_registry():
    """拿到桩里的 star_handlers_registry（真实运行时是 AstrBot 的那个）。"""
    install()
    return _stub_core.star_handlers_registry


def core_star_map() -> dict[str, Any]:
    """拿到桩里的 star_map。"""
    install()
    return _stub_platform.star_map
