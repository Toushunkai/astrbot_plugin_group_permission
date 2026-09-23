"""群聊权限门禁的「判定核心」。

这个模块刻意不 import 任何 AstrBot 的东西：这里只有纯函数，
输入「谁、在哪个群、发了什么」，输出「放行 / 拦截 / 怎么拦」。
这样做的好处是判定规则可以脱离 AstrBot 运行时直接单测（见 _tests/）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# --------------------------------------------------------------------- 常量

# 判定结果的动作
ACTION_ALLOW = "allow"  # 授权用户：完全放行，一切照旧
ACTION_BLOCK_LLM_ONLY = "block_llm_only"  # 未授权：只禁止默认 AI 对话，其它插件照常
ACTION_ALLOW_COMMAND = "allow_command"  # 未授权但命中了命令白名单：放行该指令
ACTION_BLOCK_SILENT = "block_silent"  # 未授权：静默丢弃
ACTION_BLOCK_REPLY = "block_reply"  # 未授权：回一条固定文案再丢弃

# 处理模式（每个群可单独设置）
MODE_CHAT_ONLY = "chat_only"  # 仅拦截 AI 对话
MODE_COMMAND_ONLY = "command_only"  # 仅放行指定命令
MODE_SILENT = "silent"  # 静默忽略
MODE_REPLY = "reply"  # 回复固定文案
MODES: tuple[str, ...] = (MODE_CHAT_ONLY, MODE_COMMAND_ONLY, MODE_SILENT, MODE_REPLY)
MODE_LABELS = {
    MODE_CHAT_ONLY: "仅拦截 AI 对话（其它插件照常工作）",
    MODE_COMMAND_ONLY: "仅放行指定命令（其余静默忽略）",
    MODE_SILENT: "静默忽略（什么都不做）",
    MODE_REPLY: "回复固定文案（提示无权限）",
}

# 群内角色
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_LABELS = {ROLE_OWNER: "群主", ROLE_ADMIN: "群管理", ROLE_MEMBER: "普通群员"}

# 允许出现在命令前的符号：不同适配器/用户习惯不一样，统一剥掉再比对
_LEADING_PREFIX_CHARS = "/／!#.。、，,"
_SPACES = (" ", "\u3000", "\t")

_ACTION_TABLE = {
    ACTION_ALLOW: (False, False),  # (是否禁止默认 AI, 是否中断事件)
    ACTION_BLOCK_LLM_ONLY: (True, False),
    ACTION_ALLOW_COMMAND: (True, False),
    ACTION_BLOCK_SILENT: (True, True),
    ACTION_BLOCK_REPLY: (True, True),
}


@dataclass(frozen=True)
class Decision:
    """一条消息的判定结果。"""

    action: str
    reason: str
    matched_command: str | None = None

    @property
    def blocks_llm(self) -> bool:
        """是否要禁止 AstrBot 默认的 LLM 对话链路（即「唤醒词也不响应」）。"""
        return _ACTION_TABLE.get(self.action, (False, False))[0]

    @property
    def halts_event(self) -> bool:
        """是否要 event.stop_event()（连其它插件一起拦掉）。"""
        return _ACTION_TABLE.get(self.action, (False, False))[1]

    @property
    def allowed(self) -> bool:
        return self.action in (ACTION_ALLOW, ACTION_ALLOW_COMMAND)

    @property
    def should_reply(self) -> bool:
        return self.action == ACTION_BLOCK_REPLY


# ------------------------------------------------------------------ 小工具


def _get(obj: Any, key: str) -> Any:
    """同时兼容 dict 与对象两种取值方式（不同适配器给的 raw_message 形态不同）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def as_str_list(value: Any) -> list[str]:
    """把配置里的一坨东西（list / 逗号分隔字符串 / 换行分隔字符串）整理成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(",", "\n").replace("，", "\n").replace(" ", "\n")
        items = raw.split("\n")
    elif isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            if isinstance(item, str):
                items.extend(item.replace(",", "\n").replace("，", "\n").split("\n"))
            elif item is not None:
                items.append(str(item))
    else:
        items = [str(value)]
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_user_id(value: Any) -> str:
    """用户 ID 归一化：去掉常见前缀写法（如 "qq:123" / "@123"）与空白。"""
    text = str(value or "").strip()
    if not text:
        return ""
    for prefix in ("qq:", "QQ:", "uid:", "UID:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip().lstrip("@").strip()


def normalize_group_role(value: Any) -> str | None:
    """把各平台五花八门的角色值归一化成 owner / admin / member。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in ("owner", "群主", "群owner"):
        return ROLE_OWNER
    if text in ("admin", "administrator", "setadmin", "群管理", "管理员"):
        return ROLE_ADMIN
    if text in ("member", "normal", "everyone", "群员", "成员", "普通成员"):
        return ROLE_MEMBER
    return None


