import "fake-indexeddb/auto";
import React from "react";
import { expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { apiSuccessResponse, installGatewayFetch, restoreGatewayTestEnvironment } from "../../../tests/fixtures/session/gatewayEnvironment";
import * as persistence from "../../../state/workspaceFiles/intentStore";
import { LocalDatabaseError } from "../../../state/browserPersistence";
import { useWorkspaceFileOperations } from "./useWorkspaceFileOperations";

test("存储故障停止自动重开；用户再次操作会释放失败 driver 并重新进入持久化写入", async () => {
  const owner = { gatewayId: "gateway-retry", workspaceId: "00000000-0000-4000-8000-000000000001" };
  const root = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
  installGatewayFetch(({ path }) => {
    if (path === "/api/v1/workspace") return apiSuccessResponse({ workspace_id: owner.workspaceId });
    if (path === "/api/v1/workspace/files/roots") return apiSuccessResponse({ workspace_uri: root });
    throw new Error(`恢复不应派发文件操作：${path}`);
  });
  const unavailable = new LocalDatabaseError("boxteam-workspace-file-intents", "open", new Error("临时存储故障"));
  const store = new persistence.WorkspaceFileIntentStore({} as IDBDatabase);
  const open = spyOn(persistence, "openWorkspaceFileIntentStore").mockRejectedValueOnce(unavailable)
    .mockRejectedValueOnce(unavailable).mockResolvedValue(store);
  const insert = spyOn(store, "insert").mockRejectedValue(new Error("已重新进入原持久化写入"));
  const errors: string[] = [];
  let hook!: ReturnType<typeof useWorkspaceFileOperations>;
  function Harness() {
    hook = useWorkspaceFileOperations({ port: 49441, workspaceId: gatewayWorkspaceKey(owner),
      reconcileDirectories: async () => {}, acceptSnapshots: () => {},
      directoryVersion: () => ({ key: { workspace_id: owner.workspaceId, root, path: root, scope: "workspace" }, revision: 1 }),
      onStatusChange: () => {}, onError: message => { errors.push(message); }, onCommitted: () => {} });
    return null;
  }
  let renderer: ReactTestRenderer | undefined;
  try {
    await act(async () => { renderer = create(<Harness />); });
    await new Promise(resolve => setTimeout(resolve, 600));
    expect(open).toHaveBeenCalledTimes(1);
    expect(errors).toHaveLength(1);
    const command = { kind: "create", name: "retry.txt", entryKind: "file" } as const;
    await expect(hook.submit(root, command)).rejects.toBe(unavailable);
    await expect(hook.submit(root, command)).rejects.toThrow("已重新进入原持久化写入");
    expect(open).toHaveBeenCalledTimes(3);
    expect(insert).toHaveBeenCalledTimes(1);
    expect(insert.mock.calls[0]?.[0].scope).toEqual({ ...owner, workspaceRoot: root, actor: "local" });
  } finally {
    act(() => renderer?.unmount()); open.mockRestore(); insert.mockRestore(); restoreGatewayTestEnvironment();
  }
});
