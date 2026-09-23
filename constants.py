"""插件的共享常量与配置键白名单。

单独成模块的原因：``main.py`` 只保留 AstrBot 的 handler 定义（带 ``@filter``
装饰器的方法必须留在插件主模块里，AstrBot 是按 ``handler_module_path`` 给
handler 绑定插件实例的），其余逻辑按功能拆到各个 mixin 模块，避免单文件过长。
"""

from __future__ import annotations

import logging
import sys

PLUGIN_NAME = "astrbot_plugin_group_permission"
PLUGIN_VERSION = "v1.0.4"

#: 拦截器默认优先级：sys.maxsize 保证排在内置插件（maxsize-1/-2）与普通插件（0）之前
INTERCEPT_PRIORITY = sys.maxsize

#: reply 模式的默认文案，支持 {user} {user_id} {group} {commands} {mode} 占位符
DEFAULT_REPLY = (
    "{user} 本群开启了对话权限：只有群主、群管理和白名单成员可以和我聊天。\n"
    "你可以使用这些指令：{commands}"
)

#: 固定文案的发送方式（引用 → @ → 普通，发送失败自动降级）
REPLY_STYLES = ("quote", "at", "plain")
REPLY_STYLE_LABELS = {
    "quote": "引用对方那条消息",
    "at": "@ 对方",
    "plain": "直接发（不引用也不 @）",
}

# ---- 允许通过插件页面写入的全局配置项（白名单，避免写入未知键）
BOOL_KEYS = frozenset(
    {
        "enable",
        "default_group_enabled",
        "allow_owner_admin",
        "allow_global_admin",
        "auto_query_group_info",
        "enable_log",
    }
)
INT_KEYS = {
    "priority": (0, sys.maxsize),
    "reply_cooldown": (0, 86400),
    "group_info_cache_ttl": (0, 86400),
    "group_info_timeout": (1, 60),
}
LIST_KEYS = frozenset({"whitelist", "command_whitelist"})
STR_KEYS = frozenset({"mode", "reply", "unknown_role_action", "reply_style"})

#: 「见过的群」落盘节流间隔（秒）
SEEN_FLUSH_INTERVAL = 60.0
#: 兜底文案冷却表的最大条目数，超过后按时间清理
COOLDOWN_CACHE_LIMIT = 5000
#: 群备注最大长度
NOTE_MAX_LENGTH = 64
#: 统一的分页/请求体校验报错
ERR_JSON_BODY = "请求体必须是 JSON 对象"

#: 全插件共用同一个 AstrBot 日志器（各模块从这里导入，避免重复定义）
logger = logging.getLogger("astrbot")
