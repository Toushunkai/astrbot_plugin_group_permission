"""astrbot_plugin_group_permission —— 群聊权限门禁。

群里只有群主 / 群管理（有单独开关）和白名单成员能与机器人对话；其余成员的消息按配置
的模式处理，且未授权成员的 @ 与唤醒词都不会唤起 AI。

三道闸门
--------
1. 入口拦截 ``gate_group_message``：优先级 ``sys.maxsize``，保证排在内置插件
   （``maxsize-1`` / ``maxsize-2``）与普通插件（``0``）之前；
2. LLM 请求闸门 ``gate_llm_request``：取消未授权成员的 LLM 请求，不依赖排队顺序，
   插件自己发起的 ``request_llm`` 同样拦得住；
3. 发送闸门 ``gate_outgoing``：清掉未授权成员触发的待发送内容。

模块划分
--------
本模块只保留 AstrBot 的 handler 定义 —— 带 ``@filter`` 装饰器的方法必须与插件类处于
同一模块，AstrBot 是按 ``handler_module_path`` 给 handler 绑定插件实例的。其余逻辑按
功能拆到 ``plugin_config`` / ``selfcheck`` / ``roles`` / ``enforcement`` / ``commands`` /
``page_api`` 六个 mixin 模块。
"""

from __future__ import annotations

from typing import Any

import sys

import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .commands import CommandMixin
from .constants import DEFAULT_REPLY, INTERCEPT_PRIORITY, PLUGIN_NAME, PLUGIN_VERSION, logger
from .enforcement import EnforcementMixin
from .gate import (
    ACTION_ALLOW,
    ACTION_ALLOW_COMMAND,
    ACTION_BLOCK_LLM_ONLY,
    ACTION_BLOCK_REPLY,
    ACTION_BLOCK_SILENT,
    render_reply,
)
from .page_api import PageApiMixin
from .plugin_config import ConfigMixin
from .roles import RoleResolverMixin
from .selfcheck import SelfCheckMixin
from .store import GroupStore


@register(
    PLUGIN_NAME,
    "Toushunkai",
    "群聊权限门禁：只让群主/群管理/白名单成员和机器人对话，其余成员按模式处理",
    PLUGIN_VERSION,
    "https://github.com/Toushunkai/astrbot_plugin_group_permission",
)
class GroupPermissionPlugin(
    ConfigMixin,
    SelfCheckMixin,
    RoleResolverMixin,
    EnforcementMixin,
    CommandMixin,
    PageApiMixin,
    Star,
):
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
    # ================================================================= 拦截器
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=INTERCEPT_PRIORITY,
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
    @filter.on_llm_request(priority=INTERCEPT_PRIORITY)
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

    @filter.on_decorating_result(priority=INTERCEPT_PRIORITY)
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

    @gperm.command("status")
    async def gperm_status(self, event: AstrMessageEvent):
        """查看本群当前生效的群聊权限规则"""
        async for item in self._cmd_status(event):
            yield item

    @gperm.command("debug")
    async def gperm_debug(self, event: AstrMessageEvent):
        """排查用：拦截器注册状态 + 分发顺序 + 本群生效配置"""
        async for item in self._cmd_debug(event):
            yield item

    @gperm.command("on")
    async def gperm_on(self, event: AstrMessageEvent):
        """启用本群的群聊权限门禁"""
        async for item in self._cmd_on(event):
            yield item

    @gperm.command("off")
    async def gperm_off(self, event: AstrMessageEvent):
        """停用本群的群聊权限门禁"""
        async for item in self._cmd_off(event):
            yield item

    @gperm.command("mode")
    async def gperm_mode(self, event: AstrMessageEvent):
        """切换本群的处理模式：chat_only / command_only / silent / reply"""
        async for item in self._cmd_mode(event):
            yield item

    @gperm.command("list")
    async def gperm_list(self, event: AstrMessageEvent):
        """查看本群白名单"""
        async for item in self._cmd_list(event):
            yield item

    @gperm.command("add")
    async def gperm_add(self, event: AstrMessageEvent):
        """把成员加入白名单：gperm add @某人 或 gperm add 123456"""
        async for item in self._cmd_add(event):
            yield item

    @gperm.command("del")
    async def gperm_del(self, event: AstrMessageEvent):
        """把成员移出白名单：gperm del @某人 或 gperm del 123456"""
        async for item in self._cmd_del(event):
            yield item

    @gperm.command("cmd")
    async def gperm_cmd(self, event: AstrMessageEvent):
        """维护命令白名单：gperm cmd add 签到 / gperm cmd del 签到"""
        async for item in self._cmd_cmd(event):
            yield item

    @gperm.command("reset")
    async def gperm_reset(self, event: AstrMessageEvent):
        """清除本群覆盖，恢复跟随全局默认"""
        async for item in self._cmd_reset(event):
            yield item

    async def terminate(self) -> None:
        """插件被卸载/重载时保存数据。"""
        try:
            self.store.save()
        except Exception:  # noqa: BLE001
            pass




__all__ = ["GroupPermissionPlugin"]
