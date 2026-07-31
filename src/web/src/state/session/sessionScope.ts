/** 构造跨工作区唯一的会话前端作用域键。 */
export function sessionScopeKey(
  workspaceId: string | null | undefined,
  sessionId: string,
): string {
  const resolvedWorkspaceId = workspaceId?.trim() || "workspace";
  return `${encodeURIComponent(resolvedWorkspaceId)}::${sessionId}`;
}

export function parseSessionScopeKey(value: string): {
  workspaceId: string;
  sessionId: string;
} {
  const separatorIndex = value.indexOf("::");
  if (separatorIndex <= 0 || separatorIndex === value.length - 2) {
    throw new Error(`会话作用域键格式无效: ${value}`);
  }
  return {
    workspaceId: decodeURIComponent(value.slice(0, separatorIndex)),
    sessionId: value.slice(separatorIndex + 2),
  };
}
