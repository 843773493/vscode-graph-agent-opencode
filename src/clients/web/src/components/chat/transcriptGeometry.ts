import { encodeNode, type ReadingAnchor, type TranscriptNodeKey } from "../../state/timeline/transcriptNodes";

/** DOM 只保存测量坐标；业务成员仍由原投影持有。 */
export function findTranscriptNode(scroller: HTMLElement, key: TranscriptNodeKey): { element: HTMLElement; ancestor: boolean } | undefined {
  const encoded = encodeNode(key);
  const elements = [...scroller.querySelectorAll<HTMLElement>("[data-transcript-node]")];
  const exact = elements.find(element => element.dataset.transcriptNode === encoded && element.getBoundingClientRect().height > 0);
  if (exact) return { element: exact, ancestor: false };
  const ancestor = elements.reverse().find(element => element.getBoundingClientRect().height > 0 && element.getAttribute("aria-expanded") === "false" && element.dataset.transcriptMembers
    && (JSON.parse(element.dataset.transcriptMembers) as TranscriptNodeKey[]).some(member => encodeNode(member) === encoded));
  return ancestor ? { element: ancestor, ancestor: true } : undefined;
}

const CONTENT_BLOCKS = "p, pre, li, blockquote, h1, h2, h3, h4, table, img";
function contentBlocks(element: HTMLElement): HTMLElement[] {
  return [...element.querySelectorAll<HTMLElement>(CONTENT_BLOCKS)].filter(block =>
    block.closest("[data-transcript-node]") === element
    && !block.parentElement?.closest(CONTENT_BLOCKS)?.closest("[data-transcript-node]")?.isSameNode(element));
}
export function anchorContentPoint(element: HTMLElement, position: ReadingAnchor["content_position"]): number {
  const blocks = contentBlocks(element);
  const block = blocks[position.block] ?? element;
  return block.getBoundingClientRect().top + Math.min(Math.max(0, position.offset_px), block.getBoundingClientRect().height);
}

export function captureReadingAnchor(scroller: HTMLElement): ReadingAnchor | null {
  const viewport = scroller.getBoundingClientRect();
  const visible = [...scroller.querySelectorAll<HTMLElement>("[data-transcript-node]")].filter(element => {
    const box = element.getBoundingClientRect();
    return box.height > 0 && box.bottom > viewport.top && box.top < viewport.bottom;
  });
  // 选择穿过基准线的最内层节点；下面第一项只能在它确实可见时使用。
  const element = visible.filter(element => element.getBoundingClientRect().top <= viewport.top + 1).at(-1) ?? visible[0];
  if (!element?.dataset.transcriptNode) return null;
  const blocks = contentBlocks(element);
  const blockIndex = Math.max(0, blocks.findIndex(block => block.getBoundingClientRect().bottom > viewport.top));
  const block = blocks[blockIndex] ?? element;
  const offset = Math.min(Math.max(0, viewport.top - block.getBoundingClientRect().top), block.getBoundingClientRect().height);
  return { node_key: JSON.parse(element.dataset.transcriptNode) as TranscriptNodeKey,
    content_position: { block: blockIndex, offset_px: offset },
    viewport_offset_px: block.getBoundingClientRect().top + offset - viewport.top };
}

/** 未提交的流节点只在当前编辑器内保位，跨重启只保存真实过程标题。 */
export function persistentReadingAnchor(scroller: HTMLElement, anchor: ReadingAnchor | null): ReadingAnchor | null {
  if (!anchor || anchor.node_key.source_ref.kind !== "stream") return anchor;
  let key = anchor.node_key;
  const nodes = [...scroller.querySelectorAll<HTMLElement>("[data-transcript-node]")];
  while (key.source_ref.kind === "stream") {
    const element = nodes.find(node => node.dataset.transcriptNode === encodeNode(key));
    if (!element?.dataset.transcriptParent) return null;
    key = JSON.parse(element.dataset.transcriptParent) as TranscriptNodeKey;
  }
  return {node_key: key, content_position: {block: 0, offset_px: 0}, viewport_offset_px: anchor.viewport_offset_px};
}
