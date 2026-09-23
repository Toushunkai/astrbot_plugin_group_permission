"""拦截判定与兜底闸门的公共逻辑。

带 ``@filter`` 装饰器的三个 handler 留在主模块（AstrBot 按 handler 所在模块绑定实例），
它们只做转发；判定、文案组装、冷却与日志在这里。"""

from __future__ import annotations

import time
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from .constants import COOLDOWN_CACHE_LIMIT, DEFAULT_REPLY, SEEN_FLUSH_INTERVAL, logger
from .gate import (
    ACTION_ALLOW,
    ACTION_ALLOW_COMMAND,
    Decision,
    ROLE_LABELS,
    decide,
    normalize_user_id,
    render_reply,
)


class EnforcementMixin:
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

    async def _should_block_llm(self, event: AstrMessageEvent) -> bool:
        """要不要取消这次 LLM 请求。"""
        if not self._global_cfg()["enable"]:
            return False
        if event.get_extra("_gpm_cmd_allowed"):
            return False  # 命令白名单放行的命令，允许它自己用 LLM
        if event.get_extra("_gpm_unauthorized"):
            return True  # 主拦截器已经判过了
        return await self._fallback_unauthorized(event)

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
        """依次尝试 引用 → @ → 普通，返回是否发送成功。"""
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

    def _maybe_flush_seen(self) -> None:
        """「见过的群」攒够一段时间再落盘，避免每条消息都写文件。"""
        if not self._seen_dirty:
            return
        now = time.time()
        if now - self._seen_flushed_at < SEEN_FLUSH_INTERVAL:
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
