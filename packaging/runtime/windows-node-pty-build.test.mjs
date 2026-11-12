import { describe, expect, test } from "bun:test";
import { rebuildWindowsNodePty } from "./windows-node-pty-build.mjs";

describe("Windows node-pty 构建", () => {
  test("选择空闲盘符运行 node-gyp，并在结束时释放映射", () => {
    const calls = [];
    rebuildWindowsNodePty({
      applicationRoot: "C:\\stage\\app",
      nodeExecutable: "C:\\stage\\node.exe",
      nodeGypEntrypoint: "C:\\repo\\node-gyp.js",
      exists: (path) => path === "Z:\\" || path === "Y:\\",
      run: (...args) => calls.push(args),
    });

    expect(calls).toEqual([
      ["subst.exe", ["X:", "C:\\stage\\app"]],
      [
        "C:\\stage\\node.exe",
        ["C:\\repo\\node-gyp.js", "rebuild"],
        { cwd: "X:\\node_modules\\node-pty" },
      ],
      ["subst.exe", ["X:", "/d"]],
    ]);
  });

  test("node-gyp 失败时仍释放映射并保留原错误", () => {
    const calls = [];
    const buildError = new Error("node-gyp failed");

    expect(() => rebuildWindowsNodePty({
      applicationRoot: "C:\\stage\\app",
      nodeExecutable: "C:\\stage\\node.exe",
      nodeGypEntrypoint: "C:\\repo\\node-gyp.js",
      exists: () => false,
      run: (...args) => {
        calls.push(args);
        if (calls.length === 2) throw buildError;
      },
    })).toThrow(buildError);

    expect(calls.at(-1)).toEqual(["subst.exe", ["Z:", "/d"]]);
  });
});
