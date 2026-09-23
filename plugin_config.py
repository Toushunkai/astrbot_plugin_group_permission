"""全局配置与按群配置的读取 / 收敛 / 落盘。

这些方法都挂在插件实例上（``self.config`` / ``self.store`` / ``self.context``），
因此以 mixin 形式提供，由主模块的插件类继承。"""

from __future__ import annotations

import sys

from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import (
    BOOL_KEYS,
    DEFAULT_REPLY,
    INT_KEYS,
    INTERCEPT_PRIORITY,
    LIST_KEYS,
    REPLY_STYLES,
    STR_KEYS,
    logger,
)
from .gate import MODE_COMMAND_ONLY, MODES, EffectiveSettings, as_str_list, merge_settings
from .store import make_group_key


class ConfigMixin:
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
                cfg.get("priority"), INTERCEPT_PRIORITY, 0, sys.maxsize
            ),
            "enable_log": self._as_bool(cfg.get("enable_log"), True),
        }

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        """群配置的存储键：``平台名:群号``（同一群号在不同平台是两回事）。"""
        return make_group_key(event.get_platform_name(), event.get_group_id())

    def _settings_for(self, key: str) -> EffectiveSettings:
        return merge_settings(self._global_cfg(), self.store.get_group(str(key)))

    # ================================================================= 优先级 / 自检
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
            if key in BOOL_KEYS:
                result[key] = self._as_bool(value, bool(current.get(key)))
            elif key in INT_KEYS:
                low, high = INT_KEYS[key]
                result[key] = self._as_int(value, int(current.get(key, low)), low, high)
            elif key in LIST_KEYS:
                result[key] = as_str_list(value)
            elif key in STR_KEYS:
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
