"""LLM 调用封装：按配置指定模型调用，失败抛出带上下文信息"""

import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

DEFAULT_LLM_MODEL = ""  # 空 = 使用当前会话默认模型


async def get_llm_provider_id(
    context: Any, session_umo: str | None = None
) -> str | None:
    """获取要使用的 chat provider id：优先配置，其次当前会话默认。"""
    try:
        if session_umo:
            return await context.get_current_chat_provider_id(session_umo)
    except Exception as e:
        logger.debug(f"teacher_manager: get_current_chat_provider_id failed: {e}")
    try:
        prov = await context.get_using_provider_async()
        return getattr(prov, "provider_id", None) or getattr(prov, "id", None)
    except Exception as e:
        logger.debug(f"teacher_manager: get_using_provider_async failed: {e}")
    return None


def chain_text(chain: MessageChain | None) -> str:
    """将 MessageChain 抽取为纯文本。"""
    if chain is None:
        return ""
    parts = []
    for c in chain.chain:
        if isinstance(c, Plain) and c.text:
            parts.append(str(c.text))
    return "\n".join(parts)


def llm_retry_count(config: dict | None = None) -> int:
    """从配置读取 LLM 失败重试次数（0~10，越界收敛），未配置默认 2。"""
    try:
        v = int((config or {}).get("llm_retry_count", 2))
        return max(0, min(10, v))
    except Exception:
        return 2


async def llm_call(
    context: Any,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
    session_umo: str | None = None,
    max_retry: int = 2,
) -> str:
    """调用 LLM 并返回纯文本结果。

    model 为空时使用当前会话默认模型（通过 session_umo 获取）。
    """
    provider_id = model or await get_llm_provider_id(context, session_umo)
    if not provider_id:
        raise RuntimeError("未找到可用的 LLM 提供商，请在配置面板填写 llm_model")

    last_err: Exception | None = None
    for attempt in range(1 + max_retry):
        try:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            return chain_text(resp.result_chain)
        except Exception as e:
            last_err = e
            logger.warning(
                f"teacher_manager: llm_call attempt {attempt} failed with provider {provider_id}: {e}"
            )
            await _safe_sleep(attempt)
    raise RuntimeError(f"LLM 调用失败（{provider_id}）：{last_err}")


async def _safe_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(min(sec, 3))


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def fmt_chatlog_line(dt: str, qq: str, name: str, text: str) -> str:
    name = (name or "").strip() or qq
    return f"[{dt}] {name}({qq}): {text}"
