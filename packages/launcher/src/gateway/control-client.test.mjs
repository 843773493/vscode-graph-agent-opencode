import { describe, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";

async function observeControlClient(scenario, supervisorUrl) {
  const { default: assert } = await import("node:assert/strict");
  const { default: net } = await import("node:net");
  const { requestGatewayHandoff } = await import(supervisorUrl);
  const originalCreateConnection = net.createConnection;
  const sockets = new Set();
  const requests = [];
  let notifyClientClosed;
  const clientClosed = new Promise((resolve) => { notifyClientClosed = resolve; });
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.once("close", () => {
      sockets.delete(socket);
      notifyClientClosed();
    });
    socket.on("error", () => {});
    socket.setEncoding("utf8");
    let request = "";
    socket.on("data", (chunk) => {
      request += chunk;
      if (!request.includes("\n")) return;
      requests.push(JSON.parse(request.trim()));
      if (scenario === "accepted-open") {
        socket.write('{"ok":true,"data":{"accepted":true,"target_generation":"generation-2"}}\n');
      } else if (scenario === "rejected-open") {
        socket.write('{"ok":false,"error":"pending rejected"}\n');
      } else if (scenario === "invalid-json-open") {
        socket.write('invalid-json\n');
      } else if (scenario === "partial-end") {
        socket.end('{"ok":true');
      } else if (scenario === "empty-end") {
        socket.end();
      } else if (scenario === "empty-disconnect") {
        socket.destroy();
      }
    });
  });
  async function within(promise, phase) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("control fixture超时: " + phase)), 1000);
      })]);
    } finally { clearTimeout(timer); }
  }
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const port = server.address().port;
    if (scenario === "missing-control") {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
    // 只替换IPC端点选择，实际使用Node TCP连接、数据和关闭事件；不触碰用户socket。
    net.createConnection = () => originalCreateConnection({ host: "127.0.0.1", port });
    const result = await within(requestGatewayHandoff({
      boxteamHome: process.cwd(),
      environment: {
        BOXTEAM_CONFIG_CANDIDATE_REF: "candidate-ref",
        BOXTEAM_CONFIG_GENERATION: "generation-2",
        BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
      },
      timeoutMs: 150,
    }).then((value) => ({ value }), (error) => ({ error })), "请求交付");
    if (scenario === "accepted-open") {
      assert.deepEqual(result.value, {
        handled: true, data: { accepted: true, target_generation: "generation-2" },
      });
    } else if (scenario === "missing-control") {
      assert.deepEqual(result.value, { handled: false });
    } else {
      assert.ok(result.error instanceof Error, "失败必须交付原异常");
      if (scenario === "rejected-open") {
        assert.equal(result.error.message, "pending rejected");
      } else if (scenario === "invalid-json-open") {
        assert.ok(result.error instanceof SyntaxError);
      } else if (scenario === "silent-open") {
        assert.equal(result.error.message, "Gateway supervisor handoff 在 150ms 内未响应");
      } else {
        assert.equal(result.error.message, "Gateway supervisor handoff 连接在完整响应前结束");
      }
    }
    if (scenario === "missing-control") {
      assert.deepEqual(requests, []);
    } else {
      assert.deepEqual(requests, [{
        type: "gateway_pending_handoff", candidate_ref: "candidate-ref",
        target_generation: "generation-2", fencing_token: "fence-2",
      }]);
      await within(clientClosed, "结果交付后释放连接");
      assert.equal(sockets.size, 0);
    }
  } finally {
    net.createConnection = originalCreateConnection;
    for (const socket of sockets) socket.destroy();
    if (server.listening) {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
  }
}

function runControlClientProbe(scenario) {
  // 正式Launcher由Node执行；Bun仅作为测试入口，不替代Node的连接关闭事件。
  const fixtureRoot = path.resolve(
    "out/tests/packages/launcher/src/gateway/control-client/workspace", randomUUID(),
  );
  mkdirSync(fixtureRoot, { recursive: true });
  try {
    execFileSync("node", ["--input-type=module", "--eval", `
      ${observeControlClient.toString()}
      await observeControlClient(...JSON.parse(process.argv[1]));
    `, JSON.stringify([scenario, new URL("./supervisor.mjs", import.meta.url).href])], {
      encoding: "utf8", timeout: 5000, cwd: fixtureRoot,
      env: { ...process.env, TMPDIR: ".", TMP: ".", TEMP: "." },
    });
  } finally { rmSync(fixtureRoot, { recursive: true, force: true }); }
}

describe("Gateway handoff响应与连接生命周期", () => {
  for (const [scenario, description] of [
    ["accepted-open", "成功响应后主动释放仍开放的控制连接"],
    ["rejected-open", "拒绝响应保留原因并释放连接"],
    ["invalid-json-open", "非法JSON保留解析异常并释放连接"],
    ["empty-end", "空响应EOF立即失败"],
    ["partial-end", "不完整响应EOF立即失败"],
    ["empty-disconnect", "完整响应前断连立即失败"],
    ["silent-open", "没有响应时保留原超时并释放连接"],
    ["missing-control", "连接被拒绝时保持未接管结果"],
  ]) {
    test(description, () => runControlClientProbe(scenario));
  }
});
