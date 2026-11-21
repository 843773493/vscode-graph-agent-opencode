import { afterEach, describe, expect, test } from "bun:test";
import {
  getSessionCatalogBreadcrumb,
  getSessionCatalogExport,
  listSessionCatalogChildren,
  refreshSessionCatalog,
} from "./sessionCatalog";
import {
  installSessionApiFetchMock,
  unwrapSessionApiFetch,
} from "./sessionApiFetchMock";

function catalogPage(items: unknown[]): Response {
  return Response.json({
    data: { revision: "catalog", catalog_revision: 3, parent_node_id: null, items, cursor: null, total: items.length,
      sync: { ...workspaceTarget, source: "authoritative", stale: false } },
    request_id: "req-catalog",
  });
}

afterEach(() => {
  unwrapSessionApiFetch();
});

describe("会话目录写后读取范围", () => {
  const node = { node_id: "folder-a", kind: "folder", name: "目录", has_children: false };
  const base = { revision: "r105", catalog_revision: 105, sync: { ...workspaceTarget, source: "authoritative", stale: false } };

  test("children／refresh／breadcrumb／指定节点逐跳携带105下限和精确范围", async () => {
    const paths: string[] = [];
    installSessionApiFetchMock(({ url, path, init }) => {
      const query = new URL(url).searchParams;
      expect(query.get("minimum_catalog_revision")).toBe("105");
      expect(new Headers(init?.headers).get("X-BoxTeam-Catalog-Revision")).toBe("known-projection");
      paths.push(path);
      if (path.endsWith("/export")) {
        expect(query.getAll("node_id")).toEqual(["folder-a"]);
        return Response.json({ data: { ...base, items: [node], node_ids: [node.node_id], complete: false }, request_id: "req-scope" });
      }
      if (path.includes("/breadcrumb/")) return Response.json({ data: { ...base, items: [node], node_id: node.node_id }, request_id: "req-scope" });
      return Response.json({ data: { ...base, parent_node_id: path.endsWith("/refresh") ? null : node.node_id,
        items: [], total: 0, cursor: null }, request_id: "req-scope" });
    });
    const options = { minimumCatalogRevision: 105, knownRevision: "known-projection" };
    await listSessionCatalogChildren(48_410, workspaceTarget, node.node_id, null, options);
    await refreshSessionCatalog(48_410, workspaceTarget, options);
    await getSessionCatalogBreadcrumb(48_410, workspaceTarget, node.node_id, options);
    await getSessionCatalogExport(48_410, workspaceTarget, { ...options, nodeIds: [node.node_id] });
    expect(paths).toHaveLength(4);
  });

  test("数字相同的旧投影镜像不能替代已知配置，权威更新仍可直接推进", async () => {
    let source = "gateway_mirror";
    installSessionApiFetchMock(() => Response.json({ data: {
      ...base, parent_node_id: null, items: [], total: 0, cursor: null,
      sync: { ...base.sync, source },
    }, request_id: "req-projection" }));
    const options = { minimumCatalogRevision: 105, knownRevision: "new-config" };
    await expect(listSessionCatalogChildren(48_412, workspaceTarget, null, null, options)).rejects.toThrow("镜像尚未确认已知投影版本");
    source = "authoritative";
    expect((await listSessionCatalogChildren(48_412, workspaceTarget, null, null, options)).revision).toBe("r105");
  });

  for (const failure of ["版本103", "其它Gateway", "其它父节点", "其它目标", "其它导出范围"] as const) {
    test(`${failure}不能确认105`, async () => {
      installSessionApiFetchMock(() => Response.json({ request_id: "req-scope", data: {
        ...base, items: [], total: 0, cursor: null, parent_node_id: failure === "其它父节点" ? "folder-b" : node.node_id,
        node_id: "folder-b", node_ids: ["folder-b"], complete: false,
        ...(failure === "版本103" ? { catalog_revision: 103 } : {}),
        ...(failure === "其它Gateway" ? { sync: { ...base.sync, gateway_id: "other" } } : {}),
      } }));
      const read = failure === "其它目标" ? getSessionCatalogBreadcrumb(48_411, workspaceTarget, node.node_id, { minimumCatalogRevision: 105 })
        : failure === "其它导出范围" ? getSessionCatalogExport(48_411, workspaceTarget, { minimumCatalogRevision: 105, nodeIds: [node.node_id] })
        : listSessionCatalogChildren(48_411, workspaceTarget, node.node_id, null, { minimumCatalogRevision: 105 });
      await expect(read).rejects.toThrow(failure === "版本103" ? "103 < 105" : failure === "其它Gateway" ? "归属不一致" : failure === "其它导出范围" ? "范围不一致" : "作用域不一致");
    });
  }
});

const workspaceTarget = {
  workspace_id: "00000000-0000-4000-8000-000000000001",
  gateway_id: "gateway-test",
} as const;

describe("会话目录节点合法性校验", () => {
  test("成功：合法节点原样返回", async () => {
    let request: RequestInit | undefined;
    installSessionApiFetchMock((captured) => {
      request = captured.init;
      return catalogPage([
      { node_id: "cnode_1", kind: "folder", name: "文件夹", has_children: true },
      ]);
    });
    const page = await listSessionCatalogChildren(48_401, workspaceTarget);
    expect(page.items[0]?.node_id).toBe("cnode_1");
    expect(new Headers(request?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("00000000-0000-4000-8000-000000000001");
    expect(new Headers(request?.headers).get("X-BoxTeam-Target-Gateway-Id"))
      .toBe("gateway-test");
  });

  test("缺少 node_id 的子节点必须响亮失败而不是留给 React 警告", async () => {
    installSessionApiFetchMock(() => catalogPage([
      { kind: "folder", name: "坏文件夹", has_children: true },
    ]));
    await expect(listSessionCatalogChildren(48_402, workspaceTarget))
      .rejects.toThrow("会话目录子节点第 1 个节点的 node_id 必须是非空字符串");
  });

  test("node_id 为非字符串的子节点必须响亮失败", async () => {
    installSessionApiFetchMock(() => catalogPage([
      { node_id: 42, kind: "session", name: "坏会话", session_id: "s1", has_children: false },
    ]));
    await expect(listSessionCatalogChildren(48_403, workspaceTarget))
      .rejects.toThrow("node_id 必须是非空字符串");
  });

  test("重复 node_id 的同级节点必须响亮失败", async () => {
    installSessionApiFetchMock(() => catalogPage([
      { node_id: "dup", kind: "session", name: "甲", session_id: "s-a", has_children: false },
      { node_id: "dup", kind: "session", name: "乙", session_id: "s-b", has_children: false },
    ]));
    await expect(listSessionCatalogChildren(48_404, workspaceTarget))
      .rejects.toThrow("会话目录子节点存在重复的 node_id: dup");
  });

  test("刷新目录同样校验节点合法性", async () => {
    installSessionApiFetchMock(() => catalogPage([
      { kind: "session", name: "缺 ID", session_id: "s1", has_children: false },
    ]));
    await expect(refreshSessionCatalog(48_405, workspaceTarget))
      .rejects.toThrow("会话目录刷新第 1 个节点的 node_id 必须是非空字符串");
  });

  test("面包屑同样校验节点合法性", async () => {
    installSessionApiFetchMock(() => Response.json({
      data: { revision: "catalog", items: [{ kind: "folder", name: "坏文件夹" }] },
      request_id: "req-catalog",
    }));
    await expect(getSessionCatalogBreadcrumb(48_406, workspaceTarget, "cnode_1"))
      .rejects.toThrow("会话目录面包屑第 1 个节点的 node_id 必须是非空字符串");
  });
});
