import "fake-indexeddb/auto";
import React from "react";
import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useComposerDraft, useComposerAttachments } from "./useComposerDraft";
import { composerDraftScopeKey, readComposerDraft, writeComposerDraft } from "../../state/composerDrafts/storage";
import * as drafts from "../../state/composerDrafts/storage";
import { readBrowserView } from "../../state/browserViewStore";
import type { SelectedAttachment } from "../../utils/media/mediaAttachments";

const renderers: ReactTestRenderer[] = [];
afterEach(() => { for (const renderer of renderers.splice(0)) act(() => renderer.unmount()); });
const flush = () => new Promise(resolve => setTimeout(resolve, 20));

async function mount(workspace: string, initialThread = "a") {
  const values = new Map<string, string>();
  const setters = new Map<string, React.Dispatch<React.SetStateAction<string>>>();
  const errors: string[] = [];
  function Draft({ thread }: { thread: string }) {
    const [value, update] = useComposerDraft(workspace, "session", "gateway", thread, message => errors.push(message));
    values.set(thread, value); setters.set(thread, update);
    return <span>{value}</span>;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<Draft thread={initialThread} />); await flush(); });
  renderers.push(renderer);
  return { values, setters, errors, renderer, Draft };
}

test("未选择用户也保存草稿，两个 Thread 切换及重新挂载分别恢复", async () => {
  const m = await mount("thread-drafts");
  await act(async () => { m.setters.get("a")!("A 未发送"); await flush(); });
  await act(async () => { m.renderer.update(<m.Draft thread="b" />); await flush(); });
  expect(m.values.get("b")).toBe("");
  await act(async () => { m.setters.get("b")!("B 未发送"); await flush(); });
  await act(async () => { m.renderer.update(<m.Draft thread="a" />); await flush(); });
  expect(m.values.get("a")).toBe("A 未发送");
  const other = await mount("thread-drafts", "b");
  expect(other.values.get("b")).toBe("B 未发送");
  await writeComposerDraft(composerDraftScopeKey("guest-draft", "session", null), "无需登录");
  expect(await readComposerDraft(composerDraftScopeKey("guest-draft", "session", null))).toBe("无需登录");
});

test("恢复中的旧读取不能覆盖新输入，也不能跨 Thread 回填", async () => {
  let finish!: (value: string) => void;
  const reader = spyOn(drafts, "readComposerDraft").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  try {
    const m = await mount("pending-draft");
    await act(async () => { m.setters.get("a")!("用户新输入"); finish("旧值"); await flush(); });
    expect(m.values.get("a")).toBe("用户新输入");
    expect(await readBrowserView<string>(`draft:${composerDraftScopeKey("pending-draft", "session", "gateway", "a")}`)).toBe("用户新输入");
  } finally { reader.mockRestore(); }
});

test("超长输入保留在界面，明确报告未保存且不覆盖已有草稿", async () => {
  const key = composerDraftScopeKey("overflow", "session", "gateway", "a");
  await writeComposerDraft(key, "已保存原值");
  const m = await mount("overflow");
  await act(async () => { m.setters.get("a")!("x".repeat(100_001)); await flush(); });
  expect(m.values.get("a")?.length).toBe(100_001);
  expect(m.errors.join()).toContain("草稿尚未保存");
  expect(await readComposerDraft(key)).toBe("已保存原值");
});

test("存储错误不会伪装保存成功，当前输入继续可编辑", async () => {
  const writer = spyOn(drafts, "writeComposerDraft").mockRejectedValue(new Error("磁盘配额不足"));
  try {
    const m = await mount("quota");
    await act(async () => { m.setters.get("a")!("保留输入"); await flush(); });
    expect(m.values.get("a")).toBe("保留输入");
    expect(m.errors.join()).toContain("磁盘配额不足");
  } finally { writer.mockRestore(); }
});

test("未发送附件在卸载后从 IndexedDB 恢复，清空会删除记录", async () => {
  const key = composerDraftScopeKey("attachments", "session", "gateway", "a");
  let value: SelectedAttachment[] = [], update!: React.Dispatch<React.SetStateAction<SelectedAttachment[]>>;
  function Attachments() { [value, update] = useComposerAttachments(key, message => { throw new Error(message); }); return null; }
  let renderer!: ReactTestRenderer;
  await act(async () => { renderer = create(<Attachments />); await flush(); });
  const attachment = { file_id: "file-draft", name: "图片.png", content_type: "image/png", data_url: "data:image/png;base64,AA==", mediaKind: "image", previewUrl: "data:image/png;base64,AA==" } as SelectedAttachment;
  await act(async () => { update([attachment]); await flush(); renderer.unmount(); });
  await act(async () => { renderer = create(<Attachments />); await flush(); });
  renderers.push(renderer);
  expect(value).toEqual([attachment]);
  await act(async () => { update([]); await flush(); });
  expect(await readBrowserView(`attachments:${key}`)).toBeUndefined();
});

test("切换 Thread 后，前一个 Thread 的在途读取不能显示在当前输入框", async () => {
  const pending = new Map<string, (value: string) => void>();
  const reader = spyOn(drafts, "readComposerDraft").mockImplementation(key => new Promise(resolve => { pending.set(key!, resolve); }));
  try {
    const m = await mount("switch-pending");
    await act(async () => { m.renderer.update(<m.Draft thread="b" />); await flush(); });
    await act(async () => { pending.get(composerDraftScopeKey("switch-pending", "session", "gateway", "a")!)!("A 的迟到内容"); await flush(); });
    expect(m.values.get("b")).toBe("");
    await act(async () => { pending.get(composerDraftScopeKey("switch-pending", "session", "gateway", "b")!)!("B 的内容"); await flush(); });
    expect(m.values.get("b")).toBe("B 的内容");
  } finally { reader.mockRestore(); }
});
