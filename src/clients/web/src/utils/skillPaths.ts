export function skillNameFromPath(value: unknown): string {
  if (typeof value !== "string") {
    return "";
  }
  const match = value
    .replace(/\\/g, "/")
    .match(/(?:^|\/)\.boxteam\/skills\/([^/]+)\/SKILL\.md$/);
  return match?.[1] ?? "";
}
