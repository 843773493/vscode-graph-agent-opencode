import assert from "node:assert/strict";
import { cpSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { randomUUID } from "node:crypto";
import net from "node:net";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import test from "node:test";
import { WebSocket } from "ws";

const projectRoot = process.cwd();
const artifactRoot = path.join(projectRoot, "out/tests/src/workspace-services/terminal/server/backendPluginRuntime");
const entrypoint = path.join(projectRoot, "src/workspace-services/terminal/server/backend.js");

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.listen(0, "127.0.0.1", (error) => error ? reject(error) : resolve()));
  const { port } = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function waitForStatus(url, process, token) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) throw new Error(`Terminal 插件进程提前退出: exit_code=${process.exitCode}`);
    try {
      const response = await fetch(`${url}/internal/plugin/status`, { headers: { "x-local-token": token } });
      if (response.ok) return response.json();
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
  throw new Error("Terminal 插件未在期限内 ready");
}

test("Terminal 仅以 Gateway 插件身份启动，并保护 HTTP 与 WebSocket 服务", async (t) => {
  mkdirSync(artifactRoot, { recursive: true });
  const workspaceRoot = path.join(artifactRoot, `workspace-${randomUUID()}`);
  cpSync(path.join(projectRoot, "tests/fixtures/workspaces/terminal_cwd_workspace"), workspaceRoot, { recursive: true });
  const { workspace_id: workspaceId } = JSON.parse(readFileSync(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), "utf8"));
  const port = await reservePort();
  const token = `terminal-plugin-test-${randomUUID()}`;
  const instanceId = randomUUID();
  const child = spawn(process.execPath, [entrypoint, "--host", "0.0.0.0", "--port", String(await reservePort())], {
    cwd: projectRoot,
    env: {
      ...process.env,
      BOXTEAM_PROJECT_ROOT: projectRoot,
      WORKSPACE_ROOT: workspaceRoot,
      BOXTEAM_WORKSPACE_ID: workspaceId,
      BOXTEAM_RUNTIME_CONTROL_TOKEN: token,
      BOXTEAM_RUNTIME_OBSERVATION: JSON.stringify({
        instance_id: instanceId,
        revision: "test",
        telemetry: { enabled: false, sample_ratio: 1, sample_buffer_limit: 256 },
        statistics: { enabled: false, collection_interval_ms: 5000 },
      }),
      BOXTEAM_PLUGIN_PORT: String(port),
      BOXTEAM_PLUGIN_INSTANCE: JSON.stringify({
        gateway_id: "test-gateway",
        package_id: "boxteam.developer-tools",
        package_version: "0.1.0",
        plugin_id: "terminal",
        workspace_id: workspaceId,
        instance_scope: "workspace",
        instance_id: instanceId,
        management_protocol_version: 1,
        extension_contract_version: 1,
        services: [
          { service_id: "http", service_type: "terminal", protocol: "http" },
          { service_id: "socket", service_type: "terminal", protocol: "websocket" },
        ],
      }),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const childExit = once(child, "exit");
  let output = "";
  child.stdout.setEncoding("utf8").on("data", (chunk) => { output += chunk; });
  child.stderr.setEncoding("utf8").on("data", (chunk) => { output += chunk; });
  const stop = async () => {
    if (child.exitCode === null) child.kill("SIGTERM");
    await childExit;
  };
  t.after(async () => {
    await stop();
    rmSync(workspaceRoot, { recursive: true, force: true });
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const status = await waitForStatus(baseUrl, child, token).catch((error) => {
    error.message += `\n${output}`;
    throw error;
  });
  assert.equal(status.package_id, "boxteam.developer-tools");
  assert.equal(status.plugin_id, "terminal");
  assert.equal(status.workspace_id, workspaceId);
  assert.ok(status.capabilities.includes("observation"));
  assert.deepEqual(status.services, [
    { service_id: "http", service_type: "terminal", protocol: "http" },
    { service_id: "socket", service_type: "terminal", protocol: "websocket" },
  ]);
  const observationResponse = await fetch(`${baseUrl}/internal/plugin/observation`, {
    headers: { "x-local-token": token },
  });
  assert.equal(observationResponse.status, 200);
  const observation = await observationResponse.json();
  assert.equal(observation.instance_id, instanceId);
  assert.equal(observation.applied_revision, "test");
  assert.equal(observation.telemetry.enabled, false);
  assert.equal(observation.statistics.enabled, false);
  assert.equal(observation.requests, null);
  assert.deepEqual(observation.samples, []);
  assert.deepEqual(observation.processes, []);

  const legacyHealth = await fetch(`${baseUrl}/health`);
  assert.equal(legacyHealth.status, 404);
  const legacyBusinessPath = await fetch(`${baseUrl}/api/terminals?workspace_id=${workspaceId}`, {
    headers: { "x-local-token": token },
  });
  assert.equal(legacyBusinessPath.status, 404);
  const denied = await fetch(`${baseUrl}/services/http/api/terminals?workspace_id=${workspaceId}`);
  assert.equal(denied.status, 401);
  const listed = await fetch(`${baseUrl}/services/http/api/terminals?workspace_id=${workspaceId}`, {
    headers: { "x-local-token": token },
  });
  assert.equal(listed.status, 200);
  assert.deepEqual((await listed.json()).data, []);

  const websocketUrl = `ws://127.0.0.1:${port}/services/socket/terminal?workspace_id=${workspaceId}`;
  const socket = new WebSocket(websocketUrl, {
    headers: { "x-local-token": token, "x-boxteam-workspace-id": workspaceId },
  });
  await once(socket, "open");
  const socketClosed = once(socket, "close");
  socket.close();
  await socketClosed;

  const shutdown = await fetch(`${baseUrl}/internal/plugin/shutdown`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-local-token": token },
    body: "{}",
  });
  assert.equal(shutdown.status, 200);
  await childExit;
});
