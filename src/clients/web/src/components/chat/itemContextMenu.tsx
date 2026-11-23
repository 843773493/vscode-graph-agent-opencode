import React from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import type { TimelineItem } from "../../state/timeline/timelineTypes";
import { copyTextToClipboard } from "../../utils/clipboard";
import type { ContextContentReferenceDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

export interface ChatItemMenuAction {
  id: string;
  label: string;
  tooltip: string;
  enabled?: boolean;
  run: () => void | Promise<void>;
}

export interface ChatItemTarget {
  itemIds?: Array<{ id: string; kind: string }>;
  contextSources?: Array<{ reference: ContextContentReferenceDTO; name: string }>;
  messageId?: string;
}
export const ChatItemOwnerContext = React.createContext<{ sessionId: string; threadId: string } | null>(null);
export const ContextNoticeContinuationContext = React.createContext<((cursor: string) => Promise<void>) | null>(null);

/** 聚合卡片的最后一个真实 Item 是定位边界；来源未提交时不倒退到较早来源。 */
export function responseItemTarget(item: TimelineItem): ChatItemTarget | undefined {
  if (!item.responseSources?.at(-1)?.source.item_id) return undefined;
  const seen = new Set<string>();
  return { itemIds: item.responseSources.flatMap(part => {
    const id = part.source.item_id;
    if (!id || seen.has(id)) return [];
    seen.add(id);
    return [{ id, kind: part.kind }];
  }) };
}

export interface ChatItemMenuRequest {
  source: HTMLElement;
  target?: ChatItemTarget & { sessionId: string; threadId: string };
  anchor: HTMLElement | { x: number; y: number };
  actions: readonly ChatItemMenuAction[];
}

export const ChatItemMenuContext = React.createContext<((request: ChatItemMenuRequest) => void) | null>(null);
export type ChatHoverRow = { label: string; value: string };
export const ChatHoverContext = React.createContext<((element: HTMLElement, rows: () => readonly ChatHoverRow[]) => () => void) | null>(null);

/** 使用宿主原生 Hover；数据在打开时读取，不为计时每一跳重建浮层。 */
export function useChatHover(rows: readonly ChatHoverRow[]) {
  const bind = React.useContext(ChatHoverContext);
  const latest = React.useRef(rows); latest.current = rows;
  const [element, setElement] = React.useState<HTMLElement | null>(null);
  React.useEffect(() => element && bind ? bind(element, () => latest.current) : undefined, [bind, element]);
  return setElement;
}
export type ChatItemMenuHandlers = Pick<React.HTMLAttributes<HTMLElement>,
  "tabIndex" | "aria-haspopup" | "aria-label" | "onContextMenu" | "onKeyDown">;

export function copyItemAction(text: string, tooltip = t("chat.itemMenu.copyHint")): ChatItemMenuAction {
  return { id: "copyItem", label: t("chat.itemMenu.copy"), tooltip, enabled: text.length > 0,
    run: () => copyTextToClipboard(text) };
}

/** 在现有根节点接线；动作及选区只在打开菜单时读取，不为每项挂载浮层。 */
export function useChatItemMenu(getActions: () => ChatItemMenuAction[], enabled = true, getTarget?: () => ChatItemTarget | undefined): ChatItemMenuHandlers {
  const showMenu = React.useContext(ChatItemMenuContext);
  const owner = React.useContext(ChatItemOwnerContext);
  const open = (event: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>, keyboard: boolean) => {
    if (!showMenu || !enabled || event.defaultPrevented) return;
    const target = event.target as HTMLElement;
    // 链接、输入框和嵌入编辑器继续使用自己的上下文菜单。
    if (target.closest('a[href], input, textarea, [contenteditable="true"], .monaco-editor')) return;
    const source = event.currentTarget;
    const actions = getActions();
    const selection = source.ownerDocument.getSelection();
    const root = source.getRootNode();
    // 普通选区端点会被重定位到 ShadowRoot 宿主，使用组合范围取得真实端点。
    const range = selection?.getComposedRanges({ shadowRoots: "host" in root ? [root as ShadowRoot] : [] })[0];
    const selectedText = selection && range
      && source.contains(range.startContainer) && source.contains(range.endContainer)
      ? selection.toString() : "";
    if (selectedText) actions.unshift({
      id: "copySelection", label: t("chat.itemMenu.copySelection"), tooltip: t("chat.itemMenu.copySelectionHint"),
      run: () => copyTextToClipboard(selectedText),
    });
    if (actions.length === 0) return;
    event.preventDefault();
    event.stopPropagation();
    const pointer = event as React.MouseEvent<HTMLElement>;
    const targetItem = getTarget?.();
    showMenu({ source, target: targetItem && owner ? { ...targetItem, ...owner } : undefined, anchor: keyboard ? source : { x: pointer.clientX, y: pointer.clientY }, actions });
  };
  return !showMenu || !enabled ? {} : {
    tabIndex: 0,
    "aria-haspopup": "menu",
    onContextMenu: event => open(event, false),
    onKeyDown: event => {
      if (event.key === "ContextMenu" || event.key === "F10" && event.shiftKey) open(event, true);
    },
  };
}

export function responseItemMenuActions(item: TimelineItem, onForkItem?: (itemId: string) => void): ChatItemMenuAction[] {
  if (item.kind !== "aggregated_text" && item.kind !== "aggregated_tool") return [];
  const actions: ChatItemMenuAction[] = [];
  if (item.kind === "aggregated_text") {
    actions.push(copyItemAction(item.text));
  } else {
    const text = [item.toolName,
      item.inputText ? t("chat.tool.inputBody", item.inputText) : "",
      item.resultText ? t("chat.tool.outputBody", item.resultText) : "",
    ].filter(Boolean).join("\n\n");
    actions.push(copyItemAction(text, t(item.detailsLoaded ? "chat.itemMenu.copyToolHint" : "chat.itemMenu.copyToolSummaryHint")));
    if (item.inputText) actions.push({ id: "copyArguments", label: t("chat.itemMenu.copyArguments"),
      tooltip: t("chat.itemMenu.copyArgumentsHint"), run: () => copyTextToClipboard(item.inputText) });
    if (item.resultText) actions.push({ id: "copyResult", label: t("chat.itemMenu.copyResult"),
      tooltip: t("chat.itemMenu.copyResultHint"), run: () => copyTextToClipboard(item.resultText) });
  }
  // 卡片可以合并 call/result；只取最后一项，不退回更早的可用 ID。
  const lastSource = item.responseSources?.at(-1);
  const anchor = lastSource?.source.item_id;
  const pending = item.active || item.kind === "aggregated_tool"
    && (item.incomplete || item.outcomeUnknown || lastSource?.kind !== "tool_result");
  actions.push({ id: "forkItem", label: t("chat.itemMenu.fork"),
    tooltip: t(pending ? "chat.itemMenu.forkPendingHint" : !anchor || !onForkItem
      ? "chat.itemMenu.forkUnavailableHint" : "chat.itemMenu.forkHint"),
    enabled: Boolean(anchor && onForkItem && !pending),
    run: () => { if (anchor && onForkItem) onForkItem(anchor); },
  });
  return actions;
}
