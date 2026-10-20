const WORKSPACE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validateWorkspaceId(value) {
  if (typeof value !== "string" || !WORKSPACE_ID_PATTERN.test(value)) {
    throw new Error("BrowserManager 必须显式提供标准 UUID workspace_id");
  }
  return value;
}
