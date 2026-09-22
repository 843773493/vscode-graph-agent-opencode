import { describe, expect, test } from "bun:test";

import { filesystemFileTreePath } from "../../../api";
import {
  absolutePathForTreePath,
  changedPathToTreePath,
  FILESYSTEM_ROOT_PATH,
  isTreePathInside,
  parentFileTreePath,
  ROOT_PATH,
  shortWorkspaceLabel,
} from "./workspaceFileTreePaths";

describe("文件树路径语义", () => {
  test("工作区路径的父级逐级收敛到根路径", () => {
    expect(parentFileTreePath("")).toBe(ROOT_PATH);
    expect(parentFileTreePath("src")).toBe(ROOT_PATH);
    expect(parentFileTreePath("src/components")).toBe("src");
    expect(parentFileTreePath("src/components/")).toBe("src");
  });

  test("文件系统路径的父级保留 filesystem 作用域并可停在根", () => {
    expect(parentFileTreePath(FILESYSTEM_ROOT_PATH)).toBe(FILESYSTEM_ROOT_PATH);
    expect(parentFileTreePath(filesystemFileTreePath("/home/hyf")))
      .toBe(filesystemFileTreePath("/home"));
    expect(parentFileTreePath(filesystemFileTreePath("C:/Users")))
      .toBe(filesystemFileTreePath("C:/"));
  });

  test("祖先判断按路径边界而不是字符串前缀", () => {
    expect(isTreePathInside("src/a.ts", "src")).toBe(true);
    expect(isTreePathInside("src", "src")).toBe(true);
    expect(isTreePathInside("srcs/a.ts", "src")).toBe(false);
    expect(isTreePathInside("a.ts", ROOT_PATH)).toBe(true);
    expect(isTreePathInside(
      filesystemFileTreePath("/home/hyf/a"),
      filesystemFileTreePath("/home"),
    )).toBe(true);
    expect(isTreePathInside(
      filesystemFileTreePath("/home/hyf/a"),
      "home",
    )).toBe(false);
  });

  test("变更路径按工作区根改写为相对树路径", () => {
    expect(changedPathToTreePath("/workspace/proj/src/a.ts", "/workspace/proj"))
      .toBe("src/a.ts");
    expect(changedPathToTreePath("/workspace/proj", "/workspace/proj")).toBe(ROOT_PATH);
    expect(changedPathToTreePath("./src/a.ts", "/workspace/proj")).toBe("src/a.ts");
    expect(changedPathToTreePath("/other/a.ts", "/workspace/proj"))
      .toBe(filesystemFileTreePath("/other/a.ts"));
    expect(changedPathToTreePath("C:/Proj/SRC/a.ts", "c:/proj")).toBe("SRC/a.ts");
    expect(changedPathToTreePath("../escape.ts", "/workspace/proj")).toBeNull();
    expect(changedPathToTreePath("   ", "/workspace/proj")).toBeNull();
  });

  test("工作区根就是文件系统根时绝对路径退化为相对树路径", () => {
    expect(changedPathToTreePath("/home/hyf/a.ts", "/")).toBe("home/hyf/a.ts");
    expect(changedPathToTreePath("/", "/")).toBe(ROOT_PATH);
  });

  test("绝对路径拼接区分文件系统与工作区作用域", () => {
    expect(absolutePathForTreePath("src/a.ts", "/ws")).toBe("/ws/src/a.ts");
    expect(absolutePathForTreePath(ROOT_PATH, "/ws")).toBe("/ws");
    expect(absolutePathForTreePath(FILESYSTEM_ROOT_PATH, "/ws")).toBe("/");
    expect(() => absolutePathForTreePath("src/a.ts", null))
      .toThrow("当前工作区缺少根目录");
  });

  test("工作区标签优先显示名再退回根目录末段", () => {
    expect(shortWorkspaceLabel("/a/b", "project")).toBe("project");
    expect(shortWorkspaceLabel("/a/b", null)).toBe("b");
    expect(shortWorkspaceLabel(null, null)).toBe("workspace");
  });
});
