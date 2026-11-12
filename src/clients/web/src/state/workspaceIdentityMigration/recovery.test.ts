import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { browserViewKey, readBrowserView, updateBrowserView } from "../browserViewStore";
import type { SessionSelection } from "../sessionViewState";
import { openWorkspaceFileIntentStore } from "../workspaceFiles/intentStore";
import { openCatalogOutboxDatabase } from "../session/sessionCatalogOutboxStore";
import { recoverWorkspaceIdentity } from "./recovery";
import { parseWorkspaceIdentityRecoveries, parseWorkspaceIdentityRecovery, recoverThreadEditor, type WorkspaceIdentityRecovery } from "./material";

const UUID = "10000000-0000-0000-0000-000000000001";
const OTHER_UUID = "20000000-0000-0000-0000-000000000002";
const registry = "gateway_registry", remote = "gateway_remote", alias = "gw_remote";
const material: WorkspaceIdentityRecovery = { format_version: 1, migration_id: "30000000-0000-0000-0000-000000000003",
  registry_gateway_id: registry, mappings: [{ legacy_workspace_id: alias, gateway_id: remote, workspace_id: UUID }] };
const ownerKey = JSON.stringify([remote, UUID]);
const root = `boxteam://${remote}/workspace/${UUID}/resources/files/root`;
const names = ["boxteam-browser-views", "boxteam-workspace-file-intents", "boxteam-session-catalog-outbox"];

async function deleteDatabases(): Promise<void> {
  for (const name of names) await new Promise<void>((resolve, reject) => {
    const request = indexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve(); request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error(`测试连接未关闭：${name}`));
  });
}
beforeEach(deleteDatabases);
afterEach(deleteDatabases);

function finished(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve(); transaction.onabort = () => reject(transaction.error ?? new Error("事务中止"));
  });
}

async function open(name: string, version?: number, prepare?: (database: IDBDatabase) => void): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = version === undefined ? indexedDB.open(name) : indexedDB.open(name, version);
    request.onupgradeneeded = () => prepare?.(request.result);
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
}

async function all(database: IDBDatabase, name: string): Promise<unknown[]> {
  const transaction = database.transaction(name), done = finished(transaction), request = transaction.objectStore(name).getAll();
  await done; return request.result;
}

async function seedFiles(source: WorkspaceIdentityRecovery = material): Promise<{ scopeKey: string; original: Record<string, unknown> }> {
  const mapping = source.mappings[0]!, resourceRoot = `boxteam://${mapping.gateway_id}/workspace/${mapping.workspace_id}/resources/files/root`;
  const scope = { gatewayId: source.registry_gateway_id, gatewayWorkspaceId: mapping.legacy_workspace_id, backendWorkspaceId: mapping.workspace_id, workspaceRoot: resourceRoot, actor: "local" };
  const scopeKey = [source.registry_gateway_id, mapping.legacy_workspace_id, mapping.workspace_id, resourceRoot, "local"].join("\u0000");
  const original = { scope, scopeKey, directoryPath: resourceRoot,
    intent: { client_operation_id: "op_original", client_sequence: 41, directory_key: { workspace_id: mapping.workspace_id, path: resourceRoot, root: resourceRoot, scope: "workspace" },
      expected_directory_revision: 12, depends_on: ["op_preceding"], client_ref: "opaque-original-ref" },
    command: { kind: "upload", entries: [{ name: "payload.txt", relativePath: "payload.txt", file: new Blob(["原始上传 gw_remote"], { type: "text/plain" }) }] },
    receipt: { state: "running", receipt_revision: 9, queue_seq: 37 } };
  const database = await open(names[1]!, 1, database => {
    const records = database.createObjectStore("intents", { keyPath: ["scopeKey", "intent.client_operation_id"] });
    records.createIndex("scopeKey", "scopeKey"); records.createIndex("gatewayWorkspaceId", "scope.gatewayWorkspaceId");
    database.createObjectStore("sequences", { keyPath: "scopeKey" });
  });
  const transaction = database.transaction(["intents", "sequences"], "readwrite"), done = finished(transaction);
  transaction.objectStore("intents").put(original);
  transaction.objectStore("sequences").put({ scopeKey, next: 48 });
  await done; database.close(); return { scopeKey, original };
}

