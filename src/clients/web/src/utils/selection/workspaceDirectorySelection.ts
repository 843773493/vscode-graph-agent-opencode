export function normalizeWorkspacePath(path: string): string {
  const trimmed = path.trim();
  if (!trimmed || trimmed === "/") return trimmed || "/";
  return trimmed.replace(/\/+$/, "");
}

export function workspacePathBasename(path: string): string {
  const segments = normalizeWorkspacePath(path).split("/").filter(Boolean);
  return segments[segments.length - 1] || "workspace";
}

export function workspaceParentPath(path: string): string {
  const segments = normalizeWorkspacePath(path).split("/").filter(Boolean);
  if (segments.length <= 1) return "/";
  return `/${segments.slice(0, -1).join("/")}`;
}

export function workspacePathSearchParts(path: string): {
  parentPath: string;
  query: string;
} {
  const trimmedPath = path.trim();
  if (!trimmedPath || trimmedPath === "/") {
    return { parentPath: "/", query: "" };
  }
  if (trimmedPath.endsWith("/")) {
    return { parentPath: normalizeWorkspacePath(trimmedPath), query: "" };
  }
  const normalized = normalizeWorkspacePath(trimmedPath);
  return {
    parentPath: workspaceParentPath(normalized),
    query: workspacePathBasename(normalized),
  };
}

export function workspaceDirectoryMatchesQuery(
  directory: string,
  query: string,
): boolean {
  const normalizedDirectory = directory.toLocaleLowerCase();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) return true;
  if (normalizedDirectory.includes(normalizedQuery)) return true;

  let queryIndex = 0;
  for (const character of normalizedDirectory) {
    if (character === normalizedQuery[queryIndex]) {
      queryIndex += 1;
      if (queryIndex === normalizedQuery.length) return true;
    }
  }
  return false;
}
