from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agents.agent_factory import _build_model_from_provider
from app.core.job_event_bus import EventType
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.schemas.public_v2.session import SessionUpdateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.mapping.agent_content_mapper import split_agent_content

DEFAULT_SESSION_TITLES = {"", "新会话", "未命名"}
TITLE_MAX_CHARS = 40
TITLE_INPUT_MAX_CHARS = 1600

TITLE_SYSTEM_PROMPT = """你负责给 AI 编程助手会话生成一个短标题。
只输出标题本身，不要解释，不要加引号，不要句号。
标题应使用用户主要使用的语言；中文不超过 12 个汉字，英文不超过 6 个单词。"""


class TitleModelProtocol(Protocol):
    async def ainvoke(self, input: Sequence[BaseMessage]) -> BaseMessage: ...


ModelFactory = Callable[[dict[str, object], dict[str, object]], TitleModelProtocol]


def _truncate(value: str, max_chars: int = TITLE_INPUT_MAX_CHARS) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def _extract_model_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    _, text = split_agent_content(content)
    return text.strip()


def normalize_generated_title(raw_title: str) -> str:
    title = raw_title.strip()
    title = title.splitlines()[0].strip() if title else ""
    title = re.sub(r"^(标题|会话标题|Title)\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    title = title.strip(" \t\r\n\"'`“”‘’《》「」[]()（）")
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip("。.!！?？；;，,")
    if not title:
        raise RuntimeError("自动命名模型返回了空标题")
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip()
    return title


class SessionTitleService:
    def __init__(
        self,
        *,
        config_service: ConfigService,
        session_service: SessionService,
        job_event_bus: JobEventBusProtocol,
        model_factory: ModelFactory = _build_model_from_provider,
    ) -> None:
        self._config_service = config_service
        self._session_service = session_service
        self._bus = job_event_bus
        self._model_factory = model_factory

    async def maybe_auto_title_after_first_response(
        self,
        *,
        session_id: str,
        job_id: str,
        agent_id: str,
        user_message: str,
        assistant_response: str,
    ) -> str | None:
        session = await self._session_service.get(session_id)
        if session.title.strip() not in DEFAULT_SESSION_TITLES:
            return None

        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.STATUS_CHANGE,
            payload={
                "session_id": session_id,
                "status": "正在自动命名会话",
                "reason": "session_auto_title_started",
            },
            agent_id="session_title_service",
        )

        title = await self._generate_title(
            agent_id=agent_id,
            user_message=user_message,
            assistant_response=assistant_response,
            job_id=job_id,
        )
        updated = await self._session_service.update(
            session_id,
            SessionUpdateRequest(title=title),
        )
        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.STATUS_CHANGE,
            payload={
                "session_id": session_id,
                "status": f"已自动命名会话: {updated.title}",
                "reason": "session_auto_title_updated",
                "title": updated.title,
            },
            agent_id="session_title_service",
        )
        return updated.title

    async def _generate_title(
        self,
        *,
        agent_id: str,
        user_message: str,
        assistant_response: str,
        job_id: str,
    ) -> str:
        runtime_config = self._config_service.get_agent_runtime_config(agent_id)
        providers = runtime_config.get("providers")
        if not isinstance(providers, list):
            raise TypeError("agent runtime providers 应为 list")

        last_error: Exception | None = None
        for provider in providers:
            if not isinstance(provider, dict):
                raise TypeError("provider 配置项必须是对象")
            model = self._model_factory(provider, runtime_config)
            model_name = str(provider.get("model") or "unknown_model")
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.LLM_REQUEST,
                payload={
                    "model": model_name,
                    "timestamp": int(time.time() * 1000),
                },
                agent_id="session_title_service",
            )
            try:
                response = await model.ainvoke(
                    [
                        SystemMessage(content=TITLE_SYSTEM_PROMPT),
                        HumanMessage(
                            content=(
                                "请根据下面首轮对话生成短标题。\n\n"
                                f"用户消息：\n{_truncate(user_message)}\n\n"
                                f"助手回复：\n{_truncate(assistant_response)}"
                            )
                        ),
                    ]
                )
                if not isinstance(response, AIMessage):
                    response = AIMessage(content=getattr(response, "content", ""))
                return normalize_generated_title(_extract_model_text(response))
            except Exception as error:
                last_error = error
                continue

        if last_error is None:
            raise RuntimeError("没有可用于自动命名的 LLM provider")
        raise RuntimeError(f"所有自动命名模型都失败: {last_error}") from last_error
