import type { FileTreeShortcut } from "../../../types/backend";
import AnchoredOverlay from "../../overlays/AnchoredOverlay";
import { FILESYSTEM_ROOT_PATH, ROOT_PATH } from "./workspaceFileTreePaths";
import {
  type FileTreeContextMenuTarget,
  type WorkspaceFileTreeContextMenuApi,
} from "./useWorkspaceFileTreeContextMenu";

interface WorkspaceFileTreeContextMenuProps {
  menu: WorkspaceFileTreeContextMenuApi;
  shortcuts: readonly FileTreeShortcut[];
  defaultShortcutPaths: ReadonlySet<string>;
}

function isRootTarget(target: FileTreeContextMenuTarget): boolean {
  return target.treePath === ROOT_PATH || target.treePath === FILESYSTEM_ROOT_PATH;
}

/** 文件树右键菜单的渲染层；动作与状态全部来自 useWorkspaceFileTreeContextMenu。 */
export default function WorkspaceFileTreeContextMenu({
  menu,
  shortcuts,
  defaultShortcutPaths,
}: WorkspaceFileTreeContextMenuProps) {
  const contextMenu = menu.contextMenu;
  if (!contextMenu) {
    return null;
  }
  const isDefaultShortcut = defaultShortcutPaths.has(contextMenu.absolutePath);
  const targetShortcut = shortcuts.find(
    (item) => item.path === contextMenu.absolutePath,
  );
  return (
    <AnchoredOverlay
      open
      point={contextMenu}
      placement="bottom-start"
      offset={2}
      onClose={menu.closeContextMenu}
    >
      <div
        className="agent-sessions-session-menu files-tree-context-menu"
        role="menu"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction("新建文件失败", () => menu.createEntry(target, "file"));
        }}>
          <span className="codicon codicon-new-file agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">新建文件</span>
        </button>
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction("新建文件夹失败", () => menu.createEntry(target, "directory"));
        }}>
          <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">新建文件夹</span>
        </button>
        <div className="files-tree-context-separator" role="separator" />
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.closeContextMenu();
          menu.requestUpload(target);
        }}>
          <span className="codicon codicon-cloud-upload agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">上传本地文件</span>
        </button>
        {!isRootTarget(contextMenu) ? (
          <button type="button" role="menuitem" onClick={() => {
            const target = contextMenu;
            menu.closeContextMenu();
            menu.runStatusAction("复制文件失败", () => menu.copyEntryToClipboard(target));
          }}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">复制</span>
            <span className="files-tree-context-keybinding">Ctrl+C</span>
          </button>
        ) : null}
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(
            "粘贴失败",
            () => menu.pasteEntries(target, menu.copiedEntry?.absolutePath),
          );
        }}>
          <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">粘贴</span>
          <span className="files-tree-context-keybinding">Ctrl+V</span>
        </button>
        <div className="files-tree-context-separator" role="separator" />
        {contextMenu.shortcutSource ? (
          <div className="files-tree-context-shortcut-action">
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              menu.runContextAction("删除快捷路径失败", () => menu.removeShortcut(target.absolutePath));
            }}>
              <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">删除当前会话快捷路径</span>
            </button>
            <button
              type="button"
              className="files-tree-context-apply"
              title={isDefaultShortcut
                ? "从当前会话和新会话默认快捷路径中删除"
                : "添加到当前会话，并设为新会话默认快捷路径"}
              aria-label={isDefaultShortcut
                ? `从当前会话和新会话默认快捷路径中删除 ${contextMenu.label}`
                : `将 ${contextMenu.label} 添加到当前会话并设为新会话默认快捷路径`}
              onClick={() => {
                menu.closeContextMenu();
                if (!targetShortcut) {
                  menu.onStatusChange(`快捷路径已失效: ${contextMenu.absolutePath}`);
                  return;
                }
                menu.runStatusAction(
                  "更新当前会话和新会话默认快捷路径失败",
                  () => menu.toggleDefaultShortcut(targetShortcut, isDefaultShortcut),
                );
              }}
            >
              <span
                className={`codicon ${isDefaultShortcut ? "codicon-pinned" : "codicon-pin"}`}
                aria-hidden="true"
              />
            </button>
          </div>
        ) : contextMenu.kind === "directory" ? (
          <div className="files-tree-context-shortcut-action">
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              menu.runContextAction(
                "添加快捷路径失败",
                () => menu.addShortcut(target.treePath, target.label),
              );
            }}>
              <span className="codicon codicon-bookmark agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">添加到当前会话快捷路径</span>
            </button>
            <button
              type="button"
              className="files-tree-context-apply"
              title="添加到当前会话，并设为新会话默认快捷路径"
              aria-label={`将 ${contextMenu.label} 添加到当前会话并设为新会话默认快捷路径`}
              onClick={() => {
                const target = contextMenu;
                menu.runContextAction(
                  "添加当前会话和新会话默认快捷路径失败",
                  () => menu.addShortcutAndDefault(target.treePath, target.label),
                );
              }}
            >
              <span className="codicon codicon-pin" aria-hidden="true" />
            </button>
          </div>
        ) : null}
        <div className="files-tree-context-separator" role="separator" />
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.closeContextMenu();
          menu.runStatusAction("复制路径失败", () => menu.copyPathToClipboard(target));
        }}>
          <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">复制路径</span>
        </button>
        {!isRootTarget(contextMenu) ? (
          <button type="button" role="menuitem" onClick={() => {
            const target = contextMenu;
            menu.runContextAction("下载失败", () => menu.downloadEntry(target));
          }}>
            <span className="codicon codicon-cloud-download agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">下载</span>
          </button>
        ) : null}
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction("在系统中显示失败", () => menu.revealEntry(target));
        }}>
          <span className="codicon codicon-folder-opened agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">在系统中显示</span>
        </button>
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(
            "刷新目录失败",
            () => menu.refreshTargetDirectory(target),
          );
        }}>
          <span className="codicon codicon-refresh agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">刷新根目录文件树</span>
        </button>
      </div>
    </AnchoredOverlay>
  );
}
