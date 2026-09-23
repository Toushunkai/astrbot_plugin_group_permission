"""一个「最小可用的 AstrBot 桩」，用于在没有安装 AstrBot 的环境里跑本插件的测试。

为什么需要它
============
判定逻辑（gate.py）本身不依赖 AstrBot，可以直接测；但拦截器
（main.py::gate_group_message）依赖 AstrBot 的 API 才能跑起来。这里用最小的桩
把 ``astrbot.api.*`` 与 ``astrbot.core.star.star_handler`` 补上，好处是能真的把
插件 import 起来、真的按 AstrBot 的调度语义把 handler 跑一遍，从而验证：

1. 未授权成员的消息确实会被 ``should_call_llm(True)`` + ``stop_event()`` 拦掉；
2. 「先 yield 回复、再 stop_event」这个顺序确实能把文案发出去；
3. 分发顺序到底受不受 priority / 加载顺序影响（见 test_dispatch_order.py）；
4. 插件热重载后 handler 从注册表里消失时，插件能自愈补注册（v1.0.1 新增）。

⚠️ 这里复刻的是 AstrBot v4.28.1 的行为，关键处都标注了对应的源码位置。
桩不是 AstrBot 本身，真实运行时请以 AstrBot 源码为准。
"""

from __future__ import annotations

import enum
import functools
import sys
import time
import types
from dataclasses import dataclass, field
from typing import Any, Callable


