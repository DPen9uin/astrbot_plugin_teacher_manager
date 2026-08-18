"""数据层：聊天记录 / 用户档案 / 同意缓存 / 任务 / 定时规则 的本地文件存取。

数据存放于 AstrBot 规范路径 data/plugin_data/{plugin_name}/ 下：
  data/plugin_data/astrbot_plugin_teacher_manager/
    chatlog/<session_key>.md   会话聊天记录（追加式）
    users/<qq>.md              用户档案
    consent/consents.json      总结同意缓存（key: session_key）
    tasks.json                 推送任务（收集学生回复）
    rules.json                 定时总结规则
    profile_pending.json       用户档案待总结队列（计数）

通过 init_data_dir(plugin_name) 初始化，自动从旧插件目录 data/ 迁移数据。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# === 模块级变量（先指向插件安装目录下 data/ 作为兜底）===
# init_data_dir() 调用后自动切换到规范路径 data/plugin_data/<name>/
DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
CHATLOG_DIR = DATA_DIR / "chatlog"
USERS_DIR = DATA_DIR / "users"
CONSENT_DIR = DATA_DIR / "consent"


def init_data_dir(plugin_name: str) -> None:
    """初始化插件数据目录为规范路径 data/plugin_data/{plugin_name}/。

    调用后 DATA_DIR / CHATLOG_DIR / USERS_DIR / CONSENT_DIR
    自动指向新路径。如果旧路径（插件安装目录下 data/）存在且有数据，
    且新路径为空，自动迁移并清除旧目录。
    """
    global DATA_DIR, CHATLOG_DIR, USERS_DIR, CONSENT_DIR
    new_dir = Path(get_astrbot_plugin_data_path()) / plugin_name
    DATA_DIR = new_dir
    CHATLOG_DIR = new_dir / "chatlog"
    USERS_DIR = new_dir / "users"
    CONSENT_DIR = new_dir / "consent"
    _migrate_old_data()


def _migrate_old_data() -> None:
    old = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
    # 确保新路径的子目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHATLOG_DIR.mkdir(exist_ok=True)
    USERS_DIR.mkdir(exist_ok=True)
    CONSENT_DIR.mkdir(exist_ok=True)
    # 清理旧路径（只要存在就删除）
    if not old.exists():
        return
    if old.resolve() == DATA_DIR.resolve():
        # 意外情况：旧路径和新路径相同，不删
        return
    try:
        shutil.copytree(str(old), str(DATA_DIR), dirs_exist_ok=True)
        shutil.rmtree(str(old))
        logger.info(f"✅ teacher_manager: 数据已迁移，旧目录已清理: {old}")
    except Exception as e:
        logger.warning(f"teacher_manager: 旧目录清理失败（跳过）: {e}")


MAX_PROFILE_CHAT_LINES = 500
PROFILE_SUMMARY_HINT = 20  # 累积 N 条对话后触发特征总结


def _target_to_umo(t: str, force: str | None = None, platform: str = "default") -> str:
    """把配置里的目标归一化为会话 UMO。

    支持三种写法：
      - 已有 UMO（如 default:GroupMessage:123456）原样返回（兼容旧配置）
      - 群号：前缀「群」，如 群123456789 → {platform}:GroupMessage:123456789
      - QQ 号：纯数字，如 987654321 → {platform}:FriendMessage:987654321
    platform 为当前平台实例 id（取自 event.unified_msg_origin 首段）；
    force 为 'friend'/'group' 时按指定类型解释（个人表/群表）；无法解释时回退自动识别。
    """
    t = (t or "").strip()
    if not t:
        return ""
    if re.match(r"^[^:\s]+:[a-zA-Z]+:\d+$", t):
        return t
    if force == "group":
        m = re.match(r"^(?:群)?(\d{5,})$", t)
        if m:
            return f"{platform}:GroupMessage:{m.group(1)}"
    elif force == "friend":
        m = re.match(r"^(\d{5,})$", t)
        if m:
            return f"{platform}:FriendMessage:{t}"
    m = re.match(r"^群(\d{5,})$", t)
    if m:
        return f"{platform}:GroupMessage:{m.group(1)}"
    if re.match(r"^\d{5,}$", t):
        return f"{platform}:FriendMessage:{t}"
    return t


def normalize_alias_map(
    alias_map: Any, group_map: Any = None, platform: str = "default"
) -> dict:
    """把称呼映射归一化为 {称呼: 会话UMO} dict。

    支持：
      - 新个人表 alias_map（dict：称呼 → QQ 号）
      - 新群聊表 alias_group_map（dict：称呼 → 群号，作为 group_map 传入）
      - 旧配置 dict（{"三班": "aiocqhttp:..."} 或 {"三班": "群123"}）
      - 旧配置 list（[{"alias": "三班", "target": "QQ号/群号/UMO", "target_type": "..."}]）
    """
    out: dict = {}
    # text(JSON) 类型兼容：配置面板里 alias_map 以 JSON 字符串形式存储
    if isinstance(alias_map, str):
        try:
            alias_map = json.loads(alias_map)
        except Exception:
            alias_map = {}
    if isinstance(group_map, str):
        try:
            group_map = json.loads(group_map)
        except Exception:
            group_map = {}
    if isinstance(alias_map, dict):
        for k, v in alias_map.items():
            if k:
                out[str(k)] = _target_to_umo(str(v), platform=platform)
    elif isinstance(alias_map, list):
        for item in alias_map:
            if isinstance(item, dict) and item.get("alias"):
                tt = str(item.get("target_type") or "")
                force = "group" if ("群" in tt or tt.lower() == "group") else None
                out[str(item["alias"])] = _target_to_umo(
                    str(item.get("target") or ""), force, platform
                )
    if isinstance(group_map, dict):
        for k, v in group_map.items():
            if k:
                out[str(k)] = _target_to_umo(str(v), "group", platform)
    return out


def _ensure_dirs() -> None:
    for d in (CHATLOG_DIR, USERS_DIR, CONSENT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def session_key_str(session_key: str) -> str:
    """把会话 unified_msg_origin 转成安全的文件名。"""
    return re.sub(r"[^0-9A-Za-z_:]", "_", session_key)


# ---------------- 聊天记录 ----------------


def append_chatlog(session_key: str, qq: str, name: str, text: str) -> None:
    try:
        from .llm import fmt_chatlog_line, now_iso
    except ImportError:
        from llm import fmt_chatlog_line, now_iso

    _ensure_dirs()
    path = CHATLOG_DIR / f"{session_key_str(session_key)}.md"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(fmt_chatlog_line(now_iso(), qq, name, text) + "\n")
    except Exception as e:
        logger.warning(f"teacher_manager: append_chatlog failed: {e}")


def read_chatlog(session_key: str, since: str | None = None) -> list[dict]:
    """读取会话聊天记录。返回 [{time, qq, name, text}]，time 为 ISO 字符串。"""
    path = CHATLOG_DIR / f"{session_key_str(session_key)}.md"
    if not path.exists():
        return []
    lines_out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("["):
                    continue
                m = re.match(r"\[([^]]+)\] (.+?)\((\d+)\): (.*)", line)
                if not m:
                    continue
                dt, name, qq, text = m.group(1), m.group(2), m.group(3), m.group(4)
                if since and dt < since:
                    continue
                lines_out.append({"time": dt, "qq": qq, "name": name, "text": text})
    except Exception as e:
        logger.warning(f"teacher_manager: read_chatlog failed: {e}")
    return lines_out


def chatlog_recent(session_key: str, hours: float, max_lines: int = 400) -> list[dict]:
    """取某会话最近 N 小时内的记录。"""
    import datetime

    since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    lines = read_chatlog(session_key, since=since)
    return lines[-max_lines:]


def all_sessions() -> list[str]:
    _ensure_dirs()
    return [p.stem for p in CHATLOG_DIR.glob("*.md")]


# ---------------- 用户档案 ----------------


def _user_path(qq: str) -> Path:
    return USERS_DIR / f"{qq}.md"


def ensure_user_file(qq: str, name: str = "") -> Path:
    _ensure_dirs()
    path = _user_path(qq)
    if not path.exists():
        name = name or qq
        path.write_text(
            f"# 用户档案：{name}（{qq}）\n"
            "<!-- 手工添加的条目与特征总结都放在下面 -->\n"
            "## 档案条目\n\n"
            "## 特征总结\n\n"
            "## 对话摘录\n\n",
            encoding="utf-8",
        )
    return path


def read_profile(qq: str) -> str:
    path = _user_path(qq)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def add_profile_entry(qq: str, text: str) -> None:
    path = ensure_user_file(qq)
    content = path.read_text(encoding="utf-8")
    marker = "## 档案条目"
    line = f"- {text}"
    if marker in content:
        content = content.replace(marker, marker + "\n" + line, 1)
    else:
        content = content.rstrip() + f"\n\n{marker}\n{line}\n"
    path.write_text(content, encoding="utf-8")


def delete_profile_entry(qq: str, keyword: str) -> bool:
    path = _user_path(qq)
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    new_lines = [ln for ln in content.splitlines() if keyword not in ln]
    if len(new_lines) == len(content.splitlines()):
        return False
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def append_profile_chat(qq: str, line: str) -> None:
    """把对话追加到用户档案的「对话摘录」，限制行数。"""
    path = ensure_user_file(qq)
    content = path.read_text(encoding="utf-8")
    marker = "## 对话摘录"
    if marker in content:
        head, _, _ = content.partition("## 对话摘录")
        excerpt = content.split("## 对话摘录", 1)[1].strip().splitlines()
        excerpt.append(line)
        excerpt = excerpt[-MAX_PROFILE_CHAT_LINES:]
        content = head + "## 对话摘录\n" + "\n".join(excerpt) + "\n"
    else:
        content = content.rstrip() + f"\n\n{marker}\n{line}\n"
    path.write_text(content, encoding="utf-8")


def bump_profile_pending(qq: str) -> int:
    """对话计数 +1，返回当前待总结计数。达到阈值后由调用方触发总结并清零。"""
    data = _load_json(DATA_DIR / "profile_pending.json", {})
    n = int(data.get(qq, 0)) + 1
    data[qq] = n
    _save_json(DATA_DIR / "profile_pending.json", data)
    return n


def clear_profile_pending(qq: str) -> None:
    data = _load_json(DATA_DIR / "profile_pending.json", {})
    data.pop(qq, None)
    _save_json(DATA_DIR / "profile_pending.json", data)


# ---------------- 同意缓存 ----------------


def consent_get(session_key: str) -> str | None:
    """返回同意的时间 ISO 字符串；无同意返回 None。"""
    data = _load_json(CONSENT_DIR / "consents.json", {})
    return data.get(session_key_str(session_key))


def consent_set(session_key: str) -> None:
    try:
        from .llm import now_iso
    except ImportError:
        from llm import now_iso

    _ensure_dirs()
    data = _load_json(CONSENT_DIR / "consents.json", {})
    data[session_key_str(session_key)] = now_iso()
    _save_json(CONSENT_DIR / "consents.json", data)


def consent_reset() -> None:
    """清空全部总结同意记录（配置有效期变更时调用）。"""
    _save_json(CONSENT_DIR / "consents.json", {})


# ---------------- 任务 ----------------


def task_save(task: dict) -> None:
    tasks = task_all()
    tasks[task["id"]] = task
    _save_json(DATA_DIR / "tasks.json", tasks)


def task_get(task_id: str) -> dict | None:
    return task_all().get(task_id)


def task_del(task_id: str) -> None:
    tasks = task_all()
    tasks.pop(task_id, None)
    _save_json(DATA_DIR / "tasks.json", tasks)


def task_all() -> dict[str, dict]:
    return _load_json(DATA_DIR / "tasks.json", {})


# ---------------- 定时规则 ----------------


def rule_save(rule: dict) -> None:
    rules = rule_all()
    rules[rule["id"]] = rule
    _save_json(DATA_DIR / "rules.json", rules)


def rule_del(rule_id: str) -> None:
    rules = rule_all()
    rules.pop(rule_id, None)
    _save_json(DATA_DIR / "rules.json", rules)


def rule_all() -> dict[str, dict]:
    return _load_json(DATA_DIR / "rules.json", {})
