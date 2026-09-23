"""发送者身份识别：AstrBot 全局管理员、群主、群管理、普通群员。

按「便宜 → 贵」的顺序取：消息自带的 ``sender.role``（OneBot v11）→ AstrBot 已填好的
群对象 → 平台接口（带 TTL 缓存）。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api.event import AstrMessageEvent

from .constants import logger
from .gate import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    as_str_list,
    normalize_user_id,
    role_from_group_object,
    role_from_raw_message,
)


class RoleResolverMixin:
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
