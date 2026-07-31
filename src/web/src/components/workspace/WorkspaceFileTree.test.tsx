import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import WorkspaceFileTree, { parseClipboardFilePaths } from "./WorkspaceFileTree";


describe("工作区文件树根节点", () => {
  test("默认同时展示当前工作区和纯路径根目录", () => {
    const html = renderToStaticMarkup(
      <WorkspaceFileTree
        active
        apiPort={8014}
        workspaceId="gw_workspace"
        workspaceName="project"
        workspaceRoot="/workspace/project"
        sessionId="ses_file_tree"
        activeFilePath={null}
        searchOpen={false}
        collapseVersion={0}
        expandedPaths={[""]}
        onExpandedPathsChange={() => {}}
        onOpenFile={() => {}}
        onStatusChange={() => {}}
      />,
    );

    expect(html).toContain("project");
    expect(html).toContain(">/<");
    expect(html).not.toContain("文件系统");
  });

  test("解析纯路径、file URI 和多文件剪贴板", () => {
    expect(parseClipboardFilePaths("/home/hyf/.cache/model.bin")).toEqual([
      "/home/hyf/.cache/model.bin",
    ]);
    expect(parseClipboardFilePaths(
      "copy\nfile:///home/hyf/torch%20home\nfile:///home/hyf/data",
    )).toEqual([
      "/home/hyf/torch home",
      "/home/hyf/data",
    ]);
  });

  test("拒绝把普通文本当成可粘贴路径", () => {
    expect(() => parseClipboardFilePaths("not-a-path")).toThrow("绝对文件路径");
  });
});
