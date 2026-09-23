"""``gperm`` 管理指令的实现。

装饰器形式的指令注册必须写在主模块（AstrBot 按 handler 所在模块给插件实例绑定
handler），所以这里只提供逻辑方法 ``_cmd_*``，由主模块的薄壳 handler 转发。"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from astrbot.api.event import AstrMessageEvent

from .constants import DEFAULT_REPLY, PLUGIN_NAME, logger
from .gate import (
    MODE_LABELS,
    MODES,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_MEMBER,
    ROLE_OWNER,
    as_str_list,
    decide,
    normalize_command_text,
    normalize_user_id,
)
from .store import OVERRIDE_KEYS


class CommandMixin:
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

    async def _cmd_status(self, event: AstrMessageEvent):
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

    async def _cmd_debug(self, event: AstrMessageEvent):
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

    async def _cmd_on(self, event: AstrMessageEvent):
        """启用本群的群聊权限门禁"""
        async for item in self._set_enabled(event, True):
            yield item

    async def _cmd_off(self, event: AstrMessageEvent):
        """停用本群的群聊权限门禁"""
        async for item in self._set_enabled(event, False):
            yield item

    async def _cmd_mode(self, event: AstrMessageEvent):
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

    async def _cmd_list(self, event: AstrMessageEvent):
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

    async def _cmd_add(self, event: AstrMessageEvent):
        """把成员加入白名单：gperm add @某人 或 gperm add 123456"""
        async for item in self._whitelist_edit(event, add=True):
            yield item

    async def _cmd_del(self, event: AstrMessageEvent):
        """把成员移出白名单：gperm del @某人 或 gperm del 123456"""
        async for item in self._whitelist_edit(event, add=False):
            yield item

    async def _cmd_cmd(self, event: AstrMessageEvent):
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

    async def _cmd_reset(self, event: AstrMessageEvent):
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
