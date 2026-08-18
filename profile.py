"""用户档案数据库：每个用户一个 md 档案，LLM 定期总结特征，管理员可查/增/删。"""

from __future__ import annotations

import datetime
import re
from typing import Any

from astrbot.api import logger

try:
    from . import store
    from .llm import llm_call, llm_retry_count
except ImportError:
    import store
    from llm import llm_call, llm_retry_count


def on_user_message(qq: str, name: str, text: str) -> None:
    """学生发消息后：收录到档案对话摘录，累计计数。"""
    store.ensure_user_file(qq, name)
    store.append_profile_chat(qq, f"- {text}")
    store.bump_profile_pending(qq)


def should_auto_summarize(qq: str, interval: int = 0) -> bool:
    """是否到达自动特征总结条件：累积条数达阈值，且距上次总结超过 1 小时。"""
    import datetime

    data = store._load_json(store.DATA_DIR / "profile_pending.json", {})
    if int(data.get(qq, 0)) < store.PROFILE_SUMMARY_HINT:
        return False
    last = _last_profile_summary(qq)
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except Exception:
        return True
    return (datetime.datetime.now() - last_dt).total_seconds() > 3600


def _last_profile_summary(qq: str) -> str | None:
    data = store._load_json(store.DATA_DIR / "profile_summary_time.json", {})
    return data.get(qq)


def _mark_profile_summary(qq: str) -> None:
    from .llm import now_iso

    data = store._load_json(store.DATA_DIR / "profile_summary_time.json", {})
    data[qq] = now_iso()
    store._save_json(store.DATA_DIR / "profile_summary_time.json", data)


async def auto_summarize_profile(context: Any, config: dict, qq: str) -> None:
    """用 LLM 把对话摘录总结为特征条目，写入档案。"""
    try:
        profile = store.read_profile(qq)
        excerpt = _extract_excerpt(profile)
        if not excerpt:
            store.clear_profile_pending(qq)
            return
        prompt = config.get("profile_prompt", "").replace("{chats}", excerpt)
        result = await llm_call(
            context,
            config.get("llm_model", ""),
            prompt,
            max_retry=llm_retry_count(config),
        )
        if result.strip():
            store.add_profile_entry(
                qq, f"（{datetime.datetime.now():%m-%d} 特征）{result.strip()}"
            )
        store.clear_profile_pending(qq)
        _mark_profile_summary(qq)
    except Exception as e:
        logger.warning(f"teacher_manager: auto_summarize_profile failed: {e}")


def _extract_excerpt(profile: str) -> str:
    m = re.search(r"## 对话摘录\s*(.*)", profile, re.S)
    if m:
        lines = [ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip()]
        return "\n".join(lines[-30:])
    return ""


async def query_profile(context: Any, config: dict, qq: str) -> str:
    """管理员查询用户档案：读档案文件，必要时请 LLM 结合对话摘录回答。"""
    profile = store.read_profile(qq)
    if not profile:
        return f"没有找到 {qq} 的档案。"
    return profile


def parse_qq_or_alias(alias_map, token: str, group_map=None) -> str | None:
    """把称呼/QQ 转成 QQ：支持纯数字 QQ（丢弃非数字后缀）或别名映射。"""
    alias_map = store.normalize_alias_map(alias_map, group_map)
    # 别名优先
    for k, v in alias_map.items():
        if token == k:
            # 值形如 aiocqhttp:FriendMessage:789 或 789
            m = re.search(r":(\d+)$", str(v))
            return m.group(1) if m else str(v)
    m = re.match(r"(\d{5,})", token)
    return m.group(1) if m else None


async def handle_profile_command(
    event: Any, context: Any, config: dict, parts: list[str]
) -> str | None:
    """处理 /档案 指令：show / add / del。返回要发送的结果文本。"""
    if not parts:
        return "用法：/档案 <QQ或称呼> [show|add <内容>|del <关键词>]"
    qq = parse_qq_or_alias(
        config.get("alias_map"), parts[0], config.get("alias_group_map")
    )
    if not qq:
        return f"无法识别对象「{parts[0]}」，请在配置面板的称呼映射中添加，或直接使用 QQ 号。"
    sub = parts[1] if len(parts) > 1 else "show"
    if sub == "add" and len(parts) > 2:
        store.add_profile_entry(qq, " ".join(parts[2:]))
        return f"已添加到 {qq} 的档案。"
    if sub == "del" and len(parts) > 2:
        ok = store.delete_profile_entry(qq, " ".join(parts[2:]))
        return (
            f"已从 {qq} 档案中删除相关条目。"
            if ok
            else f"在 {qq} 档案中未找到「{' '.join(parts[2:])}」。"
        )
    return (
        await query_profile(context, config, qq)
        if sub == "show"
        else "用法：/档案 <QQ或称呼> [show|add <内容>|del <关键词>]"
    )
