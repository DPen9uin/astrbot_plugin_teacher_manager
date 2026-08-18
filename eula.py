"""EULA 门禁：未同意的用户被拦截在 LLM 交互之外，只回复预设提示。"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

try:
    from . import store
except ImportError:
    import store


async def _reply(event, message: str) -> None:
    """统一回复入口：兼容部分平台缺失 event.reply 的情况。"""
    await event.send(MessageChain([Plain(message)]))


def has_consent(qq: str) -> bool:
    # 同意标记与档案目录独立：存在 data/consent/users.json
    data = store._load_json(store.CONSENT_DIR / "users.json", {})
    return bool(data.get(qq, False))


def grant_consent(qq: str) -> None:
    data = store._load_json(store.CONSENT_DIR / "users.json", {})
    data[qq] = True
    store._save_json(store.CONSENT_DIR / "users.json", data)


async def handle_eula(
    event: Any,
    config: dict,
    qq: str,
    text: str,
    name: str = "",
    is_group: bool = False,
    is_at: bool = False,
) -> bool:
    """EULA 门禁。返回 True 表示消息已被拦截（调用方应 stop_event）。"""
    if not config.get("eula_enabled", True):
        return False
    if has_consent(qq):
        return False

    agree_words = {"同意", "接受", "ok", "好", "可以", "yes", "y"}
    # 群聊中同意登记必须 @bot；但未同意者的消息即使未 @bot 也必须拦截，
    # 否则会绕过门禁直达 AstrBot 主智能体（LLM 会回复群聊消息），EULA 形同虚设。
    if is_group and not is_at:
        return True  # 静默拦截：不发送提示避免刷屏，仅阻止后续处理

    if text.strip().lower() in agree_words:
        grant_consent(qq)
        await _reply(event, "已记录你的同意，欢迎使用教学辅助智能体～")
        return True

    tip = config.get("eula_deny_tip", "").replace("{name}", name or qq)
    eula = config.get("eula_text", "")
    await _reply(event, f"{eula}\n\n---\n{tip}")
    return True
