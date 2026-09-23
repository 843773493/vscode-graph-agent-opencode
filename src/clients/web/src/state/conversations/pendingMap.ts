import type { ConversationView } from "../../types/frontend";

/** pending 会话列表的写回入口；空列表表示该 mapKey 不再有待处理会话。 */
export function writePendingList(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  list: ConversationView[],
  mapKey: string = sessionId,
) {
  if (list.length === 0) {
    map.delete(mapKey);
    return;
  }
  map.set(mapKey, list);
}
