export function fileTreeNodeFixture(
  path: string,
  kind: "file" | "directory" = "file",
  size: number | null = kind === "file" ? 1 : null,
): Record<string, unknown> {
  return {
    name: path.split("/").pop() ?? path,
    path,
    kind,
    has_children: kind === "directory",
    size,
    modified_at: null,
  };
}