async function seedCatalog(source: WorkspaceIdentityRecovery = material): Promise<Record<string, unknown>> {
  const partition_key = [source.registry_gateway_id, source.mappings[0]!.workspace_id, "user:original"].join("\u0000");
  const original = { partition_key, client_sequence: 991, operation: { client_operation_id: "op_unknown", client_sequence: 991,
    state: "unknown", depends_on: ["op_parent"], name: "正文 gw_remote 不替换", receipt_revision: 14, created_node_id: "folder-original" } };
  const database = await open(names[2]!, 2, database => {
    const operations = database.createObjectStore("operations", { keyPath: ["partition_key", "client_sequence"] });
    operations.createIndex("partition_key", "partition_key");
    operations.createIndex("partition_operation_id", ["partition_key", "operation.client_operation_id"], { unique: true });
    database.createObjectStore("partition-sequences", { keyPath: "partition_key" });
  });
  const transaction = database.transaction(["operations", "partition-sequences"], "readwrite"), done = finished(transaction);
  transaction.objectStore("operations").put(original);
  transaction.objectStore("partition-sequences").put({ partition_key, next_client_sequence: 1007 });
  await done; database.close(); return original;
}

test("材料拒绝重复映射；正确 serializer 原样保留，无来源旧编辑器保留失败", () => {
  expect(() => parseWorkspaceIdentityRecovery({ ...material, mappings: [...material.mappings, material.mappings[0]] })).toThrow("重复旧别名");
  const canonical = { version: 2, target: { gatewayId: remote, workspaceId: UUID, sessionId: "ses", threadId: "thread" }, viewState: { ratio: 0.7 } };
  expect(recoverThreadEditor(canonical, [material])).toBe(canonical);
  const old = { owner: { routeId: alias, workspaceId: UUID, sessionId: "ses", threadId: "thread", title: "未确认来源" } };
  const before = JSON.stringify(old);
  expect(() => recoverThreadEditor(old, [material])).toThrow("来源 Gateway");
  expect(JSON.stringify(old)).toBe(before);
  expect(recoverThreadEditor({ owner: { gatewayId: registry, workspaceId: alias, sessionId: "ses", threadId: "thread", title: "原标题" } }, [material]))
    .toEqual({ version: 2, target: { gatewayId: remote, workspaceId: UUID, sessionId: "ses", threadId: "thread" }, title: "原标题", viewState: undefined });
});

