import "fake-indexeddb/auto";
import { expect, test } from "bun:test";

import { openWorkspaceFileIntentStore, operationDependencyFrontier, type FileIntentScope, type NewFileIntent, type StoredFileIntent } from "./intentStore";

test("文件意图依赖前沿保留未被后继覆盖的最近操作", () => {
  const related: Parameters<typeof operationDependencyFrontier>[0] = [
    { intent: { client_operation_id: "op_4", depends_on: ["external"] } },
    { intent: { client_operation_id: "op_3", depends_on: ["op_2", "external"] } },
    { intent: { client_operation_id: "op_2", depends_on: ["op_1"] } },
    { intent: { client_operation_id: "op_1", depends_on: [] } },
  ];

  expect(operationDependencyFrontier(related)).toEqual(["op_4", "op_3"]);
  expect(operationDependencyFrontier([])).toEqual([]);
});

test("文件意图按 Gateway 与原生 UUID 查询，相同 UUID 的序号和依赖互相隔离", async () => {
  const workspaceId = "00000000-0000-0000-0000-000000000001";
  const workspaceRoot = `boxteam://workspace/${workspaceId}/fs/root`;
  const local: FileIntentScope = { gatewayId: "gateway-file-local", workspaceId, workspaceRoot, actor: "local" };
  const remote: FileIntentScope = { ...local, gatewayId: "gateway-file-remote" };
  const store = await openWorkspaceFileIntentStore();
  const records: StoredFileIntent[] = [];
  const input = (scope: FileIntentScope, name: string): NewFileIntent => ({
    scope, directoryPath: workspaceRoot,
    directoryKey: { workspace_id: workspaceId, scope: "workspace", root: workspaceRoot, path: workspaceRoot },
    directoryRevision: 1, command: { kind: "create", name, entryKind: "file" },
  });
  try {
    const first = await store.insert(input(local, "first.txt"));
    records.push(first);
    const other = await store.insert(input(remote, "other.txt"));
    records.push(other);
    const second = await store.insert(input(local, "second.txt"));
    records.push(second);

    expect(first.intent.client_sequence).toBe(1);
    expect(other.intent.client_sequence).toBe(1);
    expect(other.intent.depends_on).toEqual([]);
    expect(second.intent.client_sequence).toBe(2);
    expect(second.intent.depends_on).toEqual([first.intent.client_operation_id]);
    expect((await store.loadForWorkspace(local)).map(record => record.intent.client_operation_id).sort()).toEqual(
      [first.intent.client_operation_id, second.intent.client_operation_id].sort(),
    );
    expect(await store.loadForWorkspace(remote)).toEqual([other]);
    expect(await store.loadForWorkspace({ gatewayId: "gateway-absent", workspaceId })).toEqual([]);
    expect((await store.load(local)).map(record => record.scope)).toEqual([local, local]);
    expect((await store.load(remote))[0]?.intent.directory_key.workspace_id).toBe(workspaceId);
  } finally {
    for (const record of records) await store.remove(record);
  }
});
