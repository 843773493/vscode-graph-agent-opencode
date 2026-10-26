import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { gatewaySupervisorControlAddress } from "./control-channel.mjs";

const fixtureRoot = path.resolve("out/tests/packages/launcher/src/gateway/control-channel/workspace");

async function observeControlFilesystem(transport, controlUrl) {
  const { default: assert } = await import("node:assert/strict");
  const { default: fs } = await import("node:fs");
  const { default: net } = await import("node:net");
  const { EventEmitter } = await import("node:events");
  const { syncBuiltinESMExports } = await import("node:module");
  const { createGatewaySupervisorControl, gatewaySupervisorControlAddress } = await import(controlUrl);
  const originalCreateServer = net.createServer;
  const originals = Object.fromEntries(["mkdirSync", "rmSync", "chmodSync"].map((key) => [key, fs[key]]));
  const filesystemCalls = [];
  const listenerAddresses = [];
  const platform = transport === "pipe" ? "win32" : "linux";
  const address = transport === "pipe"
    ? gatewaySupervisorControlAddress("C:\\工作区\\Box Team", { platform })
    : "fixture-control.sock";
  const server = new EventEmitter();
  server.listening = false;
  server.listen = (address) => {
    listenerAddresses.push(address);
    server.listening = true;
    queueMicrotask(() => server.emit("listening"));
  };
  server.close = (callback) => { server.listening = false; callback(); };
  try {
    net.createServer = () => server;
    for (const key of Object.keys(originals)) fs[key] = (...args) => { filesystemCalls.push([key, ...args]); };
    syncBuiltinESMExports();
    const control = createGatewaySupervisorControl({ address, onHandoff: async () => ({ accepted: true }) });
    await control.listen();
    await control.close();
    assert.deepEqual(listenerAddresses, [address]);
    assert.equal(server.listening, false);
    if (transport === "pipe") assert.deepEqual(filesystemCalls, []);
    else assert.deepEqual(filesystemCalls, [
      ["mkdirSync", ".", { recursive: true, mode: 0o700 }],
      ["rmSync", address, { force: true }],
      ["chmodSync", address, 0o600],
      ["rmSync", address, { force: true }],
    ]);
  } finally {
    net.createServer = originalCreateServer;
    for (const [key, value] of Object.entries(originals)) fs[key] = value;
    syncBuiltinESMExports();
  }
}

describe("Gateway控制端点的平台与路径归属", () => {
  test("未提供home时不建立控制通道", () => {
    for (const value of [null, undefined, "", "   "]) {
      expect(gatewaySupervisorControlAddress(value)).toBeNull();
    }
  });
  test("POSIX短home保持原state socket位置", () => {
    for (const platform of ["linux", "darwin"]) {
      expect(gatewaySupervisorControlAddress("/boxteam", { platform }))
        .toBe("/boxteam/state/gateway-supervisor.sock");
    }
  });
  test("长home按规范化归属使用同一有界临时socket", () => {
    const options = { platform: process.platform === "win32" ? "darwin" : process.platform, temporaryDirectory: "/ipc" };
    const home = path.posix.resolve("/", fixtureRoot.replaceAll("\\", "/"), "long-home-".repeat(10));
    const address = gatewaySupervisorControlAddress(home, options);
    expect(address).toMatch(/^\/ipc\/boxteam-gateway-supervisor-[a-f0-9]{32}\.sock$/);
    expect(Buffer.byteLength(address)).toBeLessThanOrEqual(100);
    expect(gatewaySupervisorControlAddress(home + "/.", options)).toBe(address);
    expect(gatewaySupervisorControlAddress(home + "-other", options)).not.toBe(address);
  });
  test("临时socket仍过长时在listen前明确拒绝", () => {
    expect(() => gatewaySupervisorControlAddress("/long-home/".repeat(20), {
      platform: "linux", temporaryDirectory: "/long-temp/".repeat(20),
    })).toThrow("路径超过 100 字节");
  });
  test("Windows使用按home稳定区分的命名管道", () => {
    const options = { platform: "win32" };
    const home = "C:\\工作区\\Box Team";
    const address = gatewaySupervisorControlAddress(home, options);
    expect(address.startsWith("\\\\.\\pipe\\boxteam-gateway-supervisor-")).toBe(true);
    expect(gatewaySupervisorControlAddress(home + "\\.", options)).toBe(address);
    expect(gatewaySupervisorControlAddress(home + "-other", options)).not.toBe(address);
  });
  for (const [transport, description] of [["pipe", "命名管道不执行文件创建chmod或删除"], ["unix", "Unix socket保持创建权限及关闭清理"]]) {
    test(description, () => {
      // 文件与server为替身，只证明接线；Windows原生命名管道另验。
      execFileSync("node", ["--input-type=module", "--eval", `
        ${observeControlFilesystem.toString()}
        await observeControlFilesystem(...JSON.parse(process.argv[1]));
      `, JSON.stringify([transport, new URL("./control-channel.mjs", import.meta.url).href])], {
        encoding: "utf8", timeout: 5000,
      });
    });
  }
});
