import { afterEach, describe, expect, test } from "bun:test";
import {
  activateNodeDebugConfiguration,
  applyNodeDebugAction,
  copyNodeDebugConfiguration,
  createNodeDebugConfiguration,
  deleteNodeDebugConfiguration,
  getNodeDebugCapabilities,
  getNodeDebugConfiguration,
  getNodeDebugState,
  importNodeDebugConfiguration,
  startNodeDebug,
  updateNodeDebugConfiguration,
} from "./nodeDebug";
import type {
  NodeDebugActionRequest,
  NodeDebugCapabilities,
  NodeDebugConfiguration,
  NodeDebugState,
} from "../types/backend";

const originalFetch = globalThis.fetch;
const nodeDebugTestPort = 59_731;

type CapturedRequest = {
  url: URL;
  method: string;
  headers: Headers;
  body: Record<string, unknown> | null;
};

function state(sessionId: string, threadId: string): NodeDebugState {
  return {
    session_id: sessionId,
    status: "idle",
    configurations: [],
    args: [],
    call_stack: [],
    breakpoints: [],
    output: [],
    evaluations: [],
    actions: [],
    source_changed_paths: [],
    thread_id: threadId,
  };
}

function configuration(configurationId: string): NodeDebugConfiguration {
  return {
    configuration_id: configurationId,
    name: "Node 调试配置",
    args: [],
    breakpoints: [],
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
  };
}

const capabilities: NodeDebugCapabilities = {
  enabled: true,
  default_adapter: "node",
  supported_adapters: ["node"],
  launch_profiles: [],
};

function parseBody(init: RequestInit | undefined): Record<string, unknown> | null {
  if (typeof init?.body !== "string") return null;
  return JSON.parse(init.body) as Record<string, unknown>;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("Node Debug API client", () => {
  test("为所有 Node Debug 写操作传递 thread_id 并编码会话查询参数", async () => {
    const sessionId = "session / 需要编码";
    const threadId = "thread/? &需要编码";
    const configurationId = "configuration/id";
    const workspaceId = "workspace-node-debug";
    const requests: CapturedRequest[] = [];

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const url = new URL(String(input));
        if (url.pathname === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "node-debug-token" } });
        }
        if (url.pathname === "/api/gateway/users/current") {
          return Response.json({ data: {}, request_id: "req-user" });
        }

        requests.push({
          url,
          method: init?.method ?? "GET",
          headers: new Headers(init?.headers),
          body: parseBody(init),
        });

        const data = url.pathname === "/api/v1/debug/node/capabilities"
          ? capabilities
          : url.pathname.endsWith("/copy")
            || (url.pathname.includes("/configurations/") && !init?.method)
            ? configuration(configurationId)
            : state(sessionId, threadId);
        return Response.json({ data, request_id: "req-node-debug" });
      },
      { preconnect: originalFetch.preconnect },
    );

    expect(await getNodeDebugCapabilities(nodeDebugTestPort, workspaceId))
      .toEqual(capabilities);
    expect(await getNodeDebugState(
      nodeDebugTestPort,
      sessionId,
      threadId,
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await getNodeDebugConfiguration(
      nodeDebugTestPort,
      sessionId,
      threadId,
      configurationId,
      workspaceId,
    )).toEqual(configuration(configurationId));
    expect(await startNodeDebug(
      nodeDebugTestPort,
      {
        session_id: sessionId,
        thread_id: threadId,
        path: "src/index.ts",
      },
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await createNodeDebugConfiguration(
      nodeDebugTestPort,
      {
        session_id: sessionId,
        thread_id: threadId,
        name: "Node 调试配置",
      },
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await updateNodeDebugConfiguration(
      nodeDebugTestPort,
      configurationId,
      {
        session_id: sessionId,
        thread_id: threadId,
        name: "更新后的配置",
      },
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await activateNodeDebugConfiguration(
      nodeDebugTestPort,
      configurationId,
      { session_id: sessionId, thread_id: threadId },
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await deleteNodeDebugConfiguration(
      nodeDebugTestPort,
      sessionId,
      threadId,
      configurationId,
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await importNodeDebugConfiguration(
      nodeDebugTestPort,
      {
        session_id: sessionId,
        thread_id: threadId,
        configuration: configuration(configurationId),
        activate: false,
      },
      workspaceId,
    )).toEqual(state(sessionId, threadId));
    expect(await copyNodeDebugConfiguration(
      nodeDebugTestPort,
      configurationId,
      {
        source_session_id: "source session / 需要编码",
        source_thread_id: "source thread / 需要编码",
        target_session_id: "target session / 需要编码",
        target_thread_id: "target thread / 需要编码",
        activate: false,
      },
      workspaceId,
    )).toEqual(configuration(configurationId));
    const action: NodeDebugActionRequest = {
      session_id: sessionId,
      thread_id: threadId,
      action: "continue",
      params: {},
    };
    expect(await applyNodeDebugAction(
      nodeDebugTestPort,
      action,
      workspaceId,
    )).toEqual(state(sessionId, threadId));

    const request = (method: string, path: string): CapturedRequest => {
      const found = requests.find(
        (candidate) => candidate.method === method && candidate.url.pathname === path,
      );
      if (!found) throw new Error(`未捕获 ${method} ${path}`);
      return found;
    };
    const stateRequest = request("GET", "/api/v1/debug/node");
    const encodedQuery = `?session_id=${encodeURIComponent(sessionId)}&thread_id=${encodeURIComponent(threadId)}`;
    expect(stateRequest.url.search).toBe(encodedQuery);
    expect(stateRequest.url.toString()).toContain(
      `session_id=${encodeURIComponent(sessionId)}`,
    );
    expect(stateRequest.url.toString()).toContain(
      `thread_id=${encodeURIComponent(threadId)}`,
    );

    const configurationPath = `/api/v1/debug/node/configurations/${encodeURIComponent(configurationId)}`;
    const configurationRequest = request("GET", configurationPath);
    expect(configurationRequest.url.search).toBe(encodedQuery);
    const deleteRequest = request("DELETE", configurationPath);
    expect(deleteRequest.url.search).toBe(encodedQuery);

    expect(request("POST", "/api/v1/debug/node/start").body).toMatchObject({
      session_id: sessionId,
      thread_id: threadId,
    });
    expect(request("POST", "/api/v1/debug/node/configurations").body)
      .toMatchObject({ session_id: sessionId, thread_id: threadId });
    expect(request("PUT", configurationPath).body).toMatchObject({
      session_id: sessionId,
      thread_id: threadId,
    });
    expect(request("POST", `${configurationPath}/activate`).body).toEqual({
      session_id: sessionId,
      thread_id: threadId,
    });
    expect(request("POST", "/api/v1/debug/node/configurations/import").body)
      .toMatchObject({ session_id: sessionId, thread_id: threadId });
    expect(request("POST", "/api/v1/debug/node/action").body).toEqual({
      session_id: sessionId,
      thread_id: threadId,
      action: "continue",
      params: {},
    });

    const copyRequest = request("POST", `${configurationPath}/copy`);
    expect(copyRequest.body).toMatchObject({
      source_session_id: "source session / 需要编码",
      source_thread_id: "source thread / 需要编码",
      target_session_id: "target session / 需要编码",
      target_thread_id: "target thread / 需要编码",
    });

    for (const captured of requests) {
      expect(captured.headers.get("X-BoxTeam-Workspace-Id")).toBe(workspaceId);
    }
  });
});
