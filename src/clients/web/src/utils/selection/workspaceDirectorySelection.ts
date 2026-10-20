export function workspacePathSearchParts(path: string): {
  parentPath: string;
  query: string;
} {
  const value = path.trim();
  // URI 整体交给目标 owner；原生文本只拆分搜索词，不在浏览器解析真实位置。
  if (value.includes("://") || value === "." || value === ".." || value === "~") {
    return { parentPath: value, query: "" };
  }
  const windows = /^[A-Za-z]:[\\/]|^\\\\/.test(value);
  const index = Math.max(value.lastIndexOf("/"), windows ? value.lastIndexOf("\\") : -1);
  if (index === value.length - 1) return { parentPath: value, query: "" };
  return {
    parentPath: index < 0 ? "" : value.slice(0, index + 1),
    query: value.slice(index + 1),
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
