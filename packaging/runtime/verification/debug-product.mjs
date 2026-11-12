import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

import { installPlugin } from "../../../tests/support/plugin_package.mjs";
import { parse } from "jsonc-parser";

function assertTargetNode(pid, executable) {
  const powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  const script = `$p=Get-CimInstance Win32_Process -Filter "ProcessId=${pid}"; if(!$p -or $p.ExecutablePath -ne '${executable.replaceAll("'", "''")}'){exit 7}; Write-Output $p.ExecutablePath`;
  const result = spawnSync(powershell, ["-NoProfile", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")], {
    encoding: "utf8", stdio: "pipe", timeout: 10_000,
  });
  if (result.error) throw result.error;
  if (result.status !== 0 || result.stdout.trim().toLowerCase() !== executable.toLowerCase()) {
    throw new Error(`Debug 目标不是发行 Node: ${result.stdout.trim()} ${result.stderr.trim()}`);
  }
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") return false;
    throw error;
  }
}

async function waitForProcessExit(pid) {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline && processAlive(pid)) await delay(50);
  if (processAlive(pid)) throw new Error(`Debug 目标进程未退出: pid=${pid}`);
}

export async function verifyDebugProduct(requestJson, { gatewayId, workspaceId, nodeExecutable }) {
  if (!nodeExecutable) throw new Error("发行 Debug 验收缺少目标 Node executable");
  const packagePath = fileURLToPath(new URL("../../../resources/plugins/developer-tools.zip", import.meta.url));
  const archive = await readFile(packagePath);
  const digest = createHash("sha256").update(archive).digest("hex");
  await installPlugin(async (pathname, options) => (await requestJson(pathname, {
    ...options,
    body: JSON.stringify(options.body),
  })).data, { gateway_id: gatewayId, path: packagePath, expected_digest: digest });

  const headers = { "X-BoxTeam-Workspace-Id": workspaceId };
  const sourcePath = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/config/sources/workspace_root_mutable_override`;
  const document = (await requestJson(sourcePath, { headers })).data;
  const parseErrors = [];
  const config = parse(document.jsonc, parseErrors, { allowTrailingComma: true });
  if (parseErrors.length || !config || typeof config !== "object") {
    throw new Error(`Debug 插件配置 JSONC 无效: ${JSON.stringify(parseErrors)}`);
  }
  config.plugins ??= {};
  config.plugins.runtime ??= {};
  config.plugins.runtime.entries = (config.plugins.runtime.entries ?? []).filter(
    (entry) => entry.package_id !== "boxteam.developer-tools" || entry.plugin_id !== "debugging",
  );
  config.plugins.runtime.entries.push({ package_id: "boxteam.developer-tools", package_version: "0.1.0",
    plugin_id: "debugging", enabled: true });
  const updated = await requestJson(sourcePath, { method: "PUT", headers, body: JSON.stringify({
    gateway_id: gatewayId, workspace_id: workspaceId, source_key: "workspace_root_mutable_override",
    jsonc: `${JSON.stringify(config, null, 2)}\n`, base_file_digest: document.file_digest,
    base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest,
    idempotency_key: randomUUID(),
  }) });
  if (updated.data?.edit?.state !== "active") throw new Error(`Debug 插件绑定未生效: ${JSON.stringify(updated.data?.edit)}`);

  const status = async () => (await requestJson("/api/gateway/service-plugins/status", {
    method: "POST", body: JSON.stringify({ gateway_id: gatewayId }),
  })).data.plugins?.find((item) => item.binding?.package_id === "boxteam.developer-tools"
    && item.binding?.plugin_id === "debugging" && item.binding?.workspace_id === workspaceId);
  const readyDeadline = Date.now() + 30_000;
  let plugin;
  while (Date.now() < readyDeadline) {
    plugin = await status();
    if (plugin?.state === "ready") break;
    if (plugin?.state === "failed") throw new Error(`发行 Debug 插件启动失败: ${JSON.stringify(plugin)}`);
    await delay(100);
  }
  if (plugin?.state !== "ready") throw new Error("发行 Debug 插件未在期限内就绪");

  const headersForWorkspace = { ...headers };
  const session = (await requestJson("/api/v1/sessions", { method: "POST", headers: headersForWorkspace,
    body: JSON.stringify({ title: "Windows 发行 Debug" }) })).data;
  const owner = { session_id: session.session_id, thread_id: session.thread_id };
  const workspaceFiles = (await requestJson("/api/v1/workspace/files?scope=workspace", { headers })).data;
  const directory = workspaceFiles.directory_key;
  const filePath = "distribution-debug.mjs";
  const intent = { client_operation_id: `op_${randomUUID().replaceAll("-", "")}`,
    client_sequence: 1, expected_directory_revision: workspaceFiles.directory_revision, depends_on: [],
    relative_child_path: "", directory_key: directory };
  await requestJson("/api/v1/workspace/files/entries", { method: "POST", headers,
    body: JSON.stringify({ name: filePath, kind: "file", intent }) });
  const operationDeadline = Date.now() + 15_000;
  let receipt;
  while (Date.now() < operationDeadline) {
    const queue = (await requestJson(`/api/v1/workspace/files/operations?operation_id=${encodeURIComponent(intent.client_operation_id)}&expected_workspace_id=${encodeURIComponent(workspaceId)}`,
      { headers })).data.operations;
    receipt = queue.find((item) => item.operation_id === intent.client_operation_id);
    if (receipt?.state === "committed") break;
    if (["rejected", "cancelled", "dependency_failed"].includes(receipt?.state)) {
      throw new Error(`Debug fixture 文件创建失败: ${JSON.stringify(receipt)}`);
    }
    await delay(50);
  }
  if (receipt?.state !== "committed" || !receipt.created_path) throw new Error("Debug fixture 文件未创建");
  const source = "const state = { counter: 1 };\nstate.counter += 2;\nstate.counter += 3;\nconsole.log('windows-distribution-debug', state.counter);\nsetInterval(() => {}, 1000);\n";
  const fileUrl = `/api/v1/workspace/files/content?scope=workspace&path=${encodeURIComponent(receipt.created_path)}`;
  const emptyFile = (await requestJson(fileUrl, { headers })).data;
  await requestJson(fileUrl, { method: "PUT", headers, body: JSON.stringify({ content: source,
    expected_revision: emptyFile.revision }) });

  const configured = (await requestJson("/api/v1/debug/node/configurations", { method: "POST", headers,
    body: JSON.stringify({ ...owner, name: "Windows 发行 Debug", script_path: filePath,
      working_directory: ".", breakpoints: [{ path: filePath, line: 2 }] }) })).data;
  const startPayload = (await requestJson("/api/v1/debug/node/start", { method: "POST", headers,
    body: JSON.stringify({ ...owner, path: filePath, configuration_id: configured.active_configuration_id }) })).data;
  if (startPayload.status !== "paused" || startPayload.call_stack?.[0]?.line !== 2) {
    throw new Error(`发行 Node 未在断点停住: ${JSON.stringify(startPayload)}`);
  }
  if (startPayload.debugger_binding?.instance_id !== plugin.instance?.instance_id) {
    throw new Error(`发行 Debug 启动未复用绑定实例: ${JSON.stringify(startPayload.debugger_binding)}`);
  }
  assertTargetNode(startPayload.pid, nodeExecutable);
  if (!processAlive(startPayload.pid)) throw new Error("发行 Node 在断点暂停前已退出");
  const evaluation = (await requestJson("/api/v1/debug/node/action", { method: "POST", headers,
    body: JSON.stringify({ ...owner, action: "evaluate", params: { expression: "state.counter += 10" } }) })).data;
  if (evaluation.last_evaluation?.value !== "11") throw new Error(`发行 Debug 求值错误: ${JSON.stringify(evaluation.last_evaluation)}`);
  await requestJson("/api/v1/debug/node/action", { method: "POST", headers,
    body: JSON.stringify({ ...owner, action: "continue" }) });
  const outputDeadline = Date.now() + 10_000;
  let observed;
  while (Date.now() < outputDeadline) {
    observed = (await requestJson(`/api/v1/debug/node?session_id=${encodeURIComponent(owner.session_id)}&thread_id=${encodeURIComponent(owner.thread_id)}`,
      { headers })).data;
    if (observed.output?.includes("windows-distribution-debug 16")) break;
    await delay(100);
  }
  if (!observed?.output?.includes("windows-distribution-debug 16")) {
    throw new Error(`发行 Debug 未从目标 Node 收到继续执行输出: ${JSON.stringify(observed)}`);
  }
  const stopped = (await requestJson("/api/v1/debug/node/action", { method: "POST", headers,
    body: JSON.stringify({ ...owner, action: "stop" }) })).data;
  if (stopped.status !== "exited" || stopped.pid !== startPayload.pid) {
    throw new Error(`发行 Debug stop 未回报目标进程退出: ${JSON.stringify(stopped)}`);
  }
  await waitForProcessExit(startPayload.pid);
  const restart = (await requestJson("/api/gateway/service-plugins/restart", { method: "POST",
    body: JSON.stringify({ gateway_id: gatewayId, package_id: "boxteam.developer-tools",
      package_version: "0.1.0", plugin_id: "debugging", workspace_id: workspaceId,
      revision: plugin.revision }) })).data;
  if (restart.state !== "ready" || restart.instance?.instance_id === plugin.instance?.instance_id) {
    throw new Error(`发行 Debug 插件显式重启失败: ${JSON.stringify(restart)}`);
  }
  const secondRun = (await requestJson("/api/v1/debug/node/start", { method: "POST", headers,
    body: JSON.stringify({ ...owner, path: filePath, configuration_id: configured.active_configuration_id }) })).data;
  if (secondRun.status !== "paused" || secondRun.debugger_binding?.instance_id !== restart.instance.instance_id) {
    throw new Error(`发行 Debug 重启后运行归属错误: ${JSON.stringify(secondRun)}`);
  }
  assertTargetNode(secondRun.pid, nodeExecutable);
  if (!processAlive(secondRun.pid)) throw new Error("发行 Node 第二次启动后已退出");
  const secondStop = (await requestJson("/api/v1/debug/node/action", { method: "POST", headers,
    body: JSON.stringify({ ...owner, action: "stop" }) })).data;
  if (secondStop.status !== "exited" || secondStop.pid !== secondRun.pid) {
    throw new Error(`发行 Debug 第二次 stop 未退出: ${JSON.stringify(secondStop)}`);
  }
  await waitForProcessExit(secondRun.pid);
  const disabled = await requestJson(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/config/sources/workspace_root_mutable_override`, {
    method: "GET", headers,
  });
  if (!disabled.data) throw new Error("无法读取 Debug 插件解除绑定配置");
  const finalConfig = JSON.parse(disabled.data.jsonc);
  finalConfig.plugins.runtime.entries = finalConfig.plugins.runtime.entries.filter(
    (entry) => entry.package_id !== "boxteam.developer-tools" || entry.plugin_id !== "debugging",
  );
  const unbound = await requestJson(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/config/sources/workspace_root_mutable_override`, {
    method: "PUT", headers, body: JSON.stringify({ gateway_id: gatewayId, workspace_id: workspaceId,
      source_key: "workspace_root_mutable_override", jsonc: `${JSON.stringify(finalConfig, null, 2)}\n`,
      base_file_digest: disabled.data.file_digest, base_layer_revision: disabled.data.layer_revision,
      base_layer_digest: disabled.data.layer_digest, expected_active_revision: disabled.data.active_revision,
      expected_active_digest: disabled.data.active_digest, idempotency_key: randomUUID() }),
  });
  if (unbound.data?.edit?.state !== "active") throw new Error("Debug 插件解绑未生效");
  const packageStatus = await requestJson("/api/gateway/service-plugins/status", {
    method: "POST", body: JSON.stringify({ gateway_id: gatewayId }),
  });
  if (packageStatus.data.plugins?.some((item) => item.binding?.package_id === "boxteam.developer-tools"
      && item.binding?.plugin_id === "debugging" && item.state !== "stopped")) {
    throw new Error("Debug 插件解绑后实例没有停止");
  }
  process.stdout.write(`Windows发行Debug验收通过: runs=${startPayload.run_id},${secondRun.run_id}; pids=${startPayload.pid},${secondRun.pid}\n`);
}
