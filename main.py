"""astrbot_plugin_group_permission —— 群聊权限门禁。

一句话说明
==========
在群里，**只有群主 / 群管理（可单独开关）和白名单成员能和机器人对话**；
其他群友的消息按配置的模式处理（静默忽略 / 回复固定文案 / 只放行指定命令 /
只拦截 AI 对话）。未授权成员的 @ 与唤醒词同样不会唤起 AI。

为什么要写 priority = sys.maxsize
================================
AstrBot 把一条消息能触发的所有 handler 收集到一个列表里，然后**按 priority
从大到小**依次调用（``astrbot/core/star/star_handler.py`` 的
``StarHandlerRegistry.append``：``self._handlers.sort(key=lambda h: -h.extras_configs["priority"])``），
并且一旦某个 handler 里 ``event.stop_event()``，后面所有 handler 都会被跳过
（``astrbot/core/pipeline/process_stage/method/star_request.py``：
``for handler in activated_handlers: if event.is_stopped(): break``）。

所以「拦得住」的前提是**我们排在前面**。默认 priority 是 0，同优先级的顺序
由注册顺序（也就是插件加载顺序）决定；而 AstrBot 内置的 ``astrbot`` 插件里的
``handle_empty_mention`` 用的是 ``priority=maxsize - 1``，专门处理「只 @ 一下
机器人」和「只发一个唤醒前缀」的消息，还会等待用户下一条消息——如果我们的
priority 比它低，未授权成员只发一个 ``@机器人`` 就已经被它接管了。

结论：本插件的拦截器默认用 ``sys.maxsize``，并且可以在插件配置页里调整
（调低它就会受加载顺序影响，页面里的「分发顺序」页签会直接把运行时顺序列出来）。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .gate import (
    ACTION_ALLOW,
    ACTION_ALLOW_COMMAND,
    ACTION_BLOCK_LLM_ONLY,
    ACTION_BLOCK_REPLY,
    ACTION_BLOCK_SILENT,
    MODE_COMMAND_ONLY,
    MODE_LABELS,
    MODES,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_MEMBER,
    ROLE_OWNER,
    Decision,
    EffectiveSettings,
    as_str_list,
    decide,
    merge_settings,
    normalize_command_text,
    normalize_group_role,
    normalize_user_id,
    render_reply,
    role_from_group_object,
    role_from_raw_message,
)
from .store import OVERRIDE_KEYS, GroupStore, make_group_key, split_group_key

try:  # AstrBot >= 4.24 的插件 Pages 后端 API
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001 - 低版本 AstrBot 仍可用指令与拦截功能
    WEB_API_AVAILABLE = False

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

PLUGIN_NAME = "astrbot_plugin_group_permission"
PLUGIN_VERSION = "v1.0.4"

# 拦截器的默认优先级：sys.maxsize 保证排在所有 handler 之前（含内置插件）
_INTERCEPT_PRIORITY = sys.maxsize

DEFAULT_REPLY = (
    "{user} 本群开启了对话权限：只有群主、群管理和白名单成员可以和我聊天。\n"
    "你可以使用这些指令：{commands}"
)

logger = logging.getLogger("astrbot")

# 允许在插件页面里修改的全局配置项（白名单，避免写入奇怪的东西）
_BOOL_KEYS = {
    "enable",
    "default_group_enabled",
    "allow_owner_admin",
    "allow_global_admin",
    "auto_query_group_info",
    "enable_log",
}
_INT_KEYS = {
    "priority": (0, sys.maxsize),
    "reply_cooldown": (0, 86400),
    "group_info_cache_ttl": (0, 86400),
    "group_info_timeout": (1, 60),
}
_LIST_KEYS = {"whitelist", "command_whitelist"}
_STR_KEYS = {"mode", "reply", "unknown_role_action", "reply_style"}

# 固定文案的发送方式（借鉴 astrbot_plugin_llmallowlist：引用 → @ → 普通，逐级降级）
REPLY_STYLES = ("quote", "at", "plain")
REPLY_STYLE_LABELS = {
    "quote": "引用对方那条消息",
    "at": "@ 对方",
    "plain": "直接发（不引用也不 @）",
}

_SEEN_FLUSH_INTERVAL = 60.0


@register(
    PLUGIN_NAME,
    "Soulter",
    "群聊权限门禁：只让群主/群管理/白名单成员和机器人对话，其余成员按模式处理",
    PLUGIN_VERSION,
    "https://github.com/Soulter/astrbot_plugin_group_permission",
)
class GroupPermissionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.store = GroupStore()
        self._group_cache: dict[str, tuple[float, dict]] = {}
        self._reply_cooldown: dict[tuple[str, str], float] = {}
        self._seen_dirty = False
        self._seen_flushed_at = time.time()
        self._handler_status = self._ensure_handler()
        self._register_page_apis()
        cfg = self._global_cfg()
        logger.info(
            "群聊权限门禁已加载：总开关=%s 模式=%s 群主/管理放行=%s 白名单=%d 人 拦截优先级=%s",
            cfg["enable"],
            cfg["mode"],
            cfg["allow_owner_admin"],
            len(cfg["whitelist"]),
            cfg["priority"],
        )
        self._log_self_check()

    # ================================================================= 配置读取
    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开启")
        return default

    @staticmethod
    def _as_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    def _raw_config(self) -> dict:
        cfg = self.config
        return cfg if isinstance(cfg, dict) else {}

    def _global_cfg(self) -> dict:
        """全局默认配置（从 AstrBot 插件配置里读，带默认值与类型收敛）。"""
        cfg = self._raw_config()
        mode = str(cfg.get("mode") or MODE_COMMAND_ONLY)
        if mode not in MODES:
            mode = MODE_COMMAND_ONLY
        unknown_role_action = str(cfg.get("unknown_role_action") or "block").lower()
        if unknown_role_action not in ("block", "allow"):
            unknown_role_action = "block"
        # reply_style：引用回复 / @ / 普通（旧版本只有布尔 reply_with_at，这里做兼容）
        reply_style = str(cfg.get("reply_style") or "").strip().lower()
        if reply_style not in REPLY_STYLES:
            if "reply_with_at" in cfg:
                reply_style = "at" if self._as_bool(cfg.get("reply_with_at"), True) else "plain"
            else:
                reply_style = "quote"
        return {
            "enable": self._as_bool(cfg.get("enable"), True),
            "enabled": self._as_bool(cfg.get("default_group_enabled"), True),
            "allow_owner_admin": self._as_bool(cfg.get("allow_owner_admin"), True),
            "allow_global_admin": self._as_bool(cfg.get("allow_global_admin"), True),
            "mode": mode,
            "reply": str(cfg.get("reply") or DEFAULT_REPLY),
            "whitelist": as_str_list(cfg.get("whitelist")),
            "command_whitelist": as_str_list(cfg.get("command_whitelist")),
            "auto_query_group_info": self._as_bool(cfg.get("auto_query_group_info"), True),
            "group_info_cache_ttl": self._as_int(
                cfg.get("group_info_cache_ttl"), 300, 0, 86400
            ),
            "group_info_timeout": self._as_int(cfg.get("group_info_timeout"), 8, 1, 60),
            "unknown_role_action": unknown_role_action,
            "reply_cooldown": self._as_int(cfg.get("reply_cooldown"), 60, 0, 86400),
            "reply_style": reply_style,
            "priority": self._as_int(
                cfg.get("priority"), _INTERCEPT_PRIORITY, 0, sys.maxsize
            ),
            "enable_log": self._as_bool(cfg.get("enable_log"), True),
        }

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        """群配置的存储键：``平台名:群号``（参考实现也是按平台分别维护白名单的）。"""
        return make_group_key(event.get_platform_name(), event.get_group_id())

    def _settings_for(self, key: str) -> EffectiveSettings:
        return merge_settings(self._global_cfg(), self.store.get_group(str(key)))

    # ================================================================= 优先级 / 自检
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

    # ================================================================= 拦截器
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=_INTERCEPT_PRIORITY,
    )
    async def gate_group_message(self, event: AstrMessageEvent):
        """群聊权限门禁：非群主/群管理/白名单成员的消息按配置拦截。

        注意这里的顺序：需要「先 yield 回复、再 stop_event」。
        AstrBot 的调度器在每个 yield 点会先跑完后续阶段（消息就是在
        RespondStage 真正发出去的），之后才回到本函数；如果先 stop_event
        再 yield，调度器会在 yield 点直接 break，回复就发不出去了。
        """
        try:
            if not self._global_cfg()["enable"]:
                return
            decision, settings, group_id, meta = await self._evaluate(event)
            group_key = self._group_key(event)
        except Exception:  # noqa: BLE001 - 判定异常一律放行，避免把机器人锁死
            logger.exception("群聊权限门禁：判定失败，本条消息已放行")
            return

        self._log_decision(event, decision, meta)

        # 给后面两道兜底闸门留标记（L2 取消 LLM 请求、L3 拦下待发送消息）
        self._mark(event, "_gpm_evaluated", True)
        self._mark(event, "_gpm_action", decision.action)
        self._mark(event, "_gpm_reason", decision.reason)
        if decision.action != ACTION_ALLOW:
            self._mark(event, "_gpm_unauthorized", True)
        if decision.action == ACTION_ALLOW_COMMAND:
            self._mark(event, "_gpm_cmd_allowed", True)

        if decision.action == ACTION_ALLOW:
            return

        # 未授权：先确保默认 AI 链路一定不会跑（这就是「唤醒词也不响应」）。
        #
        # ⚠️ 这个 API 的名字是反的，很容易踩坑：
        #   AstrMessageEvent.call_llm 的默认值是 False，字段含义其实是
        #   「是否【禁止】默认的 LLM 请求」；ProcessStage 里的判断是
        #   `if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:`
        #   也就是 call_llm 为 False（默认）时才会调用 LLM。
        #   所以「禁止默认 AI 对话」要传 True，传 False 等于什么都没做。
        #   源码：astrbot/core/platform/astr_message_event.py:372 与
        #        astrbot/core/pipeline/process_stage/stage.py:55-66
        try:
            event.should_call_llm(True)
        except Exception:  # noqa: BLE001
            pass

        if decision.action in (ACTION_BLOCK_LLM_ONLY, ACTION_ALLOW_COMMAND):
            return

        if decision.action == ACTION_BLOCK_REPLY:
            text = render_reply(
                settings.reply_template or DEFAULT_REPLY,
                user_name=event.get_sender_name(),
                user_id=str(event.get_sender_id() or ""),
                group_id=group_id,
                commands=settings.command_whitelist,
                mode=settings.mode,
            )
            # 这是我们自己发的权限提示，兜底闸门 L3 要放它过去
            self._mark(event, "_gpm_fallback", True)
            if text and self._cooldown_ok(group_key, str(event.get_sender_id() or "")):
                if self._global_cfg()["reply_style"] == "plain":
                    yield event.plain_result(text)
                else:
                    yield event.chain_result(self._reply_chain(event, text))
            event.stop_event()
            return

        # ACTION_BLOCK_SILENT
        event.stop_event()
        return

    # ------------------------------------------------------- 兜底闸门 L2 / L3
    @filter.on_llm_request(priority=_INTERCEPT_PRIORITY)
    async def gate_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """兜底闸门 1：未授权成员的 LLM 请求直接取消。

        为什么需要它：主拦截器（``gate_group_message``）依赖「排在别人前面」，
        而 AstrBot 里还有 ``plugin_set``、会话级插件禁用、插件热重载等因素能让它
        整个不执行。这个钩子不参与「谁先谁后」的竞争——它是管线在真正调用模型前
        主动回调的：

        源码 astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py::

            if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                return          # ← 我们在这里 stop_event()，这次 LLM 请求就彻底不发生

        它拦的是**所有** LLM 请求：默认对话、插件里的 ``request_llm``、主动回复
        （active_reply）都会经过这里，所以「唤醒词也不响应」在模式选宽松时同样成立。
        """
        try:
            if not await self._should_block_llm(event):
                return
            event.stop_event()
            logger.info(
                "群聊权限门禁：已取消未授权成员的 LLM 请求（群=%s 用户=%s）",
                event.get_group_id(),
                event.get_sender_id(),
            )
        except Exception:  # noqa: BLE001 - 钩子出错一律放行，不能把机器人搞坏
            logger.exception("群聊权限门禁：LLM 兜底闸门异常（本条已放行）")

    @filter.on_decorating_result(priority=_INTERCEPT_PRIORITY)
    async def gate_outgoing(self, event: AstrMessageEvent) -> None:
        """兜底闸门 2：未授权成员触发的回复一律不让发出去。

        ResultDecorateStage 在发送前会调用这个钩子，清掉 result 消息就不会发出去
        （astrbot/core/pipeline/result_decorate/stage.py:157-190）。
        只在主拦截器**已经判定为拦截**的事件上生效，所以不会误伤定时推送、
        也不会影响授权成员和其它插件。
        """
        try:
            action = event.get_extra("_gpm_action")
            if not action or action in (ACTION_ALLOW, ACTION_ALLOW_COMMAND):
                return
            if event.get_extra("_gpm_fallback"):
                return  # 我们自己发的那条权限提示
            result = event.get_result()
            if result is None:
                return
            if action == ACTION_BLOCK_LLM_ONLY:
                # chat_only 模式：只挡 AI 回复，其它插件的正常消息照发
                checker = getattr(result, "is_model_result", None)
                if not callable(checker) or not checker():
                    return
            event.clear_result()
            logger.info(
                "群聊权限门禁：已拦下未授权成员的一条待发送消息（群=%s 用户=%s 动作=%s）",
                event.get_group_id(),
                event.get_sender_id(),
                action,
            )
        except Exception:  # noqa: BLE001
            logger.exception("群聊权限门禁：发送兜底闸门异常（本条已放行）")

    def _reply_chain(self, event: AstrMessageEvent, text: str) -> list[Any]:
        """按 reply_style 组装固定文案：引用对方原消息，或 @ 对方。

        参考 astrbot_plugin_llmallowlist 的做法；引用不到时（某些平台不支持）
        由 ``_send_with_fallback`` 逐级降级成 @ / 普通消息。
        """
        style = self._global_cfg()["reply_style"]
        chain: list[Any] = []
        if style == "quote":
            message_id = getattr(event.message_obj, "message_id", None)
            if message_id:
                chain.append(Comp.Reply(id=str(message_id)))
        if style in ("quote", "at"):
            chain.append(Comp.At(qq=str(event.get_sender_id() or "")))
        chain.append(Comp.Plain(text))
        return chain

    async def _send_with_fallback(self, event: AstrMessageEvent, text: str) -> bool:
        """依次尝试 引用 → @ → 普通，返回是否成功（参考实现的三级降级）。"""
        message_id = getattr(event.message_obj, "message_id", None)
        attempts: list[list[Any]] = []
        if message_id:
            attempts.append([Comp.Reply(id=str(message_id)), Comp.Plain(text)])
        attempts.append([Comp.At(qq=str(event.get_sender_id() or "")), Comp.Plain(text)])
        attempts.append([Comp.Plain(text)])
        for chain in attempts:
            try:
                await event.send(event.chain_result(chain))
                return True
            except Exception as exc:  # noqa: BLE001 - 换下一种发法
                logger.debug("群聊权限门禁：固定文案发送失败，降级重试：%s", exc)
        return False

    @staticmethod
    def _mark(event: AstrMessageEvent, key: str, value: Any) -> None:
        try:
            event.set_extra(key, value)
        except Exception:  # noqa: BLE001 - 老版本可能没有 set_extra
            pass

    async def _should_block_llm(self, event: AstrMessageEvent) -> bool:
        """要不要取消这次 LLM 请求。"""
        if not self._global_cfg()["enable"]:
            return False
        if event.get_extra("_gpm_cmd_allowed"):
            return False  # 命令白名单放行的命令，允许它自己用 LLM
        if event.get_extra("_gpm_unauthorized"):
            return True  # 主拦截器已经判过了
        return await self._fallback_unauthorized(event)

    async def _fallback_unauthorized(self, event: AstrMessageEvent) -> bool:
        """主拦截器没跑时的兜底判定。

        必须只对「真实入站群消息」生效，否则会误伤定时任务和主动推送：
        ``CronMessageEvent`` 的 sender 就是 session_id、self_id 是 "astrbot"、
        platform 名是 "cron"（astrbot/core/cron/events.py）。
        """
        try:
            if event.get_platform_name() == "cron":
                return False
            group_id = str(event.get_group_id() or "")
            group_key = self._group_key(event)
            sender_id = normalize_user_id(event.get_sender_id())
            if not group_id or not sender_id:
                return False
            if sender_id in (str(event.get_self_id() or ""), group_id):
                return False
            if getattr(event.message_obj, "raw_message", None) is None:
                return False
            settings = self._settings_for(group_key)
            if not settings.enabled:
                return False
            role, _source = await self._resolve_group_role(event, sender_id)
            if role is None and self._global_cfg()["unknown_role_action"] == "allow":
                return False
            decision = decide(
                is_group=True,
                user_id=sender_id,
                group_role=role,
                is_global_admin=self._is_global_admin(event),
                message_text=event.message_str,
                settings=settings,
            )
            return decision.action not in (ACTION_ALLOW, ACTION_ALLOW_COMMAND)
        except Exception:  # noqa: BLE001
            logger.exception("群聊权限门禁：兜底判定失败（本条已放行）")
            return False

    async def _evaluate(
        self, event: AstrMessageEvent
    ) -> tuple[Decision, EffectiveSettings, str, dict]:
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        sender_id = normalize_user_id(event.get_sender_id())
        settings = self._settings_for(group_key)
        if group_id:
            group = getattr(event.message_obj, "group", None)
            self.store.touch_group(group_key, str(getattr(group, "group_name", "") or ""))
            self._seen_dirty = True
            self._maybe_flush_seen()

        global_cfg = self._global_cfg()
        role, role_source = await self._resolve_group_role(event, sender_id)
        is_global_admin = self._is_global_admin(event)
        meta = {
            "role": role,
            "role_source": role_source,
            "global_admin": is_global_admin,
            "user_id": sender_id,
        }

        # 角色识别不出来时按配置决定：默认按「未授权」处理（更安全）
        if role is None and global_cfg["unknown_role_action"] == "allow":
            return (
                Decision(ACTION_ALLOW, "群内角色无法识别，且设置为「识别失败则放行」"),
                settings,
                group_id,
                meta,
            )

        decision = decide(
            is_group=bool(group_id),
            user_id=sender_id,
            group_role=role,
            is_global_admin=is_global_admin,
            message_text=event.message_str,
            settings=settings,
        )
        return decision, settings, group_id, meta

    # ------------------------------------------------------------- 角色识别
    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        """AstrBot 的全局管理员（配置里的 admins_id）。"""
        try:
            if event.role == "admin":
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg = self.context.get_config()
            admins = cfg.get("admins_id", []) if cfg is not None else []
            return normalize_user_id(event.get_sender_id()) in {
                normalize_user_id(item) for item in (admins or [])
            }
        except Exception:  # noqa: BLE001
            return False

    async def _resolve_group_role(
        self, event: AstrMessageEvent, sender_id: str
    ) -> tuple[str | None, str]:
        """尽量便宜地拿到发送者的群身份，拿不到再考虑调平台接口。"""
        # 1) 平台原始事件里的 sender.role（OneBot v11 免费自带，最准最快）
        raw = getattr(event.message_obj, "raw_message", None)
        role = role_from_raw_message(raw)
        if role:
            return role, "raw_message"

        # 2) AstrBot 已经填好的群对象
        group = getattr(event.message_obj, "group", None)
        if group is not None and (
            getattr(group, "group_admins", None) is not None
            or getattr(group, "group_owner", None)
        ):
            return role_from_group_object(group, sender_id) or ROLE_MEMBER, "group_object"

        # 3) 调平台接口（带 TTL 缓存，避免每条消息都问一次）
        if not self._global_cfg()["auto_query_group_info"]:
            return None, "unknown"
        return await self._query_group_role(event, sender_id)

    async def _fetch_group_roles(
        self, event: AstrMessageEvent, group_id: str, cfg: dict
    ) -> dict | None:
        """拿群主/群管理列表。

        优先直接调 OneBot 的 ``get_group_member_list``（参考 astrbot_plugin_llmallowlist
        的做法），只需一次 API 调用；拿不到再退回跨平台的 ``event.get_group()``
        （aiocqhttp 上它会多调一次 ``get_group_info``）。
        """
        timeout = float(cfg["group_info_timeout"])
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            try:
                members = await asyncio.wait_for(
                    call_action("get_group_member_list", group_id=group_id),
                    timeout=timeout,
                )
                if isinstance(members, list):
                    owner = ""
                    admins: list[str] = []
                    for member in members:
                        if not isinstance(member, dict):
                            continue
                        uid = normalize_user_id(member.get("user_id"))
                        if not uid:
                            continue
                        if member.get("role") == "owner":
                            owner = uid
                        elif member.get("role") == "admin":
                            admins.append(uid)
                    return {"owner": owner, "admins": admins}
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "群聊权限门禁：get_group_member_list 失败，改用 event.get_group()：%s", exc
                )
        try:
            group = await asyncio.wait_for(event.get_group(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("群聊权限门禁：查询群 %s 信息失败：%s", group_id, exc)
            return None
        return {
            "owner": normalize_user_id(getattr(group, "group_owner", None)),
            "admins": [
                normalize_user_id(item)
                for item in as_str_list(getattr(group, "group_admins", None))
            ],
        }

    async def _query_group_role(
        self, event: AstrMessageEvent, sender_id: str
    ) -> tuple[str | None, str]:
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id or not sender_id:
            return None, "unknown"
        cfg = self._global_cfg()
        ttl = cfg["group_info_cache_ttl"]
        now = time.time()
        cached = self._group_cache.get(group_id)
        if cached and ttl > 0 and now - cached[0] < ttl:
            data = cached[1]
        else:
            data = await self._fetch_group_roles(event, group_id, cfg)
            if data is None:
                return None, "query_failed"
            self._group_cache[group_id] = (now, data)
        if data.get("owner") and data["owner"] == sender_id:
            return ROLE_OWNER, "platform_api"
        if sender_id in (data.get("admins") or []):
            return ROLE_ADMIN, "platform_api"
        return ROLE_MEMBER, "platform_api"

    # ------------------------------------------------------------- 其它小工具
    def _maybe_flush_seen(self) -> None:
        """「见过的群」攒够一段时间再落盘，避免每条消息都写文件。"""
        if not self._seen_dirty:
            return
        now = time.time()
        if now - self._seen_flushed_at < _SEEN_FLUSH_INTERVAL:
            return
        self._seen_flushed_at = now
        self._seen_dirty = False
        self.store.save()

    def _cooldown_ok(self, group_id: str, sender_id: str) -> bool:
        cooldown = self._global_cfg()["reply_cooldown"]
        if cooldown <= 0:
            return True
        key = (group_id, sender_id)
        now = time.time()
        last = self._reply_cooldown.get(key, 0.0)
        if now - last < cooldown:
            return False
        self._reply_cooldown[key] = now
        if len(self._reply_cooldown) > 5000:  # 防止无限膨胀
            cutoff = now - max(cooldown, 60)
            self._reply_cooldown = {
                k: v for k, v in self._reply_cooldown.items() if v >= cutoff
            }
        return True

    def _log_decision(self, event: AstrMessageEvent, decision: Decision, meta: dict) -> None:
        if not self._global_cfg()["enable_log"]:
            return
        role_text = ROLE_LABELS.get(meta.get("role") or "", meta.get("role") or "未知")
        if decision.action == ACTION_ALLOW:
            logger.debug(
                "群聊权限门禁：放行 群=%s 用户=%s 角色=%s(%s) 原因=%s",
                event.get_group_id(),
                event.get_sender_id(),
                role_text,
                meta.get("role_source"),
                decision.reason,
            )
            return
        logger.info(
            "群聊权限门禁：拦截 群=%s 用户=%s 角色=%s(%s) 动作=%s 原因=%s",
            event.get_group_id(),
            event.get_sender_id(),
            role_text,
            meta.get("role_source"),
            decision.action,
            decision.reason,
        )

    # ================================================================= 指令
    @filter.command_group("gperm", alias={"群权限"})
    def gperm(self):
        """群聊权限门禁管理

        用法（群主 / 群管理 / AstrBot 管理员可用）：
        - gperm status             查看本群当前生效的规则
        - gperm on / off           启用或停用本群的门禁
        - gperm mode <模式>        切换模式：chat_only / command_only / silent / reply
        - gperm debug              排查用：拦截器注册状态 + 分发顺序 + 本群生效配置
        - gperm add <ID 或 @某人>   把成员加入白名单
        - gperm del <ID 或 @某人>   把成员移出白名单
        - gperm list               查看本群白名单
        - gperm cmd add|del <命令>  维护「命令白名单」
        - gperm reset              清除本群覆盖，恢复跟随全局默认
        """
        pass

    async def _ensure_manager(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """管理指令的准入：AstrBot 管理员、群主、群管理。"""
        sender_id = normalize_user_id(event.get_sender_id())
        if self._is_global_admin(event):
            return True, "AstrBot 全局管理员"
        role, source = await self._resolve_group_role(event, sender_id)
        if role == ROLE_OWNER:
            return True, "群主"
        if role == ROLE_ADMIN:
            return True, "群管理"
        if role == ROLE_MEMBER:
            return False, "普通群员"
        return False, f"身份未知（{source}）"

    @staticmethod
    def _split_args(event: AstrMessageEvent) -> list[str]:
        """取命令后面的参数。

        ``event.message_str`` 是唤醒前缀已被剥掉后的完整文本，例如
        ``gperm cmd add 签到``；前两个词是指令组名和子指令名，都不算参数。
        """
        parts = normalize_command_text(event.message_str).split()
        return parts[2:] if len(parts) > 2 else []

    @staticmethod
    def _extract_ids(event: AstrMessageEvent, args: list[str]) -> list[str]:
        ids: list[str] = []
        self_id = str(event.get_self_id() or "")
        for comp in event.get_messages() or []:
            qq = getattr(comp, "qq", None)
            if qq is None:
                continue
            raw = str(qq)
            if raw in ("", "all", self_id):  # 跳过 @全体 和 @机器人自己
                continue
            uid = normalize_user_id(raw)
            if uid and uid not in ids:
                ids.append(uid)
        for arg in args:
            uid = normalize_user_id(arg)
            if uid and uid.isdigit() and uid not in ids:
                ids.append(uid)
        return ids

    async def _deny(self, event: AstrMessageEvent, why: str) -> AsyncGenerator[Any, None]:
        yield event.plain_result(
            f"这条指令只有群主、群管理或 AstrBot 管理员可以使用（你当前是：{why}）。"
        )
        event.stop_event()

    @gperm.command("status")
    async def gperm_status(self, event: AstrMessageEvent):
        """查看本群当前生效的群聊权限规则"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            yield event.plain_result("这条指令要在群里用哦～")
            return
        sender_id = normalize_user_id(event.get_sender_id())
        settings = self._settings_for(group_key)
        role, source = await self._resolve_group_role(event, sender_id)
        decision = decide(
            is_group=True,
            user_id=sender_id,
            group_role=role,
            is_global_admin=self._is_global_admin(event),
            message_text=event.message_str,
            settings=settings,
        )
        lines = [
            "【群聊权限门禁】本群生效规则",
            f"总开关：{'✅ 开' if self._global_cfg()['enable'] else '❌ 关（插件整体不生效）'}",
            f"本群启用：{'✅ 是' if settings.enabled else '❌ 否（所有人可对话）'}",
            f"群主/群管理放行：{'开' if settings.allow_owner_admin else '关'}",
            f"AstrBot 全局管理员放行：{'开' if settings.allow_global_admin else '关'}",
            f"处理模式：{MODE_LABELS.get(settings.mode, settings.mode)}",
            f"白名单（{len(settings.whitelist)}）：{'、'.join(settings.whitelist) or '（空）'}",
            f"命令白名单（{len(settings.command_whitelist)}）："
            f"{'、'.join(settings.command_whitelist) or '（空）'}",
            f"配置来源：{'本群单独配置' if settings.source == 'group' else '跟随全局默认'}",
            "",
            f"你的身份：{ROLE_LABELS.get(role or '', '未知')}（识别来源：{source}）",
            f"你这条消息会被：{decision.action} —— {decision.reason}",
        ]
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @gperm.command("debug")
    async def gperm_debug(self, event: AstrMessageEvent):
        """排查用：打印拦截器的注册状态、分发顺序和本群生效配置"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        status = self._ensure_handler()
        self._handler_status = status
        lines = ["【群聊权限门禁 · 自检】"]
        if status.get("error"):
            lines.append(f"自检异常：{status['error']}")
        lines.append(f"拦截器已注册：{'是' if status.get('registered') else '否'}")
        if status.get("self_healed"):
            lines.append("⚠️ 之前不在注册表里，本次已自动补注册（说明曾因重载丢过 handler）")
        lines.append(f"优先级：{status.get('priority')}")
        if status.get("position") == 0:
            lines.append(f"消息分发顺序：第 1/{status.get('total')} 个 ✅（在所有人前面）")
        else:
            lines.append(
                f"消息分发顺序：第 {status.get('position')}/{status.get('total')} 个 ⚠️"
                f"，前面还有：{'、'.join(status.get('ahead') or [])}"
            )
        try:
            cfg = self.context.get_config()
            plugin_set = cfg.get("plugin_set", ["*"]) if cfg is not None else ["*"]
            if plugin_set in (None, ["*"]):
                lines.append("plugin_set：全部启用 ✅")
            elif PLUGIN_NAME in plugin_set:
                lines.append("plugin_set：包含本插件 ✅")
            else:
                lines.append(f"plugin_set：❌ 不包含本插件（{plugin_set}），handler 会被全部跳过！")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"plugin_set：读取失败（{exc}）")
        # 会话级插件禁用（AstrBot 的「会话插件管理」）会静默过滤掉本插件的 handler
        try:
            from astrbot.core import sp as _sp

            umo = getattr(event, "unified_msg_origin", "")
            session_cfg = await _sp.get_async(
                scope="umo",
                scope_id=umo,
                key="session_plugin_config",
                default={},
            )
            disabled = (session_cfg.get(umo) or {}).get("disabled_plugins", [])
            if PLUGIN_NAME in disabled:
                lines.append(f"会话插件管理：❌ 本插件在本会话被禁用（{umo}）")
            else:
                lines.append("会话插件管理：未被禁用 ✅")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"会话插件管理：读取失败（{exc}）")
        if group_id:
            settings = self._settings_for(group_key)
            sender_id = normalize_user_id(event.get_sender_id())
            role, source = await self._resolve_group_role(event, sender_id)
            decision = decide(
                is_group=True,
                user_id=sender_id,
                group_role=role,
                is_global_admin=self._is_global_admin(event),
                message_text=event.message_str,
                settings=settings,
            )
            lines += [
                "",
                f"本群启用：{settings.enabled}｜模式：{settings.mode}｜"
                f"群主管理放行：{settings.allow_owner_admin}｜配置来源：{settings.source}",
                f"你的身份：{ROLE_LABELS.get(role or '', '未知')}（来源：{source}）",
                f"本条判定：{decision.action} —— {decision.reason}",
            ]
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @gperm.command("on")
    async def gperm_on(self, event: AstrMessageEvent):
        """启用本群的群聊权限门禁"""
        async for item in self._set_enabled(event, True):
            yield item

    @gperm.command("off")
    async def gperm_off(self, event: AstrMessageEvent):
        """停用本群的群聊权限门禁"""
        async for item in self._set_enabled(event, False):
            yield item

    async def _set_enabled(
        self, event: AstrMessageEvent, enabled: bool
    ) -> AsyncGenerator[Any, None]:
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        self.store.set_group(group_key, {"enabled": enabled})
        self.store.save()
        logger.info("群聊权限门禁：%s 把群 %s 的门禁设为 %s", why, group_id, enabled)
        yield event.plain_result(
            f"本群群聊权限门禁已{'启用 ✅' if enabled else '停用 ❌'}（{why} 操作）"
        )
        event.stop_event()

    @gperm.command("mode")
    async def gperm_mode(self, event: AstrMessageEvent):
        """切换本群的处理模式：chat_only / command_only / silent / reply"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        args = self._split_args(event)
        if not args:
            yield event.plain_result(
                "用法：gperm mode <模式>\n"
                + "\n".join(f"- {m}：{label}" for m, label in MODE_LABELS.items())
            )
            event.stop_event()
            return
        mode = args[0].strip().lower()
        if mode not in MODES:
            yield event.plain_result(f"未知模式「{mode}」，可选：{'、'.join(MODES)}")
            event.stop_event()
            return
        self.store.set_group(group_key, {"mode": mode})
        self.store.save()
        yield event.plain_result(f"本群处理模式已切换为：{MODE_LABELS[mode]}（{mode}）")
        event.stop_event()

    @gperm.command("list")
    async def gperm_list(self, event: AstrMessageEvent):
        """查看本群白名单"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        settings = self._settings_for(group_key)
        yield event.plain_result(
            f"本群白名单（{len(settings.whitelist)}）：{'、'.join(settings.whitelist) or '（空）'}\n"
            f"命令白名单（{len(settings.command_whitelist)}）："
            f"{'、'.join(settings.command_whitelist) or '（空）'}"
        )
        event.stop_event()

    @gperm.command("add")
    async def gperm_add(self, event: AstrMessageEvent):
        """把成员加入白名单：gperm add @某人 或 gperm add 123456"""
        async for item in self._whitelist_edit(event, add=True):
            yield item

    @gperm.command("del")
    async def gperm_del(self, event: AstrMessageEvent):
        """把成员移出白名单：gperm del @某人 或 gperm del 123456"""
        async for item in self._whitelist_edit(event, add=False):
            yield item

    async def _whitelist_edit(
        self, event: AstrMessageEvent, add: bool
    ) -> AsyncGenerator[Any, None]:
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        args = self._split_args(event)
        ids = self._extract_ids(event, args)
        verb = "add" if add else "del"
        if not ids:
            yield event.plain_result(f"用法：gperm {verb} @某人 或 gperm {verb} 123456")
            event.stop_event()
            return
        override = self.store.get_group(group_key)
        current = as_str_list(
            override.get("whitelist")
            if override.get("whitelist") is not None
            else self._global_cfg()["whitelist"]
        )
        if add:
            merged = current + [i for i in ids if i not in current]
        else:
            merged = [i for i in current if i not in ids]
        self.store.set_group(group_key, {"whitelist": merged})
        self.store.save()
        yield event.plain_result(
            f"白名单已{'添加' if add else '移除'}：{'、'.join(ids)}\n"
            f"当前本群白名单（{len(merged)}）：{'、'.join(merged) or '（空）'}"
        )
        event.stop_event()

    @gperm.command("cmd")
    async def gperm_cmd(self, event: AstrMessageEvent):
        """维护命令白名单：gperm cmd add 签到 / gperm cmd del 签到"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        args = self._split_args(event)
        if len(args) < 2 or args[0] not in ("add", "del"):
            yield event.plain_result("用法：gperm cmd add <命令名> / gperm cmd del <命令名>")
            event.stop_event()
            return
        action, command = args[0], normalize_command_text(args[1])
        override = self.store.get_group(group_key)
        current = as_str_list(
            override.get("command_whitelist")
            if override.get("command_whitelist") is not None
            else self._global_cfg()["command_whitelist"]
        )
        if action == "add":
            merged = current + [command] if command not in current else current
        else:
            merged = [c for c in current if c != command]
        self.store.set_group(group_key, {"command_whitelist": merged})
        self.store.save()
        yield event.plain_result(
            f"命令白名单已{'添加' if action == 'add' else '移除'}：{command}\n"
            f"当前（{len(merged)}）：{'、'.join(merged) or '（空）'}"
        )
        event.stop_event()

    @gperm.command("reset")
    async def gperm_reset(self, event: AstrMessageEvent):
        """清除本群覆盖，恢复跟随全局默认"""
        group_id = str(event.get_group_id() or "")
        group_key = self._group_key(event)
        if not group_id:
            return
        ok, why = await self._ensure_manager(event)
        if not ok:
            async for item in self._deny(event, why):
                yield item
            return
        self.store.reset_group(group_key)
        self.store.save()
        yield event.plain_result("本群已清除单独配置，恢复跟随全局默认 ✅")
        event.stop_event()

    # ================================================================= 插件页面
    def _register_page_apis(self) -> None:
        if not WEB_API_AVAILABLE:
            logger.warning(
                "当前 AstrBot 版本不支持插件 Pages（astrbot.api.web 不可用），"
                "配置页不可用，请用插件配置面板或 gperm 指令。"
            )
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("当前 AstrBot 版本不支持 context.register_web_api，配置页不可用")
            return
        prefix = f"/{PLUGIN_NAME}"
        apis = [
            (f"{prefix}/page/overview", self.page_overview, ["GET"], "总览与全部配置"),
            (f"{prefix}/page/global", self.page_save_global, ["POST"], "保存全局默认配置"),
            (f"{prefix}/page/group", self.page_save_group, ["POST"], "保存某个群的配置"),
            (
                f"{prefix}/page/group/reset",
                self.page_reset_group,
                ["POST"],
                "清除某个群的配置（恢复跟随全局）",
            ),
            (
                f"{prefix}/page/group/forget",
                self.page_forget_group,
                ["POST"],
                "把某个群从列表里彻底删除（含「见过的群」记录）",
            ),
            (
                f"{prefix}/page/groups/prune",
                self.page_prune_groups,
                ["POST"],
                "清理未配置的群记录",
            ),
            (f"{prefix}/page/simulate", self.page_simulate, ["POST"], "模拟判定一条消息"),
            (f"{prefix}/page/dispatch", self.page_dispatch, ["GET"], "当前消息分发顺序"),
            (
                f"{prefix}/page/fix-plugin-set",
                self.page_fix_plugin_set,
                ["POST"],
                "把本插件加入 AstrBot 配置的 plugin_set",
            ),
            (f"{prefix}/page/reload", self.page_reload, ["POST"], "重新读取配置"),
        ]
        for route, handler, methods, desc in apis:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.error("注册插件页面 API 失败 %s: %s", route, exc)

    def _public_global(self) -> dict:
        cfg = self._global_cfg()
        return {
            "enable": cfg["enable"],
            "default_group_enabled": cfg["enabled"],
            "allow_owner_admin": cfg["allow_owner_admin"],
            "allow_global_admin": cfg["allow_global_admin"],
            "mode": cfg["mode"],
            "reply": cfg["reply"],
            "whitelist": cfg["whitelist"],
            "command_whitelist": cfg["command_whitelist"],
            "auto_query_group_info": cfg["auto_query_group_info"],
            "group_info_cache_ttl": cfg["group_info_cache_ttl"],
            "group_info_timeout": cfg["group_info_timeout"],
            "unknown_role_action": cfg["unknown_role_action"],
            "reply_cooldown": cfg["reply_cooldown"],
            "reply_style": cfg["reply_style"],
            "priority": cfg["priority"],
            "enable_log": cfg["enable_log"],
        }

    def _sanitize_global(self, payload: dict) -> dict:
        current = self._public_global()
        result = dict(current)
        for key, value in (payload or {}).items():
            if key in _BOOL_KEYS:
                result[key] = self._as_bool(value, bool(current.get(key)))
            elif key in _INT_KEYS:
                low, high = _INT_KEYS[key]
                result[key] = self._as_int(value, int(current.get(key, low)), low, high)
            elif key in _LIST_KEYS:
                result[key] = as_str_list(value)
            elif key in _STR_KEYS:
                if key == "mode":
                    mode = str(value or "").strip()
                    result[key] = mode if mode in MODES else current["mode"]
                elif key == "unknown_role_action":
                    action = str(value or "").strip().lower()
                    result[key] = action if action in ("block", "allow") else "block"
                elif key == "reply_style":
                    style = str(value or "").strip().lower()
                    result[key] = style if style in REPLY_STYLES else current["reply_style"]
                else:
                    result[key] = str(value or "")
        return result

    def _write_global(self, values: dict) -> None:
        for key, value in values.items():
            try:
                self.config[key] = value
            except Exception:  # noqa: BLE001 - 某些版本 AstrBotConfig 只读
                pass
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        else:
            logger.warning("当前 AstrBot 版本的 AstrBotConfig 不支持 save_config()")

    def _groups_payload(self) -> list[dict]:
        groups = []
        for item in self.store.known_groups():
            settings = self._settings_for(item["key"])
            merged = settings.to_dict()
            merged.pop("source", None)
            groups.append({**item, "effective": merged})
        return groups

    async def page_overview(self):
        """插件页面：一次性返回全局配置、群列表与运行状态。"""
        cfg = self._global_cfg()
        return json_response(
            {
                "ok": True,
                "global": self._public_global(),
                "groups": self._groups_payload(),
                "modes": [{"value": m, "label": MODE_LABELS[m]} for m in MODES],
                "actions": {
                    "allow": "授权通过，一切照旧",
                    "block_llm_only": "只禁止默认 AI 对话，其它插件照常",
                    "allow_command": "未授权但命中命令白名单，放行该指令（仍禁止 AI）",
                    "block_silent": "静默丢弃：不回复、不触发 AI、不触发其它插件",
                    "block_reply": "回复固定文案后丢弃",
                },
                "runtime": {
                    "plugin_version": PLUGIN_VERSION,
                    "intercept_priority": cfg["priority"],
                    "max_priority": sys.maxsize,
                    "group_cache_size": len(self._group_cache),
                    "data_file": str(self.store.path),
                    "web_api_available": WEB_API_AVAILABLE,
                    "handler": self._handler_status,
                    "plugin_set": self._plugin_set_status(),
                },
            }
        )

    async def page_save_global(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            values = self._sanitize_global(payload)
            self._write_global(values)
            self._handler_status = self._ensure_handler()
            self._log_self_check()
            self._group_cache.clear()
            return json_response(
                {
                    "ok": True,
                    "message": "全局配置已保存",
                    "global": self._public_global(),
                    "handler": self._handler_status,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("群聊权限门禁：保存全局配置失败：%s", exc)
            return error_response(f"保存失败：{exc}", status_code=500)

    async def page_save_group(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id", status_code=400)
        group_key = make_group_key(payload.get("platform"), group_id)
        values = payload.get("values")
        if not isinstance(values, dict):
            return error_response("values 必须是 JSON 对象", status_code=400)
        cleaned: dict[str, Any] = {}
        for key in OVERRIDE_KEYS:
            if key not in values:
                continue
            raw = values[key]
            if raw is None or raw == "":
                cleaned[key] = None  # 跟随全局
                continue
            if key in ("enabled", "allow_owner_admin", "allow_global_admin"):
                cleaned[key] = self._as_bool(raw, True)
            elif key in ("whitelist", "command_whitelist"):
                cleaned[key] = as_str_list(raw)
            elif key == "mode":
                mode = str(raw).strip()
                cleaned[key] = mode if mode in MODES else MODE_COMMAND_ONLY
            elif key == "reply":
                cleaned[key] = str(raw)
        note = payload.get("note")
        if isinstance(note, str):
            cleaned["note"] = note.strip()[:64]
        self.store.set_group(group_key, cleaned)
        self.store.save()
        return json_response(
            {
                "ok": True,
                "message": f"群 {group_id} 的配置已保存",
                "override": self.store.get_group(group_key),
                "effective": self._settings_for(group_key).to_dict(),
            }
        )

    async def page_reset_group(self):
        payload = await request.json(default={})
        group_key = str((payload or {}).get("key") or "").strip()
        group_id = str((payload or {}).get("group_id") or "").strip()
        if not group_key and group_id:
            group_key = make_group_key((payload or {}).get("platform"), group_id)
        if not group_key:
            return error_response("缺少 key（或 group_id + platform）", status_code=400)
        self.store.reset_group(group_key)
        self.store.save()
        return json_response(
            {
                "ok": True,
                "message": f"群 {group_id} 已恢复跟随全局默认",
                "effective": self._settings_for(group_key).to_dict(),
            }
        )

    async def page_forget_group(self):
        """把某个群从插件页面的列表里彻底删掉（配置 + 「见过的群」记录）。"""
        payload = await request.json(default={})
        group_key = str((payload or {}).get("key") or "").strip()
        group_id = str((payload or {}).get("group_id") or "").strip()
        if not group_key and group_id:
            group_key = make_group_key((payload or {}).get("platform"), group_id)
        if not group_key:
            return error_response("缺少 key（或 group_id + platform）", status_code=400)
        self.store.forget_group(group_key)
        self.store.save()
        return json_response({"ok": True, "message": f"已删除 {group_key}"})

    async def page_prune_groups(self):
        """清理「只是见过、没有单独配置」的群记录。"""
        removed = self.store.prune_seen()
        self.store.save()
        return json_response(
            {"ok": True, "removed": removed, "message": f"已清理 {removed} 条群记录"}
        )

    async def page_simulate(self):
        """模拟判定：不用真的发消息，就能看到某个人在某群会被怎么处理。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        group_id = str(payload.get("group_id") or "").strip()
        group_key = make_group_key(payload.get("platform"), group_id) or group_id
        user_id = normalize_user_id(payload.get("user_id"))
        message = str(payload.get("message") or "")
        role = normalize_group_role(payload.get("role"))
        settings = self._settings_for(group_key)
        decision = decide(
            is_group=bool(group_id),
            user_id=user_id,
            group_role=role,
            is_global_admin=bool(payload.get("is_global_admin")),
            message_text=message,
            settings=settings,
        )
        return json_response(
            {
                "ok": True,
                "action": decision.action,
                "reason": decision.reason,
                "matched_command": decision.matched_command,
                "blocks_llm": decision.blocks_llm,
                "halts_event": decision.halts_event,
                "reply_preview": (
                    render_reply(
                        settings.reply_template or DEFAULT_REPLY,
                        user_name=str(payload.get("user_name") or ""),
                        user_id=user_id,
                        group_id=group_id,
                        commands=settings.command_whitelist,
                        mode=settings.mode,
                    )
                    if decision.should_reply
                    else ""
                ),
                "effective": settings.to_dict(),
            }
        )

    async def page_dispatch(self):
        """把运行时真实的 handler 分发顺序列出来。

        这就是「效果会不会受加载顺序影响」的现场证据：AstrBot 按 priority
        从大到小调用，priority 相同的按注册顺序（插件加载顺序）排列。
        """
        items: list[dict] = []
        error = ""
        try:
            from astrbot.core.star.star import star_map
            from astrbot.core.star.star_handler import EventType, star_handlers_registry

            for index, handler in enumerate(star_handlers_registry):
                if handler.event_type != EventType.AdapterMessageEvent:
                    continue
                if not handler.enabled:
                    continue
                meta = star_map.get(handler.handler_module_path)
                items.append(
                    {
                        "index": index,
                        "plugin": getattr(meta, "name", None) or handler.handler_module_path,
                        "handler": handler.handler_name,
                        "priority": handler.extras_configs.get("priority", 0),
                        "is_gate": handler.handler_name == "gate_group_message",
                        "filters": [type(f).__name__ for f in handler.event_filters],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        return json_response(
            {
                "ok": not error,
                "error": error,
                "items": items,
                "note": (
                    "同一条群消息会按这张表从上到下依次调用 handler；"
                    "本插件（is_gate=true）一旦 stop_event，后面的 handler 全部跳过。"
                    "priority 相同则按插件加载顺序排列，所以调低本插件的优先级会让结果依赖加载顺序。"
                ),
            }
        )

    async def page_fix_plugin_set(self):
        """把本插件加进 AstrBot 全局配置的 plugin_set。

        只在用户明确点按钮时执行：往现有列表里追加本插件名（纯增量，不动别人），
        然后保存全局配置。这是 `plugin_set` 不含本插件时最省事的修法。
        """
        try:
            cfg = self.context.get_config()
            if cfg is None:
                return error_response("读不到 AstrBot 全局配置", status_code=500)
            value = cfg.get("plugin_set", ["*"])
            if not isinstance(value, list):
                return error_response(
                    f"plugin_set 不是列表（{value!r}），请手工改配置文件", status_code=400
                )
            if "*" in value:
                return json_response(
                    {"ok": True, "message": "plugin_set 是 [\"*\"]，所有插件都启用，无需修改"}
                )
            if PLUGIN_NAME in value:
                return json_response({"ok": True, "message": "plugin_set 本来就包含本插件"})
            new_value = [*value, PLUGIN_NAME]
            cfg["plugin_set"] = new_value
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
            logger.warning(
                "群聊权限门禁：已按用户操作把 %s 加入 AstrBot 配置的 plugin_set：%s",
                PLUGIN_NAME,
                new_value,
            )
            return json_response(
                {
                    "ok": True,
                    "message": "已加入 plugin_set，立即生效（若群里仍不生效，请重载一次配置）",
                    "plugin_set": new_value,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("群聊权限门禁：修改 plugin_set 失败")
            return error_response(f"修改失败：{exc}", status_code=500)

    async def page_reload(self):
        self.store.load()
        self._group_cache.clear()
        self._reply_cooldown.clear()
        return json_response({"ok": True, "message": "已重新读取按群配置"})

    # ================================================================= 收尾
    async def terminate(self) -> None:
        """插件被卸载/重载时保存数据。"""
        try:
            self.store.save()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["GroupPermissionPlugin"]
