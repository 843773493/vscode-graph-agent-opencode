import React from "react";
import type { ListItem } from "react-virtuoso";
import type { ChatTranscriptItem } from "./transcriptItems";
import type { ExecutionItemReference } from "../../state/session/internalExecutionHistory";

/** 复用虚拟列表实际挂载范围和测量高度，正文释放不折叠历史滚动空间。 */
export function useTurnBodyWindow({ items, scopeKey, retain, onError, onRendered }: {
  items: readonly ChatTranscriptItem[];
  scopeKey: string;
  retain?: (turnIds: string[], executionItems?: ExecutionItemReference[]) => Promise<void>;
  onError: (error: unknown) => void;
  onRendered: () => void;
}) {
  const heights = React.useMemo(() => new Map<string, number>(), [scopeKey]);
  const latest = React.useRef({ items, retain, onError, onRendered });
  latest.current = { items, retain, onError, onRendered };
  const frame = React.useRef<number | null>(null);
  const previousWindow = React.useRef("");
  React.useEffect(() => {
    previousWindow.current = "";
    return () => { if (frame.current !== null) cancelAnimationFrame(frame.current); frame.current = null; };
  }, [scopeKey]);
  const itemsRendered = React.useCallback((rendered: ListItem<ChatTranscriptItem>[]) => {
    latest.current.onRendered();
    if (!latest.current.retain || rendered.length === 0) return;
    for (const item of rendered) {
      if (item.size > 0 && (item.data?.kind === "turn" && !item.data.conversation.bodyEvicted
        || item.data?.kind === "internal_execution" && !item.data.displayPart.bodyEvicted
        || item.data?.kind === "execution_response" && !item.data.bodyEvicted)) {
        heights.set(item.data.key, item.size);
      }
    }
    const keys = new Set(rendered.flatMap(item => item.data ? [item.data.key] : []));
    const all = latest.current.items;
    const first = all.findIndex(item => keys.has(item.key));
    if (first < 0) return;
    let last = first;
    for (let index = first; index < all.length; index++) if (keys.has(all[index].key)) last = index;
    const window = all.slice(Math.max(0, first - 2), last + 3);
    const ids = window.flatMap(item =>
      item.kind === "turn" && item.conversation.turnId ? [item.conversation.turnId] : []);
    const executionItems = window.flatMap(item => item.kind === "internal_execution"
      ? [{ executionId: item.executionId, itemId: item.displayPart.item_id }]
      : item.kind === "execution_response" ? [{ executionId: item.executionId, itemId: item.itemId }] : []);
    const identity = JSON.stringify([ids, executionItems]);
    if (identity === previousWindow.current) return;
    previousWindow.current = identity;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      void latest.current.retain?.(ids, executionItems).catch(latest.current.onError);
    });
  }, [heights]);
  return { heights, itemsRendered };
}
