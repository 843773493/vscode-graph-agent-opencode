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
const DISPLAY_NODE_LIMIT = 5_000;
export const JSON_DISPLAY_TEXT_LIMIT = 250_000;
const DISPLAY_NODE_TEXT_RESERVE = 8;
const DISPLAY_DEPTH_LIMIT = 100;
const DISPLAY_TRUNCATION_MARKER = "…（展示已截断，后续内容未展示）";
const DISPLAY_TRUNCATED = Symbol("display-truncated");

interface DisplayBudget {
  remainingNodes: number;
  remainingText: number;
  indentationSize: number;
  truncationMarkerAdded: boolean;
}

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
  return ensureDisplayJsonLimit(redactWithinDisplayBudget(value, createDisplayBudget(), 0));
}

function createDisplayBudget(indentation?: number): DisplayBudget {
  const indentationSize = Number.isNaN(indentation)
    ? 0
    : Math.max(0, Math.min(10, Math.trunc(indentation ?? 0)));
  const truncationReserve = serializedStringLength(DISPLAY_TRUNCATION_MARKER)
    + DISPLAY_NODE_TEXT_RESERVE
    + serializedStringLength(`…[展示截断]${DISPLAY_NODE_LIMIT}`)
    + 2
    + (indentationSize > 0 ? 1 + indentationSize * DISPLAY_DEPTH_LIMIT : 0);
  return {
    remainingNodes: DISPLAY_NODE_LIMIT,
    remainingText: JSON_DISPLAY_TEXT_LIMIT - truncationReserve,
    indentationSize,
    truncationMarkerAdded: false,
  };
}

function redactWithinDisplayBudget(
  value: unknown,
  budget: DisplayBudget,
  depth: number,
): unknown {
  const reservation = displayNodeTextReservation(value, budget, depth);
  if (budget.remainingNodes <= 0 || budget.remainingText <= reservation) {
    return displayTruncationMarker(budget);
  }
  budget.remainingNodes -= 1;
  budget.remainingText -= reservation;

  if (typeof value === "string") {
    if (
      value.startsWith("data:image/") ||
      value.startsWith("data:video/") ||
      value.startsWith("data:audio/")
    ) {
      const commaIndex = value.indexOf(",");
      const header = commaIndex >= 0 ? value.slice(0, commaIndex) : "data:<media>";
      const payloadLength = commaIndex >= 0 ? value.length - commaIndex - 1 : value.length;
      return fitDisplayText(`${header},<base64 ${payloadLength} chars redacted>`, budget);
    }
    return fitDisplayText(value, budget);
  }
  if (Array.isArray(value)) {
    if (depth >= DISPLAY_DEPTH_LIMIT) return displayTruncationMarker(budget);
    const redacted: unknown[] = [];
    for (let index = 0; index < value.length; index += 1) {
      if (
        budget.remainingNodes <= 0
        || budget.remainingText <= DISPLAY_NODE_TEXT_RESERVE
      ) {
        if (!budget.truncationMarkerAdded) redacted.push(displayTruncationMarker(budget));
        break;
      }
      const item = value[index];
      const redactedItem = redactWithinDisplayBudget(item, budget, depth + 1);
      if (redactedItem === DISPLAY_TRUNCATED) break;
      if (redactedItem === undefined) {
        budget.remainingText -= 4;
        redacted.push(null);
      } else {
        redacted.push(redactedItem);
      }
    }
    return redacted;
  }
  if (isRecord(value)) {
    if (depth >= DISPLAY_DEPTH_LIMIT) return displayTruncationMarker(budget);
    const entries: Array<[string, unknown]> = [];
    for (const key in value) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      const item = value[key];
      if (item === undefined || typeof item === "function" || typeof item === "symbol") continue;
      const keyTextLength = key.length > budget.remainingText
        ? Number.POSITIVE_INFINITY
        : serializedStringLength(key);
      if (
        budget.remainingNodes <= 0
        || keyTextLength + DISPLAY_NODE_TEXT_RESERVE > budget.remainingText
      ) {
        if (!budget.truncationMarkerAdded) {
          entries.push([uniqueTruncationKey(entries), displayTruncationMarker(budget)]);
        }
        break;
      }
      budget.remainingText -= keyTextLength;
      const redactedItem = redactWithinDisplayBudget(item, budget, depth + 1);
      if (redactedItem === DISPLAY_TRUNCATED) break;
      entries.push([key, redactedItem]);
    }
    return Object.fromEntries(entries);
  }
  if (typeof value === "function" || typeof value === "symbol") return undefined;
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    const text = JSON.stringify(value);
    if (text.length > budget.remainingText) return displayTruncationMarker(budget);
    budget.remainingText -= text.length;
    return value;
  }
  return value;
}

