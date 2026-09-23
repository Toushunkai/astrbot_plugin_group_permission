"""AstrBot 桩 · 管线语义仿真与测试用的事件工厂。

``run_handler_chain`` 复刻 AstrBot 的 handler 调用顺序（``star_request.py`` 的
``if event.is_stopped(): break``）、``call_handler`` 的 yield 语义与调度器在 yield 点的
stop 判断，用来在本地验证「拦截器能不能拦住、文案发不发得出去」。

仅用于单元测试；真实运行时以 AstrBot 源码为准。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from _stub_platform import AstrMessageEvent

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
    from _fake_astrbot import install  # 惰性导入：避免两个模块在导入期互相依赖

    install()
    event_cls = AstrMessageEvent

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
