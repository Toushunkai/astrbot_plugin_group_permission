"""启动自检与 handler 自愈。

AstrBot 重载插件时会先清空 handler 注册表再重新 import 插件模块；只要这条链路
有一环没走到，handler 就会静默消失（页面照常、群里不生效、日志无报错）。这里在
实例化时检查并补注册，并把「有没有排到前面」「plugin_set 是否把自己过滤掉」写成日志。"""

from __future__ import annotations

import sys

import functools
from typing import Any

from .constants import INTERCEPT_PRIORITY, PLUGIN_NAME, logger

try:  # 自检 / 自愈需要的内部 API（拿不到也不影响正常拦截）
    from astrbot.core.star.filter.event_message_type import (
        EventMessageType as _CoreEventMessageType,
    )
    from astrbot.core.star.filter.event_message_type import (
        EventMessageTypeFilter as _CoreEventMessageTypeFilter,
    )
    from astrbot.core.star.star_handler import EventType as _CoreEventType
    from astrbot.core.star.star_handler import (
        StarHandlerMetadata as _CoreStarHandlerMetadata,
    )
    from astrbot.core.star.star_handler import (
        star_handlers_registry as _core_registry,
    )

    CORE_API_AVAILABLE = True
except Exception:  # noqa: BLE001
    CORE_API_AVAILABLE = False


