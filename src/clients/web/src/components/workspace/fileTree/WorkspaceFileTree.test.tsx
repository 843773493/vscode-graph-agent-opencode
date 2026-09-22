import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { SessionFileTreeSettings } from "../../../types/backend";
import { readFilePathTextFromClipboardData } from "../../../utils/clipboard";
import WorkspaceFileTree from "./WorkspaceFileTree";
import { runCurrentAndDefaultShortcutMutation } from "./useWorkspaceFileTreeContextMenu";
import { parseClipboardFilePaths } from "./workspaceFileTreePaths";

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
        onCloseSearch={() => {}}
        onOpenFile={() => {}}
        onStatusChange={() => {}}
      />,
    );

    expect(html).toContain("project");
    expect(html).toContain(">/<");
    expect(html).not.toContain("文件系统");
  });

  test("文件树加载提示位于整个目录之后，不推移目录行", () => {
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
        expandedPaths={[]}
        onExpandedPathsChange={() => {}}
        onCloseSearch={() => {}}
        onOpenFile={() => {}}
        onStatusChange={() => {}}
      />,
    );

    const treeIndex = html.indexOf('role="tree"');
    const loadingIndex = html.indexOf("正在加载工作区文件");

    expect(treeIndex).toBeGreaterThanOrEqual(0);
    expect(loadingIndex).toBeGreaterThan(treeIndex);
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

  test("file 地址无法被 URL 解析时给出中文错误而不是原生 TypeError", () => {
    expect(() => parseClipboardFilePaths("file://%zz")).toThrow(
      "剪贴板中的 file 地址无法解析: file://%zz",
    );
  });

  test("file 地址含非法百分号转义时给出中文错误而不是原生 URIError", () => {
    expect(() => parseClipboardFilePaths("file:///a%")).toThrow(
      "剪贴板中的 file 地址包含非法百分号转义: file:///a%",
    );
    expect(() => parseClipboardFilePaths("file:///a%2")).toThrow("非法百分号转义");
  });

  test("file 地址解码后含空字符时在入口拒绝", () => {
    // %00 会被 decodeURIComponent 还原成 NUL，后端 Path.resolve 只会抛英文
    // "embedded null byte"，必须在剪贴板入口拦下并说明原始输入。
    expect(() => parseClipboardFilePaths("file:///tmp/x%00y")).toThrow(
      "剪贴板中的文件路径包含空字符: file:///tmp/x%00y",
    );
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
