import React from "react";
import type { TurnMessageLocationDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";

export const MessageSearchLocationContext = React.createContext<{
  message?: TurnMessageLocationDTO;
  close: () => void;
  onMount: () => void;
}>({ close: () => {}, onMount: () => {} });

/** 码点区间只在原文上转换，避免从 Markdown DOM 或截断片段猜测位置。 */
export function messageRangeOffsets(content: string, start: number, end: number): [number, number] {
  let codepoint = 0, offset = 0, startOffset = start === 0 ? 0 : -1;
  for (const character of content) {
    if (codepoint === end) break;
    offset += character.length;
    codepoint += 1;
    if (codepoint === start) startOffset = offset;
  }
  if (startOffset < 0 || codepoint !== end || end <= start) throw new Error("消息原文定位区间越界");
  return [startOffset, offset];
}

/** 本次已核验的原文属于当前 Turn；关闭即释放，不进入时间线缓存或持久展示态。 */
export function MessageSearchLocation({ turnId, itemId }: { turnId?: string; itemId?: string }): React.ReactNode {
  const { message, close, onMount } = React.useContext(MessageSearchLocationContext);
  // 工具原文要等嵌套详情展开后才挂载，不能只依赖外层 Turn 的挂载通知。
  const markRef = React.useCallback((element: HTMLElement | null) => {
    if (element) onMount();
  }, [onMount, message]);
  if (!message) return null;
  if (itemId ? message.role !== "tool" || message.reference?.item_id !== itemId
    : message.role === "tool" || message.turn_id !== turnId) return null;
  const { reference, content } = message;
  if (!reference) throw new Error("消息原文缺少已核验的位置引用");
  const [start, end] = messageRangeOffsets(content, reference.start_codepoint, reference.end_codepoint);
  return <section className="chat-message-location" aria-label={message.role === "tool" ? "工具结果原文定位" : "消息原文定位"}>
    <header>命中原文 · {message.role === "tool" ? `工具结果 ${message.tool_name}` : message.role === "user" ? "用户消息" : "助手消息"}
      <button type="button" onClick={close}>关闭定位原文</button>
    </header>
    <pre>{content.slice(0, start)}<mark ref={markRef} data-message-item-id={reference.item_id}>{content.slice(start, end)}</mark>{content.slice(end)}</pre>
  </section>;
}
