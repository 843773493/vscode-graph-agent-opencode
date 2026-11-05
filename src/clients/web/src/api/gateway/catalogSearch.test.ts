import { gatewayWorkspaceKey } from "../workspaceIdentity";
import { afterEach, expect, test } from "bun:test";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "../session/sessionApiFetchMock";
import { FederatedSearchEventDTO } from "../../types/protocol_generated/boxteam/search/v1/public";
import { resolveCatalogSearchTarget, searchCatalogView, type CatalogSearchView } from "./catalogSearch";

const owners = ["A", "B"].map(workspace_id => ({ gateway_id: "gateway", workspace_id }));
const scope = { version: 1, gateway_id: "gateway", resolved_at: "2026-11-03T00:00:00Z", mode: "all", owners };
const branch = (index: number) => ({ branch: { owner: owners[index], category: "catalog", operation: "name" },
  state: "complete", retryable: false, allocated_results: 50, returned_results: 1 });
afterEach(unwrapSessionApiFetch);

test("目录消费者仅发一次明确 all，快页先发布并保留离线来源及控制面文件夹", async () => {
  const calls: { path: string; body: unknown }[] = [];
  let output!: ReadableStreamDefaultController<Uint8Array>, sequence = 0;
  const send = (type: string, payload: Record<string, unknown>) => {
    const event = FederatedSearchEventDTO.create({ type, sequence: ++sequence, request_id: "catalog-request", ...payload });
    output.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify(event)}\n\n`));
  };
  installSessionApiFetchMock(({ path, init }) => {
    calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path === "/api/gateway/search/workspaces") return Response.json({ code: 0, request_id: "discovery", data: {
      gateway_id: "gateway", query: "needle", resolved_at: scope.resolved_at, source: "registry", items: [], complete: true,
      folders: [{ name: "Needle folder", navigation_node_id: "gateway-folder", breadcrumb_node_ids: ["gateway-folder"], breadcrumb_names: ["Needle folder"] }],
    } });
    expect(path).toBe("/api/gateway/search");
    return new Response(new ReadableStream<Uint8Array>({ start(controller) { output = controller; } }),
      { headers: { "Content-Type": "text/event-stream", "X-Request-ID": "catalog-request" } });
  });
  const views: CatalogSearchView[] = [];
  const running = searchCatalogView(49_361, "needle", null, new AbortController().signal, view => views.push(structuredClone(view)));
  for (let count = 0; !output && count < 100; count++) await Bun.sleep(1);
  send("scope", { scope: { scope, branches: owners.map(owner => ({ owner, state: "registered" })) } });
  const page = (index: number) => ({ provider_id: "session-catalog", workspace_id: owners[index].workspace_id,
    category: "catalog", operation: "name", contract_version: 1, state: "complete", catalog: { revision: "r1", catalog_revision: 7,
      total: 1, items: [{ node: { node_id: `folder-${index}`, kind: "folder", name: "Needle", node_revision: 1, state: "active" },
        breadcrumb: [{ node_id: `folder-${index}`, kind: "folder", name: "Needle", node_revision: 1, state: "active" }], relative_path: "Needle" }] } });
  send("page", { branch: branch(0), page: page(0), source: { kind: "owner", stale: false } });
  send("branch", { branch: branch(0) });
  for (let count = 0; !views.some(view => view.items.some(item => item.node_id === "folder-0")) && count < 100; count++) await Bun.sleep(1);
  expect(views.at(-1)?.items.some(item => item.node_id === "folder-0")).toBe(true);
  expect(views.at(-1)?.workspaces.find(item => item.workspace_id === gatewayWorkspaceKey(owners[1]))?.status).toBe("searching");
  const source = { kind: "gateway_catalog_mirror", updated_at: scope.resolved_at, stale: true, unavailable_reason: "目标断开" };
  send("page", { branch: branch(1), page: page(1), source });
  send("branch", { branch: branch(1) });
  send("end", { summary: { state: "complete", order: "category-scope-owner-v1", returned_results: 2, branches: [branch(0), branch(1)] } });
  output.close();
  await running;
  expect(calls).toHaveLength(2);
  expect(calls.find(call => call.path === "/api/gateway/search")?.body).toMatchObject({ scope: { mode: "all" }, queries: [{ category: "catalog", allow_catalog_mirror: true }] });
  expect(views.at(-1)?.items.map(item => item.node_id)).toEqual(["gateway-folder", "folder-0", "folder-1"]);
  expect(views.at(-1)?.workspaces[1]).toMatchObject({ workspace_id: gatewayWorkspaceKey(owners[1]), status: "stale", source });
  expect(views.at(-1)?.items.at(-1)?.owner).toEqual(owners[1]);
});

test("目录命中按完整 owner 解析目标，明确失败不改投活动目标", async () => {
  let calls = 0;
  installSessionApiFetchMock(({ path, init }) => {
    expect(path).toBe("/api/gateway/search/resolve");
    expect(JSON.parse(String(init?.body))).toEqual({ mode: "origin", origin: owners[1] });
    return ++calls === 1 ? Response.json({ code: 0, request_id: "resolve", data: { scope, branches: [
      { owner: owners[1], state: "registered" },
    ] } }) : Response.json({ detail: { code: "owner_unavailable", message: "原目标已移除" } }, { status: 404 });
  });
  expect(await resolveCatalogSearchTarget(49_362, owners[1])).toBe(gatewayWorkspaceKey(owners[1]));
  await expect(resolveCatalogSearchTarget(49_362, owners[1])).rejects.toThrow(/404/);
  expect(calls).toBe(2);
});
