/** 构造跨工作区唯一的会话前端作用域键。 */
export function sessionScopeKey(
  workspaceId: string | null | undefined,
  sessionId: string,
): string {
  const resolvedWorkspaceId = workspaceId?.trim() || "workspace";
  return `${encodeURIComponent(resolvedWorkspaceId)}::${sessionId}`;
}
