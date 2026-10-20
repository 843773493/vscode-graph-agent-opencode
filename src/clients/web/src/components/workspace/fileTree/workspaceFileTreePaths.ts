// 这里只操作后端已通过 grammar 的 URI；不解析原生目标机器路径。
export function parentFileTreePath(uri: string): string {
  const files = uri.indexOf("/resources/files/");
  if (files < 0) return "";
  const rootEnd = uri.indexOf("/", files + "/resources/files/".length);
  if (rootEnd < 0) return uri;
  return uri.slice(0, uri.lastIndexOf("/"));
}

export function changedPathToTreePath(uri: string): string {
  if (!uri.startsWith("boxteam://")) throw new Error("文件变更事件缺少资源 URI");
  return uri;
}

export function isTreePathInside(candidate: string, ancestor: string): boolean {
  return candidate === ancestor || Boolean(ancestor && candidate.startsWith(ancestor + "/"));
}

export function shortWorkspaceLabel(_workspaceRoot: string | null, workspaceName: string | null): string {
  return workspaceName || "workspace";
}

export function referenceForTreePath(uri: string): string {
  if (!uri.startsWith("boxteam://")) throw new Error("文件树尚未取得资源 URI");
  return uri;
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
      if (unquoted.startsWith("boxteam://")) return unquoted;
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
