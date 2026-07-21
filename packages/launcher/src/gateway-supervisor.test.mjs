import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";

import {
  gatewayEnvironment,
  installSignalForwarding,
  superviseGateway,
} from "./gateway-supervisor.mjs";

function fakeChild() {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.killedWith = [];
  child.kill = (signal) => {
    child.killedWith.push(signal);
  };
  return child;
}

const runtime = {
  distribution: "source-development",
  version: "0.1.0",
  manifestPath: "/runtime/runtime-manifest.json",
  pythonExecutable: "/runtime/python",
  applicationRoot: "/runtime/application",
  nodeExecutable: "/usr/bin/node",
  webAssets: null,
  chromiumExecutable: "/runtime/chromium",
};

describe("gateway supervisor", () => {
  test("向 Gateway 传入 manifest 资源", () => {
    const environment = gatewayEnvironment(runtime, {
      BOXTEAM_HOME: "/tmp/boxteams",
    });

    expect(environment.BOXTEAM_RUNTIME_MANIFEST).toBe(runtime.manifestPath);
    expect(environment.BOXTEAM_NODE_BIN).toBe("/usr/bin/node");
    expect(environment.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH).toBe(
      "/runtime/chromium",
    );
  });

  test("转发并清理进程信号监听器", () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const remove = installSignalForwarding(child, processObject);

    processObject.emit("SIGTERM");
    expect(child.killedWith).toEqual(["SIGTERM"]);
    remove();
    processObject.emit("SIGTERM");
    expect(child.killedWith).toEqual(["SIGTERM"]);
  });

  test("Gateway 就绪后以前台退出码结束", async () => {
    const child = fakeChild();
    const processObject = new EventEmitter();
    const output = [];
    const resultPromise = superviseGateway({
      runtime,
      environment: {},
      openBrowser: false,
      spawnImpl() {
        setTimeout(() => {
          child.exitCode = 0;
          child.emit("exit", 0, null);
        }, 20);
        return child;
      },
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        statusText: "OK",
      }),
      stdout: {
        write(value) {
          output.push(value);
        },
      },
      stderr: {
        write() {},
      },
      processObject,
    });

    expect(await resultPromise).toBe(0);
    expect(output.join("")).toContain("Gateway 已就绪");
    expect(processObject.listenerCount("SIGTERM")).toBe(0);
  });
});
