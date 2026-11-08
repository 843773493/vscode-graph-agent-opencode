import "fake-indexeddb/auto";
import { expect, test } from "bun:test";
import { readBrowserView, updateBrowserView } from "../browserViewStore";
import { composerDraftScopeKey, readComposerDraft, writeComposerDraft } from "./storage";

test("空记录、合法正文和明确清空继续使用同一草稿存储", async () => {
  const key = composerDraftScopeKey("valid-draft", "session", "gateway", "a")!;
  expect(await readComposerDraft(null)).toBe("");
  expect(await readComposerDraft(key)).toBe("");
  await writeComposerDraft(key, "原生路径 /work/project 与资源 URI 都是用户正文");
  expect(await readComposerDraft(key)).toBe("原生路径 /work/project 与资源 URI 都是用户正文");
  await writeComposerDraft(key, "");
  expect(await readBrowserView(`draft:${key}`)).toBeUndefined();
});

test.each([
  { label: "null", value: null }, { label: "布尔值", value: false }, { label: "数字", value: 7 },
  { label: "数组", value: ["旧内容"] }, { label: "未知版本对象", value: { version: 2, text: "未来版本草稿" } },
])(
  "不可读草稿 $label 明确报错，后续写入或清空均保留原件", async ({ value: original }) => {
    const key = composerDraftScopeKey(`unknown-draft-${JSON.stringify(original)}`, "session", "gateway", "a")!;
    await writeComposerDraft(key, "通过原 writer 创建的草稿");
    // 只在存储边界注入未知格式，不替换生产 reader／writer 或事务。
    await updateBrowserView<unknown>(`draft:${key}`, () => original);
    await expect(readComposerDraft(key)).rejects.toThrow("草稿格式无法识别");
    for (const next of ["不得覆盖原件", ""]) {
      await expect(writeComposerDraft(key, next)).rejects.toThrow("原记录");
      expect(await readBrowserView<typeof original>(`draft:${key}`)).toEqual(original);
    }
    const other = composerDraftScopeKey(`unknown-draft-${JSON.stringify(original)}`, "session", "gateway", "b")!;
    await writeComposerDraft(other, "其它 Thread 继续保存");
    expect(await readComposerDraft(other)).toBe("其它 Thread 继续保存");
  });
