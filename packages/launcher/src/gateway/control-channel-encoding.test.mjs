import { describe, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { tmpdir } from "node:os";

const outputRoot = path.resolve("out/tests/packages/launcher/src/gateway/control-channel-encoding");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}

async function observeFragmentedControl(scenario, splitCharacter, controlUrl, supervisorUrl, artifacts) {
  const { default: assert } = await import("node:assert/strict");
  const { default: net } = await import("node:net");
  const { once } = await import("node:events");
  const { writeFileSync } = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createGatewaySupervisorControl } = await import(controlUrl);
  const { requestGatewayHandoff } = await import(supervisorUrl);
  const original = { createServer: net.createServer, createConnection: net.createConnection };
  const observation = { scenario, splitCharacter, receivedRequests: [], receivedChunks: [], socketErrors: [] };
  const sockets = new Set();
  let server, control, productionSocket;
  const label = "目录 é中文 😀 空格";
  const requestPayload = {
    type: "gateway_pending_handoff", candidate_ref: label,
    target_generation: "generation-2", fencing_token: "fence-2",
  };
  const acceptedData = { accepted: true, target_generation: label };
  async function within(promise, phase) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("encoding fixture超时: " + phase)), 1000);
      })]);
    } finally { clearTimeout(timer); }
  }
  function track(socket) {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
    socket.on("error", (error) => observation.socketErrors.push(error.message));
    return socket;
  }
  function readLine(socket) {
    socket.setEncoding("utf8");
    return new Promise((resolve, reject) => {
      let text = "";
      socket.once("error", reject);
      socket.on("data", (chunk) => {
        text += chunk;
        const end = text.indexOf("\n");
        if (end >= 0) resolve(JSON.parse(text.slice(0, end)));
      });
    });
  }
  async function writeFragmented(sender, receiver, payload) {
    const bytes = Buffer.from(`${JSON.stringify(payload)}\n`);
    const characterStart = bytes.indexOf(Buffer.from(splitCharacter));
    assert.notEqual(characterStart, -1);
    const split = characterStart + 1;
    observation.fragment = { first: bytes.subarray(0, split).toString("hex"), second: bytes.subarray(split).toString("hex") };
    receiver.on("data", (chunk) => observation.receivedChunks.push({
      type: typeof chunk, text: chunk.toString("utf8"),
    }));
    const firstReceived = once(receiver, "data");
    sender.write(bytes.subarray(0, split));
    await within(firstReceived, "第一个网络数据块");
    // 等第一块已经进入真实Socket的data事件，再发送余下字节，避免TCP合并掩盖故障。
    await new Promise((resolve) => setImmediate(resolve));
    sender.write(bytes.subarray(split));
  }
  try {
    if (scenario === "request") {
      // 只把IPC监听地址映射到随机TCP端口；保留真实server回调、Socket和data事件。
      net.createServer = (listener) => {
        server = original.createServer((socket) => {
          productionSocket = track(socket);
          listener(socket);
        });
        const listen = server.listen;
        server.listen = () => listen.call(server, { host: "127.0.0.1", port: 0 });
        return server;
      };
      control = createGatewaySupervisorControl({
        address: String.raw`\\.\pipe\encoding-fixture`,
        onHandoff: async (payload) => {
          observation.receivedRequests.push(payload);
          return acceptedData;
        },
      });
      net.createServer = original.createServer;
      await within(control.listen(), "控制监听");
      const connectionAccepted = once(server, "connection");
      const client = track(original.createConnection({ host: "127.0.0.1", port: server.address().port }));
      await within(Promise.all([once(client, "connect"), connectionAccepted]), "控制连接");
      const response = readLine(client);
      await writeFragmented(client, productionSocket, requestPayload);
      observation.response = await within(response, "控制响应");
    } else {
      server = original.createServer((socket) => {
        track(socket);
        readLine(socket).then(async (payload) => {
          observation.receivedRequests.push(payload);
          await writeFragmented(socket, productionSocket,
            scenario === "accepted" ? { ok: true, data: acceptedData } : { ok: false, error: label });
        }).catch((error) => { observation.peerError = error.message; socket.destroy(); });
      });
      server.listen({ host: "127.0.0.1", port: 0 });
      await within(once(server, "listening"), "对端监听");
      net.createConnection = () => {
        productionSocket = track(original.createConnection({ host: "127.0.0.1", port: server.address().port }));
        return productionSocket;
      };
      const outcome = await within(requestGatewayHandoff({
        boxteamHome: process.cwd(),
        environment: {
          BOXTEAM_CONFIG_CANDIDATE_REF: label, BOXTEAM_CONFIG_GENERATION: "generation-2",
          BOXTEAM_CONFIG_FENCING_TOKEN: "fence-2",
        }, timeoutMs: 500,
      }).then((value) => ({ value }), (error) => ({ error })), "请求交付");
      observation.result = outcome.value;
      observation.error = outcome.error ? { name: outcome.error.name, message: outcome.error.message } : null;
    }
    writeFileSync(path.join(artifacts, "observation.json"), `${JSON.stringify(observation, null, 2)}\n`, { flag: "wx" });
    assert.equal(observation.receivedChunks.length, 2, "必须真实接收两个数据块");
    assert.deepEqual(observation.receivedRequests, [requestPayload]);
    if (scenario === "request") assert.deepEqual(observation.response, { ok: true, data: acceptedData });
    else if (scenario === "accepted") assert.deepEqual(observation.result, { handled: true, data: acceptedData });
    else assert.deepEqual(observation.error, { name: "Error", message: label });
  } finally {
    net.createServer = original.createServer;
    net.createConnection = original.createConnection;
    for (const socket of sockets) socket.destroy();
    if (control) await control.close();
    else if (server?.listening) await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

const observerPath = path.join(artifactsRunRoot, "observer.mjs");
writeFileSync(observerPath, `import { readFileSync } from "node:fs";\n${observeFragmentedControl.toString()}\nawait observeFragmentedControl(...JSON.parse(readFileSync(process.argv[2], "utf8")));\n`, { flag: "wx" });

describe("Gateway控制JSON的跨块UTF-8", () => {
  for (const scenario of ["request", "accepted", "rejected"]) {
    for (const [width, splitCharacter] of [[2, "é"], [3, "中"], [4, "😀"]]) {
      test(`${scenario}/${width}字节字符拆在两个网络块时保留原文`, () => {
        const name = `${scenario}-${width}-byte-character`;
        const workspace = path.join(fixtureRunRoot, name);
        const artifacts = path.join(artifactsRunRoot, name);
        mkdirSync(workspace);
        mkdirSync(artifacts);
        const inputPath = path.join(artifacts, "input.json");
        writeFileSync(inputPath, `${JSON.stringify([scenario, splitCharacter,
          new URL("./control-channel.mjs", import.meta.url).href,
          new URL("./supervisor.mjs", import.meta.url).href, artifacts])}\n`, { flag: "wx" });
        const args = [observerPath, inputPath];
        const result = spawnSync("node", args, {
          cwd: workspace, timeout: 5000,
          env: { ...process.env, TMPDIR: tmpdir(), TMP: tmpdir(), TEMP: tmpdir() },
        });
        writeFileSync(path.join(artifacts, "stdout.raw"), result.stdout ?? "", { flag: "wx" });
        writeFileSync(path.join(artifacts, "stderr.raw"), result.stderr ?? "", { flag: "wx" });
        writeFileSync(path.join(artifacts, "result.json"), `${JSON.stringify({
          command: ["node", ...args], cwd: workspace, timeout_ms: 5000,
          temporaryDirectory: ".",
          status: result.status, signal: result.signal,
          error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
        }, null, 2)}\n`, { flag: "wx" });
        if (result.error || result.status !== 0) throw new Error(`控制字符分块 ${name}失败；artifacts=${artifacts}\n${result.stderr ?? ""}`, { cause: result.error });
      });
    }
  }
});
