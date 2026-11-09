import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";

import { parse } from "jsonc-parser";

export async function verifyBrowserResource(requestJson, { gatewayId, workspaceId, packagePath, url }) {
  const packageDigest = createHash("sha256").update(await readFile(packagePath)).digest("hex");
  await requestJson("/api/gateway/service-plugins/install", {
    method: "POST",
    body: JSON.stringify({ gateway_id: gatewayId, path: packagePath, expected_digest: packageDigest }),
  });
  const headers = { "X-BoxTeam-Workspace-Id": workspaceId };
  const sourcePath = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/config/sources/workspace_root_mutable_override`;
  const document = (await requestJson(sourcePath, { headers })).data;
  const errors = [];
  const config = parse(document.jsonc, errors, { allowTrailingComma: true });
  if (errors.length || !config || typeof config !== "object") {
    throw new Error(`Workspace 插件配置 JSONC 无效: ${JSON.stringify(errors)}`);
  }
  const entries = config.plugins?.runtime?.entries ?? [];
  config.plugins ??= {};
  config.plugins.runtime ??= {};
  config.plugins.runtime.entries = entries.filter(
    (entry) => entry.package_id !== "boxteam.developer-tools" || entry.plugin_id !== "browser",
  );
  config.plugins.runtime.entries.push({
    package_id: "boxteam.developer-tools",
    package_version: "0.1.0",
    plugin_id: "browser",
    enabled: true,
  });
  const update = await requestJson(sourcePath, {
    method: "PUT",
    headers,
    body: JSON.stringify({
      gateway_id: gatewayId,
      workspace_id: workspaceId,
      source_key: "workspace_root_mutable_override",
      jsonc: `${JSON.stringify(config, null, 2)}\n`,
      base_file_digest: document.file_digest,
      base_layer_revision: document.layer_revision,
      base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision,
      expected_active_digest: document.active_digest,
      idempotency_key: randomUUID(),
    }),
  });
  if (update.data?.edit?.state !== "active") {
    throw new Error(`Browser 插件配置未生效: ${JSON.stringify(update.data?.edit)}`);
  }

  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const status = (await requestJson("/api/gateway/service-plugins/status", {
      method: "POST",
      body: JSON.stringify({ gateway_id: gatewayId }),
    })).data;
    const browser = status.plugins?.find((item) =>
      item.binding?.package_id === "boxteam.developer-tools"
      && item.binding?.plugin_id === "browser"
      && item.binding?.workspace_id === workspaceId,
    );
    if (browser?.state === "ready") break;
    if (browser?.state === "failed") {
      throw new Error(`Browser 插件启动失败: ${JSON.stringify(browser)}`);
    }
    await delay(100);
  }
  if (Date.now() >= deadline) throw new Error("Browser 插件未在期限内就绪");

  const session = (await requestJson("/api/v1/sessions", {
    method: "POST",
    headers,
    body: JSON.stringify({ title: "Packaged Browser Smoke" }),
  })).data;
  const resource = (await requestJson("/api/v1/resources/browsers", {
    method: "POST",
    headers,
    body: JSON.stringify({ session_id: session.session_id, thread_id: session.thread_id,
      retention_scope: "workspace", url }),
  })).data;
  if (resource.status !== "running" || !resource.metadata?.page_id) {
    throw new Error(`安装版 Gateway 未通过 Browser 产品入口启动页面: ${JSON.stringify(resource)}`);
  }
  return { resource_id: resource.resource_id, page_id: resource.metadata.page_id };
}
