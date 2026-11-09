import assert from "node:assert/strict";
import { closeSync, fstatSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

const projectRoot = process.cwd();
const artifactRoot = path.join(projectRoot, "out/tests/src/shared/serverPluginRuntime/artifacts");

test("受管 Node 插件提供生成合同身份、日志控制和原关闭回执", () => {
  mkdirSync(artifactRoot, { recursive: true });
  const runId = randomUUID();
  const logPath = path.join(artifactRoot, `plugin-runtime-${runId}.log`);
  const resultPath = path.join(artifactRoot, `plugin-runtime-${runId}.json`);
  const logDescriptor = openSync(logPath, "w");
  try {
    const identity = fstatSync(logDescriptor, { bigint: true });
    const script = `
      import assert from "node:assert/strict";
      import http from "node:http";
      import net from "node:net";
      import { writeFileSync } from "node:fs";
      import { createJsonRequestReader } from ${JSON.stringify(new URL("./serverJsonRequest.mjs", import.meta.url).href)};
      import { createRuntimeLogger } from ${JSON.stringify(new URL("./serverRuntimeLogging.mjs", import.meta.url).href)};

      const reservation = net.createServer();
      await new Promise(resolve => reservation.listen(0, "127.0.0.1", resolve));
      process.env.BOXTEAM_PLUGIN_PORT = String(reservation.address().port);
      await new Promise((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
      const { createPluginRuntimeManagement, readPluginRuntimeEnvironment } = await import(${JSON.stringify(new URL("./serverPluginRuntime.mjs", import.meta.url).href)});
      const workspaceInstance = process.env.BOXTEAM_PLUGIN_INSTANCE;
      process.env.BOXTEAM_PLUGIN_INSTANCE = JSON.stringify({ ...JSON.parse(workspaceInstance),
        workspace_id: null, instance_scope: "gateway", services: [] });
      const gatewayEnvironment = readPluginRuntimeEnvironment();
      assert.equal(gatewayEnvironment.identity.workspace_id, null);
      assert.deepEqual(gatewayEnvironment.identity.services, []);
      process.env.BOXTEAM_PLUGIN_INSTANCE = JSON.stringify({ ...JSON.parse(workspaceInstance), instance_id: "" });
      assert.throws(readPluginRuntimeEnvironment, /instance_id 必须是非空字符串/);
      process.env.BOXTEAM_PLUGIN_INSTANCE = JSON.stringify({ ...JSON.parse(workspaceInstance),
        services: [{ service_id: "terminal", service_type: "terminal", protocol: 42 }] });
      assert.throws(readPluginRuntimeEnvironment, /生成的插件管理合同/);
      process.env.BOXTEAM_PLUGIN_INSTANCE = workspaceInstance;
      const logger = createRuntimeLogger("terminal", "plugin.test");
      const readJson = createJsonRequestReader(logger);
      let shutdownCalled = false;
      const management = createPluginRuntimeManagement({
        service: "terminal", logger, readJson,
        shutdown: async () => {
          shutdownCalled = true;
          await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
        },
      });
      const server = http.createServer(async (request, response) => {
        try {
          if (await management.handleRequest(request, response, new URL(request.url, "http://127.0.0.1").pathname)) return;
          response.writeHead(404).end();
        } catch (error) {
          response.writeHead(error.statusCode ?? 500, { "content-type": "application/json" });
          response.end(JSON.stringify({ error: error.message }));
        }
      });
      const serverClosed = new Promise(resolve => server.once("close", resolve));
      await new Promise(resolve => server.listen(management.port, "127.0.0.1", resolve));
      const endpoint = "http://127.0.0.1:" + server.address().port;
      const headers = { "x-local-token": management.token };
      const invalidTokenResponse = await fetch(endpoint + "/internal/plugin/status", {
        headers: { "x-local-token": "wrong-token" },
      });
      assert.equal(invalidTokenResponse.status, 401);
      const browserOriginResponse = await fetch(endpoint + "/internal/plugin/status", {
        headers: { ...headers, Origin: "http://127.0.0.1" },
      });
      assert.equal(browserOriginResponse.status, 403);
      const statusResponse = await fetch(endpoint + "/internal/plugin/status", { headers });
      assert.equal(statusResponse.status, 200);
      const status = await statusResponse.json();
      assert.equal(status.process_id, process.pid);
      assert.equal(status.gateway_id, management.identity.gateway_id);
      assert.equal(status.package_id, management.identity.package_id);
      assert.equal(status.plugin_id, management.identity.plugin_id);
      assert.equal(status.instance_id, management.identity.instance_id);
      assert.equal(status.status, "ready");
      assert.deepEqual(status.capabilities, ["status", "shutdown", "observation"]);
      assert.deepEqual(status.services, [{ service_id: "terminal", service_type: "terminal", protocol: "websocket" }]);
      assert.equal(status.log_sink.device, process.env.EXPECTED_LOG_DEVICE);
      assert.equal(status.log_sink.inode, process.env.EXPECTED_LOG_INODE);

      const observationUrl = endpoint + "/internal/plugin/observation";
      const observationHeaders = { "x-local-token": management.token };
      const observationResponse = await fetch(observationUrl, { headers: observationHeaders });
      assert.equal(observationResponse.status, 200);
      const observation = await observationResponse.json();
      assert.equal(observation.instance_id, management.identity.instance_id);
      assert.equal(observation.applied_revision, "plugin-runtime-test");
      assert.equal(observation.requests, null);
      assert.deepEqual(observation.samples, []);
      const unauthorizedObservation = await fetch(observationUrl, { headers: { "x-local-token": "wrong-token" } });
      assert.equal(unauthorizedObservation.status, 401);
      const unsupportedObservationMethod = await fetch(observationUrl, {
        method: "PUT", headers: observationHeaders,
      });
      assert.equal(unsupportedObservationMethod.status, 405);

      for (const [payload, statusCode] of [
        [{ instance_id: management.identity.instance_id, revision: "invalid-type",
          telemetry: { enabled: false, sample_ratio: "invalid", sample_buffer_limit: 256 },
          statistics: { enabled: false, collection_interval_ms: 5000 } }, 422],
        [{ instance_id: management.identity.instance_id, revision: "invalid-range",
          telemetry: { enabled: true, sample_ratio: 2, sample_buffer_limit: 256 },
          statistics: { enabled: false, collection_interval_ms: 5000 } }, 422],
        [{ instance_id: "another-plugin-instance", revision: "wrong-instance",
          telemetry: { enabled: false, sample_ratio: 1, sample_buffer_limit: 256 },
          statistics: { enabled: false, collection_interval_ms: 5000 } }, 409],
      ]) {
        const rejected = await fetch(observationUrl, {
          method: "POST",
          headers: { ...observationHeaders, "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        assert.equal(rejected.status, statusCode);
        const unchangedResponse = await fetch(observationUrl, { headers: observationHeaders });
        assert.deepEqual(await unchangedResponse.json(), observation);
      }

      const applied = await fetch(observationUrl, {
        method: "POST",
        headers: { ...observationHeaders, "content-type": "application/json" },
        body: JSON.stringify({ instance_id: management.identity.instance_id, revision: "plugin-runtime-hot",
          telemetry: { enabled: false, sample_ratio: 0, sample_buffer_limit: 64 },
          statistics: { enabled: false, collection_interval_ms: 5000 } }),
      });
      assert.equal(applied.status, 200);
      assert.equal((await applied.json()).applied_revision, "plugin-runtime-hot");
      assert.equal((await (await fetch(observationUrl, { headers: observationHeaders })).json()).applied_revision,
        "plugin-runtime-hot");

      const loggingResponse = await fetch(endpoint + "/internal/runtime-logging", { headers });
      assert.equal(loggingResponse.status, 200);
      const logging = (await loggingResponse.json()).data;
      const applyResponse = await fetch(endpoint + "/internal/runtime-logging", {
        method: "POST", headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify({ runtime_instance_id: logging.runtime_instance_id,
          revision: "plugin-runtime-test", expected_revision: logging.applied_revision,
          level: "warning", pretty: false }),
      });
      assert.equal(applyResponse.status, 200);
      assert.equal((await applyResponse.json()).data.applied_revision, "plugin-runtime-test");

      const invalidShutdown = await fetch(endpoint + "/internal/plugin/shutdown", {
        method: "POST", headers: { "content-type": "application/json", "x-local-token": "wrong-token" },
        body: "{}",
      });
      assert.equal(invalidShutdown.status, 401);
      const browserShutdown = await fetch(endpoint + "/internal/plugin/shutdown", {
        method: "POST", headers: { ...headers, "content-type": "application/json", Origin: "http://127.0.0.1" },
        body: "{}",
      });
      assert.equal(browserShutdown.status, 403);
      assert.equal(shutdownCalled, false);
      const shutdownResponse = await fetch(endpoint + "/internal/plugin/shutdown", { method: "POST", headers });
      assert.equal(shutdownResponse.status, 200);
      const shutdown = await shutdownResponse.json();
      assert.equal(shutdown.accepted, true);
      assert.equal(shutdown.process_id, process.pid);
      assert.equal(shutdown.instance_id, management.identity.instance_id);
      await serverClosed;
      assert.equal(shutdownCalled, true);
      writeFileSync(process.env.RESULT_PATH, JSON.stringify({ status, logging_revision: "plugin-runtime-test", shutdown }));
    `;
    const child = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", ["--input-type=module", "-e", script], {
      cwd: projectRoot,
      env: {
        ...process.env,
        BOXTEAM_PROJECT_ROOT: projectRoot,
        BOXTEAM_RUNTIME_CONTROL_TOKEN: "plugin-runtime-test-token",
        BOXTEAM_PLUGIN_INSTANCE: JSON.stringify({ gateway_id: "gateway-plugin-test",
          package_id: "plugin.test", plugin_id: "terminal", package_version: "1.0.0",
          workspace_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", instance_scope: "workspace",
          instance_id: "plugin-instance-test", management_protocol_version: 1,
          extension_contract_version: 1, services: [{ service_id: "terminal",
            service_type: "terminal", protocol: "websocket" }] }),
        BOXTEAM_RUNTIME_LOGGING: JSON.stringify({ revision: "initial", level: "info", pretty: false }),
        BOXTEAM_RUNTIME_OBSERVATION: JSON.stringify({ instance_id: "plugin-instance-test", revision: "plugin-runtime-test",
          telemetry: { enabled: false, sample_ratio: 1, sample_buffer_limit: 256 },
          statistics: { enabled: false, collection_interval_ms: 5000 } }),
        BOXTEAM_RUNTIME_LOG_SINK: JSON.stringify({ path: logPath, owner_id: "plugin-test-owner",
          role: "service-plugin", source: "gateway", device: String(identity.dev), inode: String(identity.ino) }),
        EXPECTED_LOG_DEVICE: String(identity.dev),
        EXPECTED_LOG_INODE: String(identity.ino),
        RESULT_PATH: resultPath,
      },
      stdio: ["ignore", logDescriptor, logDescriptor],
      encoding: "utf8",
      timeout: 10000,
    });
    assert.ifError(child.error);
    assert.equal(child.status, 0, child.stderr);
    assert.equal(child.signal, null);
    const result = JSON.parse(readFileSync(resultPath, "utf8"));
    assert.equal(result.status.status, "ready");
    assert.equal(result.logging_revision, "plugin-runtime-test");
    assert.equal(result.shutdown.accepted, true);
  } finally {
    closeSync(logDescriptor);
  }
});
