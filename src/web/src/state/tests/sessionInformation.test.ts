import { describe, expect, test } from "bun:test";
import {
  buildSessionInformationDump,
  extractSessionIdFromClipboardText,
  formatSessionInformationDump,
  SESSION_INFORMATION_KIND,
} from "../session/sessionInformation";
import type {
  GatewayWorkspace,
  SessionInformationSnapshot,
} from "../../types/backend";

function information(): SessionInformationSnapshot {
  return {
    kind: SESSION_INFORMATION_KIND,
    schema_version: 1,
    generated_at: "2026-07-15T12:00:00Z",
    session: {
      session_id: "ses_test",
      workspace_id: "ws_local",
      title: "会话信息测试",
      title_source: "user",
      current_agent_id: "default",
      parent_session_id: null,
      created_at: "2026-07-15T11:00:00Z",
      updated_at: "2026-07-15T12:00:00Z",
    },
    child_session_ids: [],
    workspace: {
      workspace_id: "ws_local",
      name: "project",
      root_path: "/workspace/project",
    },
    storage_path: "/workspace/project/.boxteam/sessions/ses_test",
    execution: {
      job_id: null,
      status: "idle",
      current_tool: null,
      last_error: null,
    },
    trace: {
      event_count: 0,
      last_event_id: null,
      last_event_type: null,
      last_event_at: null,
    },
    resources: [],
    recent_errors: [],
  };
}

function gatewayWorkspace(
  connectionKind: "local" | "remote_gateway",
): GatewayWorkspace {
  return {
    workspace_id: "gateway_workspace",
    name: "project",
    root_path: "/workspace/project/",
    backend_url: "http://127.0.0.1:41001",
    connection_kind: connectionKind,
    status: "ready",
    active: true,
    managed: true,
    removable: true,
    system_default: false,
    runtime_action:
      connectionKind === "remote_gateway"
        ? "reconnect_remote_gateway"
        : "safe_restart_managed_backend",
    config_reload: {
      available: true,
      healthy: true,
      restart_required: false,
      changed_sections: [],
    },
    remote:
      connectionKind === "remote_gateway"
        ? {
            gateway_connection_id: "remote_gateway_dev",
            gateway_id: "gateway_dev",
            remote_workspace_id: "workspace_dev",
            name: "开发服务器",
            host: "192.0.2.10",
            port: 22,
            username: "developer",
            ssh_config_host: "dev-server",
            remote_gateway_port: 8014,
          }
      : null,
    services: {},
    connection_error: null,
    checked_at: "2026-07-16T00:00:00Z",
  };
}

describe("通用会话信息", () => {
  test("本地工作区生成纯 JSON，且不输出 SSH 字段", () => {
    const dump = buildSessionInformationDump(
      information(),
      gatewayWorkspace("local"),
    );
    const text = formatSessionInformationDump(dump);
    expect(JSON.parse(text).kind).toBe(SESSION_INFORMATION_KIND);
    expect(text).not.toContain('"host"');
    expect(text).not.toContain('"ssh"');
    expect(text).toContain('"connection_error": null');
  });

  test("远程 Gateway 工作区输出联邦连接信息", () => {
    const dump = buildSessionInformationDump(
      information(),
      gatewayWorkspace("remote_gateway"),
    );
    expect(dump.workspace.connection.kind).toBe("remote_gateway");
    if (dump.workspace.connection.kind !== "remote_gateway") {
      throw new Error("远程工作区没有生成 remote_gateway connection");
    }
    expect(dump.workspace.connection.gateway_id).toBe("gateway_dev");
    expect(dump.workspace.connection.gateway_proxy_url).toBe(
      "http://127.0.0.1:41001",
    );
  });

  test("远程 Gateway 权威字段缺失时明确失败", () => {
    const invalidWorkspace = gatewayWorkspace("remote_gateway");
    invalidWorkspace.remote = null;
    expect(() =>
      buildSessionInformationDump(information(), invalidWorkspace),
    ).toThrow("远程 Gateway 工作区信息缺少连接摘要");
  });

  test("粘贴纯会话 ID 时直接返回 ID", () => {
    expect(extractSessionIdFromClipboardText(" ses_direct_123 ")).toBe(
      "ses_direct_123",
    );
  });

  test("粘贴通用会话信息时提取 session.id", () => {
    const text = formatSessionInformationDump(
      buildSessionInformationDump(information(), gatewayWorkspace("local")),
    );
    expect(extractSessionIdFromClipboardText(text)).toBe("ses_test");
    expect(extractSessionIdFromClipboardText(`\`\`\`json\n${text}\n\`\`\``)).toBe(
      "ses_test",
    );
  });

  test("不接受没有协议 kind 的任意 JSON", () => {
    expect(() =>
      extractSessionIdFromClipboardText('{"session":{"id":"ses_test"}}'),
    ).toThrow("既不是会话 ID，也不是有效的通用会话信息 JSON");
  });
});