def role_from_raw_message(raw_message: Any) -> str | None:
    """从平台原始事件里取发送者的群角色。

    OneBot v11（aiocqhttp / Lagrange / NapCat 等）的群消息事件自带
    ``sender.role``（owner / admin / member），这是最便宜也最准的来源，
    不需要额外调用任何接口。
    """
    return normalize_group_role(_get(_get(raw_message, "sender"), "role"))


def role_from_group_object(group: Any, user_id: str) -> str | None:
    """从 AstrBotMessage.group 上取角色（部分适配器会填 group_owner / group_admins）。"""
    if group is None or not user_id:
        return None
    uid = str(user_id)
    owner = _get(group, "group_owner")
    if owner is not None and str(owner) == uid:
        return ROLE_OWNER
    for admin in as_str_list(_get(group, "group_admins")):
        if normalize_user_id(admin) == uid:
            return ROLE_ADMIN
    return None


def normalize_command_text(text: Any) -> str:
    """归一化命令文本：去空白、剥掉命令前缀符号。"""
    value = str(text or "").strip()
    while value and value[0] in _LEADING_PREFIX_CHARS:
        value = value[1:].strip()
    return value


def command_name(message_text: Any) -> str:
    """取消息的第一个词作为命令名（用于日志/展示）。"""
    text = normalize_command_text(message_text)
    if not text:
        return ""
    for sep in _SPACES:
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return text


def match_command(message_text: Any, whitelist: Sequence[str]) -> str | None:
    """判断这条消息是否命中了命令白名单，命中则返回命中的那一条。

    规则（两边都先归一化，大小写不敏感）：
    - 白名单里的 ``*`` 表示放行全部命令；
    - 完全相等算命中；
    - 后面跟空格（半角/全角/Tab）再带参数也算命中，例如白名单写 ``签到``
      可以命中 ``签到 1``。
    """
    text = normalize_command_text(message_text).lower()
    if not text:
        return None
    for entry in whitelist:
        candidate = normalize_command_text(entry).lower()
        if not candidate:
            continue
        if candidate == "*":
            return "*"
        if text == candidate:
            return candidate
        for sep in _SPACES:
            if text.startswith(candidate + sep):
                return candidate
    return None


# --------------------------------------------------------------- 配置合并


def pick(override: Any, fallback: Any) -> Any:
    """按群覆盖值优先、全局默认值兜底的顺序取值（None 表示「跟随全局」）。"""
    return fallback if override is None else override


@dataclass
class EffectiveSettings:
    """某个群最终生效的设置（全局默认 + 按群覆盖 合并后的结果）。"""

    enabled: bool
    allow_owner_admin: bool
    allow_global_admin: bool
    mode: str
    reply_template: str
    whitelist: list[str]
    command_whitelist: list[str]
    source: str = "global"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "allow_owner_admin": self.allow_owner_admin,
            "allow_global_admin": self.allow_global_admin,
            "mode": self.mode,
            "reply_template": self.reply_template,
            "whitelist": list(self.whitelist),
            "command_whitelist": list(self.command_whitelist),
            "source": self.source,
        }


