import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionFileTreeSettings } from "../../types/backend";
import { readFilePathTextFromClipboardData } from "../../utils/clipboard";
import WorkspaceFileTree, {
  parseClipboardFilePaths,
  runCurrentAndDefaultShortcutMutation,
} from "./WorkspaceFileTree";

const emptySettings: SessionFileTreeSettings = {
  session_id: "ses_file_tree",
  session_shortcuts: [],
  workspace_shortcuts: [],
  default_shortcuts: [],
  effective_shortcuts: [],
};


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

  test("从浏览器原生 paste 事件读取文件路径", () => {
    const values: Record<string, string> = {
      "text/uri-list": "file:///home/hyf/project%20one\nfile:///home/hyf/project-two",
      "text/plain": "/ignored/plain/path",
    };
    const text = readFilePathTextFromClipboardData({
      types: Object.keys(values),
      getData: (type) => values[type] ?? "",
    });

    expect(parseClipboardFilePaths(text)).toEqual([
      "/home/hyf/project one",
      "/home/hyf/project-two",
    ]);
  });

  test("快捷添加依次更新当前会话和工作区默认配置", async () => {
    const calls: string[] = [];

    const result = await runCurrentAndDefaultShortcutMutation(
      async () => {
        calls.push("add-current-session");
        return emptySettings;
      },
      async () => {
        calls.push("update-workspace-default");
        return emptySettings;
      },
      async () => {
        calls.push("recover");
      },
    );

    expect(result).toBe(emptySettings);
    expect(calls).toEqual([
      "add-current-session",
      "update-workspace-default",
    ]);
  });

  test("快捷删除失败后重新读取后端权威状态", async () => {
    const calls: string[] = [];
    const failure = new Error("workspace config failed");

    await expect(runCurrentAndDefaultShortcutMutation(
      async () => {
        calls.push("remove-current-session");
        return emptySettings;
      },
      async () => {
        calls.push("remove-workspace-default");
        throw failure;
      },
      async () => {
        calls.push("recover");
      },
    )).rejects.toBe(failure);
    expect(calls).toEqual([
      "remove-current-session",
      "remove-workspace-default",
      "recover",
    ]);
  });
});
