import { describe, expect, test } from "bun:test";

import {
  changedPathToTreePath,
  isTreePathInside,
  parentFileTreePath,
  parseClipboardFilePaths,
  referenceForTreePath,
  shortWorkspaceLabel,
} from "./workspaceFileTreePaths";

const workspaceRoot = "boxteam://workspace/ws-1/resources/files/root";
const workspaceSource = `${workspaceRoot}/src`;
const gatewayId = "gateway_0123456789abcdef0123456789abcdef";
const gatewayRoot = `boxteam://${gatewayId}/gateway/${gatewayId}/resources/files/0123456789ab7abc8abc012345678901`;
const attachmentRoot = `boxteam://${gatewayId}/workspace/ws-1/resources/attachments/att_00000000000000000000000000000000`;

describe("文件树 URI 路径语义", () => {
  test("文件与根目录的父级按 URI segment 收敛，根停在自身", () => {
    const sourceDirectory = `${workspaceSource}/components`;
    const sourceFile = `${sourceDirectory}/%E4%B8%AD%E6%96%87%20file.ts`;

    expect(parentFileTreePath(workspaceRoot)).toBe(workspaceRoot);
    expect(parentFileTreePath(sourceDirectory)).toBe(workspaceSource);
    expect(parentFileTreePath(sourceFile)).toBe(sourceDirectory);
    expect(parentFileTreePath(gatewayRoot)).toBe(gatewayRoot);
    expect(parentFileTreePath(attachmentRoot)).toBe(attachmentRoot);
    expect(parentFileTreePath(`${attachmentRoot}/original/content`)).toBe(`${attachmentRoot}/original`);
    expect(parentFileTreePath(`${attachmentRoot}/derived`)).toBe(attachmentRoot);
  });

  test("祖先判断使用 URI segment 边界并隔离不同资源根", () => {
    const child = `${workspaceSource}/a.ts`;

    expect(isTreePathInside(child, workspaceSource)).toBe(true);
    expect(isTreePathInside(workspaceSource, workspaceSource)).toBe(true);
    expect(isTreePathInside(`${workspaceRoot}/srcs/a.ts`, workspaceSource)).toBe(false);
    expect(isTreePathInside(child, workspaceRoot)).toBe(true);
    expect(isTreePathInside(child, gatewayRoot)).toBe(false);
    expect(isTreePathInside(`${attachmentRoot}/derived/result.md`, attachmentRoot)).toBe(true);
    expect(isTreePathInside(`${attachmentRoot}1/derived/result.md`, attachmentRoot)).toBe(false);
    expect(isTreePathInside(`${attachmentRoot}/derived/result.md`, workspaceRoot)).toBe(false);
    expect(isTreePathInside(
      "boxteam://workspace/ws-2/resources/files/root/src/a.ts",
      workspaceSource,
    )).toBe(false);
  });

  test("变更路径只接受并原样保留资源 URI", () => {
    const encodedFile = `${workspaceSource}/%E4%B8%AD%E6%96%87%20file.ts`;

    expect(changedPathToTreePath(encodedFile)).toBe(encodedFile);
    expect(changedPathToTreePath(gatewayRoot)).toBe(gatewayRoot);
    expect(() => changedPathToTreePath("/workspace/src/a.ts"))
      .toThrow("文件变更事件缺少资源 URI");
  });

  test("文件引用原样保留 URI，拒绝原生路径", () => {
    const encodedFile = `${workspaceSource}/%E4%B8%AD%E6%96%87%20file.ts`;

    expect(referenceForTreePath(encodedFile)).toBe(encodedFile);
    expect(() => referenceForTreePath("C:/workspace/src/a.ts"))
      .toThrow("文件树尚未取得资源 URI");
  });

  test("剪贴板保留资源 URI 与原生路径入口的既有语义", () => {
    expect(parseClipboardFilePaths(`${workspaceSource}/a.ts\n/workspace/b.ts`))
      .toEqual([`${workspaceSource}/a.ts`, "/workspace/b.ts"]);
  });

  test("工作区标签使用显示名，缺失时显示通用名称", () => {
    expect(shortWorkspaceLabel("/a/b", "project")).toBe("project");
    expect(shortWorkspaceLabel("/a/b", null)).toBe("workspace");
    expect(shortWorkspaceLabel(null, null)).toBe("workspace");
  });
});
