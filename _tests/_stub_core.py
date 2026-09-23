"""AstrBot 桩 · 核心 handler 注册表（复刻 astrbot/core/star/star_handler.py 的排序与幂等注册）。

仅用于本插件的单元测试：让插件在没安装 AstrBot 的环境里也能被真的 import 起来跑通。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


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
