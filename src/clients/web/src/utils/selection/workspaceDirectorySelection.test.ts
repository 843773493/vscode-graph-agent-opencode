import { describe, expect, test } from "bun:test";

import {
  workspaceDirectoryMatchesQuery,
  workspacePathSearchParts,
} from "./workspaceDirectorySelection";

describe("工作区目录选择路径行为", () => {
  test.each([
    [" /repo/ap ", "/repo/", "ap"],
    ["/repo/", "/repo/", ""],
    ["/", "/", ""],
    ["alpha", "", "alpha"],
    [".", ".", ""],
    ["..", "..", ""],
    ["~", "~", ""],
    ["relative/ap", "relative/", "ap"],
    ["C:\\projects\\ap", "C:\\projects\\", "ap"],
    ["C:\\", "C:\\", ""],
    ["\\\\host\\share\\ap", "\\\\host\\share\\", "ap"],
    ["/repo/literal\\name", "/repo/", "literal\\name"],
    ["boxteam://gateway/gateway_00000000000070008000000000000001/resources/files/00000000000070008000000000000001/space%20name", "boxteam://gateway/gateway_00000000000070008000000000000001/resources/files/00000000000070008000000000000001/space%20name", ""],
  ])("保留目标文本 %s", (input, parentPath, query) => {
    expect(workspacePathSearchParts(input)).toEqual({ parentPath, query });
  });

  test("目录查询同时支持包含匹配与顺序模糊匹配", () => {
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "agent")).toBe(true);
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "gpa")).toBe(true);
    expect(workspaceDirectoryMatchesQuery("GraphAgent", "zzz")).toBe(false);
  });
});