test("多 Gateway 旧队列必须同时导入各来源材料，相同 UUID 不混用归属", async () => {
  const second: WorkspaceIdentityRecovery = { format_version: 1, migration_id: "40000000-0000-0000-0000-000000000004",
    registry_gateway_id: "gateway_other_registry", mappings: [{ legacy_workspace_id: alias, gateway_id: "gateway_other_native", workspace_id: UUID }] };
  expect(() => parseWorkspaceIdentityRecoveries([material, material])).toThrow("同一登记 Gateway");
  expect(() => parseWorkspaceIdentityRecoveries([])).toThrow("至少一份");
  const firstFile = await seedFiles(), secondFile = await seedFiles(second);
  const firstCatalog = await seedCatalog(), secondCatalog = await seedCatalog(second);
  await updateBrowserView(browserViewKey(second.registry_gateway_id, "selection"), () => ({ workspace_id: alias, session_id: "second-session" }));
  const incomplete = await recoverWorkspaceIdentity(material);
  expect(incomplete.batches[1]?.changed).toBe(0);
  expect(incomplete.batches[2]?.changed).toBe(0);
  expect(incomplete.batches[1]?.issues.some(issue => issue.message.includes(second.registry_gateway_id))).toBe(true);
  for (const [name, store, originals, version] of [
    [names[1]!, "intents", [firstFile.original, secondFile.original], 1],
    [names[2]!, "operations", [firstCatalog, secondCatalog], 2],
  ] as const) {
    const database = await open(name);
    expect(database.version).toBe(version);
    expect(await all(database, store)).toHaveLength(originals.length);
    expect(await all(database, store)).toEqual(expect.arrayContaining([...originals]));
    database.close();
  }
  const result = await recoverWorkspaceIdentity([material, second]);
  expect(result.batches.flatMap(batch => batch.issues)).toEqual([]);
  expect(result.migrations.map(item => item.migration_id)).toEqual([material.migration_id, second.migration_id]);
  const files = await open(names[1]!);
  const restoredFiles = await all(files, "intents") as { scope: { gatewayId: string; workspaceId: string }; intent: unknown; receipt: unknown;
    command: { entries: { file: Blob }[] } }[];
  expect(restoredFiles.map(item => item.scope.gatewayId).sort()).toEqual([remote, "gateway_other_native"].sort());
  expect(restoredFiles.every(item => item.scope.workspaceId === UUID)).toBe(true);
  for (const [gatewayId, original] of [[remote, firstFile.original], ["gateway_other_native", secondFile.original]] as const) {
    const row = restoredFiles.find(item => item.scope.gatewayId === gatewayId)!;
    expect(row.intent).toEqual(original.intent);
    expect(row.receipt).toEqual(original.receipt);
    expect(await row.command.entries[0]!.file.text()).toBe("原始上传 gw_remote");
  }
  expect(await all(files, "sequences")).toHaveLength(2); files.close();
  const catalog = await openCatalogOutboxDatabase();
  expect(await all(catalog, "operations")).toEqual(expect.arrayContaining([
    { ...firstCatalog, partition_key: [remote, UUID, "user:original"].join("\u0000") },
    { ...secondCatalog, partition_key: ["gateway_other_native", UUID, "user:original"].join("\u0000") },
  ]));
  expect(await all(catalog, "partition-sequences")).toEqual(expect.arrayContaining([
    { partition_key: [remote, UUID, "user:original"].join("\u0000"), next_client_sequence: 1007 },
    { partition_key: ["gateway_other_native", UUID, "user:original"].join("\u0000"), next_client_sequence: 1007 },
  ]));
  expect(await all(catalog, "identity-recovery")).toHaveLength(2); catalog.close();
  expect(await readBrowserView<SessionSelection>(browserViewKey(second.registry_gateway_id, "selection")))
    .toEqual({ gateway_id: "gateway_other_native", workspace_id: UUID, session_id: "second-session" });
  expect((await recoverWorkspaceIdentity([material, second])).batches.every(batch => batch.changed === 0)).toBe(true);
});

test("显式升级旧 selection、会话视图、设置和草稿；历史正文与其他 Gateway 保持原值", async () => {
  const view = { workspace_id: alias, session_id: "ses", scroll_offset: 73, follow_latest: false, updated_at: "原更新时间" };
  const selection = browserViewKey(registry, "selection"), oldView = browserViewKey(registry, "session", alias, "ses", "main");
  const settingsKey = browserViewKey(registry, "settings"), other = browserViewKey("gateway_other", "selection");
  await updateBrowserView(selection, () => ({ workspace_id: alias, session_id: "ses" }));
  await updateBrowserView(oldView, () => view);
  await updateBrowserView(settingsKey, () => ({ session_sidebar: { collapsed_workspace_ids: [alias], expanded_root_tree_ids: [`workspace:${alias}`, "folder:unchanged"], collapsed_session_ids: ["ses"] },
    workspace_file_tree: { expanded_paths_by_workspace: { [alias]: [root] } } }));
  const oldDraft = `draft:${registry}::${alias}::ses::thread:thread`;
  await updateBrowserView(oldDraft, () => "草稿正文 gw_remote 不替换");
  await updateBrowserView(other, () => ({ workspace_id: "gw_unknown", session_id: "other" }));
  const historyKey = browserViewKey(registry, "thread-history", OTHER_UUID, "history-session", "history-thread");
  const history = { version: 1, owner: { gatewayId: registry, workspaceId: OTHER_UUID, sessionId: "history-session", threadId: "history-thread" },
    timeline: { turns: [{ text: alias, session_id: "canonical-session" }] }, savedAt: 12, bytes: 68 };
  await updateBrowserView(historyKey, () => history);
  const result = await recoverWorkspaceIdentity(material);
  expect(result.batches.flatMap(batch => batch.issues)).toEqual([]);
  expect(await readBrowserView<SessionSelection>(selection)).toEqual({ gateway_id: remote, workspace_id: UUID, session_id: "ses" });
  expect(await readBrowserView(oldView)).toBeUndefined();
  expect(await readBrowserView<typeof view & SessionSelection>(browserViewKey(registry, "session", ownerKey, "ses", "main"))).toEqual({ ...view, gateway_id: remote, workspace_id: UUID });
  expect(await readBrowserView(oldDraft)).toBeUndefined();
  expect(await readBrowserView<string>(`draft:${registry}::${encodeURIComponent(ownerKey)}::ses::thread:thread`)).toBe("草稿正文 gw_remote 不替换");
  expect(await readBrowserView<typeof history>(historyKey)).toEqual(history);
  expect(await readBrowserView<Pick<SessionSelection, "workspace_id" | "session_id">>(other)).toEqual({ workspace_id: "gw_unknown", session_id: "other" });
  const settings = await readBrowserView<{ session_sidebar: { collapsed_workspace_ids: string[]; expanded_root_tree_ids: string[] }; workspace_file_tree: { expanded_paths_by_workspace: Record<string, string[]> } }>(settingsKey);
  expect(settings?.session_sidebar.collapsed_workspace_ids).toEqual([ownerKey]);
  expect(settings?.session_sidebar.expanded_root_tree_ids).toEqual([`workspace:${ownerKey}`, "folder:unchanged"]);
  expect(settings?.workspace_file_tree.expanded_paths_by_workspace).toEqual({ [ownerKey]: [root] });
  expect((await recoverWorkspaceIdentity(material)).batches.every(batch => batch.changed === 0)).toBe(true);
});

