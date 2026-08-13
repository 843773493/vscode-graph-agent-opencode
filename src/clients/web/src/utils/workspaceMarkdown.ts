export type WorkspaceMarkdownTarget =
  | { kind: "external"; href: string }
  | { kind: "anchor"; href: string }
  | { kind: "workspace"; path: string; fragment: string };

const URI_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const FILESYSTEM_PATH_PREFIX = "filesystem:";

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
  if (URI_SCHEME.test(trimmed) || trimmed.startsWith("//")) {
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

  const filesystemPath = markdownPath.startsWith(FILESYSTEM_PATH_PREFIX);
  const baseMarkdownPath = filesystemPath
    ? markdownPath.slice(FILESYSTEM_PATH_PREFIX.length)
    : markdownPath;
  const baseSegments = decodedPath.startsWith("/")
    ? []
    : baseMarkdownPath.split("/").slice(0, -1).filter(Boolean);
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
    path: filesystemPath
      ? `${FILESYSTEM_PATH_PREFIX}/${baseSegments.join("/")}`
      : baseSegments.join("/"),
    fragment,
  };
}
