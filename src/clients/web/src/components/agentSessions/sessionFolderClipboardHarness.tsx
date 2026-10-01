import { afterEach, spyOn } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import SessionResourceOverlays from "./SessionResourceOverlays";
import * as clipboard from "../../utils/clipboard";
import type { SessionResourceExplorerController } from "../../hooks/session/useSessionResourceExplorer";

/**
 * 「复制 → 粘贴移动」真实链路的共享装配：驱动 SessionResourceOverlays 与
 * AgentSessionsContextMenus 的真实菜单按钮，经应用内剪贴板桩与假 explorer 观察
 * 提交结果。只服务 agentSessions 下两个链路测试文件。
 */

// 后端 canonical：folder 与 session 同表、id 一律 ses_ + 32 位小写 hex +
// UUIDv7 位 profile（app/core/session_catalog_store.py:validate_session_id）。
export const FOLDER_ID = "ses_0190f2a3b4c5700080000000000000ab";
export const TARGET_FOLDER_ID = "ses_0190f2a3b4c5700080000000000000cd";
export const SESSION_ID = "ses_0190f2a3b4c570008000000000000001";
export const PARENT_SESSION_ID = "ses_0190f2a3b4c570008000000000000002";

let clipboardText = "";
let restoreClipboard: (() => void) | null = null;

afterEach(() => {
  clipboardText = "";
  restoreClipboard?.();
  restoreClipboard = null;
});

/** 当前剪贴板内容；链路测试用它模拟「先复制、后粘贴」。 */
export function setClipboardText(text: string): void {
  clipboardText = text;
}

/** 读取剪贴板桩里的内容。 */
export function getClipboardText(): string {
  return clipboardText;
}

/** 安装应用内剪贴板桩：复制写入、读取返回，模拟「复制 → 粘贴」的真实链路。 */
export function installClipboard(): void {
  const readSpy = spyOn(clipboard, "readTextFromClipboard")
    .mockImplementation(async () => clipboardText);
  const copySpy = spyOn(clipboard, "copyTextToClipboard")
    .mockImplementation(async (text: string) => {
      clipboardText = text;
    });
  restoreClipboard = () => {
    readSpy.mockRestore();
    copySpy.mockRestore();
  };
}

/** 假 explorer：只记录本链路真正触发的动作，其余动作无副作用。 */
export function fakeExplorer(): {
  explorer: SessionResourceExplorerController;
  moveSessionFolderCalls: unknown[][];
  assignSessionFolderCalls: unknown[][];
} {
  const moveSessionFolderCalls: unknown[][] = [];
  const assignSessionFolderCalls: unknown[][] = [];
  const explorer = {
    expandedIds: new Set<string>(),
    toggleExpanded: () => undefined,
    placeWorkspaceNode: async () => undefined,
    loadBranch: async () => undefined,
    moveSessionFolder: async (...args: unknown[]) => {
      moveSessionFolderCalls.push(args);
    },
    assignSessionFolder: async (...args: unknown[]) => {
      assignSessionFolderCalls.push(args);
    },
  } as unknown as SessionResourceExplorerController;
  return { explorer, moveSessionFolderCalls, assignSessionFolderCalls };
}

/** 按可见菜单标签定位并点击真实 button，走组件自己的 onClick 链路。 */
export function clickMenuButton(
  tree: ReactTestRenderer,
  label: string,
): Promise<void> {
  const buttons = tree.root.findAll(
    (node) =>
      node.type === "button"
      && node.findAll((child) => child.props?.children === label).length > 0,
  );
  const button = buttons[0];
  if (!button) throw new Error("未找到菜单按钮: " + label);
  return act(async () => {
    button.props.onClick();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

export interface OverlaysHarness {
  tree: ReactTestRenderer;
  moveSessionFolderCalls: unknown[][];
  assignSessionFolderCalls: unknown[][];
  statuses: string[];
  errors: string[];
}

/** 渲染 SessionResourceOverlays 的文件夹右键菜单并收集链路观测值。 */
export async function renderFolderMenu(folderMenu: {
  workspaceId: string;
  folderId: string;
  parentNodeId: string | null;
  name: string;
}): Promise<OverlaysHarness> {
  const { explorer, moveSessionFolderCalls, assignSessionFolderCalls } = fakeExplorer();
  const statuses: string[] = [];
  const errors: string[] = [];
  let tree!: ReactTestRenderer;
  await act(async () => {
    tree = create(
      <SessionResourceOverlays
        workspaceFolderMenu={null}
        setWorkspaceFolderMenu={() => undefined}
        setWorkspaceFolderEditor={() => undefined}
        folderMenu={{ ...folderMenu, x: 10, y: 10 }}
        setFolderMenu={() => undefined}
        resourceDialog={null}
        setResourceDialog={() => undefined}
        explorer={explorer}
        onCreateSessionInFolder={async () => undefined}
        onSessionFolderDeleted={async () => undefined}
        onStatusChange={(message) => statuses.push(message)}
        setActionError={() => undefined}
        handleError={(prefix, error) => {
          errors.push(prefix + ": " + String((error as Error)?.message ?? error));
        }}
      />,
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
  return { tree, moveSessionFolderCalls, assignSessionFolderCalls, statuses, errors };
}
