import { describe, expect, test } from "bun:test";

import {
  normalizeWorkspacePath,
  workspaceDirectoryMatchesQuery,
  workspaceParentPath,
  workspacePathSearchParts,
} from "./workspaceDirectorySelection";

describe("工作区目录选择路径行为", () => {
  test("规范化路径并解析父目录与查询", () => {
    expect(normalizeWorkspacePath(" /repo/app/// ")).toBe("/repo/app");
    expect(workspaceParentPath("/repo/app")).toBe("/repo");
    expect(workspacePathSearchParts("/repo/ap")).toEqual({
      parentPath: "/repo",
      query: "ap",
    });
    expect(workspacePathSearchParts("/repo/")).toEqual({
      parentPath: "/repo",
      query: "",
    });
  });

  test("目录查询同时支持包含匹配与顺序模糊匹配", () => {
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "agent")).toBe(true);
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "gpa")).toBe(true);
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "zzz")).toBe(false);
  });
});
