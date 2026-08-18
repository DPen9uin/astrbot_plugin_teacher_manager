"""聊天内容总结：管理员要求（或定时规则）→ 征求目标方同意 → LLM 总结 → 发管理员。

同意机制：
  - 群聊目标：目标群内群主/管理员回复「同意」生效
  - 私聊目标：对象本人回复「同意」生效
  - 同意后记录有效期（agreement_valid_days），有效期内定时总结免确认
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Plain

try:
    from . import store
    from .dispatch import _disp_target, _teacher_session, fmt_hours, resolve_target
    from .llm import llm_call, llm_retry_count
except ImportError:
    import store
    from dispatch import _disp_target, _teacher_session, fmt_hours, resolve_target
    from llm import llm_call, llm_retry_count

SUMMARY_CONFIRM_WORDS = {"同意", "可以", "ok", "没问题", "同意总结"}


def get_out_dir() -> Path:
    """打包输出目录（统一在 DATA_DIR 下）。"""
    return store.DATA_DIR / "out"


# 定时规则类型
RULE_TYPES = ("hourly", "daily", "weekly")


def parse_window(text: str, default_hours: float) -> float:
    """解析“最近 X 小时/天/周” → 小时数；解析失败用默认。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(小时|天|周)", text or "")
    if not m:
        return float(default_hours or 24)
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "小时":
        return max(1.0, n)
    if unit == "天":
        return max(1.0, n * 24)
    return max(1.0, n * 168)


def parse_rule(text: str) -> dict | None:
    """解析“每天/每周日 晚上8点 总结 <对象>” → {type, target, hour, weekday}。"""
    text = text.strip()
    m = re.search(r"(每天|每周[一二三四五六日天]?|每周\d)", text)
    if not m:
        return None
    freq = m.group(1)
    if freq == "每天":
        rule_type = "daily"
        weekday = None
    else:
        rule_type = "weekly"
        wd_map = {
            "一": 0,
            "二": 1,
            "三": 2,
            "四": 3,
            "五": 4,
            "六": 5,
            "日": 6,
            "天": 6,
        }
        wd_char = freq[-1]
        weekday = wd_map.get(wd_char, 0)

    hour = 20  # 默认
    hm = re.search(r"(\d{1,2})\s*[点时:：]\s*(\d{0,2})", text)
    if hm:
        hour = int(hm.group(1))
        if hour <= 12 and re.search(r"下午|傍晚|晚上|晚间|晚\d|pm|PM", text):
            hour += 12
        if hour > 23:
            hour = hour % 24
    minute = int(hm.group(2)) if hm and hm.group(2) else 0

    target = None
    for token in re.split(r"[，,、。;；]", text):
        tk = token.strip()
        if not tk or tk == freq:
            continue
        # 目标一般在“总结”之后
        m2 = re.search(r"总结\s*([^\s，,。]+)", tk)
        if m2:
            target = m2.group(1)
            break
    if not target:
        return None
    return {
        "type": rule_type,
        "hour": hour,
        "minute": minute,
        "weekday": weekday,
        "target_token": target,
    }


def due_check(rule: dict, now: datetime.datetime | None = None) -> bool:
    """判断规则在当前时刻是否应触发。"""
    now = now or datetime.datetime.now()
    if now.hour != int(rule.get("hour", 20)) or now.minute != int(
        rule.get("minute", 0)
    ):
        return False
    if rule.get("type") == "daily":
        return True
    if rule.get("type") == "weekly":
        return now.weekday() == int(rule.get("weekday", 0))
    return False


# ---------------- 同意 ----------------


def consent_valid(session_key: str, valid_days: int) -> bool:
    """判断总结同意是否仍有效。valid_days<=0 表示永久有效，登记过即视为有效。"""
    ts = store.consent_get(session_key)
    if not ts:
        return False
    if valid_days and valid_days > 0:
        try:
            dt = datetime.datetime.fromisoformat(ts)
        except Exception:
            return False
        return (datetime.datetime.now() - dt).total_seconds() < valid_days * 86400
    return True


def is_group_admin(event: Any) -> bool:
    """判断发送者是否为群主/管理员。

    兼容多种信息来源（aiocqhttp 的 event.sender 是 MessageMember，
    只有 user_id/nickname，QQ 群角色在 OneBot 原始事件的 sender.role）：
      1) event.sender.role（部分平台提供）
      2) event.role == "admin"（AstrBot 全局管理员，waking_check 注入）
      3) event.message_obj.raw_message.sender.role（OneBot 标准字段）
    """
    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", "") or "")
    if role in ("owner", "admin", "administrator"):
        return True
    if str(getattr(event, "role", "") or "") == "admin":
        return True
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is not None:
            role = str((raw.get("sender", {}) or {}).get("role", "") or "")
            if role in ("owner", "admin", "administrator"):
                return True
    except Exception:
        pass
    return False


def is_target_person(event: Any, target: str) -> bool:
    """私聊目标：消息来源会话就是目标会话。"""
    return getattr(event, "unified_msg_origin", "") == target


async def request_consent(context: Any, event: Any, target: str) -> None:
    """向目标会话发送总结申请。"""
    await context.send_message(
        target,
        MessageChain(
            [
                Plain(
                    "【总结申请】教师希望总结本会话最近一段时间的聊天内容（仅教师可见）。"
                    "同意请回复「同意」——群聊请由群主/管理员回复，私聊请本人回复。"
                )
            ]
        ),
    )


# ---------------- 执行总结 ----------------


