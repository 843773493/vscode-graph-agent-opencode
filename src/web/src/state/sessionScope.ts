export function sessionScopeKey(
  workspaceId: string | null | undefined,
  sessionId: string,
): string {
  const resolvedWorkspaceId = workspaceId?.trim() || "workspace";
  return `${encodeURIComponent(resolvedWorkspaceId)}::${sessionId}`;
}
