import { t } from "../../../../../shared/core/i18n/index.js";
import type { FileTreeShortcut } from "../../../types/backend";
import AnchoredOverlay from "../../overlays/AnchoredOverlay";
import { parentFileTreePath } from "./workspaceFileTreePaths";
import {
  type FileTreeContextMenuTarget,
  type WorkspaceFileTreeContextMenuApi,
} from "./useWorkspaceFileTreeContextMenu";

interface WorkspaceFileTreeContextMenuProps {
  menu: WorkspaceFileTreeContextMenuApi;
  shortcuts: readonly FileTreeShortcut[];
  defaultShortcutPaths: ReadonlySet<string>;
  onFileOpenMenu?: FileOpenMenuHandler;
}

/** 宿主提供专属打开方式，共享文件树不依赖宿主的编辑器服务。 */
export type FileOpenMenuHandler = (path: string, anchor: { x: number; y: number }) => void;

function isRootTarget(target: FileTreeContextMenuTarget): boolean {
  return parentFileTreePath(target.treePath) === target.treePath;
}

/** 文件树右键菜单的渲染层；动作与状态全部来自 useWorkspaceFileTreeContextMenu。 */
export default function WorkspaceFileTreeContextMenu({
  menu,
  shortcuts,
  defaultShortcutPaths,
  onFileOpenMenu,
}: WorkspaceFileTreeContextMenuProps) {
  const contextMenu = menu.contextMenu;
  if (!contextMenu) {
    return null;
  }
  const isDefaultShortcut = defaultShortcutPaths.has(contextMenu.uri);
  const targetShortcut = shortcuts.find(
    (item) => item.path === contextMenu.uri,
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
        {onFileOpenMenu && contextMenu.kind !== "directory" ? <>
          <button type="button" role="menuitem" aria-haspopup="menu" onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            menu.closeContextMenu();
            onFileOpenMenu(contextMenu.uri, { x: rect.right, y: rect.top });
          }}>
            <span className="codicon codicon-code agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">VS Code</span>
            <span className="codicon codicon-chevron-right" aria-hidden="true" />
          </button>
          <div className="files-tree-context-separator" role="separator" />
        </> : null}
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(t("fileTree.menu.createFileFailed"), () => menu.createEntry(target, "file"));
        }}>
          <span className="codicon codicon-new-file agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.createFile")}</span>
        </button>
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(t("fileTree.menu.createFolderFailed"), () => menu.createEntry(target, "directory"));
        }}>
          <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.createFolder")}</span>
        </button>
        <div className="files-tree-context-separator" role="separator" />
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.closeContextMenu();
          menu.requestUpload(target);
        }}>
          <span className="codicon codicon-cloud-upload agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.uploadLocal")}</span>
        </button>
        {!isRootTarget(contextMenu) ? (
          <button type="button" role="menuitem" onClick={() => {
            const target = contextMenu;
            menu.closeContextMenu();
            menu.runStatusAction(t("fileTree.menu.copyFileFailed"), () => menu.copyEntryToClipboard(target));
          }}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t("fileTree.menu.copy")}</span>
            <span className="files-tree-context-keybinding">Ctrl+C</span>
          </button>
        ) : null}
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(
            t("fileTree.menu.pasteFailed"),
            () => menu.pasteEntries(target, menu.copiedEntry?.uri),
          );
        }}>
          <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.paste")}</span>
          <span className="files-tree-context-keybinding">Ctrl+V</span>
        </button>
        <div className="files-tree-context-separator" role="separator" />
        {contextMenu.shortcutSource ? (
          <div className="files-tree-context-shortcut-action">
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              menu.runContextAction(t("fileTree.menu.removeShortcutFailed"), () => menu.removeShortcut(target.uri));
            }}>
              <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">{t("fileTree.menu.removeCurrentShortcut")}</span>
            </button>
            <button
              type="button"
              className="files-tree-context-apply"
              title={isDefaultShortcut
                ? t("fileTree.shortcut.removeCurrentAndDefault")
                : t("fileTree.shortcut.addCurrentAndDefault")}
              aria-label={isDefaultShortcut
                ? t("fileTree.shortcut.removeNamedCurrentAndDefault", contextMenu.label)
                : t("fileTree.shortcut.addNamedCurrentAndDefault", contextMenu.label)}
              onClick={() => {
                menu.closeContextMenu();
                if (!targetShortcut) {
                  menu.onStatusChange(t("fileTree.shortcut.invalid", contextMenu.uri));
                  return;
                }
                menu.runStatusAction(
                  t("fileTree.shortcut.updateCurrentAndDefaultFailed"),
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
                t("fileTree.menu.addShortcutFailed"),
                () => menu.addShortcut(target.treePath, target.label),
              );
            }}>
              <span className="codicon codicon-bookmark agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">{t("fileTree.menu.addToCurrentShortcuts")}</span>
            </button>
            <button
              type="button"
              className="files-tree-context-apply"
              title={t("fileTree.shortcut.addCurrentAndDefault")}
              aria-label={t("fileTree.shortcut.addNamedCurrentAndDefault", contextMenu.label)}
              onClick={() => {
                const target = contextMenu;
                menu.runContextAction(
                  t("fileTree.shortcut.addCurrentAndDefaultFailed"),
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
          menu.runStatusAction(t("fileTree.menu.copyPathFailed"), () => menu.copyPathToClipboard(target));
        }}>
          <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.copyPath")}</span>
        </button>
        {!isRootTarget(contextMenu) ? (
          <button type="button" role="menuitem" onClick={() => {
            const target = contextMenu;
            menu.runContextAction(t("fileTree.menu.downloadFailed"), () => menu.downloadEntry(target));
          }}>
            <span className="codicon codicon-cloud-download agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t("fileTree.menu.download")}</span>
          </button>
        ) : null}
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(t("fileTree.menu.revealFailed"), () => menu.revealEntry(target));
        }}>
          <span className="codicon codicon-folder-opened agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.reveal")}</span>
        </button>
        <button type="button" role="menuitem" onClick={() => {
          const target = contextMenu;
          menu.runContextAction(
            t("fileTree.menu.refreshDirectoryFailed"),
            () => menu.refreshTargetDirectory(target),
          );
        }}>
          <span className="codicon codicon-refresh agent-sessions-menu-item-icon" aria-hidden="true" />
          <span className="agent-sessions-menu-item-label">{t("fileTree.menu.refreshRoot")}</span>
        </button>
      </div>
    </AnchoredOverlay>
  );
}
