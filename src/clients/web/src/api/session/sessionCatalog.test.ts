import { afterEach, describe, expect, test } from "bun:test";
import {
  getSessionCatalogBreadcrumb,
  listSessionCatalogChildren,
  refreshSessionCatalog,
} from "./sessionCatalog";
import {
  installSessionCatalogFetchMock,
  unwrapSessionCatalogFetch,
} from "./sessionApiFetchMock";

function catalogPage(items: unknown[]): Response {
  return Response.json({
    data: { revision: "catalog", parent_node_id: null, items, cursor: null, total: items.length },
    request_id: "req-catalog",
  });
}

afterEach(() => {
  unwrapSessionCatalogFetch();
});

describe("会话目录节点合法性校验", () => {
  test("成功：合法节点原样返回", async () => {
    installSessionCatalogFetchMock(() => catalogPage([
      { node_id: "cnode_1", kind: "folder", name: "文件夹", has_children: true },
    ]));
    const page = await listSessionCatalogChildren(48_401, "workspace-1");
    expect(page.items[0]?.node_id).toBe("cnode_1");
  });

  test("缺少 node_id 的子节点必须响亮失败而不是留给 React 警告", async () => {
    installSessionCatalogFetchMock(() => catalogPage([
      { kind: "folder", name: "坏文件夹", has_children: true },
    ]));
    await expect(listSessionCatalogChildren(48_402, "workspace-1"))
      .rejects.toThrow("会话目录子节点第 1 个节点的 node_id 必须是非空字符串");
  });

  test("node_id 为非字符串的子节点必须响亮失败", async () => {
    installSessionCatalogFetchMock(() => catalogPage([
      { node_id: 42, kind: "session", name: "坏会话", session_id: "s1", has_children: false },
    ]));
    await expect(listSessionCatalogChildren(48_403, "workspace-1"))
      .rejects.toThrow("node_id 必须是非空字符串");
  });

  test("重复 node_id 的同级节点必须响亮失败", async () => {
    installSessionCatalogFetchMock(() => catalogPage([
      { node_id: "dup", kind: "session", name: "甲", session_id: "s-a", has_children: false },
      { node_id: "dup", kind: "session", name: "乙", session_id: "s-b", has_children: false },
    ]));
    await expect(listSessionCatalogChildren(48_404, "workspace-1"))
      .rejects.toThrow("会话目录子节点存在重复的 node_id: dup");
  });

  test("刷新目录同样校验节点合法性", async () => {
    installSessionCatalogFetchMock(() => catalogPage([
      { kind: "session", name: "缺 ID", session_id: "s1", has_children: false },
    ]));
    await expect(refreshSessionCatalog(48_405, "workspace-1"))
      .rejects.toThrow("会话目录刷新第 1 个节点的 node_id 必须是非空字符串");
  });

  test("面包屑同样校验节点合法性", async () => {
    installSessionCatalogFetchMock(() => Response.json({
      data: { revision: "catalog", items: [{ kind: "folder", name: "坏文件夹" }] },
      request_id: "req-catalog",
    }));
    await expect(getSessionCatalogBreadcrumb(48_406, "workspace-1", "cnode_1"))
      .rejects.toThrow("会话目录面包屑第 1 个节点的 node_id 必须是非空字符串");
  });
});
