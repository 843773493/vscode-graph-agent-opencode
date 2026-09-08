import { describe, expect, test } from "bun:test";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { appendInspectionPage, emptyInspectionPage, type ContextItem } from "./pagination";

const resource = "boxteam://session/ses_test#assembly=assembly-test";
function item(text: string): ContextItem {
  return { kind: "message", locator: resource, text, tool_summary: [], tool_calls: [], tool_results: [] };
}
function page(items: ContextItem[], changes: Partial<SessionContextReadResultDTO> = {}): SessionContextReadResultDTO {
  return { resource, view: "assembly", revision: "sealed", items, partial_errors: [], has_more: false, ...changes };
}

describe("冻结上下文只拼接服务端分页", () => {
  test("不按文字或 identity 重新排序", () => {
    const first = appendInspectionPage(emptyInspectionPage(), page([item("z")], { has_more: true, next_cursor: "next" }), resource);
    const last = appendInspectionPage(first, page([item("a")]), resource);
    expect(last.items.map((value) => value.text)).toEqual(["z", "a"]);
  });
  test("Unicode 分片使用后端 code point 偏移并完整还原", () => {
    const source = item("😀中文正文");
    const codePoints = [...JSON.stringify(source)];
    const split = codePoints.indexOf("😀") + 1;
    const first = appendInspectionPage(emptyInspectionPage(), page([{
      ...item(codePoints.slice(0, split).join("")), kind: "message_chunk", data: { chunk_start: 0 }, truncated: true,
    }], { has_more: true, next_cursor: "next" }), resource);
    expect(first.items).toEqual([]);
    const last = appendInspectionPage(first, page([{
      ...item(codePoints.slice(split).join("")), kind: "message_chunk", data: { chunk_start: split }, truncated: false,
    }]), resource);
    expect(last.items).toEqual([source]);
  });
  test("跨 resource 的迟到响应拒绝", () => {
    expect(() => appendInspectionPage(emptyInspectionPage(), page([]), "another-session")).toThrow("resource");
  });
  test("跨 revision 拒绝", () => {
    const first = appendInspectionPage(emptyInspectionPage(), page([]), resource);
    expect(() => appendInspectionPage(first, page([], { revision: "changed" }), resource)).toThrow("revision");
  });
  test("分页不推进明确报错", () => {
    const first = appendInspectionPage(emptyInspectionPage(), page([], { has_more: true, next_cursor: "same" }), resource);
    expect(() => appendInspectionPage(first, page([], { has_more: true, next_cursor: "same" }), resource)).toThrow("cursor");
  });
  test("尾部分片不完整不伪造成功", () => {
    expect(() => appendInspectionPage(emptyInspectionPage(), page([{
      ...item("partial"), kind: "selection_chunk", truncated: true, data: { chunk_start: 0 },
    }]), resource)).toThrow("未完成");
  });
  test("partial errors 不当作完整快照", () => {
    expect(() => appendInspectionPage(emptyInspectionPage(), page([], { partial_errors: [{ resource, error: "missing" }] }), resource)).toThrow("不完整");
  });
});
