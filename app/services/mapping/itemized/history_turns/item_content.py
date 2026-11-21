"""单个已核验 Item 的只读内容投影；文本与媒体不互相冒充。"""

import json
import re
from collections.abc import Mapping

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn_search import ThreadItemContentDTO
from app.services.mapping.itemized.content_blocks import (
    iter_direct_content_blocks,
    reasoning_content_rows,
)


def _text_content(name: str, text: str) -> ThreadItemContentDTO:
    language = "markdown"
    fenced = re.fullmatch(r"\s*```([\w+-]+)\n([\s\S]*?)\n```\s*", text)
    if fenced:
        language, text = fenced.groups()
    elif text.lstrip().startswith(("{", "[")):
        try:
            json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            language = "json"
    return ThreadItemContentDTO(name=name, language=language, content=text, encoding="utf-8")


def context_source_contents(body: object) -> list[ThreadItemContentDTO]:
    """冻结来源保留完整原文，不构造不存在的 canonical Item。"""
    return [ThreadItemContentDTO(
        name="context", language="plaintext" if isinstance(body, str) else "json",
        content=body if isinstance(body, str) else canonical_json_bytes(body).decode("utf-8"), encoding="utf-8",
    )]


def item_contents(item: CanonicalItemRecord, *, tool_output: str | None = None) -> list[ThreadItemContentDTO]:
    if (item.source_manifest is not None and item.source_manifest.protection != "public") or item.payload_kind == "opaque":
        raise ValueError("此 Item 的受保护正文不可打开")
    payload = item.payload
    if item.semantic_kind == "runtime_notice":
        # 保留控制提示原文；XML 标签不能被 Markdown 渲染吞掉。
        prompt = payload.get("prompt") if isinstance(payload, Mapping) else payload
        if not isinstance(prompt, str):
            raise TypeError("runtime_notice 缺少提示正文")
        return [ThreadItemContentDTO(name="context", language="plaintext", content=prompt, encoding="utf-8")]
    if item.semantic_kind == "tool_call":
        return [ThreadItemContentDTO(name="arguments", language="json", encoding="utf-8",
            content=json.dumps(payload, ensure_ascii=False, indent=2))]
    if item.semantic_kind == "compaction_summary" and isinstance(payload, Mapping):
        payload = payload.get("summary")
    if item.semantic_kind == "tool_result" and isinstance(payload, Mapping):
        payload = tool_output if tool_output is not None else payload.get("content", payload)
    contents: list[ThreadItemContentDTO] = []
    if isinstance(payload, Mapping):
        if item.semantic_kind == "attachment":
            blocks = [{"uri": payload["ref"], "name": payload.get("name", "attachment"),
                       "mime_type": payload.get("mime_type")}]
        elif "content" in payload:
            blocks = list(iter_direct_content_blocks(payload["content"]))
        else:
            blocks = [payload] if "type" in payload else []
    else:
        blocks = list(iter_direct_content_blocks(payload))
    texts = [block["text"] for block in blocks if isinstance(block.get("text"), str)]
    if item.semantic_kind == "reasoning":
        texts = [row.reasoning_text or row.summary_text for row in reasoning_content_rows(payload) if row.reasoning_text or row.summary_text] or texts
    if texts:
        contents.append(_text_content("result" if item.semantic_kind == "tool_result" else "content", "".join(texts)))
    if isinstance(item.payload, Mapping) and isinstance(item.payload.get("attachments"), list):
        blocks.extend(item.payload["attachments"])
    for ordinal, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        uri = block.get("uri") or block.get("path") or block.get("file_id")
        name = str(block.get("name") or f"attachment-{ordinal + 1}")
        mime = block.get("mime_type") or block.get("content_type")
        if isinstance(uri, str) and uri.startswith("boxteam://"):
            contents.append(ThreadItemContentDTO(name=name, language="", uri=uri,
                mime_type=mime if isinstance(mime, str) else None))
            continue
        image = block.get("image_url")
        url = image.get("url") if isinstance(image, Mapping) else image
        source = block.get("source")
        if isinstance(url, str) and url.startswith("data:"):
            header, separator, data = url.partition(",")
            if not separator or not header.endswith(";base64"):
                raise ValueError("不支持的内联媒体编码")
            mime = header[5:-7]
        elif isinstance(source, Mapping) and source.get("type") == "base64":
            data, mime = source.get("data"), source.get("media_type")
        elif block.get("type") in {"image", "audio", "video"} and block.get("source_type") == "base64":
            data = block.get("data")
        elif block.get("type") == "input_audio" and isinstance(block.get("input_audio"), Mapping):
            audio = block["input_audio"]
            data, mime = audio.get("data"), f"audio/{audio.get('format')}"
        else:
            continue
        if isinstance(data, str) and isinstance(mime, str) and mime.startswith(("image/", "audio/", "video/")):
            contents.append(ThreadItemContentDTO(name=name, language="", mime_type=mime, content=data, encoding="base64"))
    if not contents:
        contents.append(ThreadItemContentDTO(name="content", language="json", encoding="utf-8",
            content=json.dumps(payload, ensure_ascii=False, indent=2)))
    return contents
