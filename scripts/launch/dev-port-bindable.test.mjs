import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";

import { assertDevelopmentPortsBindable } from "./dev-environment.mjs";

const outputRoot = path.resolve("out/tests/scripts/launch/dev-port-bindable");
const workspace = path.join(outputRoot, "workspace");
const artifacts = path.join(outputRoot, "artifacts", randomUUID());
mkdirSync(workspace, { recursive: true });
mkdirSync(artifacts, { recursive: true });

async function listenOnRandomPort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("未取得本地 TCP fixture 端口");
  return { server, port: address.port };
}

function closeServer(server) {
  return new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

function save(name, result) {
  writeFileSync(path.join(artifacts, `${name}.json`), `${JSON.stringify(result, null, 2)}\n`, { flag: "wx" });
}

describe("开发端口真实 bind 预检", () => {
  test("空闲随机端口可以绑定并在检查后释放", async () => {
    const { server, port } = await listenOnRandomPort();
    await closeServer(server);
    await assertDevelopmentPortsBindable([port], "127.0.0.1");
    save("free-port", { host: "127.0.0.1", port, result: "bind-and-close" });
    expect(true).toBe(true);
  });

  test("真实监听者导致准确的 EADDRINUSE 错误，不依赖 lsof", async () => {
    const { server, port } = await listenOnRandomPort();
    let message = "";
    try {
      await assertDevelopmentPortsBindable([port], "127.0.0.1");
    } catch (error) {
      message = error.message;
    } finally {
      await closeServer(server);
    }
    save("occupied-port", { host: "127.0.0.1", port, error: message });
    expect(message).toContain(`host=127.0.0.1`);
    expect(message).toContain(`port=${port} code=EADDRINUSE`);
  });

  test("其它 bind 错误保留地址、端口和原始错误码", async () => {
    let message = "";
    try {
      await assertDevelopmentPortsBindable([65536], "127.0.0.1");
    } catch (error) {
      message = error.message;
    }
    save("invalid-port", { host: "127.0.0.1", port: 65536, error: message });
    expect(message).toContain("host=127.0.0.1");
    expect(message).toContain("port=65536 code=ERR_SOCKET_BAD_PORT");
  });
});