# ============================================================ 顶层 package 桩
def install() -> None:
    """把 astrbot 相关的模块塞进 sys.modules（重复调用无副作用）。"""
    if "astrbot" in sys.modules and getattr(
        sys.modules["astrbot"], "__is_group_permission_stub__", False
    ):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__is_group_permission_stub__ = True
    api = types.ModuleType("astrbot.api")
    api_event = types.ModuleType("astrbot.api.event")
    api_star = types.ModuleType("astrbot.api.star")
    api_web = types.ModuleType("astrbot.api.web")
    msg_components = types.ModuleType("astrbot.api.message_components")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_star_handler = types.ModuleType("astrbot.core.star.star_handler")
    core_filter_emt = types.ModuleType("astrbot.core.star.filter.event_message_type")
    core_filter = types.ModuleType("astrbot.core.star.filter")

    class AstrBotConfig(dict):
        """AstrBotConfig 的最小替身（支持 save_config）。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved = 0

        def save_config(self, replace_config=None, **kwargs):
            if replace_config:
                self.update(replace_config)
            self.saved += 1

    # ------------------------------------------------------------ core 注册表
    class EventType(enum.Enum):
        """只列本插件用得到的几个（对应 astrbot/core/star/star_handler.py::EventType）。"""

        AdapterMessageEvent = enum.auto()
        OnLLMRequestEvent = enum.auto()
        OnDecoratingResultEvent = enum.auto()
        OnWaitingLLMRequestEvent = enum.auto()
        OnAstrBotLoadedEvent = enum.auto()

    @dataclass
    class StarHandlerMetadata:
        """对应 astrbot/core/star/star_handler.py::StarHandlerMetadata。"""

        event_type: Any
        handler_full_name: str
        handler_name: str
        handler_module_path: str
        handler: Any
        event_filters: list = field(default_factory=list)
        desc: str = ""
        extras_configs: dict = field(default_factory=dict)
        enabled: bool = True

    class StarHandlerRegistry:
        """复刻 ``StarHandlerRegistry``（astrbot/core/star/star_handler.py:17-30）。"""

        def __init__(self) -> None:
            self.star_handlers_map: dict[str, StarHandlerMetadata] = {}
            self._handlers: list[StarHandlerMetadata] = []

        def append(self, handler: StarHandlerMetadata) -> None:
            if "priority" not in handler.extras_configs:
                handler.extras_configs["priority"] = 0
            self.star_handlers_map[handler.handler_full_name] = handler
            self._handlers.append(handler)
            self._handlers.sort(key=lambda h: -h.extras_configs["priority"])

        def remove(self, handler: StarHandlerMetadata) -> None:
            self.star_handlers_map.pop(handler.handler_full_name, None)
            self._handlers = [h for h in self._handlers if h != handler]

        def get_handler_by_full_name(self, full_name):
            return self.star_handlers_map.get(full_name)

        def get_handlers_by_event_type(self, event_type, plugins_name=None):
            return [h for h in self._handlers if h.event_type == event_type and h.enabled]

        def get_handlers_by_module_name(self, module_name):
            return [h for h in self._handlers if h.handler_module_path == module_name]

        def clear(self) -> None:
            self.star_handlers_map.clear()
            self._handlers.clear()

        def __iter__(self):
            return iter(self._handlers)

        def __len__(self):
            return len(self._handlers)

    star_handlers_registry = StarHandlerRegistry()

    class EventMessageType(enum.Flag):
        GROUP_MESSAGE = enum.auto()
        PRIVATE_MESSAGE = enum.auto()
        OTHER_MESSAGE = enum.auto()
        ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE

    class EventMessageTypeFilter:
        """对应 astrbot/core/star/filter/event_message_type.py::EventMessageTypeFilter。"""

        def __init__(self, event_message_type) -> None:
            self.event_message_type = event_message_type

        def filter(self, event, cfg=None) -> bool:
            text = str(getattr(event.message_obj, "type", "")).lower()
            if "group" in text:
                return bool(self.event_message_type & EventMessageType.GROUP_MESSAGE)
            if "friend" in text or "private" in text:
                return bool(self.event_message_type & EventMessageType.PRIVATE_MESSAGE)
            return bool(self.event_message_type & EventMessageType.OTHER_MESSAGE)

    def get_handler_or_create(handler, event_type, dont_add=False, **kwargs):
        """对应 astrbot/core/star/register/star_handler.py::get_handler_or_create（含幂等）。"""
        full_name = f"{handler.__module__}_{handler.__name__}"
        md = star_handlers_registry.get_handler_by_full_name(full_name)
        if md:
            return md
        md = StarHandlerMetadata(
            event_type=event_type,
            handler_full_name=full_name,
            handler_name=handler.__name__,
            handler_module_path=handler.__module__,
            handler=handler,
            event_filters=[],
            desc=(handler.__doc__ or "").strip(),
            extras_configs=dict(kwargs),
        )
        if not dont_add:
            star_handlers_registry.append(md)
        return md

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

    # ------------------------------------------------------------ 装配模块
    # 真实的 `from astrbot.api.event import filter` 拿到的是
    # astrbot/api/event/filter/ 这个**子包**，它同时导出装饰器和 EventMessageType。
    filter_ns = types.SimpleNamespace(
        event_message_type=_Filter.event_message_type,
        command=_Filter.command,
        command_group=_Filter.command_group,
        on_llm_request=_Filter.on_llm_request,
        on_decorating_result=_Filter.on_decorating_result,
        permission_type=_Filter.permission_type,
        platform_adapter_type=_Filter.platform_adapter_type,
        regex=_Filter.regex,
        EventMessageType=EventMessageType,
        PermissionType=PermissionType,
    )

    core_star_handler.EventType = EventType
    core_star_handler.StarHandlerMetadata = StarHandlerMetadata
    core_star_handler.star_handlers_registry = star_handlers_registry
    core_filter_emt.EventMessageType = EventMessageType
    core_filter_emt.EventMessageTypeFilter = EventMessageTypeFilter
    core_filter.event_message_type = core_filter_emt

    msg_components.Plain = Plain
    msg_components.At = At
    msg_components.Reply = Reply
    astrbot.api = api
    astrbot.core = core
    api.AstrBotConfig = AstrBotConfig
    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.MessageChain = MessageChain
    api_event.MessageEventResult = MessageEventResult
    api_event.filter = filter_ns
    api_event.EventMessageType = EventMessageType
    api_event.AstrBotConfig = AstrBotConfig
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    api_web.request = request
    api_web.json_response = json_response
    api_web.error_response = error_response
    core_star.star_handler = core_star_handler
    core_star.filter = core_filter
    core.star = core_star

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = api_event
    sys.modules["astrbot.api.event.filter"] = types.ModuleType(
        "astrbot.api.event.filter"
    )
    sys.modules["astrbot.api.event.filter"].__dict__.update(filter_ns.__dict__)
    sys.modules["astrbot.api.star"] = api_star
    sys.modules["astrbot.api.web"] = api_web
    sys.modules["astrbot.api.message_components"] = msg_components
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.star"] = core_star
    sys.modules["astrbot.core.star.star_handler"] = core_star_handler
    sys.modules["astrbot.core.star.filter"] = core_filter
    sys.modules["astrbot.core.star.filter.event_message_type"] = core_filter_emt


def core_registry():
    """拿到桩里的 star_handlers_registry（真实运行时是 AstrBot 的那个）。"""
    install()
    return sys.modules["astrbot.core.star.star_handler"].star_handlers_registry


def core_star_map() -> dict:
    install()
    return star_map


# =============================================== 复刻 AstrBot 的分发语义


@dataclass
class HandlerMeta:
    """测试用的简化 handler（只用于分发顺序实验）。"""

    name: str
    plugin: str
    handler: Callable
    priority: int = 0
    filters: list[str] = field(default_factory=list)
    event_type: str = "AdapterMessageEvent"
    enabled: bool = True


class HandlerRegistry:
    """复刻 ``StarHandlerRegistry``（astrbot/core/star/star_handler.py:17-30）。

    ``append`` 里那句 ``self._handlers.sort(key=lambda h: -h.extras_configs["priority"])``
    就是「效果会不会受加载顺序影响」的根源：Python 的 list.sort 是**稳定排序**，
    priority 相同时保持插入顺序，而插入顺序 = 插件加载顺序。
    """

    def __init__(self) -> None:
        self._handlers: list[HandlerMeta] = []

    def append(self, handler: HandlerMeta) -> None:
        self._handlers.append(handler)
        self._handlers.sort(key=lambda h: -h.priority)

    def remove(self, handler: HandlerMeta) -> None:
        self._handlers = [h for h in self._handlers if h is not handler]

    def __iter__(self):
        return iter(self._handlers)

    def __len__(self):
        return len(self._handlers)


async def run_handler_chain(event, handlers, *, reply_sink=None) -> dict:
    """复刻 ``StarRequestSubStage.process`` + 调度器的关键语义。

    源码位置：
    - astrbot/core/pipeline/process_stage/method/star_request.py
      ``for handler in activated_handlers: if event.is_stopped(): break``
    - astrbot/core/pipeline/context_utils.py::call_handler
      handler yield 出 MessageEventResult 时 ``event.set_result(ret)`` 然后 yield
    - astrbot/core/pipeline/scheduler.py::_process_stages
      yield 点先跑后续阶段（消息在这里发出去）——但如果 yield 时事件已经被 stop，
      就直接 break，消息发不出去。

    返回值里 ``executed`` 是按顺序真正执行到的插件名。
    """

    executed: list[str] = []
    replies: list[str] = list(reply_sink or [])
    stopped_by: str | None = None

    async def _drive(ret):
        """复刻 ``call_handler`` 的包装语义（astrbot/core/pipeline/context_utils.py）。"""
        if not hasattr(ret, "__aiter__"):
            await ret
            return
        async for item in ret:
            if item is not None and hasattr(item, "chain"):
                event.set_result(item)
            if event.is_stopped():
                return
            result = event.get_result()
            if result is not None:
                text = result.get_plain_text() if hasattr(result, "get_plain_text") else ""
                if text:
                    replies.append(text)
                    event._has_send_oper = True  # event.send() 会置位
                event.clear_result()

    for handler in handlers:
        if event.is_stopped():
            break
        if not handler.enabled:
            continue
        executed.append(handler.plugin)
        await _drive(handler.handler(event))
        if event.is_stopped():
            stopped_by = handler.plugin
            break

    return {
        "executed": executed,
        "stopped_by": stopped_by,
        "replies": replies,
        "llm_called": _llm_would_run(event),
        "stopped": event.is_stopped(),
    }


def _llm_would_run(event) -> bool:
    """复刻 ProcessStage 里「要不要调用默认 LLM」的判定。

    源码 astrbot/core/pipeline/process_stage/stage.py:52-66::

        if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:
            if (event.get_result() and not event.is_stopped()) or not event.get_result():
                ... 调用 LLM ...
    """
    if event._has_send_oper or not event.is_at_or_wake_command or event.call_llm:
        return False
    return (event.get_result() is not None and not event.is_stopped()) or event.get_result() is None


# =============================================== 测试用的事件/配置工厂


@dataclass
class FakeSender:
    user_id: str = "10001"
    nickname: str = "测试用户"


@dataclass
class FakeGroup:
    group_id: str = "123456"
    group_name: str = "测试群"
    group_owner: str | None = None
    group_admins: list[str] | None = None


@dataclass
class FakeMessageObj:
    group_id: str = "123456"
    self_id: str = "9999"
    sender: FakeSender = field(default_factory=FakeSender)
    group: FakeGroup | None = None
    raw_message: Any = None
    message: list[Any] = field(default_factory=list)
    type: str = "GroupMessage"
    platform_name: str = "aiocqhttp"
    message_id: str = "mid-0001"
    bot: Any = None


def make_event(
    *,
    group_id="123456",
    user_id="10001",
    nickname="测试用户",
    role: str | None = "member",
    message: str = "你好",
    self_id="9999",
    group_owner=None,
    group_admins=None,
    is_global_admin=False,
    platform_name="aiocqhttp",
    raw=True,
    message_id="mid-0001",
    bot=None,
):
    """造一个假事件。``role`` 会写进 raw_message.sender.role（模拟 OneBot v11）。"""
    install()
    event_cls = sys.modules["astrbot.api.event"].AstrMessageEvent

    raw_message = None
    if raw:
        raw_message = {"sender": {"user_id": user_id, "role": role, "nickname": nickname}}
        if role is None:
            raw_message["sender"].pop("role")
    message_obj = FakeMessageObj(
        group_id=group_id,
        self_id=self_id,
        sender=FakeSender(user_id=user_id, nickname=nickname),
        group=FakeGroup(
            group_id=group_id,
            group_owner=group_owner,
            group_admins=group_admins,
        ),
        raw_message=raw_message,
        platform_name=platform_name,
        message_id=message_id,
        bot=bot,
    )
    event = event_cls(message_obj=message_obj, message_str=message)
    event.bot = bot
    event.role = "admin" if is_global_admin else "member"
    event.is_at_or_wake_command = True
    return event


def now() -> float:
    return time.time()


def partial_instance(func, instance):
    """把 handler 绑到实例上（真实 AstrBot 用 functools.partial 做绑定）。"""
    return functools.partial(func, instance)
