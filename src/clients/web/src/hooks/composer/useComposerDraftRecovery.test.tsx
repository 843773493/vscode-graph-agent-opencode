import "fake-indexeddb/auto";
import React from "react";
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import { composerDraftScopeKey, readComposerDraft, writeComposerDraft } from "../../state/composerDrafts/storage";
import { useComposerDraft, type ComposerDraftController } from "./useComposerDraft";

let renderer: ReactTestRenderer | undefined;
afterEach(() => { if (renderer) act(() => renderer!.unmount()); renderer = undefined; });
const flush = () => new Promise(resolve => setTimeout(resolve, 20));

test("未知格式恢复通过原 Hook 报错，不发布附加句柄，新输入不覆盖原件且其它 Thread 正常", async () => {
  const key = composerDraftScopeKey("unreadable-hook", "session", "gateway", "a")!;
  const original = { version: 2, text: "必须保留的旧草稿" };
  await writeComposerDraft(key, "原草稿");
  await updateBrowserView<unknown>(`draft:${key}`, () => original);
  const values = new Map<string, string>(), controllers = new Map<string, ComposerDraftController | undefined>();
  const setters = new Map<string, React.Dispatch<React.SetStateAction<string>>>(), errors: string[] = [];
  function Draft({ thread }: { thread: string }) {
    const [value, update, controller] = useComposerDraft("unreadable-hook", "session", "gateway", thread, message => errors.push(message));
    values.set(thread, value); setters.set(thread, update); controllers.set(thread, controller);
    return <span>{value}</span>;
  }
  await act(async () => { renderer = create(<Draft thread="a" />); await flush(); });
  expect(values.get("a")).toBe("");
  expect(controllers.get("a")).toBeUndefined();
  expect(errors.join()).toContain("草稿格式无法识别");
  expect(await readBrowserView<typeof original>(`draft:${key}`)).toEqual(original);
  await act(async () => { setters.get("a")!("本次输入留在界面"); await flush(); });
  expect(values.get("a")).toBe("本次输入留在界面");
  expect(errors.join()).toContain("草稿尚未保存");
  expect(await readBrowserView<typeof original>(`draft:${key}`)).toEqual(original);
  await act(async () => { renderer!.update(<Draft thread="b" />); await flush(); });
  await act(async () => { setters.get("b")!("B 的正常草稿"); await flush(); });
  expect(await readComposerDraft(composerDraftScopeKey("unreadable-hook", "session", "gateway", "b"))).toBe("B 的正常草稿");
  expect(await readBrowserView<typeof original>(`draft:${key}`)).toEqual(original);
});

test("恢复后存储被换成未知格式时，原附加动作拒绝写入并保留该记录", async () => {
  const key = composerDraftScopeKey("changed-draft-hook", "session", "gateway", "a")!;
  await writeComposerDraft(key, "已恢复正文");
  let value = "", controller: ComposerDraftController | undefined;
  const errors: string[] = [];
  function Draft() {
    [value, , controller] = useComposerDraft("changed-draft-hook", "session", "gateway", "a", message => errors.push(message));
    return <span>{value}</span>;
  }
  await act(async () => { renderer = create(<Draft />); await flush(); });
  expect(value).toBe("已恢复正文");
  expect(controller).toBeDefined();
  const original = { version: 3, text: "另一版本保留的正文" };
  await updateBrowserView<unknown>(`draft:${key}`, () => original);
  await act(async () => { await expect(controller!.append("新附加正文")).rejects.toThrow("草稿格式无法识别"); });
  expect(value).toBe("已恢复正文\n新附加正文");
  expect(errors.join()).toContain("草稿尚未保存");
  expect(await readBrowserView<typeof original>(`draft:${key}`)).toEqual(original);
});
