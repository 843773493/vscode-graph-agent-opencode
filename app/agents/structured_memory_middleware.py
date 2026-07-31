from __future__ import annotations

from deepagents.middleware.memory import MemoryMiddleware

from app.agents.middleware_prompts import MEMORY_SYSTEM_PROMPT
from app.prompting import PromptSection, internal_message_factory


class StructuredMemoryMiddleware(MemoryMiddleware):
    """让第三方 MemoryMiddleware 的动态内容经过项目结构化提示编码器。"""

    # TODO: deepagents 提供公开 memory formatter hook 后改用公开扩展点。
    def _format_agent_memory(
        self,
        contents: dict[str, str],
        template: str = MEMORY_SYSTEM_PROMPT,
    ) -> str:
        sections = [
            f"{path}\n\n{contents[path].rstrip()}"
            for path in self.sources
            if contents.get(path)
        ]
        memory_body = "\n\n".join(sections) if sections else "(No memory loaded)"
        rendered_memory = internal_message_factory.render_system_prompt_section(
            PromptSection("agent_memory", memory_body)
        )
        return template.format(agent_memory=rendered_memory)


__all__ = ["StructuredMemoryMiddleware"]
