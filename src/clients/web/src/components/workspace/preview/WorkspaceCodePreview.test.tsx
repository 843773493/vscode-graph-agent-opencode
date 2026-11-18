import { expect, test, jest } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import WorkspaceCodePreview, { WorkspaceCodePreviewContext, type WorkspaceCodePreviewProps } from "./WorkspaceCodePreview";

test("同一文件的草稿和列定位交给宿主，切换文件与卸载释放旧编辑器", () => {
  const handles: Array<{ update: ReturnType<typeof jest.fn>; dispose: ReturnType<typeof jest.fn> }> = [];
  const render = jest.fn(() => {
    const handle = { update: jest.fn(), dispose: jest.fn() }; handles.push(handle); return handle;
  });
  const host = {};
  const props: WorkspaceCodePreviewProps = { path: "a.ts", content: "中文🙂", language: "typescript", readOnly: true,
    selection: { startLine: 12, endLine: 12, startColumn: 7 }, onBeginEdit() {}, onChange() {}, onSave() {} };
  const view = (value: WorkspaceCodePreviewProps) => <WorkspaceCodePreviewContext.Provider value={render}>
    <WorkspaceCodePreview {...value} />
  </WorkspaceCodePreviewContext.Provider>;
  let root!: ReactTestRenderer;
  act(() => { root = create(view(props), { createNodeMock: () => host }); });
  expect(render).toHaveBeenCalledWith(host, props);
  const draft = { ...props, content: "草稿", readOnly: false };
  act(() => root.update(view(draft)));
  expect(render).toHaveBeenCalledTimes(1);
  expect(handles[0].update).toHaveBeenLastCalledWith(draft);
  act(() => root.update(view({ ...draft, path: "b.ts" })));
  expect(handles[0].dispose).toHaveBeenCalledTimes(1);
  expect(render).toHaveBeenCalledTimes(2);
  act(() => root.unmount());
  expect(handles[1].dispose).toHaveBeenCalledTimes(1);
});
