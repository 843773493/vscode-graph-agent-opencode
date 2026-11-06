import { describe, expect, test } from "bun:test";
import path from "node:path";

import { DEVELOPMENT_BASE_PORTS, remainingOccupiedPorts, resolveDevelopmentLayout } from "./dev-environment.mjs";

test("开发端口布局包含主服务、辅助服务和调试监听端口", () => {
  expect(DEVELOPMENT_BASE_PORTS).toEqual({
    backend: 8010,
    frontend: 8011,
    terminalBackend: 8012,
    terminalFrontend: 8013,
    gateway: 8014,
    browserBackend: 8015,
    browserFrontend: 8016,
    backendDebug: 8002,
  });
});

describe("开发 Node 宿主 manifest 参数", () => {
  test("缺少参数时默认不提供 Node 宿主", () => {
    expect(resolveDevelopmentLayout({ cwd: "/project", args: [] }).extensionHost).toBeNull();
  });

  test("两个参数成对解析并相对于当前仓库目录冻结", () => {
    expect(resolveDevelopmentLayout({
      cwd: "/project",
      args: ["--extension-host-node", "runtime/node", "--extension-host-component=build/extension-host"],
    }).extensionHost).toEqual({
      nodeExecutable: path.resolve("/project", "runtime/node"),
      componentRoot: path.resolve("/project", "build/extension-host"),
    });
  });

  test("单独提供或空值明确拒绝", () => {
    expect(() => resolveDevelopmentLayout({ cwd: "/project", args: ["--extension-host-node", "runtime/node"] })).toThrow("必须成对");
    expect(() => resolveDevelopmentLayout({ cwd: "/project", args: ["--extension-host-node=", "--extension-host-component=component"] })).toThrow("非空路径");
    expect(() => resolveDevelopmentLayout({ cwd: "/project", args: ["--extension-host-node", "node", "--extension-host-component="] })).toThrow("非空路径");
  });
});

describe("开发端口清理后的占用核实", () => {
  test("仍被占用的端口连同监听 PID 一起返回", () => {
    const occupied = remainingOccupiedPorts([9501, 9502, 9503], (port) =>
      port === 9502 ? ["1234"] : port === 9503 ? ["5678", "9012"] : [],
    );

    expect(occupied).toEqual([
      { port: 9502, pids: ["1234"] },
      { port: 9503, pids: ["5678", "9012"] },
    ]);
  });

  test("全部释放时返回空列表", () => {
    expect(remainingOccupiedPorts([9501, 9502], () => [])).toEqual([]);
  });
});
