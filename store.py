"""按群配置的持久化。

全局默认值放在 AstrBot 的插件配置（``_conf_schema.json``）里，
而「某个群单独怎么配」放在插件数据目录的 ``group_settings.json``：

.. code-block:: json

    {
      "version": 1,
      "groups": {
        "123456789": {
          "note": "测试群",
          "enabled": true,
          "mode": "command_only",
          "whitelist": ["10001"],
          "command_whitelist": ["help", "sid"]
        }
      },
      "seen": { "123456789": { "name": "测试群", "last_seen": 1710000000 } }
    }

约定：某个键**不存在或为 null** 表示「跟随全局默认」，只有显式写入的值才算覆盖。
写入一律「先写临时文件再原子替换」，避免进程被杀时把数据文件写坏。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_group_permission"
DATA_FILENAME = "group_settings.json"
MAX_SEEN_GROUPS = 2000

# 允许写入的按群覆盖字段（白名单，防止页面/命令写入奇怪的东西）
OVERRIDE_KEYS = (
    "enabled",
    "allow_owner_admin",
    "allow_global_admin",
    "mode",
    "reply",
    "whitelist",
    "command_whitelist",
)


def resolve_data_dir(plugin_name: str = PLUGIN_NAME) -> Path:
    """定位插件数据目录，兼容不同版本的 AstrBot。"""
    try:  # AstrBot >= 4.x
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / plugin_name
    except Exception:  # noqa: BLE001 - 旧版本 / 单测环境
        pass
    try:  # 旧版 SDK
        from astrbot.api.star import StarTools  # type: ignore

        return Path(StarTools.get_data_dir(plugin_name))
    except Exception:  # noqa: BLE001
        return Path("data") / "plugin_data" / plugin_name


def make_group_key(platform: str | None, group_id: str | None) -> str:
    """群配置的存储键：``平台名:群号``。

    为什么要带平台：同一个群号在不同平台完全是两回事，白名单需要分开维护。
    不带平台的旧格式数据仍然可用，见 ``GroupStore.get_group``。
    """
    gid = str(group_id or "").strip()
    name = str(platform or "").strip()
    if not gid:
        return ""
    return f"{name}:{gid}" if name else gid


def split_group_key(key: str) -> tuple[str, str]:
    """把存储键拆回 ``(平台, 群号)``；旧格式（裸群号）的平台是空串。"""
    text = str(key or "")
    if ":" in text:
        platform, _, gid = text.partition(":")
        return platform, gid
    return "", text


class GroupStore:
    """``group_settings.json`` 的读写封装。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else resolve_data_dir() / DATA_FILENAME
        self.data: dict[str, Any] = {"version": 1, "groups": {}, "seen": {}}
        self.load()

    # ------------------------------------------------------------- 基础读写
    def load(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                with self.path.open(encoding="utf-8") as fp:
                    raw = json.load(fp)
                if isinstance(raw, dict):
                    self.data = self._sanitize(raw)
        except Exception as exc:  # noqa: BLE001 - 数据坏了也不能让插件起不来
            logger.warning("群聊权限门禁：读取 %s 失败，使用空配置：%s", self.path, exc)
        return self.data

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("群聊权限门禁：保存 %s 失败：%s", self.path, exc)
            return False

    @staticmethod
    def _sanitize(raw: dict[str, Any]) -> dict[str, Any]:
        groups_raw = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
        seen_raw = raw.get("seen") if isinstance(raw.get("seen"), dict) else {}
        groups: dict[str, dict] = {}
        for gid, item in groups_raw.items():
            if not isinstance(item, dict):
                continue
            cleaned = {k: v for k, v in item.items() if k in OVERRIDE_KEYS or k == "note"}
            if cleaned:
                groups[str(gid)] = cleaned
        seen: dict[str, dict] = {}
        for gid, item in seen_raw.items():
            if isinstance(item, dict):
                seen[str(gid)] = {
                    "name": str(item.get("name") or ""),
                    "last_seen": int(item.get("last_seen") or 0),
                }
        return {"version": 1, "groups": groups, "seen": seen}

    # ------------------------------------------------------------- 群配置
    def groups(self) -> dict[str, dict]:
        return self.data.get("groups", {})

    def get_group(self, key: str) -> dict:
        """取某个群的覆盖配置。

        ``key`` 是 ``平台名:群号``（见 ``make_group_key``）。如果精确匹配不到，
        再退回到「裸群号」的旧格式，保证升级前写的数据还能用。
        """
        groups = self.groups()
        text = str(key or "")
        if text in groups:
            return dict(groups[text])
        platform, gid = split_group_key(text)
        if gid and gid in groups:
            # 旧格式（裸群号）
            return dict(groups[gid])
        if gid and not platform:
            # 反向兼容：用裸群号查一个按平台存过的群；只有唯一匹配时才认，
            # 多个平台同群号时按「不确定」处理，避免串配置。
            matched = [k for k in groups if split_group_key(k)[1] == gid]
            if len(matched) == 1:
                return dict(groups[matched[0]])
        return {}

    def set_group(self, key: str, values: dict[str, Any]) -> dict:
        """写入某个群的覆盖配置（只接受白名单字段，None 表示删除该覆盖）。"""
        text = str(key or "")
        if not text:
            return {}
        current = dict(self.groups().get(text, {}))
        for name, value in (values or {}).items():
            if name not in OVERRIDE_KEYS and name != "note":
                continue
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
        if current:
            self.data.setdefault("groups", {})[text] = current
        else:
            self.data.setdefault("groups", {}).pop(text, None)
        return dict(current)

    def reset_group(self, key: str) -> None:
        """清除某个群的覆盖；连带清掉同群号的旧格式记录。"""
        groups = self.data.setdefault("groups", {})
        text = str(key or "")
        groups.pop(text, None)
        _, gid = split_group_key(text)
        if gid and gid != text:
            groups.pop(gid, None)

    def forget_group(self, key: str) -> None:
        """把某个群从列表里彻底删掉：连「见过的群」记录一起清掉。

        只调 ``reset_group`` 是不够的——群一旦发过消息就会被 ``touch_group``
        记进 ``seen``，那行会一直留在插件页面的列表里（看起来就是「删不掉」）。
        """
        self.reset_group(key)
        seen = self.data.setdefault("seen", {})
        text = str(key or "")
        seen.pop(text, None)
        _, gid = split_group_key(text)
        if gid and gid != text:
            seen.pop(gid, None)

    def prune_seen(self) -> int:
        """清理「只是见过、没有单独配置」的群记录，返回清掉的条数。"""
        configured = set(self.groups())
        seen = self.data.setdefault("seen", {})
        removed = [k for k in list(seen) if k not in configured]
        for key in removed:
            seen.pop(key, None)
        return len(removed)

    # ------------------------------------------------------------- 群列表
    def touch_group(self, key: str, name: str = "") -> None:
        """记录「见过这个群」，这样插件配置页能列出机器人待过的群。"""
        text = str(key or "").strip()
        if not text:
            return
        seen = self.data.setdefault("seen", {})
        item = seen.get(text) or {}
        item["last_seen"] = int(time.time())
        if name:
            item["name"] = str(name)
        seen[text] = item
        if len(seen) > MAX_SEEN_GROUPS:  # 防止无限膨胀
            ordered = sorted(
                seen.items(),
                key=lambda kv: int((kv[1] or {}).get("last_seen") or 0),
                reverse=True,
            )[:MAX_SEEN_GROUPS]
            self.data["seen"] = dict(ordered)

    def known_groups(self) -> list[dict]:
        """返回所有「配置过」或「见过」的群，配置过的排在前面。"""
        configured = self.groups()
        seen = self.data.get("seen", {})
        result: list[dict] = []
        for key in sorted(set(configured) | set(seen)):
            info = seen.get(key) or {}
            platform, gid = split_group_key(key)
            result.append(
                {
                    "key": key,
                    "platform": platform,
                    "group_id": gid,
                    "name": info.get("name") or "",
                    "last_seen": int(info.get("last_seen") or 0),
                    "configured": key in configured,
                    "override": dict(configured.get(key, {})),
                }
            )
        return result
