"""教师管理插件主入口：消息路由 + 意图识别 + 指令兜底 + 后台任务。

消息处理顺序（on_message）：
  1. 收录对话（会话日志 + 用户档案摘录）
  2. EULA 门禁（⚠️ 必须在总结同意之前，防止「同意」被截胡）
  3. 总结同意登记（目标方回复「同意」且角色匹配）
  4. 任务回复收集（目标会话匹配活跃任务）
  5. 管理员路由：指令（/xxx）→ 自然语言（正则快速路径 → LLM 意图兜底）
  6. 其余消息放行给本体 LLM
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star

try:
    from . import dispatch, eula, profile, store
    from . import summary as summary_mod
    from .llm import llm_call, llm_retry_count, now_iso
except ImportError:
    import profile

    import dispatch
    import eula
    import store
    import summary as summary_mod
    from llm import llm_call, llm_retry_count, now_iso

# 正则快速路径（命中即路由，避免每次调 LLM）
RE_DISPATCH = re.compile(r"推送|推送给|发给|发到|通知|布置|发布|告知|告诉|交作业|任务")
RE_SUMMARY = re.compile(r"总结|回顾|整理.*聊天|看看.*聊")
RE_PROFILE = re.compile(r"档案|薄弱|进步|成绩|分析一下|学情|表现")
RE_RULE = re.compile(r"定时|每天|每周|规则")
RE_COLLECT = re.compile(r"^(收|截止|统计回复|收尾|汇总回复)$")
RE_CONFIRM = re.compile(r"^(确认|确定)$")
RE_CANCEL = re.compile(r"^(取消)$")
RE_AGREE = re.compile(r"^(同意|接受|可以|没问题|ok)$", re.I)

# 意图关键词默认值（可被 keywords_dispatch/summary/profile/rule 配置覆盖，支持正则写法）
DEFAULT_KEYWORDS = {
    "dispatch": [
        "推送",
        "推送给",
        "发给",
        "发到",
        "通知",
        "布置",
        "发布",
        "告知",
        "告诉",
        "交作业",
        "任务",
    ],
    "summary": ["总结", "回顾", "整理.*聊天", "看看.*聊"],
    "profile": ["档案", "薄弱", "进步", "成绩", "分析一下", "学情", "表现"],
    "rule": ["定时", "每天", "每周", "规则"],
}


def _build_intent_regex(cfg: dict) -> dict:
    """根据配置的 keywords_* 构建意图正则；未配置或为空时使用内置默认。"""
    res = {}
    for key, defaults in DEFAULT_KEYWORDS.items():
        kws = cfg.get(f"keywords_{key}")
        if isinstance(kws, list) and kws:
            pat = "|".join(str(k) for k in kws if str(k).strip())
        else:
            pat = "|".join(defaults)
        try:
            res[key] = re.compile(pat)
        except re.error:
            res[key] = re.compile("|".join(defaults))
    return res


# 疑问句特征：命中则放行，不触发任何教学意图（避免问功能/问配置时误触发）
Q_RE = re.compile(
    r"[?？]$|[吗呢吧呀]$|怎么|为什么|是否|能不能|可不可以|是不是|会不会|有没有|如何"
)


def _is_question(text: str) -> bool:
    return bool(Q_RE.search(text))


def _flatten_config(cfg: dict) -> dict:
    """将分组(object+items)配置拍平为顶层 key，兼容旧扁平配置。

    分组块的值是 dict（如 basic/llm_intent/summary/...），拍平后
    插件代码统一用 cfg.get("admin_qqs") 读取，无需感知嵌套层级。
    """
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out.update(v)
        else:
            out[k] = v
    return out


PROFILE_SUMMARY_CHECK_INTERVAL = 300  # 秒


async def _reply(event, message: str) -> None:
    """统一回复入口：兼容部分平台缺失 event.reply 的情况。"""
    await event.send(MessageChain([Plain(message)]))


def _attach_tag(comp) -> str:
    """附件组件的文本标记（图片/语音/视频/文件）。"""
    if isinstance(comp, Image):
        return "[图片]"
    if isinstance(comp, Record):
        return "[语音]"
    if isinstance(comp, Video):
        return "[视频]"
    return f"[文件] {getattr(comp, 'name', None) or '未命名'}"


class TeacherManager(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        # 初始化数据目录为规范路径 data/plugin_data/<name>/
        store.init_data_dir(getattr(self, "name", "astrbot_plugin_teacher_manager"))
        self.config = config  # 基类不保存 config，这里显式保存
        self._task: asyncio.Task | None = None
        self._profile_task: asyncio.Task | None = None
        self._pending_tasks: set[asyncio.Task] = set()

    def _get_config(self) -> dict:
        """获取插件配置：优先注入值，其次从 context 动态获取。返回拍平后的 dict。"""
        cfg = getattr(self, "config", None)
        if cfg:
            return _flatten_config(cfg)
        return _flatten_config(self._get_context_config() or {})

    # ---------- 后台任务 ----------

    async def _background_loop(self):
        while True:
            try:
                cfg = dict(self._get_config())
                platform = getattr(self, "_last_platform", "") or "default"
                await summary_mod.run_due_rules(self.context, cfg, platform)
            except Exception as e:
                logger.warning(f"teacher_manager: rules loop: {e}")
            try:
                await dispatch.check_deadlines(self.context, cfg)
            except Exception as e:
                logger.warning(f"teacher_manager: deadline check: {e}")
            await asyncio.sleep(60)

    async def _profile_summary_loop(self):
        while True:
            try:
                cfg = dict(self._get_config())
                pending = store._load_json(store.DATA_DIR / "profile_pending.json", {})
                for qq in list(pending.keys()):
                    if profile.should_auto_summarize(qq):
                        await profile.auto_summarize_profile(self.context, cfg, qq)
            except Exception as e:
                logger.warning(f"teacher_manager: profile loop: {e}")
            await asyncio.sleep(PROFILE_SUMMARY_CHECK_INTERVAL)

    def _ensure_tasks(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._background_loop())
        if self._profile_task is None or self._profile_task.done():
            self._profile_task = asyncio.create_task(self._profile_summary_loop())

    async def initialize(self):
        """插件启用后立即拉起后台任务（on_message 仍保留兜底）。"""
        self._ensure_tasks()

    def _sync_agreement_days(self) -> None:
        """检测 agreement_valid_days 配置变化：变化时重置所有总结同意记录。

        基准值持久化到 data/agreement_last_days.txt，插件重载/重启后仍能检测到变更。
        """
        try:
            cfg = dict(self._get_config())
            days = float(cfg.get("agreement_valid_days", 30) or 0)
        except Exception:
            return
        path = store.DATA_DIR / "agreement_last_days.txt"
        last: float | None = None
        try:
            if path.exists():
                last = float((path.read_text(encoding="utf-8") or "").strip())
        except Exception:
            last = None
        if last is not None and last != days:
            store.consent_reset()
            logger.info(
                "teacher_manager: agreement_valid_days 配置已变更，已重置全部总结同意记录"
            )
        try:
            path.write_text(str(days), encoding="utf-8")
        except Exception:
            pass

    async def terminate(self):
        for t in (self._task, self._profile_task):
            if t and not t.done():
                t.cancel()
        for t in list(self._pending_tasks):
            if not t.done():
                t.cancel()

    # ---------- 主入口 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        self._ensure_tasks()
        self._sync_agreement_days()
        # 文件消息补偿：纯文件消息 message_str 为空，但消息链里带 File 段
        attach_parts = [
            c
            for c in (
                getattr(getattr(event, "message_obj", None), "message", None) or []
            )
            if isinstance(c, (File, Image, Record, Video))
        ]
        text = (event.message_str or "").strip()
        tags = [_attach_tag(fp) for fp in attach_parts]
        if tags:
            text = (text + " " + " ".join(tags)).strip()
        if not text:
            return
        session = event.unified_msg_origin
        self._last_platform = session.split(":", 1)[0]
        qq = self._sender_id(event)
        name = self._sender_name(event)
        is_group = "GroupMessage" in session
        is_at = bool(getattr(event, "is_at_or_wake_command", False))
        cfg = self._get_config()
        admin_qqs = [str(x) for x in (cfg.get("admin_qqs", []) or [])]
        is_admin = qq in admin_qqs
        cfg = dict(self._get_config())

        # 1. 收录对话
        store.append_chatlog(session, qq, name, text)
        if not is_admin:
            profile.on_user_message(qq, name, text)

        # 2. EULA 门禁（必须在总结同意之前，否则未同意 EULA 的用户回复「同意」
        #    会被总结同意截胡，导致 EULA 同意永远注册不上）
        if not is_admin:
            blocked = await eula.handle_eula(
                event, cfg, qq, text, name=name, is_group=is_group, is_at=is_at
            )
            if blocked:
                event.stop_event()
                return

        # 3. 总结同意登记（目标会话 + 角色匹配 + 有申请）
        if RE_AGREE.match(text):
            if self._try_summary_agree(event, session, qq, cfg):
                event.stop_event()
                return

        # 4. 任务回复收集
        if not is_admin:
            for task in dispatch.active_task_for_session(session):
                dispatch.collect_reply(
                    task,
                    session,
                    qq,
                    name,
                    text,
                    hist_max=int(cfg.get("reply_hist_max", 8) or 8),
                )
                store.task_save(task)
            # 4.1 附件消息（文件/图片/语音/视频）转发给管理员（无论有无进行中的任务）
            if attach_parts:
                for fp in attach_parts:
                    await self._forward_attach(cfg, qq, name, fp)

        # 5. 管理员路由
        if is_admin and (not is_group or is_at):
            handled = await self._route_admin(event, text, session, qq, cfg)
            if handled:
                event.stop_event()
                return
        # 6. 放行

    # ---------- 管理员路由 ----------

    async def _forward_attach(self, cfg: dict, qq: str, name: str, comp) -> None:
        """把学生发来的文件/图片消息转发给全部管理员（QQ 私聊）。"""
        admins = [str(x) for x in (cfg.get("admin_qqs", []) or [])]
        if not admins:
            return
        platform = self._last_platform or "aiocqhttp"
        if isinstance(comp, Image):
            kind = "图片"
        elif isinstance(comp, Record):
            kind = "语音"
        elif isinstance(comp, Video):
            kind = "视频"
        else:
            kind = "文件"
        disp = dispatch._alias_of(cfg, str(qq)) or name or qq
        fname = getattr(comp, "name", None) or kind
        note = f"📎 {disp}({qq}) 发来{kind}：{fname}"
        try:
            # 直接复用原组件（字段完整），仅补一条说明文字
            chain = MessageChain([Plain(note), comp])
        except Exception:
            chain = MessageChain([Plain(f"{note}（转发失败：无可用地址）")])
        for a in admins:
            try:
                target = f"{platform}:FriendMessage:{a}"
                await self.context.send_message(target, chain)
            except Exception as e:
                logger.warning(f"teacher_manager: 转发附件给 {a} 失败: {e}")

    async def _route_admin(
        self, event, text: str, session: str, qq: str, cfg: dict
    ) -> bool:
        # 指令兜底
        if text.startswith("/"):
            return await self._route_command(event, text, session, qq, cfg)
        # 确认/取消
        if RE_CONFIRM.match(text):
            return await self._confirm_dispatch(event, qq, cfg)
        if RE_CANCEL.match(text):
            dispatch.clear_pending(qq)
            await _reply(event, "已取消待确认的操作。")
            return True
        if RE_COLLECT.match(text):
            result = await dispatch.close_task(self.context, cfg, qq)
            await _reply(event, result)
            return True
        # 自然语言意图识别（模式可配置：keywords 仅关键词 / llm 仅LLM / hybrid LLM优先关键词兜底）
        mode = str(cfg.get("intent_mode", "hybrid") or "hybrid")
        # 疑问句过滤：疑问语气一律放行，不触发任何教学功能（可配置关闭）
        if cfg.get("intent_question_filter", True) and _is_question(text):
            return False
        rx = _build_intent_regex(cfg)

        def _keyword_intent() -> str:
            if rx["dispatch"].search(text):
                return "dispatch"
            if rx["summary"].search(text):
                return "summary"
            if rx["rule"].search(text):
                return "rule"
            if rx["profile"].search(text):
                return "profile"
            return "none"

        async def _llm_intent() -> str:
            try:
                prompt = cfg.get("intent_prompt", "").replace("{msg}", text)
                raw = await llm_call(
                    self.context,
                    cfg.get("llm_model", ""),
                    prompt,
                    session_umo=session,
                    max_retry=llm_retry_count(cfg),
                )
                data = _parse_intent_json(raw)
                return str(data.get("intent", "none") or "none")
            except Exception as e:
                logger.warning(f"teacher_manager: intent LLM failed: {e}")
                return ""

        intent = "none"
        if mode == "keywords":
            # 仅关键词快速识别
            intent = _keyword_intent()
        elif mode == "llm":
            # 仅 LLM 语义判断，LLM 异常时放行不处理
            intent = await _llm_intent()
            if intent == "":
                return False
        else:
            # hybrid（默认）：LLM 优先，异常时回退关键词
            intent = await _llm_intent()
            if intent == "":
                intent = _keyword_intent()
        return await self._exec_intent(event, text, session, qq, cfg, intent)

    async def _exec_intent(self, event, text, session, qq, cfg, intent) -> bool:
        if intent == "dispatch":
            content, targets = dispatch.parse_dispatch_text(
                cfg, text, session.split(":", 1)[0]
            )
            if not targets:
                await _reply(
                    event,
                    "未识别到推送对象。可以这么说：\n「把 XXX 推送给三班」\n「通知小明、小红明天考试」\n或在配置面板添加称呼映射。",
                )
                return True
            if not content:
                await _reply(event, "未识别到推送内容。")
                return True
            dispatch.register_dispatch_pending(qq, content, targets)
            proc = self._parse_process(text)
            if proc:
                pass
            target_names = "、".join(dispatch._disp_target(cfg, t) for t in targets)
            await _reply(
                event,
                f"请确认推送内容：\n\n【目标】{target_names}\n【内容】{content}\n\n"
                + ("检测到处理方式声明：" + proc + "\n" if proc else "")
                + "回复「确认」执行，回复「取消」放弃。",
            )
            return True
        if intent == "summary":
            content, targets = dispatch.parse_dispatch_text(
                cfg, text, session.split(":", 1)[0]
            )
            target = targets[0] if targets else None
            if not target:
                target = self._find_summary_target(text, cfg, session.split(":", 1)[0])
            if not target:
                await _reply(
                    event,
                    "未识别到总结对象。可以这么说：\n「总结三班最近两天的聊天」\n或在配置面板添加称呼映射。",
                )
                return True
            hours = summary_mod.parse_window(
                text, cfg.get("summary_interval_hours", 24) or 24
            )
            need_agree = bool(cfg.get("summary_need_agree", True))
            if (not need_agree) or summary_mod.consent_valid(
                target, float(cfg.get("agreement_valid_days", 30) or 30)
            ):
                result = await summary_mod.do_summary(
                    self.context, cfg, qq, target, hours
                )
                await _reply(event, result)
                return True
            # 征求同意
            self._pending_summary_set(qq, target)
            await summary_mod.request_consent(self.context, event, target)
            await _reply(
                event,
                f"已向「{dispatch._disp_target(cfg, target)}」发送总结申请，对方回复「同意」后我会自动总结并发送给您。",
            )
            return True
        if intent == "profile":
            target_token = self._find_profile_target(text, cfg)
            qq_target = profile.parse_qq_or_alias(
                cfg.get("alias_map"), target_token or "", cfg.get("alias_group_map")
            )
            if not qq_target:
                await _reply(
                    event,
                    "未识别到目标同学。用「/档案 <QQ或称呼>」查询，或在配置面板添加称呼映射。",
                )
                return True
            from .store import read_profile

            content = read_profile(qq_target)
            if not content:
                await _reply(
                    event, f"没有找到 {qq_target} 的档案，该同学尚未产生对话记录。"
                )
                return True
            # 结合提问请 LLM 解读
            try:
                q = text
                prompt = f"你是教师助手。教师问：{q}\n\n学生档案如下，请结合档案回答教师的问题（简洁）：\n{content}"
                result = await llm_call(
                    self.context,
                    cfg.get("llm_model", ""),
                    prompt,
                    session_umo=session,
                    max_retry=llm_retry_count(cfg),
                )
                await _reply(event, f"【{qq_target} 学情】\n\n{result}")
            except Exception:
                await _reply(event, f"【{qq_target} 档案】\n\n{content}")
            return True
        if intent == "rule":
            rule = summary_mod.parse_rule(text)
            if rule:
                rule["teacher_qq"] = qq
                rule["last_run"] = ""
                rid = "r_" + str(int(datetime.now().timestamp()))
                rule["id"] = rid
                store.rule_save(rule)
                await _reply(
                    event,
                    f"已添加定时总结规则：{rule['type']} {rule['hour']}:{rule['minute']:02d} 总结「{rule['target_token']}」。"
                    "到点后若目标方已有有效同意则直接总结；若无同意会通知您并跳过（请让目标方回复「同意」后下次到点生效）。",
                )
            else:
                rules = store.rule_all()
                if not rules:
                    await _reply(
                        event,
                        "当前没有定时总结规则。可声明：「每天20点总结三班」「每周日晚上8点总结小明」。",
                    )
                else:
                    lines = [
                        f"- {rid}：{r['type']} {r.get('hour', 20)}:{r.get('minute', 0):02d} 总结 {r.get('target_token')}"
                        for rid, r in rules.items()
                    ]
                    await _reply(event, "当前定时总结规则：\n" + "\n".join(lines))
            return True
        # none
        return False

    async def _confirm_dispatch(self, event, qq: str, cfg: dict) -> bool:
        item = dispatch.pop_dispatch_pending(qq)
        if not item:
            await _reply(event, "当前没有待确认的推送。")
            return True
        targets = item.get("targets", [item["target"]] if "target" in item else [])
        if not targets:
            await _reply(event, "待确认推送的目标列表为空。")
            return True
        result = await dispatch.do_dispatch(
            self.context,
            cfg,
            qq,
            item["content"],
            targets,
            getattr(event, "unified_msg_origin", ""),
        )
        await _reply(event, result)
        return True

    # ---------- 指令路由 ----------

    async def _route_command(
        self, event, text: str, session: str, qq: str, cfg: dict
    ) -> bool:
        platform = session.split(":", 1)[0]
        parts = text[1:].strip().split()
        if not parts:
            return False
        cmd = parts[0]
        args = parts[1:]
        if cmd in ("推送", "push"):
            content, targets = dispatch.parse_dispatch_text(cfg, text[1:], platform)
            if not targets or not content:
                await _reply(
                    event,
                    "用法：/推送 <内容> <对象>（如：/推送 明天带实验报告 到 三班）",
                )
                return True
            dispatch.register_dispatch_pending(qq, content, targets)
            target_str = "、".join(targets)
            await _reply(
                event,
                f"请确认推送：\n【目标】{target_str}\n【内容】{content}\n回复「确认」执行。",
            )
            return True
        if cmd in ("总结", "summary"):
            target = args[0] if args else None
            target = (
                dispatch.resolve_target(cfg, target or "", platform) if target else None
            )
            if not target:
                await _reply(event, "用法：/总结 <对象> [最近 X 天]")
                return True
            hours = summary_mod.parse_window(
                text, cfg.get("summary_interval_hours", 24) or 24
            )
            need_agree = bool(cfg.get("summary_need_agree", True))
            if (not need_agree) or summary_mod.consent_valid(
                target, float(cfg.get("agreement_valid_days", 30) or 30)
            ):
                result = await summary_mod.do_summary(
                    self.context, cfg, qq, target, hours
                )
                await _reply(event, result)
            else:
                self._pending_summary_set(qq, target)
                await summary_mod.request_consent(self.context, event, target)
                await _reply(
                    event, f"已向「{dispatch._disp_target(cfg, target)}」发送总结申请。"
                )
            return True
        if cmd in ("档案", "profile"):
            result = await profile.handle_profile_command(
                event, self.context, cfg, args
            )
            if result:
                await _reply(event, result)
            return True
        if cmd in ("规则", "rule"):
            if args and args[0] == "del" and len(args) > 1:
                store.rule_del(args[1])
                await _reply(event, f"已删除规则 {args[1]}。")
            else:
                rules = store.rule_all()
                if not rules:
                    await _reply(event, "当前没有定时总结规则。")
                else:
                    lines = [
                        f"- {rid}：{r['type']} {r.get('hour', 20)}:{r.get('minute', 0):02d} 总结 {r.get('target_token')}（/规则 del {rid} 删除）"
                        for rid, r in rules.items()
                    ]
                    await _reply(event, "定时总结规则：\n" + "\n".join(lines))
            return True
        if cmd in ("收", "collect"):
            tid = args[0] if args else None
            result = await dispatch.close_task(self.context, cfg, qq, tid)
            await _reply(event, result)
            return True
        if cmd in ("同意", "agree"):
            # 管理员手动同意某个申请（私聊自己时）
            result = await self._summary_agree_for_latest(event, qq, cfg)
            await _reply(event, result if result else "没有待处理的总结申请。")
            return True
        if cmd in ("取消", "cancel"):
            dispatch.clear_pending(qq)
            await _reply(event, "已取消待确认操作。")
            return True
        if cmd in ("任务", "tasks", "task"):
            msgs = []
            tasks = [
                t
                for t in store.task_all().values()
                if t.get("collecting") and not t.get("closed")
            ]
            if tasks:
                msgs.append("📌 收集中任务：")
                for t in tasks:
                    msgs.append(
                        f"- {t.get('id')}：{str(t.get('content', ''))[:24]}｜目标 {t.get('target')}｜已收 {len(t.get('replies') or {})} 份"
                    )
            else:
                msgs.append("📌 没有正在收集的任务。")
            rules = store.rule_all()
            if rules:
                msgs.append("⏰ 定时总结规则：")
                for rid, r in rules.items():
                    msgs.append(
                        f"- {rid}：每天 {r.get('hour', 20)}:{r.get('minute', 0):02d} 总结 {r.get('target_token')}（/规则 del {rid} 删除）"
                    )
            else:
                msgs.append("⏰ 没有定时总结规则。")
            pd = store._load_json(store.DATA_DIR / "pending_confirm.json", {})
            dp = pd.get("dispatch") or {}
            for tqq, it in dp.items():
                msgs.append(f"✉️ 待确认推送（{tqq}）：{str(it.get('content', ''))[:24]}")
            sp = pd.get("summary") or {}
            for tqq, it in sp.items():
                msgs.append(f"💬 待同意总结申请（{tqq}）：目标 {it.get('target')}")
            if not dp and not sp:
                msgs.append("✉️ 没有待确认操作。")
            await _reply(event, "\n".join(msgs))
            return True
        return False

    # ---------- 工具方法 ----------

    def _sender_id(self, event) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            sender = getattr(event, "sender", None)
            return str(getattr(sender, "user_id", "") or "")

    def _sender_name(self, event) -> str:
        try:
            sender = getattr(event, "sender", None)
            if sender is None:
                return ""
            return getattr(sender, "card", "") or getattr(sender, "nickname", "") or ""
        except Exception:
            return ""

    def _parse_process(self, text: str) -> str:
        """识别教师声明的处理方式：打包/总结/评分。"""
        if "打包" in text or "文档" in text:
            return "pack"
        if "评分" in text or "打分" in text:
            return "score"
        if "总结" in text:
            return "summarize"
        return ""

    def _find_summary_target(
        self, text: str, cfg: dict, platform: str = "default"
    ) -> str | None:
        for k in sorted(
            store.normalize_alias_map(
                cfg.get("alias_map"), cfg.get("alias_group_map"), platform
            ).keys(),
            key=len,
            reverse=True,
        ):
            if k in text:
                return dispatch.resolve_target(cfg, k, platform)
        m = re.search(r"(?:总结|回顾)\s*([^\s，,。]{2,20}?)", text)
        if m:
            return dispatch.resolve_target(cfg, m.group(1), platform)
        return None

    def _find_profile_target(self, text: str, cfg: dict) -> str | None:
        # 别名优先
        for k in sorted(
            store.normalize_alias_map(
                cfg.get("alias_map"), cfg.get("alias_group_map")
            ).keys(),
            key=len,
            reverse=True,
        ):
            if k in text:
                return k
        m = re.search(
            r"(?:关于|看下|看看|查一下|分析|说说)?\s*([^\s，,。]{2,20}?)(?:的|同学|学生)?(?:薄弱|进步|成绩|档案|表现|学情)",
            text,
        )
        if m:
            return m.group(1)
        return None

    def _pending_summary_set(self, teacher_qq: str, target: str) -> None:
        data = store._load_json(store.DATA_DIR / "pending_confirm.json", {})
        data.setdefault("summary", {})[teacher_qq] = {"target": target, "at": now_iso()}
        store._save_json(store.DATA_DIR / "pending_confirm.json", data)

    def _try_summary_agree(self, event, session: str, qq: str, cfg: dict) -> bool:
        """目标方回复「同意」：匹配待处理总结申请，登记同意并执行总结。"""
        data = store._load_json(store.DATA_DIR / "pending_confirm.json", {})
        for tqq, item in list((data.get("summary", {}) or {}).items()):
            if item.get("target") != session:
                continue
            # 校验角色
            if "GroupMessage" in session:
                if not summary_mod.is_group_admin(event):
                    return False
            else:
                if qq != tqq and qq != str(session.split(":")[-1]):
                    return False
            # 登记同意
            store.consent_set(session)
            data["summary"].pop(tqq, None)
            store._save_json(store.DATA_DIR / "pending_confirm.json", data)
            # 异步执行总结（不阻塞当前消息）
            _t = asyncio.create_task(self._run_summary_after_agree(tqq, session, cfg))
            self._pending_tasks.add(_t)
            _t.add_done_callback(lambda _: self._pending_tasks.discard(_t))
            return True
        # 无待处理申请：接受目标方的主动同意登记（定时总结请求场景）
        if "GroupMessage" in session:
            if not summary_mod.is_group_admin(event):
                return False
        else:
            if qq != str(session.split(":")[-1]):
                return False
        store.consent_set(session)
        _t = asyncio.create_task(self._confirm_agree_registered(session))
        self._pending_tasks.add(_t)
        _t.add_done_callback(lambda _: self._pending_tasks.discard(_t))
        return True

    async def _confirm_agree_registered(self, target: str):
        try:
            await self.context.send_message(
                target,
                MessageChain(
                    [Plain("✅ 已登记同意，定时总结将在下一个到点时刻自动执行～")]
                ),
            )
        except Exception:
            pass

    async def _run_summary_after_agree(self, teacher_qq: str, target: str, cfg: dict):
        try:
            await summary_mod.do_summary(
                self.context,
                cfg,
                teacher_qq,
                target,
                float(cfg.get("summary_interval_hours", 24) or 24),
            )
            # 结果也告知目标方已授权（简短）
            try:
                await self.context.send_message(
                    target, MessageChain([Plain("已同意总结，感谢配合～")])
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"teacher_manager: summary after agree failed: {e}")


def _parse_intent_json(raw: str) -> dict:
    """从 LLM 输出中提取 JSON。"""
    if not raw:
        return {}
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    # 兜底：找 intent 值
    m = re.search(r'intent["\']?\s*[:=]\s*["\']([a-z_]+)["\']', raw)
    return {"intent": m.group(1)} if m else {}
