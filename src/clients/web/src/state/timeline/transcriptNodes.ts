import type { TurnResponsePart } from "../../types/backend";
import type { ContextNoticeAnchorDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

/** scope 由编辑器固定；坐标不包含展示父链、折叠阶段或可变文案。 */
export type ProcessRef = { kind: "turn" | "execution"; id: string };
export type TranscriptSource = ProcessRef
  | { kind: "item"; id: string; part: number }
  | { kind: "stream"; id: string; model?: string; block?: number; tool?: string; part_kind?: string }
  | { kind: "context"; id: string; process?: ProcessRef };
export interface TranscriptNodeKey { source_ref: TranscriptSource; slot: string }
export interface ReadingAnchor {
  node_key: TranscriptNodeKey;
  content_position: { block: number; offset_px: number };
  viewport_offset_px: number;
}
export type DisclosureOverrides = Record<string, boolean>;

export function nodeKey(source_ref: TranscriptSource, slot = "body"): TranscriptNodeKey {
  return { source_ref, slot };
}
export function contextNode(part: TurnResponsePart): TranscriptNodeKey {
  return nodeKey({kind: "context", id: JSON.stringify([part.context_model_call_id, part.context_anchor]),
    ...(part.source.turn_id ? {process: {kind:"turn" as const, id:part.source.turn_id}}
      : part.source.execution_id ? {process:{kind:"execution" as const, id:part.source.execution_id}} : {})}, "marker");
}
export function encodeNode(key: TranscriptNodeKey): string { return JSON.stringify(key); }
export function contextAnchorReference(id: string): ContextNoticeAnchorDTO | undefined {
  let value: unknown;
  try { value = JSON.parse(id); } catch { return undefined; }
  if (!Array.isArray(value) || value.length !== 2 || typeof value[0] !== "string" || !value[0]
    || !["before_user_message", "before_model_call"].includes(value[1])) return undefined;
  return {model_call_id:value[0],anchor:value[1]};
}
export function partNode(part: {part_id: string; source: TurnResponsePart["source"]; kind: string; tool_call_id?: string | null}, slot = "body"): TranscriptNodeKey {
  return nodeKey(part.source.item_id
    ? { kind: "item", id: part.source.item_id, part: part.source.part_ordinal ?? 0 }
    : { kind: "stream", id: part.part_id, model: part.source.model_call_id,
      block: part.source.content_block_index, tool: part.tool_call_id ?? undefined,
      part_kind: part.kind }, slot);
}
export function nodeAttributes(key: TranscriptNodeKey, parent?: TranscriptNodeKey) {
  return { "data-transcript-node": encodeNode(key),
    ...(parent ? { "data-transcript-parent": encodeNode(parent) } : {}) };
}
export function disclosureKey(key: TranscriptNodeKey, phase?: "live" | "settled"): string {
  return JSON.stringify([key, phase ?? null]);
}
export function readableAnchor(value: unknown): value is ReadingAnchor {
  if (!value || typeof value !== "object") return false;
  const anchor = value as Partial<ReadingAnchor>;
  const source = anchor.node_key?.source_ref;
  return Boolean(source && ["turn", "execution", "item", "context"].includes(source.kind)
    && typeof source.id === "string" && source.id && typeof anchor.node_key?.slot === "string" && anchor.node_key.slot.length > 0
    && (source.kind !== "item" || Number.isSafeInteger(source.part) && source.part >= 0)
    && (source.kind !== "context" || contextAnchorReference(source.id) !== undefined
      && (!source.process || ["turn", "execution"].includes(source.process.kind) && typeof source.process.id === "string" && source.process.id.length > 0))
    && anchor.content_position && Number.isSafeInteger(anchor.content_position.block) && anchor.content_position.block >= 0
    && Number.isFinite(anchor.content_position.offset_px) && Number.isFinite(anchor.viewport_offset_px));
}