test("未知 selection 与目标冲突逐项保留，可确认的其他记录仍事务提交", async () => {
  const selection = browserViewKey(registry, "selection"), source = browserViewKey(registry, "session", alias, "ses", "main");
  const target = browserViewKey(registry, "session", ownerKey, "ses", "main");
  await updateBrowserView(selection, () => ({ workspace_id: "gw_missing", session_id: "ses" }));
  await updateBrowserView(source, () => ({ workspace_id: alias, session_id: "ses", scroll_offset: 1 }));
  await updateBrowserView(target, () => ({ gateway_id: remote, workspace_id: UUID, session_id: "ses", scroll_offset: 2 }));
  const result = await recoverWorkspaceIdentity(material);
  expect(result.batches[0]?.issues.map(issue => issue.key)).toEqual([selection, source]);
  expect(await readBrowserView<{ workspace_id: string; session_id: string; scroll_offset: number }>(source)).toEqual({ workspace_id: alias, session_id: "ses", scroll_offset: 1 });
  expect((await readBrowserView<{ scroll_offset: number }>(target))?.scroll_offset).toBe(2);
  expect(await readBrowserView<Pick<SessionSelection, "workspace_id" | "session_id">>(selection)).toEqual({ workspace_id: "gw_missing", session_id: "ses" });
});

test("文件 v1 普通打开拒绝，显式迁移保留 Blob、命令、回执和序号高水位", async () => {
  const { original } = await seedFiles();
  await expect(openWorkspaceFileIntentStore()).rejects.toThrow("原记录已保留");
  const report = await recoverWorkspaceIdentity(material);
  expect(report.batches.flatMap(batch => batch.issues)).toEqual([]);
  const store = await openWorkspaceFileIntentStore(), scope = { gatewayId: remote, workspaceId: UUID, workspaceRoot: root, actor: "local" as const };
  const [restored] = await store.load(scope);
  expect(restored?.scope).toEqual(scope);
  expect(original.intent).toEqual(restored?.intent);
  expect(original.receipt).toEqual(restored?.receipt);
  if (restored?.command.kind !== "upload") throw new Error("上传命令丢失");
  expect(await restored.command.entries[0]!.file.text()).toBe("原始上传 gw_remote");
  const next = await store.insert({ scope, directoryPath: root, directoryKey: { workspace_id: UUID, path: root, root, scope: "workspace" },
    directoryRevision: 12, command: { kind: "create", name: "next.txt", entryKind: "file" } });
  expect(next.intent.client_sequence).toBe(48);
  expect(next.intent.depends_on).toEqual(["op_original"]);
});