def merge_settings(global_cfg: dict, override: dict | None) -> EffectiveSettings:
    """把全局默认配置和某个群的覆盖配置合并成最终生效的设置。"""
    override = override or {}
    has_override = bool(override)
    mode = str(pick(override.get("mode"), global_cfg.get("mode", MODE_COMMAND_ONLY)) or "")
    if mode not in MODES:
        mode = MODE_COMMAND_ONLY
    return EffectiveSettings(
        enabled=bool(pick(override.get("enabled"), global_cfg.get("enabled", True))),
        allow_owner_admin=bool(
            pick(override.get("allow_owner_admin"), global_cfg.get("allow_owner_admin", True))
        ),
        allow_global_admin=bool(
            pick(
                override.get("allow_global_admin"),
                global_cfg.get("allow_global_admin", True),
            )
        ),
        mode=mode,
        reply_template=str(
            pick(override.get("reply"), global_cfg.get("reply", "")) or ""
        ),
        whitelist=as_str_list(
            pick(override.get("whitelist"), global_cfg.get("whitelist", []))
        ),
        command_whitelist=as_str_list(
            pick(
                override.get("command_whitelist"),
                global_cfg.get("command_whitelist", []),
            )
        ),
        source="group" if has_override else "global",
    )


# ------------------------------------------------------------------ 判定


def decide(
    *,
    is_group: bool,
    user_id: str,
    group_role: str | None,
    is_global_admin: bool,
    message_text: str,
    settings: EffectiveSettings,
) -> Decision:
    """核心判定：这条群消息该放行还是拦截。"""
    if not is_group:
        return Decision(ACTION_ALLOW, "非群聊消息，本插件不干预")

    if not settings.enabled:
        return Decision(ACTION_ALLOW, "本群未启用群聊权限门禁")

    uid = normalize_user_id(user_id)
    role = normalize_group_role(group_role)

    # ---- 授权判定：管理员开关 -> 白名单（顺序只影响日志里的原因，不影响结果）
    if settings.allow_global_admin and is_global_admin:
        return Decision(ACTION_ALLOW, "发送者是 AstrBot 全局管理员")
    if uid and uid in {normalize_user_id(item) for item in settings.whitelist}:
        return Decision(ACTION_ALLOW, f"发送者 {uid} 在群白名单中")
    if settings.allow_owner_admin and role == ROLE_OWNER:
        return Decision(ACTION_ALLOW, "发送者是群主")
    if settings.allow_owner_admin and role == ROLE_ADMIN:
        return Decision(ACTION_ALLOW, "发送者是群管理")

    # ---- 未授权：按模式决定怎么处理
    if role in (ROLE_OWNER, ROLE_ADMIN) and not settings.allow_owner_admin:
        base_reason = "发送者是群主/群管理，但「放行群主与群管理」开关已关闭"
    else:
        base_reason = f"发送者 {uid or '未知'} 既不是群主/群管理，也不在白名单中"

    mode = settings.mode
    if mode == MODE_CHAT_ONLY:
        return Decision(ACTION_BLOCK_LLM_ONLY, base_reason + "；仅拦截 AI 对话")

    if mode == MODE_COMMAND_ONLY:
        hit = match_command(message_text, settings.command_whitelist)
        if hit:
            return Decision(
                ACTION_ALLOW_COMMAND,
                f"{base_reason}；但命中命令白名单「{hit}」，放行该指令",
                matched_command=hit,
            )
        return Decision(ACTION_BLOCK_SILENT, base_reason + "；且未命中命令白名单，静默忽略")

    if mode == MODE_REPLY:
        return Decision(ACTION_BLOCK_REPLY, base_reason + "；回复固定文案后拦截")

    return Decision(ACTION_BLOCK_SILENT, base_reason + "；静默忽略")


def render_reply(
    template: str,
    *,
    user_name: str = "",
    user_id: str = "",
    group_id: str = "",
    commands: Sequence[str] | None = None,
    mode: str = "",
) -> str:
    """渲染兜底文案里的占位符。"""
    text = str(template or "")
    command_list = "、".join(commands or []) or "（未配置）"
    replacements = {
        "{user}": user_name or user_id or "你",
        "{user_id}": user_id,
        "{group}": group_id,
        "{group_id}": group_id,
        "{commands}": command_list,
        "{mode}": MODE_LABELS.get(mode, mode),
    }
    for key, value in replacements.items():
        text = text.replace(key, str(value))
    return text.strip()
