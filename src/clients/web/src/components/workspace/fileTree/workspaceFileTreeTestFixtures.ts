import type { WorkspaceFileNode } from "../../../types/backend";

export function fileTreeNodeFixture(
  treePath: string,
  kind: "file" | "directory" = "file",
  size: number | null = kind === "file" ? 1 : null,
): WorkspaceFileNode {
  const name = treePath.slice(treePath.lastIndexOf("/") + 1) || treePath;
  return {
    name,
    path: treePath,
    kind,
    has_children: kind === "directory",
    size,
    modified_at: null,
  };
}