async def do_summary(
    context: Any, config: dict, teacher_qq: str, target: str, hours: float
) -> str:
    """执行总结并发送给管理员。返回状态文本。"""
    try:
        lines = store.chatlog_recent(target, hours)
    except Exception as e:
        logger.warning(f"teacher_manager: chatlog read failed: {e}")
        lines = []
    if not lines:
        return f"「{_disp_target(config, target)}」最近 {fmt_hours(hours)}内没有可总结的聊天记录。"
    chat_text = "\n".join(
        f"[{ln['time'][5:16]}] {ln['name']}({ln['qq']}): {ln['text']}" for ln in lines
    )
    # 按发言人分别总结：在用户自定义提示词前附加分组指令
    group_instruction = (
        "请对聊天记录中的每位发言者分别总结：按发言人分组，每组先写「姓名(QQ)」再列要点"
        "（3~5 条，包含学情信号、常问问题、薄弱点、表现），必须覆盖全部发言人，不得遗漏。\n\n"
    )
    prompt = config.get("summarize_prompt", "").replace("{chat}", chat_text)
    prompt = group_instruction + prompt
    try:
        result = await llm_call(
            context,
            config.get("llm_model", ""),
            prompt,
            max_retry=llm_retry_count(config),
        )
    except Exception as e:
        logger.warning(f"teacher_manager: summarize failed: {e}")
        result = f"❌ 总结失败（LLM 调用异常：{e}）"
    await context.send_message(
        _teacher_session(target, teacher_qq),
        MessageChain(
            [
                Plain(
                    f"【聊天总结】{_disp_target(config, target)}（近 {fmt_hours(hours)}）\n\n{result}"
                )
            ]
        ),
    )
    if config.get("include_raw_material", False):
        path = _pack_raw(target, lines)
        await context.send_message(
            _teacher_session(target, teacher_qq),
            MessageChain(
                [File(name=f"{target.split(':')[-1]}_chatlog.md", file=str(path))]
            ),
        )
    return (
        f"✅ 已发送「{_disp_target(config, target)}」近 {fmt_hours(hours)}的聊天总结。"
    )


def _pack_raw(target: str, lines: list[dict]) -> Path:
    get_out_dir().mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_:]", "_", target)
    path = get_out_dir() / f"{safe}_chatlog.md"
    text = "\n".join(
        f"[{ln['time']}] {ln['name']}({ln['qq']}): {ln['text']}" for ln in lines
    )
    path.write_text(f"# 聊天记录：{target}\n\n{text}\n", encoding="utf-8")
    return path


# ---------------- 定时规则 ----------------


async def run_due_rules(context: Any, config: dict, platform: str = "default") -> None:
    """后台任务：执行到点的定时总结。"""
    rules = store.rule_all()
    now = datetime.datetime.now()
    for rid, rule in rules.items():
        if not due_check(rule, now):
            continue
        last = rule.get("last_run", "")
        if last and last[:16] == now.isoformat(timespec="minutes")[:16]:
            continue  # 同一分钟内不重复
        target = resolve_target(config, rule.get("target_token", ""), platform)
        if not target:
            logger.warning(
                f"teacher_manager: 规则 {rid} 目标无法解析：{rule.get('target_token')}"
            )
            continue
        if config.get("summary_need_agree", True):
            if not consent_valid(
                target, float(config.get("agreement_valid_days", 30) or 30)
            ):
                # 到点但无有效同意：通知老师（同一天只提醒一次），不静默跳过
                today = now.strftime("%Y-%m-%d")
                if rule.get("last_notify") != today:
                    rule["last_notify"] = today
                    rule["last_run"] = now.isoformat(timespec="seconds")
                    store.rule_save(rule)
                    try:
                        days = float(config.get("agreement_valid_days", 30) or 30)
                        valid_txt = (
                            "永久"
                            if days <= 0
                            else (
                                f"{round(days * 24)} 小时"
                                if days < 1
                                else f"{days:.0f} 天"
                            )
                        )
                        await context.send_message(
                            _teacher_session(target, rule.get("teacher_qq", "")),
                            MessageChain(
                                [
                                    Plain(
                                        f"⏰ 定时总结到点，但「{_disp_target(config, target)}」没有有效同意，本次已跳过。\n"
                                        f"群聊请让群主/管理员、私聊请让本人回复「同意」，即可登记同意并立即总结；"
                                        f"当前同意有效期：{valid_txt}（agreement_valid_days 可调整）。"
                                    )
                                ]
                            ),
                        )
                        # 同时向目标方发出同意请求（同一天一次，随通知去重）
                        try:
                            await context.send_message(
                                target,
                                MessageChain(
                                    [
                                        Plain(
                                            f"📋 定时总结·同意请求\n"
                                            f"老师设置了定时总结「{rule.get('target_token', '')}」，"
                                            f"需要征得您的同意才能执行。\n"
                                            f"群聊请群主/管理员、私聊请本人回复「同意」即可授权"
                                            f"（同意有效期：{valid_txt}）。\n"
                                            f"不需要的话忽略本条即可，老师会收到未同意通知。"
                                        )
                                    ]
                                ),
                            )
                        except Exception as e:
                            logger.warning(f"teacher_manager: 同意请求发送失败: {e}")
                    except Exception as e:
                        logger.warning(f"teacher_manager: 通知无同意状态失败: {e}")
                continue
        summary_status = ""
        try:
            summary_status = await do_summary(
                context, config, rule.get("teacher_qq", ""), target, 24
            )
        except Exception as e:
            logger.warning(f"teacher_manager: 定时总结失败: {e}")
            rule["last_summary_error"] = now.isoformat(timespec="seconds")
        if summary_status.startswith("❌"):
            rule["last_summary_error"] = now.isoformat(timespec="seconds")
        else:
            rule["last_summary"] = now.isoformat(timespec="seconds")
        rule["last_run"] = now.isoformat(timespec="seconds")
        store.rule_save(rule)
