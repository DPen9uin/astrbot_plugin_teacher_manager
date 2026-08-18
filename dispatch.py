"""分发与任务收集：管理员推送 → 二次确认 → 主动发送 → 收集学生回复 → 按处理方式转发教师。

任务流程：
  1. 管理员表示“推送/通知/布置任务 + 内容 + 对象”（或 /推送 指令）
  2. 插件向管理员二次确认（内容 + 目标会话）
  3. 管理员回复“确认” → 主动发送；若判定为任务（含任务关键字）则开启回复收集
  4. 目标会话中学生/对象回复 → 记录（同一人覆盖最新）
  5. 教师“收/截止”或到达 deadline → 按处理方式汇总转发教师
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Plain

try:
    from . import store
    from .llm import llm_call, llm_retry_count, now_iso
except ImportError:
    import store
    from llm import llm_call, llm_retry_count, now_iso

TASK_KEYWORDS = ("任务", "作业", "提交", "收集", "回复我", "截止", "交上来", "发我")
DEADLINE_KEYWORDS = ("截止时间", "截止", "截至", "提交时间", "DDL", "deadline")


def _deadline_words(config: dict) -> tuple:
    """截止语境触发词：默认 + 配置自定义（兼容 list 与逗号分隔文本）。"""
    kws = config.get("deadline_keywords")
    if not kws:
        return DEADLINE_KEYWORDS
    if isinstance(kws, str):
        words = tuple(w.strip() for w in re.split(r"[,，、\n]+", kws) if w.strip())
        return words or DEADLINE_KEYWORDS
    return tuple(kws) or DEADLINE_KEYWORDS


# 关键词之外的任务句式（正则，命中即视为任务）
TASK_PATTERNS = (
    r"要求.{0,30}?(前|截止).{0,10}?(回复|提交|交|作答|回答|完成|发)",
    r"\d{1,2}[:：]\d{2}\s*前",  # 如 20:30前
    r"今天.{0,20}?前.{0,10}?(回复|提交|交|作答|回答|完成)",
    r"(截止|截至).{0,20}?(回复|提交|交|作答|回答|完成)",
)


def get_out_dir() -> Path:
    """打包输出目录（统一在 DATA_DIR 下）。"""
    return store.DATA_DIR / "out"


def _pending_path() -> Path:
    return store.DATA_DIR / "pending_confirm.json"


def _pending() -> dict:
    return store._load_json(_pending_path(), {})


def _save_pending(data: dict) -> None:
    store._save_json(_pending_path(), data)


# ---------------- 解析 ----------------


def fmt_hours(hours: float) -> str:
    """把小时数显示为友好文本：>=1 显示小时，<1 显示分钟。"""
    if hours < 1:
        return f"{round(hours * 60)} 分钟"
    return f"{hours:.0f} 小时"


def resolve_target(config: dict, token: str, platform: str = "default") -> str | None:
    """把对象称呼解析为 unified_msg_origin；支持配置别名、群号直用、介词前缀。"""
    alias_map = store.normalize_alias_map(
        config.get("alias_map"), config.get("alias_group_map"), platform
    )
    t = (token or "").strip()
    if not t:
        return None
    if t in alias_map:
        return str(alias_map[t])
    # 形如 default:GroupMessage:123456 直接可用
    if re.match(r"^[^:\s]+:[a-zA-Z]+:\d+$", t):
        return t
    if re.match(r"^\d{5,}$", t):
        return f"{platform}:GroupMessage:{t}"
    # 从尾巴切出对象候选：最后一个「介词/动词」之后的部分
    m = re.search(r"(?:推送给|发给|发到|到|给|对|向|通知)\s*([^\s，,。；;]+)$", t)
    if m:
        cand = m.group(1)
        for k, v in alias_map.items():
            if cand == k or (len(cand) >= 2 and k in cand):
                return str(v)
        if re.match(r"^\d{5,}$", cand):
            return f"{platform}:GroupMessage:{cand}"
        return None
    # 无介词：仅接受「别名+少量后缀」（如 小明同学），
    # 避免把整句（如「小明明天记得交作业」）误判为目标
    for k, v in alias_map.items():
        if t == k or (t.startswith(k) and len(t) <= len(k) + 3):
            return str(v)
    return None


def resolve_targets(config: dict, token: str, platform: str = "default") -> list[str]:
    """把可能包含多个对象的称呼解析为 UMO 列表。支持 、和与逗号分隔。"""
    if not token:
        return []
    parts = re.split(r"[、和与,，]\s*", token)
    targets = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        t = resolve_target(config, p, platform)
        if t:
            targets.append(t)
    return targets


def parse_dispatch_text(
    config: dict, text: str, platform: str = "default"
) -> tuple[str | None, list[str] | None]:
    """从自然语言中提取（内容, 目标会话列表）。返回 (content, targets_umo_list)。

    支持句式：
      把 XXX 推送给/发给/发到/通知 YYY        （对象在句尾）
      通知 YYY 内容 / 发到 YYY 内容           （对象紧跟动词后）
      通知小明、小红明天考试                   （多目标，用 、和与, 分隔）
      内容中含别名（配置 alias_map）           （兜底）
    """
    text = (text or "").strip()
    alias_map = store.normalize_alias_map(
        config.get("alias_map"), config.get("alias_group_map"), platform
    )
    aliases = sorted(alias_map.keys(), key=len, reverse=True)

    verbs = [
        "推送给",
        "发布给",
        "布置给",
        "发给",
        "发到",
        "告知",
        "告诉",
        "通知",
        "推送",
        "发布",
        "布置",
        "到",
        "给",
        "发",
    ]
    verb_re = "|".join(sorted(verbs, key=len, reverse=True))

    # 句式一：对象在句尾（贪婪 group1 保证取最右侧动词；优先排除作为内容名词的“通知”）
    verb_res = (
        "|".join(sorted([v for v in verbs if v != "通知"], key=len, reverse=True)),
        verb_re,
    )
    for vr in verb_res:
        # 逐动词按长度降序尝试：避免「推送给」被拆成 group1+「给」、内容残留「推送」
        for v in sorted([x for x in vr.split("|") if x], key=len, reverse=True):
            m = re.search(
                r"^(.*)\s*" + re.escape(v) + r"\s*([^\s，,。；;]+?)\s*$", text
            )
            if m:
                target = resolve_target(config, m.group(2), platform)
                if target:
                    content = re.sub(r"^(?:把|将|向)\s*", "", m.group(1)).strip()
                    if content:
                        return content, [target]
    # 句式二：对象紧跟动词后，其余为内容（目标和内容间允许逗号/空格分隔）
    m = re.search(
        r"^(?:把|将|向)?\s*(?:" + verb_re + r")\s*([^\s，,。；;]+)\s*[，,]?\s*(.+)$",
        text,
        re.S,
    )
    if m:
        target = resolve_target(config, m.group(1), platform)
        if target:
            return m.group(2).strip(), [target]
    # 多目标检测：内容中有多个别名用 、和与, 分隔
    present_aliases = [a for a in aliases if a in text]
    if len(present_aliases) >= 2:
        present_aliases.sort(key=lambda a: text.index(a))
        sep_re = r"[、和与,，]\s*"
        valid = True
        for i in range(len(present_aliases) - 1):
            a1, a2 = present_aliases[i], present_aliases[i + 1]
            idx1 = text.index(a1) + len(a1)
            idx2 = text.index(a2)
            gap = text[idx1:idx2]
            if not re.fullmatch(sep_re, gap):
                valid = False
                break
        if valid:
            targets = resolve_targets(config, "、".join(present_aliases), platform)
            if targets:
                remaining = text
                for a in present_aliases:
                    remaining = remaining.replace(a, "", 1)
                # 先打散去除别名后的残留前缀词
                remaining = re.sub(
                    r"^(?:请帮我|帮我|帮我给|帮我叫|帮我把|帮我跟|帮我向|帮我用|帮我通过)\s*",
                    "",
                    remaining,
                )
                remaining = re.sub(
                    r"^(?:把|将|向|给|推送给|送给|通知|推送|发布|布置|告知|告诉|发到|发|到)\s*",
                    "",
                    remaining,
                )
                remaining = re.sub(r"^[、和与,，]\s*", "", remaining)
                remaining = re.sub(r"^(?::|：)\s*", "", remaining)
                remaining = re.sub(
                    r"(?:推送给|发给|发到|通知|推送|发布|布置|告知|告诉|发|到|给)$",
                    "",
                    remaining,
                )
                remaining = remaining.strip(" \t，,。；;、：:\n\r").strip()
                if remaining:
                    return remaining.strip(), targets
    # 兜底：内容中含单一别名
    strip_verb_re = r"(?:推送给|发给|发到|通知|推送|发布|布置|告知|告诉|发|到|给)$"
    for alias in aliases:
        if alias in text:
            head, _, tail = text.partition(alias)
            head = re.sub(strip_verb_re, "", head.rstrip(" \t，,。 ；;：:"))
            head = re.sub(
                r"^(?:请帮我|帮我|帮我给|帮我叫|帮我把|帮我跟|帮我向|帮我用|帮我通过)\s*",
                "",
                head,
            ).strip()
            tail = tail.lstrip(" \t，,。 ；;：:")
            content = (head + " " + tail).strip(" \t，,。；;、：:\n\r").strip()
            target = resolve_target(config, alias, platform)
            if content and target:
                return content, [target]
    return (text, None)


def is_task_content(content: str, extra_words: tuple = ()) -> bool:
    """任务判定：任务关键词 / 截止触发词（DDL、deadline 等，含自定义）/ 任务句式。
    触发词是强任务语义（出现即代表有截止要求），但截止时间解析仍按 intent_mode 三档走。"""
    if any(k in content for k in TASK_KEYWORDS):
        return True
    if extra_words and any(k in content for k in extra_words):
        return True
    return any(re.search(p, content) for p in TASK_PATTERNS)


async def extract_deadline_llm(
    context: Any, model: str, content: str, session_umo: str = "", max_retry: int = 2
) -> datetime.datetime | None:
    """用 LLM 从任务内容中提取截止时间。返回 datetime 或 None。"""
    prompt = (
        "你是教学助手的截止时间解析器。从下面的任务内容中提取【截止/提交/交作业的时间点】，"
        "只输出 ISO 格式（如 2026-08-17T21:15:00）或 none（没有截止时间时）。\n"
        "今天是 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + "。\n"
        "规则：\n"
        "1. 以显式截止标记处的时间为准（DDL、截止时间、截止、提交时间、交作业等标记后的时间）；\n"
        "2. 题干、例题、场景描述里出现的时间（如『小明明天晚上10点前交』中的场景时间）不是截止时间，忽略；\n"
        "3. 纯通知性内容输出 none。\n"
        "内容：\n" + content
    )
    try:
        raw = await llm_call(
            context, model, prompt, session_umo=session_umo, max_retry=max_retry
        )
        raw = raw.strip().strip("`").strip()
        if not raw or raw.lower() in ("none", "null", "无", "没有"):
            return None
        return datetime.datetime.fromisoformat(raw)
    except Exception as e:
        logger.warning(f"teacher_manager: deadline LLM 提取失败: {e}")
        return None


def _resolve_deadline(
    day: datetime.date, tag: str, hour: int, minute: int
) -> datetime.datetime | None:
    """按标签（明天/今晚/晚上）归一化小时并处理跨日进位。"""
    if hour > 23:
        return None
    if "晚" in (tag or "") and hour <= 12:
        hour += 12  # 今晚/晚上 12点前 = 次日0点；8点前 = 20点前
    if hour >= 24:
        day = day + datetime.timedelta(days=1)
        hour -= 24
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def parse_deadline(content: str, trigger_words: tuple = ()) -> datetime.datetime | None:
    """从内容中解析截止时间，按优先级依次尝试：
    1. 「今天20:30前」「明天9点半前」「18:00前」（时间后带“前”）
    2. 「截止时间21:15」「截止到明天18:00」「截至21点」（截止/截至语境，无需“前”）
    3. 自定义触发词后跟随时刻（如「DDL 21:15」「deadline：明早8点」）
    """
    now = datetime.datetime.now()
    # 句式1：时间后带“前”
    m = re.search(r"(明天|今天|今晚|晚上)?\s*(\d{1,2})[:：](\d{2})\s*前", content)
    if m:
        day = now.date()
        if "明天" in (m.group(1) or ""):
            day = day + datetime.timedelta(days=1)
        return _resolve_deadline(day, m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(明天|今天|今晚|晚上)?\s*(\d{1,2})点(半|钟)?前", content)
    if m:
        day = now.date()
        if "明天" in (m.group(1) or ""):
            day = day + datetime.timedelta(days=1)
        return _resolve_deadline(
            day, m.group(1), int(m.group(2)), 30 if m.group(3) == "半" else 0
        )
    # 句式2：截止/截至语境（如「截止时间21:15」「截止到明天18:00」）
    m = re.search(
        r"(截止|截至)(时间|日期)?\s*[到于为是]?\s*(今天|明天|今晚|晚上)?\s*(\d{1,2})[:：](\d{2})",
        content,
    )
    if m:
        day = now.date()
        if "明天" in (m.group(3) or ""):
            day = day + datetime.timedelta(days=1)
        return _resolve_deadline(day, m.group(3), int(m.group(4)), int(m.group(5)))
    m = re.search(
        r"(截止|截至)(时间|日期)?\s*[到于为是]?\s*(今天|明天|今晚|晚上)?\s*(\d{1,2})点(半|钟)?",
        content,
    )
    if m:
        day = now.date()
        if "明天" in (m.group(3) or ""):
            day = day + datetime.timedelta(days=1)
        return _resolve_deadline(
            day, m.group(3), int(m.group(4)), 30 if m.group(5) == "半" else 0
        )
    # 句式3：触发词后跟随时刻（仅 keywords/hybrid 正则路径使用）
    for w in trigger_words:
        m = re.search(re.escape(w) + r"[\s:：]*(\d{1,2})[:：](\d{2})", content)
        if m:
            return _resolve_deadline(now.date(), "", int(m.group(1)), int(m.group(2)))
        m = re.search(
            re.escape(w) + r"[\s:：]*(今天|明天|今晚|晚上)?\s*(\d{1,2})点(半|钟)?",
            content,
        )
        if m:
            day = now.date()
            if "明天" in (m.group(1) or ""):
                day = day + datetime.timedelta(days=1)
            return _resolve_deadline(
                day, m.group(1), int(m.group(2)), 30 if m.group(3) == "半" else 0
            )
        # 句式4：标注后带完整日期，同行或下一行皆可（如「DDL：2026/8/18 0:05」「DDL：\n2026/8/18 0:05」）
        m = re.search(
            re.escape(w) + r"[\s:：]*\n*\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
            r"(?:\s*(?:今天|明天)?\s*(\d{1,2})[:：](\d{2}))?",
            content,
        )
        if m:
            try:
                day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            return datetime.datetime.combine(
                day,
                datetime.time(
                    int(m.group(4)) if m.group(4) else 23,
                    int(m.group(5)) if m.group(5) else 59,
                ),
            )
    return None


# ---------------- 二次确认 ----------------


def register_dispatch_pending(
    teacher_qq: str, content: str, targets: list[str]
) -> None:
    data = _pending()
    data.setdefault("dispatch", {})[teacher_qq] = {
        "content": content,
        "targets": targets,
        "at": now_iso(),
    }
    _save_pending(data)


def pop_dispatch_pending(teacher_qq: str) -> dict | None:
    data = _pending()
    item = data.get("dispatch", {}).get(teacher_qq)
    if item:
        data["dispatch"].pop(teacher_qq, None)
        _save_pending(data)
    return item


def clear_pending(teacher_qq: str) -> None:
    data = _pending()
    data.get("dispatch", {}).pop(teacher_qq, None)
    data.get("summary", {}).pop(teacher_qq, None)
    _save_pending(data)


# ---------------- 执行发送 ----------------


def split_deadline_mark(content: str, trigger_words: tuple) -> tuple:
    """把独立的截止标注行（可含下一行时间）从内容中拆出。
    返回 (标注原文, 剩余正文)；无标注时返回 (None, 原内容)。"""
    time_re = re.compile(
        r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s*)?(\d{1,2}月\d{1,2}日\s*)?(\d{1,2}[:：]\d{2}|\d{1,2}点(半|钟)?)"
    )
    date_re = re.compile(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$")

    def _is_standalone_time(rest: str) -> bool:
        """rest 整体就是时间（可带 今天/明天/今晚/晚上 前缀）。"""
        if time_re.fullmatch(rest):
            return True
        m = re.match(r"^(今天|明天|今晚|晚上)\s*", rest)
        return bool(m) and time_re.fullmatch(rest[m.end() :])

    lines = content.split("\n")
    out = []
    mark_lines = []
    skip_next = False
    for idx, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        s = line.strip()
        is_time_line = bool(time_re.search(s)) or bool(date_re.match(s))
        is_mark_line = False
        for w in trigger_words:
            if w in s:
                rest = s.replace(w, "").strip(" :：\t,，。;；、")
                # 标注行判定：除触发词外几乎无内容，或剩余部分本身就是时间（如「DDL：21:15」「DDL：晚上10点」）
                if len(rest) <= 4 or _is_standalone_time(rest):
                    is_mark_line = True
                    mark_lines.append(line)
                    # 本行无时间但下一行是时间行 → 一并拆出
                    if (
                        not is_time_line
                        and idx + 1 < len(lines)
                        and (
                            time_re.search(lines[idx + 1].strip())
                            or date_re.match(lines[idx + 1].strip())
                        )
                        and len(lines[idx + 1].strip()) <= 24
                    ):
                        mark_lines.append(lines[idx + 1])
                        skip_next = True
                    break
        if not is_mark_line:
            out.append(line)
    mark = "\n".join(mark_lines).strip()
    rest = "\n".join(out).strip()
    return (mark, rest) if mark else (None, rest)


async def do_dispatch(
    context: Any,
    config: dict,
    teacher_qq: str,
    content: str,
    targets: list[str],
    session_umo: str = "",
) -> str:
    """执行推送。返回插件对管理员展示的说明文本。支持多目标遍历。"""
    words = _deadline_words(config)
    is_task = is_task_content(content, words)
    push_content = content
    parsed_dl = None
    dl_source = ""
    if is_task:
        mode = str(config.get("intent_mode", "hybrid") or "hybrid")
        if mode in ("llm", "hybrid"):
            parsed_dl = await extract_deadline_llm(
                context,
                config.get("llm_model", ""),
                content,
                session_umo=session_umo,
                max_retry=llm_retry_count(config),
            )
            if parsed_dl is not None:
                dl_source = "LLM 解析"
        if parsed_dl is None and mode in ("keywords", "hybrid"):
            parsed_dl = parse_deadline(content, words)
            if parsed_dl is not None:
                dl_source = "从内容解析"
        mark_text, rest = split_deadline_mark(content, words)
        if mark_text is not None:
            if parsed_dl is not None:
                push_content = (
                    rest + "\n\n截止时间：" + parsed_dl.strftime("%m-%d %H:%M")
                ).strip()
            else:
                push_content = (rest + "\n\n" + mark_text).strip()

    sent_ok = 0
    failed_names = []
    first_task_id = None
    dl_show = ""

    for target in targets:
        chain = MessageChain([Plain(push_content)])
        ok = await context.send_message(target, chain)
        if not ok:
            failed_names.append(target)
            continue
        sent_ok += 1
        task_id = _new_task_id()
        if first_task_id is None:
            first_task_id = task_id
        sent_at = now_iso()
        task = {
            "id": task_id,
            "teacher_qq": teacher_qq,
            "target": target,
            "content": content,
            "process": "none",
            "process_desc": "",
            "deadline": None,
            "collecting": is_task,
            "is_task": is_task,
            "replies": [],
            "created": sent_at,
            "sent": True,
            "sent_at": sent_at,
            "closed": not is_task,
            "closed_at": sent_at if not is_task else None,
        }
        store.task_save(task)
        if is_task:
            hours = float(config.get("task_collect_hours", 48) or 0)
            if parsed_dl is not None:
                task["deadline"] = parsed_dl.isoformat(timespec="seconds")
                store.task_save(task)
                if not dl_show:
                    dl_show = f"· 截止时间（{dl_source}）：{parsed_dl:%m-%d %H:%M}\n"
            elif hours and hours > 0:
                deadline = datetime.datetime.now() + datetime.timedelta(hours=hours)
                task["deadline"] = deadline.isoformat(timespec="seconds")
                store.task_save(task)
                if not dl_show:
                    dl_show = (
                        f"· 默认窗口 {fmt_hours(hours)}（至 {deadline:%m-%d %H:%M}）\n"
                    )
            else:
                if not dl_show:
                    dl_show = "· 收集窗口：不限时（task_collect_hours=0），不自动截止\n"

    reply_lines = []
    if sent_ok > 0:
        reply_lines.append(
            f"✅ 已推送到「{'、'.join(_disp_target(config, t) for t in targets[:5])}{'…' if len(targets) > 5 else ''}」。"
        )
    if failed_names:
        reply_lines.append(
            f"❌ 发送失败：{'、'.join(_disp_target(config, t) for t in failed_names)}，请检查目标配置。"
        )
    if is_task and sent_ok > 0:
        reply_lines.append("检测到这是任务（含任务关键字），已开启回复收集：")
        if push_content != content:
            reply_lines.append("· 截止标注已与问题分开，规范化附在推送末尾，学生可见")
        reply_lines.append(
            f"· 共创建 {sent_ok} 个任务"
            + (f"（首个ID：{first_task_id}）" if first_task_id else "")
        )
        if dl_show:
            reply_lines.append(dl_show.rstrip("\n"))
        reply_lines.append(
            "· 对我说「收」或「截止」可立即结束收集，也可声明处理方式（打包/总结/评分）"
        )
    elif sent_ok == 0:
        reply_lines.append("❌ 所有目标发送失败。")
    return "\n".join(reply_lines)


def _new_task_id() -> str:
    import uuid

    return "t_" + uuid.uuid4().hex[:8]


# ---------------- 学生回复收集 ----------------


def collect_reply(
    task: dict, session_key: str, qq: str, name: str, text: str, hist_max: int = 8
) -> None:
    """记录一条回复：每条消息入 hist（保留最近 hist_max 条），text 保持最新一条供旧逻辑使用。

    注意：hist 条目永远是【不含 hist 键的纯消息副本】，避免 self-reference 导致 json.dump 循环引用。
    """
    if session_key != task["target"]:
        return
    repl = (
        dict(task.get("replies") or {}) if isinstance(task.get("replies"), dict) else {}
    )
    rec = {"name": name or qq, "text": text, "time": now_iso()}
    prev = repl.get(str(qq))
    hist = [dict(rec)]  # 纯副本，不含 hist 键
    if isinstance(prev, dict):
        old_hist = prev.get("hist")
        if isinstance(old_hist, list) and old_hist:
            hist = [_hist_item(h) for h in old_hist] + hist
            # 防御：旧数据 hist 缺失最后一条（prev.text）时补齐
            prev_txt = str(prev.get("text", "")).strip()
            if (
                prev_txt
                and prev_txt != text
                and prev_txt not in [h.get("text") for h in hist if isinstance(h, dict)]
            ):
                hist = [_hist_item(prev)] + hist
        elif str(prev.get("text", "")).strip() and prev.get("text") != text:
            hist = [_hist_item(prev)] + hist
    if hist_max and hist_max > 0 and len(hist) > hist_max:
        hist = hist[-hist_max:]
    rec["hist"] = hist
    repl[str(qq)] = rec
    task["replies"] = repl


def _hist_item(h) -> dict:
    """把历史条目消毒为纯消息副本（剥掉可能存在的 hist 键，防循环引用）。"""
    if isinstance(h, dict):
        return {"name": h.get("name"), "text": h.get("text"), "time": h.get("time")}
    return {}


def active_task_for_session(session_key: str) -> list[dict]:
    """找到目标会话为 session_key 且仍在收集的任务。"""
    return [
        t
        for t in store.task_all().values()
        if t.get("collecting") and t.get("target") == session_key
    ]


# ---------------- 结束收集与处理 ----------------


async def close_task(
    context: Any, config: dict, teacher_qq: str, task_id: str | None = None
) -> str:
    """结束任务收集并按处理方式汇总，发送给教师。"""
    if task_id:
        task = store.task_get(task_id)
        tasks = [task] if task and task.get("teacher_qq") == teacher_qq else []
    else:
        tasks = [
            t
            for t in store.task_all().values()
            if t.get("teacher_qq") == teacher_qq
            and t.get("collecting")
            and not t.get("closed")
        ]
    if not tasks:
        return "当前没有正在收集的任务。"
    msgs = []
    for task in tasks:
        task["collecting"] = False
        task["closed"] = True
        task["closed_at"] = now_iso()
        store.task_save(task)
        reply_text = await _process_task(context, config, task)
        await context.send_message(
            _teacher_session(task["target"], teacher_qq),
            MessageChain([Plain(reply_text)]),
        )
        tgt = _disp_target(config, task.get("target", ""))
        msgs.append(f"任务 {task['id']}（{tgt}）已结束并发送汇总。")
    return "\n".join(msgs)


def _teacher_session(target: str, teacher_qq: str) -> str:
    """向教师私聊发送：借用目标平台。"""
    platform = target.split(":", 1)[0]
    return f"{platform}:FriendMessage:{teacher_qq}"


def _alias_of(config: dict, qq) -> str:
    """从 alias_map 反查 QQ 号的映射称呼；无则返回空串。"""
    try:
        amap = config.get("alias_map", {}) or {}
        if isinstance(amap, dict):
            for k, v in amap.items():
                if str(v).strip() == str(qq).strip():
                    return str(k).strip()
    except Exception:
        pass
    return ""


def _disp_target(config: dict, target: str) -> str:
    """从 target UMO 中提取可读的别名/群名展示。"""
    if ":" not in target:
        return target
    target_umo = target  # 如 丰川祥子:GroupMessage:812805771
    # 用 normalize_alias_map 统一解析（兼容 str JSON + dict + list）
    am = store.normalize_alias_map(
        config.get("alias_map"),
        config.get("alias_group_map"),
        target.rsplit(":", 2)[0],  # platform
    )
    for alias, umo in am.items():
        if umo == target_umo:
            return str(alias)
    # 兜底：只比对末尾 ID
    target_id = target.rsplit(":", 1)[-1]
    for alias, umo in am.items():
        if umo.endswith(":" + target_id):
            return str(alias)
    return target_id


def _disp_name(config: dict, r, qq) -> str:
    """展示名：alias_map 称呼 > 记录的昵称 > QQ 号，统一追加 (QQ号)。"""
    alias = _alias_of(config, qq)
    name = str(r.get("name") or "").strip() if isinstance(r, dict) else ""
    if alias and alias != str(qq):
        base = alias
    elif name and name != str(qq):
        base = name
    else:
        base = str(qq)
    return f"{base}({qq})"


async def _pick_answers(context: Any, config: dict, task: dict) -> tuple[dict, bool]:
    """用 LLM 判断每位学生的哪些消息是对任务的回答（过滤闲聊/提问）。

    返回 (过滤后的 replies, 是否成功)；失败时调用方回退为原文转发。
    """
    replies = task.get("replies", {}) or {}
    if not replies:
        return {}, True
    hist_max = int(config.get("reply_hist_max", 8) or 8)
    blocks, index, seq = [], {}, 0
    for qq, r in replies.items():
        base = r if isinstance(r, dict) else {}
        hist = base.get("hist") or [base]
        if hist_max and hist_max > 0:
            hist = hist[-hist_max:]
        for item in hist:
            if not item or not str(item.get("text", "")).strip():
                continue
            seq += 1
            index[seq] = (str(qq), str(item["text"]).strip())
            blocks.append(
                f"{seq}. [{_alias_of(config, str(qq)) or base.get('name') or qq}({qq})] {item['text']}"
            )
    if seq == 0:
        return {}, True
    question = (
        str(task.get("content") or task.get("desc") or "").strip() or "（无任务内容）"
    )
    prompt = (
        "你是教师助手。下面是任务内容与若干学生的聊天消息（已按时间顺序编号）。\n"
        "请判断哪些消息【是对任务的回答】：比如给出答案、说明作业完成情况、提交作业/文件等；"
        "与任务无关的提问、闲聊、寒暄、表情不算回答。同一学生可能有多条回答，请全部挑出。\n\n"
        f"任务内容：{question}\n\n消息列表：\n"
        + "\n".join(blocks)
        + '\n\n只输出 JSON，格式：{"answers": [回答消息的编号, ...]}。没有任何回答时输出 {"answers": []}。不要输出其他内容。'
    )
    try:
        raw = await llm_call(
            context,
            config.get("llm_model", ""),
            prompt,
            max_retry=llm_retry_count(config),
        )
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        answers = data.get("answers") or []
        if isinstance(answers, (int, str)):
            answers = [answers]
        nums = [int(x) for x in answers if str(x).lstrip("-").isdigit()]
    except Exception as e:
        logger.warning(f"teacher_manager: pick answers LLM failed: {e}")
        return {}, False
    agg: dict[str, list[str]] = {}
    for n in sorted(nums):
        got = index.get(n)
        if got:
            agg.setdefault(got[0], []).append(got[1])
    picked: dict[str, dict] = {}
    for qq, texts in agg.items():
        base = dict(replies[qq]) if isinstance(replies[qq], dict) else {"name": qq}
        base["text"] = "\n".join(texts)
        picked[str(qq)] = base
    return picked, True


async def _process_task(context: Any, config: dict, task: dict) -> str:
    target_disp = _disp_target(config, task.get("target", ""))
    replies = task.get("replies", {}) or {}
    reply_lines = []
    for qq, r in replies.items():
        reply_lines.append(f"{_disp_name(config, r, qq)}: {r['text']}")
    if not reply_lines:
        return f"任务「{task['content'][:40]}」（{target_disp}）没有收集到任何回复。"
    raw = "\n".join(reply_lines)
    process = task.get("process", "none")

    if process == "summarize":
        prompt = (
            config.get("task_process_prompt", "")
            .replace(
                "{desc}",
                task.get("process_desc")
                or "请总结每位同学的回复要点，并标出需要教师注意的信息。",
            )
            .replace("{replies}", raw)
        )
        try:
            result = await llm_call(
                context,
                config.get("llm_model", ""),
                prompt,
                max_retry=llm_retry_count(config),
            )
            return f"【任务汇总 - {target_disp}】{task['content'][:60]}\n\n{result}"
        except Exception as e:
            logger.warning(f"teacher_manager: summarize task failed: {e}")
            return f"【任务原始回复 - {target_disp}】{task['content'][:60]}\n\n{raw}"
    if process == "score":
        prompt = (
            config.get("task_process_prompt", "")
            .replace(
                "{desc}",
                task.get("process_desc")
                or "请为每位同学的回复评分（百分制），输出列表。",
            )
            .replace("{replies}", raw)
        )
        try:
            result = await llm_call(
                context,
                config.get("llm_model", ""),
                prompt,
                max_retry=llm_retry_count(config),
            )
            return f"【任务评分 - {target_disp}】{task['content'][:60]}\n\n{result}"
        except Exception as e:
            logger.warning(f"teacher_manager: score task failed: {e}")
            return f"【任务原始回复 - {target_disp}】{task['content'][:60]}\n\n{raw}"
    if process == "pack":
        path = _pack_replies(task, raw)
        await context.send_message(
            _teacher_session(task["target"], task["teacher_qq"]),
            MessageChain(
                [
                    Plain(f"【任务回复打包 - {target_disp}】{task['content'][:60]}"),
                    File(name=f"{task['id']}_replies.md", file=str(path)),
                ]
            ),
        )
        return f"任务 {task['id']}（{target_disp}）回复已打包发送（{path.name}）。"
    # none：原文转发（开启 AI 判定时先过滤闲聊/提问）
    if config.get("reply_llm_filter", True):
        picked, ok = await _pick_answers(context, config, task)
        if ok:
            if not picked:
                return f"【任务回复 - {target_disp}】{task['content'][:60]}\n\n（所有学生均未对任务作出回答）"
            lines = [
                f"{_disp_name(config, r, qq)}: {r['text']}"
                for qq, r in sorted(picked.items())
                if str(r.get("text", "")).strip()
            ]
            body = "\n".join(lines)
            missing = [
                _disp_name(config, r, qq)
                for qq, r in (task.get("replies") or {}).items()
                if qq not in picked
            ]
            if missing:
                body += "\n\n❌ 未回答：" + "、".join(missing)
            return f"【任务回复 - {target_disp}】{task['content'][:60]}\n\n{body}"
        # AI 判定失败：回退原文转发
    return f"【任务回复 - {target_disp}】{task['content'][:60]}\n\n{raw}"


def _pack_replies(task: dict, raw: str) -> Path:
    get_out_dir().mkdir(parents=True, exist_ok=True)
    md = get_out_dir() / f"{task['id']}_replies.md"
    md.write_text(
        f"# 任务回复：{task['content']}\n\n时间：{now_iso()}\n\n{raw}\n",
        encoding="utf-8",
    )
    return md


def set_process(task: dict, process: str, desc: str = "") -> None:
    task["process"] = process
    task["process_desc"] = desc
    store.task_save(task)


async def check_deadlines(context: Any, config: dict) -> None:
    """后台任务调用：到截止时间自动结束收集，并把回复汇总发送给教师。"""
    now = datetime.datetime.now()
    for task in store.task_all().values():
        if not task.get("collecting") or not task.get("deadline"):
            continue
        try:
            dl = datetime.datetime.fromisoformat(task["deadline"])
        except Exception:
            continue
        if now >= dl:
            task["collecting"] = False
            task["closed"] = True
            task["closed_at"] = now_iso()
            store.task_save(task)
            logger.info(
                f"teacher_manager: task {task['id']} 到达截止时间，停止收集并发送汇总。"
            )
            try:
                reply_text = await _process_task(context, config, task)
                await context.send_message(
                    _teacher_session(task["target"], task["teacher_qq"]),
                    MessageChain([Plain(reply_text)]),
                )
            except Exception as e:
                logger.warning(
                    f"teacher_manager: task {task['id']} 到期汇总发送失败: {e}"
                )
