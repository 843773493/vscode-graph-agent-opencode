// 请求日志 JSON 归一：把同一段增量展示块合并成一条，避免展示层出现碎块。

import { isRecord } from "../../utils/jsonDisplay";

function mergeCandidate(value: unknown): {
  kind: string;
  text: string;
  metadata: string;
} | null {
  if (!isRecord(value) || typeof value.type !== "string") {
    return null;
  }
  if (
    (value.type === "text" || value.type === "output_text") &&
    typeof value.text === "string"
  ) {
    const metadata = { ...value };
    delete metadata.text;
    return { kind: "text", text: value.text, metadata: JSON.stringify(metadata) };
  }
  if (value.type === "reasoning" && typeof value.reasoning === "string") {
    const metadata = { ...value };
    delete metadata.reasoning;
    return { kind: "reasoning", text: value.reasoning, metadata: JSON.stringify(metadata) };
  }
  if (
    value.type === "text_delta" &&
    isRecord(value.payload) &&
    typeof value.payload.text === "string"
  ) {
    const metadata = { ...value, payload: { ...value.payload } };
    delete metadata.payload.text;
    return { kind: "text_delta", text: value.payload.text, metadata: JSON.stringify(metadata) };
  }
  return null;
}

function mergeDisplayItems(previous: unknown, current: unknown): unknown | null {
  const left = mergeCandidate(previous);
  const right = mergeCandidate(current);
  if (!left || !right || left.kind !== right.kind || left.metadata !== right.metadata) {
    return null;
  }
  if (!isRecord(previous)) {
    return null;
  }
  if (left.kind === "reasoning") {
    return { ...previous, reasoning: left.text + right.text };
  }
  if (left.kind === "text_delta" && isRecord(previous.payload)) {
    return {
      ...previous,
      payload: { ...previous.payload, text: left.text + right.text },
    };
  }
  return { ...previous, text: left.text + right.text };
}

export function normalizeRequestLogJsonForDisplay(value: unknown): unknown {
  if (Array.isArray(value)) {
    const normalized: unknown[] = [];
    for (const item of value) {
      const next = normalizeRequestLogJsonForDisplay(item);
      const merged = mergeDisplayItems(normalized[normalized.length - 1], next);
      if (merged === null) {
        normalized.push(next);
      } else {
        normalized[normalized.length - 1] = merged;
      }
    }
    return normalized;
  }
  if (!isRecord(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      normalizeRequestLogJsonForDisplay(item),
    ]),
  );
}