test("文件迁移写入中断整体回滚，同一材料重试成功", async () => {
  const { original } = await seedFiles();
  const put = IDBObjectStore.prototype.put;
  const failure = spyOn(IDBObjectStore.prototype, "put").mockImplementation(function(this: IDBObjectStore, value: unknown, key?: IDBValidKey) {
    if (this.name === "sequences") throw new Error("注入高水位发布失败");
    return key === undefined ? put.call(this, value) : put.call(this, value, key);
  });
  try {
    const report = await recoverWorkspaceIdentity(material);
    expect(report.batches[1]?.changed).toBe(0);
    expect(report.batches[1]?.issues[0]?.message).toBe("注入高水位发布失败");
    const database = await open(names[1]!);
    expect(database.version).toBe(1);
    expect(await all(database, "intents")).toEqual([original]); database.close();
  } finally { failure.mockRestore(); }
  expect((await recoverWorkspaceIdentity(material)).batches[1]?.issues).toEqual([]);
  expect((await recoverWorkspaceIdentity(material)).batches[1]?.changed).toBe(0);
});

test("目录 v2 歧义不派发且保留原分区，精确材料升级后不改 unknown 与高水位", async () => {
  const original = await seedCatalog();
  await expect(openCatalogOutboxDatabase()).rejects.toThrow("原记录已保留");
  const ambiguous = { ...material, mappings: [...material.mappings, { legacy_workspace_id: "gw_local", gateway_id: registry, workspace_id: UUID }] };
  const blocked = await recoverWorkspaceIdentity(ambiguous);
  expect(blocked.batches[2]?.issues.some(issue => issue.message.includes("无法唯一确认"))).toBe(true);
  let database = await open(names[2]!);
  expect(database.version).toBe(2); expect(await all(database, "operations")).toEqual([original]); database.close();
  const result = await recoverWorkspaceIdentity(material);
  expect(result.batches[2]?.issues).toEqual([]);
  database = await openCatalogOutboxDatabase();
  const partition_key = [remote, UUID, "user:original"].join("\u0000");
  expect(database.version).toBe(3);
  expect(await all(database, "operations")).toEqual([{ ...original, partition_key }]);
  expect(await all(database, "partition-sequences")).toEqual([{ partition_key, next_client_sequence: 1007 }]);
  expect(await all(database, "identity-recovery")).toEqual([{ migration_id: material.migration_id, registry_gateway_id: registry, records: 1 }]);
  database.close();
  expect((await recoverWorkspaceIdentity(material)).batches[2]?.changed).toBe(0);
});

test("旧命令已清理时仍须显式恢复独存高水位，不能从 1 重新分配序号", async () => {
  await seedFiles(); await seedCatalog();
  for (const [name, store] of [[names[1]!, "intents"], [names[2]!, "operations"]]) {
    const database = await open(name), transaction = database.transaction(store, "readwrite"), done = finished(transaction);
    transaction.objectStore(store!).clear();
    await done; database.close();
  }
  await expect(openWorkspaceFileIntentStore()).rejects.toThrow("原记录已保留");
  await expect(openCatalogOutboxDatabase()).rejects.toThrow("原记录已保留");
  expect((await recoverWorkspaceIdentity(material)).batches.flatMap(batch => batch.issues)).toEqual([]);
  const files = await open(names[1]!);
  expect(await all(files, "sequences")).toEqual([{ scopeKey: [remote, UUID, root, "local"].join("\u0000"), next: 48 }]);
  files.close();
  const catalog = await openCatalogOutboxDatabase();
  expect(await all(catalog, "partition-sequences")).toEqual([{ partition_key: [remote, UUID, "user:original"].join("\u0000"), next_client_sequence: 1007 }]);
  catalog.close();
});

for (const kind of ["files", "catalog"] as const) test(`${kind} 升级被旧窗口阻塞后放弃原请求，关闭旧窗口不会触发迟到写入`, async () => {
  const original = kind === "files" ? (await seedFiles()).original : await seedCatalog();
  const name = names[kind === "files" ? 1 : 2]!, store = kind === "files" ? "intents" : "operations";
  const held = await open(name);
  held.onversionchange = () => {};
  const report = await recoverWorkspaceIdentity(material);
  expect(report.batches[kind === "files" ? 1 : 2]?.issues[0]?.message).toContain("旧窗口阻塞");
  held.close();
  // 新 open 排在放弃的 upgrade 之后，验证旧窗口关闭后的最终持久状态。
  const database = await open(name);
  expect(database.version).toBe(kind === "files" ? 1 : 2);
  expect(await all(database, store)).toEqual([original]); database.close();
  expect((await recoverWorkspaceIdentity(material)).batches.flatMap(batch => batch.issues)).toEqual([]);
});
