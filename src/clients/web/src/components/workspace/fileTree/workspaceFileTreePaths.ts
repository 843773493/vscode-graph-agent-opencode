import {
  decodeFileTreePath,
  filesystemFileTreePath,
} from "../../../api";

export const ROOT_PATH = "";
export const FILESYSTEM_ROOT_PATH = filesystemFileTreePath("/");

export interface FileTreePathLocation {
  scope: "workspace" | "filesystem";
  path: string;
}

// 文件树路径的权威语义由 api.workspaceFilesystem 的 decodeFileTreePath 决定；
// 这里只做“去掉尾部斜杠并把反斜杠折成斜杠”的展示级归一，供父子比较使用。
export function normalizedTreePath(treePath: string): FileTreePathLocation {
  const location = decodeFileTreePath(treePath);
  return {
    scope: location.scope,
    path: location.path.replace(/\\/g, "/").replace(/\/$/, ""),
  };
}

export function parentFileTreePath(treePath: string): string {
  const location = normalizedTreePath(treePath);
  const separatorIndex = location.path.lastIndexOf("/");
  if (location.scope === "workspace") {
    return separatorIndex < 0 ? ROOT_PATH : location.path.slice(0, separatorIndex);
  }
  let parent = separatorIndex <= 0 ? "/" : location.path.slice(0, separatorIndex);
  if (/^[A-Za-z]:$/.test(parent)) {
    parent += "/";
  }
  return filesystemFileTreePath(parent);
}

function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:\//.test(path);
}

function caseInsensitiveIfWindowsDrive(path: string): string {
  return /^[A-Za-z]:\//.test(path) ? path.toLowerCase() : path;
}

export function changedPathToTreePath(
  path: string,
  workspaceRoot: string | null,
): string | null {
  const normalized = path.trim().replace(/\\/g, "/");
  if (!normalized) {
    return null;
  }
  if (!isAbsolutePath(normalized)) {
    const relative = normalized.replace(/^\.\//, "");
    if (relative.split("/").includes("..")) {
      return null;
    }
    return relative;
  }
  const rawRoot = workspaceRoot?.replace(/\\/g, "/") ?? "";
  const normalizedRoot = rawRoot === "/" ? "/" : rawRoot.replace(/\/$/, "");
  const comparePath = caseInsensitiveIfWindowsDrive(normalized);
  const compareRoot = caseInsensitiveIfWindowsDrive(normalizedRoot);
  if (compareRoot && comparePath === compareRoot) {
    return ROOT_PATH;
  }
  if (compareRoot === "/" && comparePath.startsWith("/")) {
    return normalized.slice(1);
  }
  if (compareRoot && comparePath.startsWith(`${compareRoot}/`)) {
    return normalized.slice(normalizedRoot.length + 1);
  }
  return filesystemFileTreePath(normalized);
}

export function isTreePathInside(candidate: string, ancestor: string): boolean {
  const candidateLocation = normalizedTreePath(candidate);
  const ancestorLocation = normalizedTreePath(ancestor);
  if (candidateLocation.scope !== ancestorLocation.scope) {
    return false;
  }
  if (!ancestorLocation.path) {
    return true;
  }
  return candidateLocation.path === ancestorLocation.path
    || candidateLocation.path.startsWith(`${ancestorLocation.path}/`);
}

export function shortWorkspaceLabel(
  workspaceRoot: string | null,
  workspaceName: string | null,
): string {
  return workspaceName || workspaceRoot?.split(/[\\/]/).filter(Boolean).pop() || "workspace";
}

export function absolutePathForTreePath(
  treePath: string,
  workspaceRoot: string | null,
): string {
  const location = decodeFileTreePath(treePath);
  if (location.scope === "filesystem") {
    return location.path;
  }
  if (!workspaceRoot) {
    throw new Error("当前工作区缺少根目录，无法添加快捷路径");
  }
  if (!location.path) {
    return workspaceRoot;
  }
  const separator = workspaceRoot.includes("\\") ? "\\" : "/";
  return `${workspaceRoot.replace(/[\\/]$/, "")}${separator}${location.path.split("/").join(separator)}`;
}

export function parseClipboardFilePaths(text: string): [string, ...string[]] {
  const parseFileUri = (raw: string): string => {
    let url: URL;
    try {
      url = new URL(raw);
    } catch (error) {
      throw new Error(`剪贴板中的 file 地址无法解析: ${raw}`, { cause: error });
    }
    let decodedPath: string;
    try {
      decodedPath = decodeURIComponent(url.pathname);
    } catch (error) {
      throw new Error(`剪贴板中的 file 地址包含非法百分号转义: ${raw}`, { cause: error });
    }
    // 空字符会让下游后端路径解析与文件系统调用只看到截断后的前缀，必须在入口拒绝。
    if (decodedPath.includes("\0")) {
      throw new Error(`剪贴板中的文件路径包含空字符: ${raw}`);
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
      if (unquoted.startsWith("file://")) {
        return parseFileUri(unquoted);
      }
      if (unquoted.startsWith("/") || /^[A-Za-z]:[\\/]/.test(unquoted)) {
        return unquoted;
      }
      throw new Error(`剪贴板内容不是绝对文件路径: ${unquoted}`);
    });
  if (paths.length === 0) {
    throw new Error("剪贴板中没有可粘贴的文件路径");
  }
  return [...new Set(paths)] as [string, ...string[]];
}
