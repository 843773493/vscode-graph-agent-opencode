import { t } from "../../../../../shared/core/i18n/index.js";
import { resourceFileRootUri } from "../../../utils/workspaceFileReferences";

// 这里只操作后端已通过 grammar 的 URI；不解析原生目标机器路径。
export function parentFileTreePath(uri: string): string {
  const root = resourceFileRootUri(uri);
  if (root === null) return "";
  if (root === uri) return uri;
  return uri.slice(0, uri.lastIndexOf("/"));
}

export function changedPathToTreePath(uri: string): string {
  if (!uri.startsWith("boxteam://")) throw new Error(t("fileTree.errors.changeMissingUri"));
  return uri;
}

export function isTreePathInside(candidate: string, ancestor: string): boolean {
  return candidate === ancestor || Boolean(ancestor && candidate.startsWith(ancestor + "/"));
}

export function shortWorkspaceLabel(_workspaceRoot: string | null, workspaceName: string | null): string {
  return workspaceName || t("fileTree.workspaceFallback");
}

export function referenceForTreePath(uri: string): string {
  if (!uri.startsWith("boxteam://")) throw new Error(t("fileTree.errors.resourceUriUnavailable"));
  return uri;
}

export function parseClipboardFilePaths(text: string): [string, ...string[]] {
  const parseFileUri = (raw: string): string => {
    let url: URL;
    try {
      url = new URL(raw);
    } catch (error) {
      throw new Error(t("fileTree.clipboard.fileUriUnparseable", raw), { cause: error });
    }
    let decodedPath: string;
    try {
      decodedPath = decodeURIComponent(url.pathname);
    } catch (error) {
      throw new Error(t("fileTree.clipboard.fileUriInvalidEscape", raw), { cause: error });
    }
    // 空字符会让下游后端路径解析与文件系统调用只看到截断后的前缀，必须在入口拒绝。
    if (decodedPath.includes("\0")) {
      throw new Error(t("fileTree.clipboard.pathContainsNull", raw));
    }
    if (url.hostname) {
      return `//${url.hostname}${decodedPath}`;
    }
    return /^\/[A-Za-z]:\//.test(decodedPath)
      ? decodedPath.slice(1)
      : decodedPath;
  };
  const paths = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => (
      line
      && line !== "copy"
      && line !== "cut"
      && !line.startsWith("#")
    ))
    .map((line) => {
      const unquoted = line.length >= 2 && line.startsWith('"') && line.endsWith('"')
        ? line.slice(1, -1)
        : line;
      if (/^file:\/\//i.test(unquoted)) {
        return parseFileUri(unquoted);
      }
      if (unquoted.startsWith("boxteam://")) return unquoted;
      if (unquoted.startsWith("/") || /^[A-Za-z]:[\\/]/.test(unquoted)) {
        return unquoted;
      }
      throw new Error(t("fileTree.clipboard.notAbsolutePath", unquoted));
    });
  if (paths.length === 0) {
    throw new Error(t("fileTree.clipboard.noPaths"));
  }
  return [...new Set(paths)] as [string, ...string[]];
}
