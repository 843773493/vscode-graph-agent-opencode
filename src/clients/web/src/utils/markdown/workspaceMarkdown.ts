export type WorkspaceMarkdownTarget =
  | { kind: "external"; href: string }
  | { kind: "anchor"; href: string }
  | { kind: "workspace"; path: string; fragment: string };

const URI_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

export function resolveWorkspaceMarkdownTarget(
  markdownPath: string,
  target: string,
): WorkspaceMarkdownTarget {
  const trimmed = target.trim();
  if (!trimmed) {
    throw new Error("Markdown 资源地址不能为空");
  }
  if (trimmed.startsWith("#")) {
    return { kind: "anchor", href: trimmed };
  }
  if (trimmed.startsWith("boxteam://")) {
    const index = trimmed.indexOf("#");
    return { kind: "workspace", path: index < 0 ? trimmed : trimmed.slice(0, index), fragment: index < 0 ? "" : trimmed.slice(index) };
  }
  if ((URI_SCHEME.test(trimmed) && !/^[A-Za-z]:[\\/]/.test(trimmed)) || trimmed.startsWith("//")) {
    return { kind: "external", href: trimmed };
  }

  const hashIndex = trimmed.indexOf("#");
  const fragment = hashIndex >= 0 ? trimmed.slice(hashIndex) : "";
  const withoutFragment = hashIndex >= 0 ? trimmed.slice(0, hashIndex) : trimmed;
  const queryIndex = withoutFragment.indexOf("?");
  const encodedPath = queryIndex >= 0
    ? withoutFragment.slice(0, queryIndex)
    : withoutFragment;
  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(encodedPath);
  } catch (error) {
    throw new Error(`Markdown 资源地址包含非法转义: ${target}`, { cause: error });
  }

  if (decodedPath.startsWith("/") || /^[A-Za-z]:[\\/]/.test(decodedPath)) {
    return { kind: "workspace", path: decodedPath, fragment };
  }
  const marker = "/resources/files/";
  const files = markdownPath.indexOf(marker);
  if (files < 0) throw new Error("Markdown 文件缺少资源 URI");
  const rootEnd = markdownPath.indexOf("/", files + marker.length);
  if (rootEnd < 0) throw new Error("Markdown 地址没有指向文件");
  const root = markdownPath.slice(0, rootEnd);
  const baseSegments = markdownPath.slice(rootEnd + 1).split("/").slice(0, -1).map(decodeURIComponent);
  for (const segment of decodedPath.replace(/\\/g, "/").split("/")) {
    if (!segment || segment === ".") {
      continue;
    }
    if (segment === "..") {
      if (baseSegments.length === 0) {
        throw new Error(`Markdown 资源地址越过工作区根目录: ${target}`);
      }
      baseSegments.pop();
      continue;
    }
    baseSegments.push(segment);
  }
  if (baseSegments.length === 0) {
    throw new Error(`Markdown 资源地址没有指向文件: ${target}`);
  }
  return {
    kind: "workspace",
    path: root + "/" + baseSegments.map(encodeURIComponent).join("/"),
    fragment,
  };
}
