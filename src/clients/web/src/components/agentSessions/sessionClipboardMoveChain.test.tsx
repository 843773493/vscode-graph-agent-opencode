import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import AgentSessionsContextMenus from "./AgentSessionsContextMenus";
import WarmConfirmProvider from "../shell/WarmConfirmProvider";
import {
  PARENT_SESSION_ID,
  SESSION_ID,
  TARGET_FOLDER_ID,
  clickMenuButton,
  installClipboard,
  renderFolderMenu,
  setClipboardText,
} from "./sessionFolderClipboardHarness";

describe("会话「复制 ID → 粘贴移动」真实链路", () => {
  test("复制 canonical 会话 ID 后，将剪贴板会话移动到此处把该 ID 交给 assignSessionFolder", async () => {
    installClipboard();
    setClipboardText(SESSION_ID);

    const target = await renderFolderMenu({
      workspaceId: "gw_1",
      folderId: TARGET_FOLDER_ID,
      parentNodeId: null,
      name: "目标文件夹",
    });
    await clickMenuButton(target.tree, "将剪贴板会话移动到此处");

    expect(target.errors).toEqual([]);
    expect(target.assignSessionFolderCalls).toEqual([
      ["gw_1", SESSION_ID, TARGET_FOLDER_ID],
    ]);
    expect(target.statuses).toEqual([
      "已将会话 " + SESSION_ID + " 移动到 目标文件夹",
    ]);
  });

  test("会话右键菜单「绑定为子会话」把剪贴板 canonical 会话 ID 交给绑定回调", async () => {
    installClipboard();
    setClipboardText(SESSION_ID);
    const bindCalls: unknown[][] = [];
    const statuses: string[] = [];
    let tree!: ReactTestRenderer;
    await act(async () => {
      tree = create(
        <WarmConfirmProvider>
          <AgentSessionsContextMenus
            sessionMenu={{
              sessionId: PARENT_SESSION_ID,
              workspaceId: "gw_1",
              title: "父会话",
              parentSessionId: null,
              x: 10,
              y: 10,
            }}
            workspaceMenu={null}
            onCloseSessionMenu={() => undefined}
            onCloseWorkspaceMenu={() => undefined}
            onRenameSession={() => undefined}
            onDeleteSession={() => undefined}
            onUnbindSession={() => undefined}
            onBindClipboardSession={async (...args) => {
              bindCalls.push(args);
            }}
            onForkSessionContext={async () => undefined}
            onCreateWorkspaceSession={async () => undefined}
            onRequestCreateSessionFolder={() => undefined}
            onCopySessionInformation={async () => undefined}
            onRenameWorkspace={() => undefined}
            onCopyWorkspaceInformation={async () => undefined}
            onRemoveWorkspace={() => undefined}
            onStartWorkspace={async () => undefined}
            onStopWorkspace={async () => undefined}
            startingWorkspaceIds={new Set()}
            onStatusChange={(message) => statuses.push(message)}
          />
        </WarmConfirmProvider>,
      );
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    });

    await clickMenuButton(tree, "绑定为子会话");

    expect(bindCalls).toEqual([[SESSION_ID, PARENT_SESSION_ID, "gw_1"]]);
    expect(statuses).toEqual([
      "已将 " + SESSION_ID + " 绑定到 " + PARENT_SESSION_ID,
    ]);
  });
});
