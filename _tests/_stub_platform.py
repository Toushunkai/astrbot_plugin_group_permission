"""AstrBot 桩 · 平台对象（事件、消息组件、装饰器、Context 与 web 桩）。

仅用于本插件的单元测试。装饰器与注册表语义对齐 AstrBot v4.28.1：``register_*`` 把
handler 记进 ``star_handlers_registry``，由它按 ``-priority`` 稳定排序。
"""

from __future__ import annotations

import enum
import types
from dataclasses import dataclass, field
from typing import Any

from _stub_core import (
    EventMessageType,
    EventMessageTypeFilter,
    EventType,
    StarHandlerMetadata,
    get_handler_or_create,
    star_handlers_registry,
)


class AstrBotConfig(dict):
    """AstrBotConfig 的最小替身（支持 save_config）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self, replace_config=None, **kwargs):
        if replace_config:
            self.update(replace_config)
        self.saved += 1


# ------------------------------------------------------------ 事件基类
class AstrMessageEvent:
    """只实现本插件用到的那部分 AstrMessageEvent 接口。"""

    def __init__(self, message_obj=None, message_str="", role="member"):
        self.message_obj = message_obj
        self.message_str = message_str
        self.role = role
        # 与 AstrBot 一致：call_llm 默认 False，而它的含义是「是否【禁止】默认 LLM 请求」
        # （astrbot/core/platform/astr_message_event.py:94 与 :372）
        self.call_llm = False
        self._has_send_oper = False
        self._stopped = False
        self._force_stopped = False
        self.sent: list[Any] = []
        self._result = None
        self._extras: dict[str, Any] = {}
        self.is_at_or_wake_command = False
        self.is_wake = False

    # --- 取值
    def get_group_id(self):
        return getattr(self.message_obj, "group_id", "") or ""

    def get_sender_id(self):
        return str(getattr(getattr(self.message_obj, "sender", None), "user_id", ""))

    def get_self_id(self):
        return str(getattr(self.message_obj, "self_id", ""))

    def get_sender_name(self):
        return str(getattr(getattr(self.message_obj, "sender", None), "nickname", ""))

    def get_messages(self):
        return list(getattr(self.message_obj, "message", []) or [])

    def get_message_type(self):
        return getattr(self.message_obj, "type", None)

    def get_platform_name(self):
        return getattr(self.message_obj, "platform_name", "aiocqhttp")

    async def get_group(self, group_id=None, **kwargs):
        return getattr(self.message_obj, "group", None)

    # --- extras
    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    # --- 结果与事件传播
    def plain_result(self, text):
        return MessageEventResult().message(text)

    def chain_result(self, chain):
        result = MessageEventResult()
        result.chain = list(chain)
        return result

    def set_result(self, result):
        self._result = result

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    def should_call_llm(self, call_llm: bool) -> None:
        self.call_llm = call_llm

    def stop_event(self) -> None:
        # 与 AstrBot 一致（astr_message_event.py:348-362）
        self._force_stopped = True
        self._stopped = True
        if self._result is None:
            self.set_result(MessageEventResult().stop_event())
        else:
            self._result.stop_event()

    def continue_event(self) -> None:
        self._force_stopped = False

    def is_stopped(self) -> bool:
        if self._force_stopped:
            return True
        if self._result is None:
            return False
        return self._result.is_stopped()

    async def send(self, message):
        self.sent.append(message)
        self._has_send_oper = True

# ------------------------------------------------------------ 组件 / 结果
class Plain:
    def __init__(self, text=""):
        self.text = text

class At:
    def __init__(self, qq="", name=None):
        self.qq = qq
        self.name = name

class Reply:
    def __init__(self, id="", sender_id=None):
        self.id = id
        self.sender_id = sender_id

class MessageEventResult:
    def __init__(self):
        self.chain: list[Any] = []
        self._stopped = False
        self.result_content_type = None

    def message(self, text):
        self.chain.append(Plain(text))
        return self

    def stop_event(self):
        self._stopped = True
        return self

    def is_stopped(self):
        return self._stopped

    def is_model_result(self):
        return self.result_content_type == "llm_result"

    def get_plain_text(self):
        return "".join(getattr(c, "text", "") for c in self.chain)

class MessageChain(list):
    @classmethod
    def message(cls, text):
        return cls([Plain(text)])

class PermissionType(enum.Flag):
    ADMIN = enum.auto()
    MEMBER = enum.auto()

# ------------------------------------------------------------ 装饰器
class _RegisteringCommandable:
    """指令组：模拟 AstrBot 的 RegisteringCommandable，让链式装饰器能跑通。"""

    def __init__(self, group_name: str):
        self.group_name = group_name
        self.sub_commands: list[str] = []

    def command(self, name=None, **kwargs):
        def decorator(func):
            self.sub_commands.append(name or getattr(func, "__name__", "?"))
            get_handler_or_create(
                func, EventType.AdapterMessageEvent, **(kwargs or {})
            )
            return func

        return decorator

    def group(self, name=None, **kwargs):
        def decorator(func):
            return _RegisteringCommandable(name or getattr(func, "__name__", "?"))

        return decorator

class _Filter:
    @staticmethod
    def event_message_type(event_message_type, **kwargs):
        def decorator(func):
            md = get_handler_or_create(
                func, EventType.AdapterMessageEvent, **(kwargs or {})
            )
            md.event_filters.append(EventMessageTypeFilter(event_message_type))
            return func

        return decorator

    @staticmethod
    def command(name=None, alias=None, **kwargs):
        def decorator(func):
            get_handler_or_create(func, EventType.AdapterMessageEvent, **(kwargs or {}))
            return func

        return decorator

    @staticmethod
    def command_group(name=None, alias=None, **kwargs):
        def decorator(obj):
            if isinstance(obj, _RegisteringCommandable):
                return obj
            # 指令组本身也会注册一个 handler（真实 AstrBot 里会被 CommandGroupFilter 标记跳过）
            get_handler_or_create(
                obj, EventType.AdapterMessageEvent, **(kwargs or {})
            )
            return _RegisteringCommandable(name)

        return decorator

    @staticmethod
    def on_llm_request(**kwargs):
        def decorator(func):
            get_handler_or_create(
                func, EventType.OnLLMRequestEvent, **(kwargs or {})
            )
            return func

        return decorator

    @staticmethod
    def on_decorating_result(**kwargs):
        def decorator(func):
            get_handler_or_create(
                func, EventType.OnDecoratingResultEvent, **(kwargs or {})
            )
            return func

        return decorator

    @staticmethod
    def permission_type(permission_type, raise_error=True, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def platform_adapter_type(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def regex(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

# ------------------------------------------------------------ star 相关
class Star:
    def __init__(self, context=None, config=None):
        self.context = context

    async def terminate(self) -> None: ...

star_map: dict[str, Any] = {}

class _StarMeta:
    def __init__(self, name, module_path):
        self.name = name
        self.module_path = module_path
        self.activated = True
        self.reserved = False

class Context:
    def __init__(self, config=None):
        self._config = config or AstrBotConfig()
        self.web_apis: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc=""):
        self.web_apis.append((route, handler, tuple(methods), desc))

    def get_config(self, umo=None):
        return self._config

    def get_all_stars(self):
        return []

def register(name, author, desc, version, repo=None):
    def decorator(cls):
        cls.plugin_name = name
        cls.plugin_version = version
        return cls

    return decorator

# ------------------------------------------------------------ web 桩
class _Request:
    def __init__(self):
        self._json: dict = {}

    def set_json(self, payload: dict):
        self._json = payload or {}

    async def json(self, default=None):
        return self._json if self._json is not None else (default or {})

request = _Request()

def json_response(payload, status_code=200):
    return {"__type__": "json", "status": status_code, "payload": payload}

def error_response(message, status_code=400):
    return {"__type__": "error", "status": status_code, "message": message}
