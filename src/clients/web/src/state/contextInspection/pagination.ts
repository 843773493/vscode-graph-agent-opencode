import type {
  SessionContextItemDTO,
  SessionContextReadResultDTO,
} from "../../types/protocol_generated/boxteam/workspace/v2/public";

export type ContextItem = SessionContextItemDTO;
export interface InspectionPage {
  items: ContextItem[];
  fragment: string;
  revision: string | null;
  nextCursor: string | null;
  hasMore: boolean;
}

export function emptyInspectionPage(): InspectionPage {
  return { items: [], fragment: "", revision: null, nextCursor: null, hasMore: false };
}

export function objectField(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("上下文协议字段必须是对象");
  }
  return value as Record<string, unknown>;
}

export function textField(data: unknown, name: string): string {
  const value = objectField(data)[name];
  if (typeof value !== "string" || !value) throw new Error(`上下文协议缺少 ${name}`);
  return value;
}

export function appendInspectionPage(
  previous: InspectionPage,
  page: SessionContextReadResultDTO,
  resource: string,
): InspectionPage {
  if (page.resource !== resource || !page.revision) throw new Error("上下文响应 resource/revision 不匹配");
  if (previous.revision && previous.revision !== page.revision) throw new Error("上下文分页 revision 已变化，请重新加载");
  if (page.partial_errors.length || page.omitted_partial_error_count) throw new Error("上下文返回不完整错误，请重新加载");
  if (page.has_more && (!page.next_cursor || page.next_cursor === previous.nextCursor)) throw new Error("上下文分页 cursor 未推进");
  const items = [...previous.items];
  let fragment = previous.fragment;
  for (const item of page.items) {
    if (item.kind.endsWith("_chunk")) {
      if (objectField(item.data).chunk_start !== [...fragment].length || typeof item.text !== "string") {
        throw new Error("上下文分片偏移不连续");
      }
      fragment += item.text;
      if (!item.truncated) {
        const restored = objectField(JSON.parse(fragment));
        if (restored.kind !== item.kind.slice(0, -6) || restored.locator !== item.locator) {
          throw new Error("上下文分片 identity 不一致");
        }
        items.push(restored as unknown as ContextItem);
        fragment = "";
      }
    } else {
      if (fragment) throw new Error("上下文分片尚未完成");
      items.push(item);
    }
  }
  if (!page.has_more && fragment) throw new Error("上下文结束时仍有未完成分片");
  return { items, fragment, revision: page.revision, nextCursor: page.next_cursor ?? null, hasMore: Boolean(page.has_more) };
}