class SelfCheckMixin:
    def _handler_full_name(self) -> str:
        func = type(self).gate_group_message
        return f"{func.__module__}_{func.__name__}"

    def _ensure_handler(self) -> dict:
        """确保拦截器在注册表里、优先级正确、并且绑定到当前实例。

        为什么需要这个（v1.0.1 新增）
        ---------------------------
        AstrBot 重载插件时会 ``star_handlers_registry.clear()`` 再重新 import 插件模块，
        靠 import 时的 ``@filter.xxx`` 装饰器把 handler 重新注册回去。只要这个链条里
        有一步没走到（模块没被重新执行、注册表被清掉而模块还在 sys.modules 缓存里、
        或者插件是被手工热替换的），handler 就会**静默消失**：

        - 插件页面一切正常（页面走的是实例上的 ``register_web_api``，跟 handler 无关）；
        - 群里完全不生效（没有任何 handler 被调用）；
        - 日志里也不会有任何报错。

        所以这里做一次兜底：发现自己的 handler 不在注册表里就手动补注册，并把
        注册状态记下来，启动时打日志、插件页面也能看到。
        """
        status: dict[str, Any] = {
            "core_api": CORE_API_AVAILABLE,
            "registered": False,
            "self_healed": False,
            "priority": None,
            "position": None,
            "total": None,
            "ahead": [],
            "error": "",
        }
        if not CORE_API_AVAILABLE:
            status["error"] = "当前 AstrBot 没有 astrbot.core.star.star_handler，跳过自检"
            return status
        want = self._global_cfg()["priority"]
        func = type(self).gate_group_message
        try:
            md = _core_registry.get_handler_by_full_name(self._handler_full_name())
            if md is None:
                md = _CoreStarHandlerMetadata(
                    event_type=_CoreEventType.AdapterMessageEvent,
                    handler_full_name=self._handler_full_name(),
                    handler_name=func.__name__,
                    handler_module_path=func.__module__,
                    handler=functools.partial(func, self),
                    event_filters=[
                        _CoreEventMessageTypeFilter(_CoreEventMessageType.GROUP_MESSAGE)
                    ],
                    desc=(func.__doc__ or "").strip(),
                    extras_configs={"priority": want},
                )
                _core_registry.append(md)
                status["self_healed"] = True
            else:
                md.enabled = True
                raw = md.handler.func if isinstance(md.handler, functools.partial) else md.handler
                md.handler = functools.partial(raw, self)
                if md.extras_configs.get("priority") != want:
                    # ⚠️ 只有优先级真的变了才重排。
                    # registry 是「append 之后按 -priority 稳定排序」，同优先级保持插入顺序；
                    # 插件加载顺序是「用户插件先、内置插件后」，所以在 maxsize 这一档我们本来
                    # 天然排在内置插件前面。如果每次实例化都无条件 remove+append，就会把自己
                    # 挪到同级最后面，反而丢掉这个优势（实测会把
                    # handle_session_control_agent 排到我们前面）。
                    md.extras_configs["priority"] = want
                    _core_registry.remove(md)
                    _core_registry.append(md)

            status["registered"] = True
            status["priority"] = md.extras_configs.get("priority")
            adapter_handlers = [
                h
                for h in _core_registry
                if h.event_type == _CoreEventType.AdapterMessageEvent and h.enabled
            ]
            status["total"] = len(adapter_handlers)
            for index, handler in enumerate(adapter_handlers):
                if handler is md:
                    status["position"] = index
                    break
            if status["position"]:
                status["ahead"] = [h.handler_name for h in adapter_handlers[: status["position"]]]
        except Exception as exc:  # noqa: BLE001 - 自检失败也不能让插件崩
            status["error"] = str(exc)
        return status

    def _plugin_set_status(self) -> dict:
        """检查 AstrBot 全局配置的 plugin_set 会不会把本插件整个过滤掉。

        ``WakingCheckStage`` 每个消息都会读它::

            enabled_plugins_name = self.ctx.astrbot_config.get("plugin_set", ["*"])
            event.plugins_name = None if enabled_plugins_name == ["*"] else enabled_plugins_name

        然后 ``get_handlers_by_event_type(..., plugins_name=event.plugins_name)`` 会把
        不在名单里的插件 handler 全部跳过——**包括本插件的兜底钩子**。
        注意插件页面不受影响，所以这个坑特别难自己发现。
        """
        try:
            cfg = self.context.get_config()
            value = cfg.get("plugin_set", ["*"]) if cfg is not None else ["*"]
        except Exception as exc:  # noqa: BLE001
            return {"ok": None, "value": None, "error": str(exc), "fixable": False}
        if value is None or value == ["*"] or (isinstance(value, list) and "*" in value):
            return {"ok": True, "value": value, "error": "", "fixable": False}
        if isinstance(value, list) and PLUGIN_NAME in value:
            return {"ok": True, "value": value, "error": "", "fixable": False}
        return {
            "ok": False,
            "value": value,
            "error": f"plugin_set 里没有 {PLUGIN_NAME}，本插件的所有 handler 都会被跳过",
            "fixable": isinstance(value, list),
        }

    def _log_self_check(self) -> None:
        st = self._handler_status
        if st.get("error"):
            logger.warning("群聊权限门禁自检：%s", st["error"])
        if st.get("self_healed"):
            logger.warning(
                "群聊权限门禁：发现自己的拦截器不在 AstrBot 的 handler 注册表里"
                "（通常是插件热重载/注册表被清空导致的），已自动补注册。"
            )
        if st.get("registered"):
            if st.get("position") == 0:
                logger.info(
                    "群聊权限门禁自检：拦截器已就位（优先级=%s，消息分发顺序第 1/%s 个）",
                    st.get("priority"),
                    st.get("total"),
                )
            else:
                logger.warning(
                    "群聊权限门禁自检：拦截器前面还有 %s 个 handler：%s。"
                    "它们在 stop_event() 时会让我们失去拦截机会，"
                    "请到插件页面「分发顺序」页签确认。",
                    st.get("position"),
                    "、".join(st.get("ahead") or []),
                )
        # plugin_set 一旦不是 ["*"]，本插件所有 handler 都会被跳过（页面不受影响）
        ps = self._plugin_set_status()
        if ps.get("ok") is False:
            logger.warning(
                "群聊权限门禁：AstrBot 配置里的 plugin_set=%s 不包含本插件，"
                "所有 handler（拦截器、LLM 兜底闸门、gperm 指令）都会被直接跳过，"
                "表现为「插件页面正常但群里完全不生效」。"
                "请把它设为 [\"*\"]，或把 %s 加进去——"
                "插件页面「总览」页签有一键修复按钮（%s）。",
                ps.get("value"),
                PLUGIN_NAME,
                "可用" if ps.get("fixable") else "不可用，请手工改配置文件",
            )
