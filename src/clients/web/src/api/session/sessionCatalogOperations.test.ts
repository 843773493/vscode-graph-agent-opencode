import { afterEach, describe, expect, test } from "bun:test";
import { HttpRequestError } from "../http";
import {
  enqueueSessionCatalogOperations,
  fetchSessionCatalogSnapshot,
  isNavigationMutationTerminalState,
  listSessionCatalogNavigationEvents,
  querySessionCatalogOperationStatus,
} from "./sessionCatalogOperations";
import {
  installSessionCatalogFetchMock,
  type SessionCatalogFetchRequest,
  unwrapSessionCatalogFetch,
} from "./sessionApiFetchMock";

afterEach(() => {
  unwrapSessionCatalogFetch();
});

describe("会话目录 operation 状态闭集", () => {
  test("终态只包含 committed/rejected/cancelled/dependency_failed", () => {
    expect(["queued", "running"].map((state) =>
      isNavigationMutationTerminalState(state as "queued"))).toEqual([false, false]);
    expect(["committed", "rejected", "cancelled", "dependency_failed"].map((state) =>
      isNavigationMutationTerminalState(state as "committed"))).toEqual([true, true, true, true]);
  });
});

describe("会话目录批量入队协议", () => {
  test("POST 到写死的 operations:enqueue 路径并携带工作区头与 intents", async () => {
    let request: SessionCatalogFetchRequest | null = null;
    installSessionCatalogFetchMock((captured) => {
      request = captured;
      return Response.json({
        data: {
          workspace_id: "workspace-1",
          accepted_count: 1,
          receipts: [{
            operation_id: "op_00000000000000000000000000000001",
            client_sequence: 1,
            queue_seq: 4,
            kind: "create_folder",
            state: "queued",
            created_node_id: "cnode_1",
            committed_catalog_revision: null,
            error_code: null,
            error_detail: null,
            pending_settlement: false,
            receipt_revision: 3,
            updated_at: "2026-09-24T00:00:00Z",
          }],
          created_node_ids: { op_00000000000000000000000000000001: "cnode_1" },
        },
        request_id: "req-enqueue",
      });
    });

    const result = await enqueueSessionCatalogOperations(48_501, "workspace-1", [{
      client_operation_id: "op_00000000000000000000000000000001",
      client_sequence: 1,
      kind: "create_folder",
      base_catalog_revision: 3,
      name: "新文件夹",
      parent_node_id: null,
    }]);

    expect(request!.path).toBe("/api/v1/session-catalog/operations:enqueue");
    expect(request!.method).toBe("POST");
    expect(new Headers(request!.init?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("workspace-1");
    expect(JSON.parse(String(request!.init?.body))).toEqual({ intents: [{
      client_operation_id: "op_00000000000000000000000000000001",
      client_sequence: 1,
      kind: "create_folder",
      base_catalog_revision: 3,
      name: "新文件夹",
      parent_node_id: null,
    }] });
    expect(result.accepted_count).toBe(1);
    expect(result.receipts[0].created_node_id).toBe("cnode_1");
    expect(result.created_node_ids["op_00000000000000000000000000000001"]).toBe("cnode_1");
  });

  test("空批次不得发请求，必须响亮失败", async () => {
    installSessionCatalogFetchMock(() => {
      throw new Error("空批次不应发请求");
    });
    await expect(enqueueSessionCatalogOperations(48_502, "workspace-1", []))
      .rejects.toThrow("会话目录批量入队至少需要一个 intent");
  });

  test("4xx 明确拒绝透明抛出 HttpRequestError", async () => {
    installSessionCatalogFetchMock(() => Response.json(
      { detail: "preimage 冲突" },
      { status: 409, statusText: "Conflict" },
    ));
    try {
      await enqueueSessionCatalogOperations(48_503, "workspace-1", [{
        client_operation_id: "op_00000000000000000000000000000001",
        client_sequence: 1,
        kind: "rename_node",
        base_catalog_revision: 3,
        expected_revision: 3,
        target_node_id: "cnode_1",
        name: "新名字",
      }]);
      throw new Error("应当抛出错误");
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(HttpRequestError);
      expect((error as HttpRequestError).status).toBe(409);
      expect((error as Error).message).toContain("preimage 冲突");
    }
  });
});

describe("会话目录按 ID 状态查询与 snapshot", () => {
  test("状态查询逐 ID 携带 query 并原样返回 unknown_operation_ids", async () => {
    installSessionCatalogFetchMock(() => Response.json({
      data: {
        workspace_id: "workspace-1",
        catalog_revision: 12,
        items: [],
        unknown_operation_ids: ["op_00000000000000000000000000000009"],
      },
      request_id: "req-status",
    }));
    const page = await querySessionCatalogOperationStatus(48_504, "workspace-1", [
      "op_00000000000000000000000000000009",
    ]);
    expect(page.catalog_revision).toBe(12);
    expect(page.unknown_operation_ids).toEqual(["op_00000000000000000000000000000009"]);
  });

  test("空 ID 集合不得发请求，必须响亮失败", async () => {
    installSessionCatalogFetchMock(() => {
      throw new Error("空集合不应发请求");
    });
    await expect(querySessionCatalogOperationStatus(48_505, "workspace-1", []))
      .rejects.toThrow("会话目录状态查询至少需要一个 operation_id");
  });

  test("snapshot 返回同一 revision 与事件水位", async () => {
    installSessionCatalogFetchMock(() => Response.json({
      data: { workspace_id: "workspace-1", catalog_revision: 12, event_seq_watermark: 7, generation: 12 },
      request_id: "req-snapshot",
    }));
    const snapshot = await fetchSessionCatalogSnapshot(48_506, "workspace-1");
    expect(snapshot.catalog_revision).toBe(12);
    expect(snapshot.event_seq_watermark).toBe(7);
  });
});

describe("会话目录 navigation 事件 channel", () => {
  test("按 after 拉取终态事件与可恢复 cursor", async () => {
    let request: SessionCatalogFetchRequest | null = null;
    installSessionCatalogFetchMock((captured) => {
      request = captured;
      return Response.json({
        data: {
          workspace_id: "workspace-1",
          event_seq_watermark: 9,
          items: [{
            event_seq: 8,
            workspace_id: "workspace-1",
            operation_id: "op_00000000000000000000000000000001",
            queue_seq: 1,
            kind: "rename_node",
            result_state: "committed",
            committed_catalog_revision: 12,
            affected_node_ids: ["cnode_1"],
            error_code: null,
            error_detail: null,
            created_at: "2026-09-24T00:00:00Z",
          }],
          next_cursor: null,
          has_more: false,
          cursor_gone: false,
        },
        request_id: "req-events",
      });
    });
    const page = await listSessionCatalogNavigationEvents(48_507, "workspace-1", { after: 7 });
    expect(request!.path).toBe("/api/v1/session-catalog/navigation-events");
    expect(request!.url).toContain("after=7");
    expect(page.event_seq_watermark).toBe(9);
    expect(page.items[0].result_state).toBe("committed");
    expect(page.items[0].affected_node_ids).toEqual(["cnode_1"]);
  });

  test("负 cursor 必须响亮失败而不是发请求", async () => {
    installSessionCatalogFetchMock(() => {
      throw new Error("负 cursor 不应发请求");
    });
    await expect(listSessionCatalogNavigationEvents(48_508, "workspace-1", { after: -1 }))
      .rejects.toThrow("navigation 事件 cursor 必须是非负整数: -1");
  });
});
