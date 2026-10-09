import { spyOn } from "bun:test";
import * as storage from "../state/workspaceFiles/intentStore";
import { restoreGlobalDescriptor } from "./testGlobals";

/** 仅供 UI 单元测试隔离持久层；真实 IndexedDB/Blob 验收在 Chromium 集成脚本。 */
export function installWorkspaceFileIntentStoreMock() {
  const original = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  Object.defineProperty(globalThis, "indexedDB", { configurable: true, value: {} });
  const records = new Map<string, storage.StoredFileIntent>();
  const store = new storage.WorkspaceFileIntentStore({} as IDBDatabase);
  let sequence = 0;
  const mocks = [
    spyOn(store, "load").mockImplementation(async (scope) => [...records.values()]
      .filter((record) => record.scopeKey === storage.fileIntentScopeKey(scope))),
    spyOn(store, "loadForWorkspace").mockImplementation(async (id) => [...records.values()]
      .filter((record) => record.scope.gatewayWorkspaceId === id)),
    spyOn(store, "insert").mockImplementation(async (input) => {
      const record: storage.StoredFileIntent = {
        scopeKey: storage.fileIntentScopeKey(input.scope), scope: input.scope,
        directoryPath: input.directoryPath, command: input.command, receipt: null,
        intent: { client_operation_id: `op_${crypto.randomUUID().replace(/-/g, "")}`,
          client_sequence: ++sequence, directory_key: input.directoryKey,
          expected_directory_revision: input.directoryRevision, depends_on: [],
          relative_child_path: "", client_ref: "unit-file-intent" },
      };
      records.set(record.intent.client_operation_id, record);
      return record;
    }),
    spyOn(store, "accept").mockImplementation(async (record, receipt) => {
      const current = records.get(record.intent.client_operation_id);
      if (current) records.set(record.intent.client_operation_id, { ...current, receipt });
    }),
    spyOn(store, "remove").mockImplementation(async (record) => { records.delete(record.intent.client_operation_id); }),
    spyOn(storage, "openWorkspaceFileIntentStore").mockResolvedValue(store),
  ];
  return { records, restore: () => {
    mocks.forEach((mock) => mock.mockRestore());
    restoreGlobalDescriptor("indexedDB", original);
  } };
}
