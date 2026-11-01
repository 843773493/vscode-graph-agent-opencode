import { afterEach, describe, expect, test } from "bun:test";
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

const errorTrees: ReactTestRenderer[] = [];
afterEach(async () => {
  await act(async () => { for (const tree of errorTrees.splice(0)) tree.unmount(); });
});

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
      "会话 " + SESSION_ID + " 移入 目标文件夹 的变更已保存本地，等待提交",
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
            onError={(message) => { throw new Error(`成功操作不应触发错误：${message}`); }}
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

  test.each([
    ["复制 ID", "session", "ready"],
    ["绑定为子会话", "session", "ready"],
    ["复制会话信息", "session", "ready"],
    ["从上下文创建分支", "session", "ready"],
    ["复制工作区信息", "workspace", "ready"],
    ["关闭工作区", "workspace", "ready"],
    ["启动工作区", "workspace", "offline"],
    ["新建会话", "workspace", "ready"],
  ] as const)("%s 失败交给显式错误出口并保留发起身份", async (label, kind, status) => {
    installClipboard();
    setClipboardText(SESSION_ID);
    const reject = async () => { throw new Error("fixture-operation-rejected"); };
    Object.defineProperty(navigator.clipboard, "writeText", { value: reject });
    const errors: string[] = [];
    const statuses: string[] = [];
    let tree!: ReactTestRenderer;
    await act(async () => {
      tree = create(<WarmConfirmProvider><AgentSessionsContextMenus
        sessionMenu={kind === "session" ? { sessionId: PARENT_SESSION_ID, workspaceId: "gw_origin", title: "发起会话", parentSessionId: null, x: 10, y: 10 } : null}
        workspaceMenu={kind === "workspace" ? { workspaceId: "gw_origin", name: "发起工作区", parentWorkspaceId: null,
          removable: true, managed: true, systemDefault: false, status, x: 10, y: 10 } : null}
        onCloseSessionMenu={() => undefined} onCloseWorkspaceMenu={() => undefined}
        onRenameSession={() => undefined} onDeleteSession={() => undefined} onUnbindSession={() => undefined}
        onBindClipboardSession={reject} onForkSessionContext={reject} onCreateWorkspaceSession={reject}
        onRequestCreateSessionFolder={() => undefined} onCopySessionInformation={reject}
        onRenameWorkspace={() => undefined} onCopyWorkspaceInformation={reject} onRemoveWorkspace={() => undefined}
        onStartWorkspace={reject} onStopWorkspace={reject} startingWorkspaceIds={new Set()}
        onStatusChange={message => statuses.push(message)} onError={message => errors.push(message)}
      /></WarmConfirmProvider>);
      errorTrees.push(tree);
      await new Promise<void>(resolve => setTimeout(resolve, 0));
    });
    await clickMenuButton(tree, label);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("fixture-operation-rejected");
    expect(errors[0]).toContain("gw_origin");
    if (kind === "session") expect(errors[0]).toContain(PARENT_SESSION_ID);
    expect(statuses.every(message => !message.includes("fixture-operation-rejected"))).toBe(true);
  });
});
