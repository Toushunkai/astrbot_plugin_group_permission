"""插件 Pages 的后端 API。

这些方法不经 ``@filter`` 注册，而是由 ``_register_page_apis`` 通过
``context.register_web_api`` 动态挂载，因此可以独立成模块。"""

from __future__ import annotations

from typing import Any

import sys

from .constants import (
    DEFAULT_REPLY,
    ERR_JSON_BODY,
    NOTE_MAX_LENGTH,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    REPLY_STYLES,
    logger,
)
from .gate import (
    MODE_COMMAND_ONLY,
    MODE_LABELS,
    MODES,
    as_str_list,
    decide,
    normalize_group_role,
    normalize_user_id,
    render_reply,
)
from .store import OVERRIDE_KEYS, make_group_key

try:  # AstrBot >= 4.24 的插件 Pages 后端 API
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001 - 低版本 AstrBot 仍可用指令与拦截功能
    WEB_API_AVAILABLE = False


class PageApiMixin:
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
            return error_response(ERR_JSON_BODY, status_code=400)
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
            return error_response(ERR_JSON_BODY, status_code=400)
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
            return error_response(ERR_JSON_BODY, status_code=400)
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
