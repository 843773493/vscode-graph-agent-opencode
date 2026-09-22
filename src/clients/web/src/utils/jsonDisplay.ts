/** 通用对象判定：非数组的 object。全前端共享的 isRecord 权威实现。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 单个字符串进入展示层前的保留上限。与 ProgressiveUserMessage 的
 * LARGE_USER_MESSAGE_RENDER_LIMIT 取同一量级：超大工具结果、超长 text_delta
 * 或整段 Prompt 一旦原样进 DOM，会同步撑爆主线程与内存。
 */
export const LARGE_STRING_DISPLAY_LIMIT = 20_000;

/**
 * 展示用字符串兜底：超过上限只保留头部，并显式标注原文长度。
 * 截断必须可见，绝不静默丢弃内容。
 */
export function boundedDisplayString(value: string): string {
  if (value.length <= LARGE_STRING_DISPLAY_LIMIT) {
    return value;
  }
  return `${value.slice(0, LARGE_STRING_DISPLAY_LIMIT)}…（已截断展示，原文 ${value.length} 字符）`;
}

export function redactLargeData(value: unknown): unknown {
  if (typeof value === "string") {
    if (
      value.startsWith("data:image/") ||
      value.startsWith("data:video/") ||
      value.startsWith("data:audio/")
    ) {
      const commaIndex = value.indexOf(",");
      const header = commaIndex >= 0 ? value.slice(0, commaIndex) : "data:<media>";
      const payloadLength = commaIndex >= 0 ? value.length - commaIndex - 1 : value.length;
      return `${header},<base64 ${payloadLength} chars redacted>`;
    }
    return boundedDisplayString(value);
  }
  if (Array.isArray(value)) {
    return value.map(redactLargeData);
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, redactLargeData(item)]),
    );
  }
  return value;
}

export function prettyJson(value: unknown): string {
  return JSON.stringify(redactLargeData(value), null, 2) ?? "";
}

/**
 * 从剪贴板文本里提取可解析的 JSON 候选：原文优先，其次逐个取出 ```json 围栏内容，
 * 去重后按顺序返回。会话信息与工作区信息的粘贴导入共用这一实现。
 */
export function jsonCandidates(text: string): string[] {
  const candidates = [text];
  const fencePattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  for (const match of text.matchAll(fencePattern)) {
    const fencedJson = match[1]?.trim();
    if (fencedJson) {
      candidates.push(fencedJson);
    }
  }
  return [...new Set(candidates)];
}