function displayNodeTextReservation(value: unknown, budget: DisplayBudget, depth: number): number {
  const indentation = budget.indentationSize;
  const prettyValuePrefix = depth > 0 && indentation > 0 ? 1 + indentation * depth : 0;
  const isContainer = Array.isArray(value) || isRecord(value);
  const prettyContainerClose = isContainer && indentation > 0 ? 1 + indentation * depth : 0;
  return DISPLAY_NODE_TEXT_RESERVE + prettyValuePrefix + prettyContainerClose;
}

function displayTruncationMarker(
  budget: DisplayBudget,
): string | typeof DISPLAY_TRUNCATED {
  if (budget.truncationMarkerAdded) return DISPLAY_TRUNCATED;
  budget.truncationMarkerAdded = true;
  return DISPLAY_TRUNCATION_MARKER;
}

function fitDisplayText(
  value: string,
  budget: DisplayBudget,
): string | typeof DISPLAY_TRUNCATED {
  if (budget.remainingText <= 0) return displayTruncationMarker(budget);
  const limit = Math.min(LARGE_STRING_DISPLAY_LIMIT, value.length);
  const fullValueFits = value.length <= LARGE_STRING_DISPLAY_LIMIT
    && serializedStringLength(value) <= budget.remainingText;
  if (fullValueFits) {
    budget.remainingText -= serializedStringLength(value);
    return value;
  }

  const truncationMarker = `…（已截断展示，原文 ${value.length} 字符）`;
  let lowerBound = 0;
  let upperBound = limit;
  let displayed = "…";
  while (lowerBound <= upperBound) {
    const prefixLength = Math.floor((lowerBound + upperBound) / 2);
    const candidate = `${value.slice(0, prefixLength)}${truncationMarker}`;
    if (serializedStringLength(candidate) <= budget.remainingText) {
      displayed = candidate;
      lowerBound = prefixLength + 1;
    } else {
      upperBound = prefixLength - 1;
    }
  }
  if (serializedStringLength(displayed) > budget.remainingText) {
    return displayTruncationMarker(budget);
  }
  budget.remainingText -= serializedStringLength(displayed);
  return displayed;
}

function serializedStringLength(value: string): number {
  return JSON.stringify(value).length;
}

function uniqueTruncationKey(entries: Array<[string, unknown]>): string {
  const base = "…[展示截断]";
  let key = base;
  let suffix = 2;
  while (entries.some(([existing]) => existing === key)) {
    key = `${base}${suffix}`;
    suffix += 1;
  }
  return key;
}

function ensureDisplayJsonLimit(value: unknown, indentation?: number): unknown {
  const serialized = JSON.stringify(value, null, indentation) ?? "";
  if (serialized.length > JSON_DISPLAY_TEXT_LIMIT) {
    throw new Error(
      `JSON 展示预算计算错误：序列化结果 ${serialized.length} 字符，限制 ${JSON_DISPLAY_TEXT_LIMIT} 字符`,
    );
  }
  return value;
}

export function serializeDisplayJson(value: unknown, indentation?: number): string {
  const budget = createDisplayBudget(indentation);
  const redacted = redactWithinDisplayBudget(value, budget, 0);
  return JSON.stringify(ensureDisplayJsonLimit(redacted, indentation), null, indentation) ?? "";
}

export function prettyJson(value: unknown): string {
  return serializeDisplayJson(value, 2);
}

/**
 * 从剪贴板文本里提取可解析的 JSON 候选：原文优先，其次逐个取出 ```json 围栏内容，
 * 去重后按顺序返回。会话信息与工作区信息的粘贴导入共用这一实现。
 */
export function jsonCandidates(text: string): string[] {
  const candidates = [text];
  const seen = new Set(candidates);
  const fencePattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  for (const match of text.matchAll(fencePattern)) {
    const fencedJson = match[1]?.trim();
    if (fencedJson && !seen.has(fencedJson)) {
      seen.add(fencedJson);
      candidates.push(fencedJson);
    }
  }
  return candidates;
}
