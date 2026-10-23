import { describe, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";

async function observeHandoff(scenario, supervisorUrl, controlUrl, lifetimeUrl) {
  const { default: assert } = await import("node:assert/strict");
  const { EventEmitter, once } = await import("node:events");
  const { createServer } = await import("node:http");
  const { default: net } = await import("node:net");
  const { existsSync } = await import("node:fs");
  const { requestGatewayHandoff, superviseGateway } = await import(supervisorUrl);
  const { createGatewaySupervisorControl, gatewaySupervisorControlAddress } = await import(controlUrl);
  const { listenServer, closeServer } = await import(lifetimeUrl);
  const boxteamHome = process.cwd();
  const controlAddress = gatewaySupervisorControlAddress(boxteamHome);
  const environment = {
    BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
    BOXTEAM_CONFIG_GENERATION: "generation-2",
    BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
  };
  async function within(promise, phase) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("handoff fixture超时: " + phase)), 1000);
      })]);
    } finally { clearTimeout(timer); }
  }
  if (scenario === "control-result") {
    const calls = [];
    const control = createGatewaySupervisorControl({
      address: controlAddress,
      onHandoff: async (payload) => {
        calls.push(payload);
        return { accepted: true, target_generation: payload.target_generation };
      },
    });
    try {
      await control.listen();
      assert.deepEqual(await within(requestGatewayHandoff({ boxteamHome, environment, timeoutMs: 500 }), "控制结果"), {
        handled: true, data: { accepted: true, target_generation: "generation-2" },
      });
      assert.deepEqual(calls, [{
        type: "gateway_pending_handoff", candidate_ref: "candidate-ref",
        target_generation: "generation-2", fencing_token: "fence-2",
      }]);
    } finally { await control.close(); }
    if (process.platform !== "win32") assert.equal(existsSync(controlAddress), false);
    return;
  }
  const probe = net.createServer();
  await listenServer(probe, { host: "127.0.0.1", port: 0 });
  const publicPort = probe.address().port;
  await closeServer(probe);
  const processObject = new EventEmitter();
  const children = [];
  const runtime = {
    distribution: "source-development", version: "test-version",
    manifestPath: "/runtime/runtime-manifest.json", pythonExecutable: "/runtime/python",
    applicationRoot: "/runtime/application", nodeExecutable: process.execPath,
    webAssets: null, chromiumExecutable: null,
  };
  const running = superviseGateway({
    runtime, environment: { BOXTEAM_HOME: boxteamHome, BOXTEAM_GATEWAY_PORT: String(publicPort) },
    openBrowser: false, stdout: { write() {} }, stderr: { write() {} }, processObject,
    spawnImpl(_command, args, options) {
      const child = new EventEmitter();
      child.exitCode = null;
      child.signalCode = null;
      child.killedWith = [];
      const isPending = options.env.BOXTEAM_CONFIG_CANDIDATE_REF !== undefined;
      if (isPending) {
        assert.equal(children[0].child.exitCode, null);
        assert.deepEqual(children[0].child.killedWith, []);
      }
      const server = createServer((request, response) => {
        if (scenario === "failed-handoff" && isPending && request.url === "/api/gateway/health") {
          response.writeHead(503);
          response.end("pending failed");
          setImmediate(() => exitChild(1));
          return;
        }
        response.end(isPending ? "pending" : "active");
      });
      function exitChild(code) {
        if (child.exitCode !== null) return;
        child.exitCode = code;
        server.closeAllConnections();
        server.close(() => {
          child.emit("exit", code, null);
          child.emit("close", code, null);
        });
      }
      child.kill = (signal) => {
        child.killedWith.push(signal);
        exitChild(signal === "SIGKILL" ? 137 : 0);
      };
      children.push({ child, args, environment: options.env, server });
      const privatePort = Number(args[args.indexOf("--port") + 1]);
      server.once("error", (error) => child.emit("error", error));
      server.listen(privatePort, "127.0.0.1");
      return child;
    },
  }).then((value) => ({ value }), (error) => ({ error }));
  async function waitForControl() {
    const deadline = performance.now() + 1000;
    while (performance.now() < deadline) {
      const socket = net.createConnection(controlAddress);
      try {
        await once(socket, "connect");
        const closed = once(socket, "close");
        socket.end();
        await closed;
        return;
      } catch (error) {
        if (!["ENOENT", "ECONNREFUSED"].includes(error.code)) throw error;
      } finally { socket.destroy(); }
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    throw new Error("控制监听未在fixture期限内就绪");
  }
  async function readGeneration() {
    const response = await fetch("http://127.0.0.1:" + publicPort + "/generation", {
      signal: AbortSignal.timeout(500),
    });
    assert.equal(response.status, 200);
    return response.text();
  }
  try {
    await within(waitForControl(), "控制监听就绪");
    assert.equal(await readGeneration(), "active");
    const handoff = await within(requestGatewayHandoff({ boxteamHome, environment, timeoutMs: 500 }), "generation切换");
    if (scenario === "failed-handoff") {
      assert.equal(handoff.handled, true);
      assert.equal(handoff.data.accepted, false);
      assert.equal(await readGeneration(), "active");
      assert.deepEqual(children[0].child.killedWith, []);
    } else {
      assert.deepEqual(handoff, {
        handled: true, data: { accepted: true, target_generation: "generation-2" },
      });
      assert.equal(children.length, 2);
      assert.deepEqual(children[0].child.killedWith, ["SIGTERM"]);
      assert.equal(children[1].environment.BOXTEAM_CONFIG_CANDIDATE_REF, "candidate-ref");
      const portIndex = children[1].args.indexOf("--port");
      assert(portIndex > -1);
      assert.notEqual(children[1].args[portIndex + 1], String(publicPort));
      assert.equal(await readGeneration(), "pending");
    }
    processObject.emit("SIGTERM");
    assert.deepEqual(await within(running, "supervisor退出"), { value: 0 });
  } finally {
    processObject.emit("SIGTERM");
    for (const { child } of children) if (child.exitCode === null) child.kill("SIGTERM");
    await within(running, "supervisor清理");
    for (const { server } of children) {
      server.closeAllConnections();
      await closeServer(server);
    }
  }
  assert.equal(processObject.listenerCount("SIGTERM"), 0);
  if (process.platform !== "win32") assert.equal(existsSync(controlAddress), false);
}

function runHandoff(scenario) {
  const fixtureRoot = path.resolve(
    "out/tests/packages/launcher/src/gateway/handoff/workspace", randomUUID(),
  );
  mkdirSync(fixtureRoot, { recursive: true });
  try {
    execFileSync("node", ["--input-type=module", "--eval", `
      ${observeHandoff.toString()}
      await observeHandoff(...JSON.parse(process.argv[1]));
    `, JSON.stringify([scenario, ...["supervisor", "control-channel", "server-lifetime"].map(
      (name) => new URL("./" + name + ".mjs", import.meta.url).href,
    )])], {
      encoding: "utf8", timeout: 5000, cwd: fixtureRoot,
      env: { ...process.env, TMPDIR: ".", TMP: ".", TEMP: "." },
    });
  } finally { rmSync(fixtureRoot, { recursive: true, force: true }); }
}

describe("Gateway真实控制通道与generation切换", () => {
  test("Gateway supervisor control socket 返回 handoff 结果", () => runHandoff("control-result"));
  test("pending listener handoff 失败时保留旧 public generation", () => runHandoff("failed-handoff"));
  test("supervisor 在旧 generation 仍运行时完成 pending listener handoff", () => runHandoff("accepted-handoff"));
});
