import path from "node:path";
import { readFile } from "node:fs/promises";

const WORKSPACE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validateWorkspaceId(workspaceId) {
  if (typeof workspaceId !== "string" || !WORKSPACE_ID_PATTERN.test(workspaceId)) {
    throw new Error("TerminalManager 必须显式提供标准 UUID workspace_id");
  }
  return workspaceId;
}

export async function verifyWorkspaceIdentity(workspaceRoot, workspaceId) {
  const identityPath = path.join(workspaceRoot, ".boxteam", "workspace-identity.json");
  const identity = JSON.parse(await readFile(identityPath, "utf8"));
  const actual = validateWorkspaceId(identity.workspace_id);
  if (actual !== workspaceId) {
    throw new Error(`Terminal 工作区 UUID 与权威身份文件不一致: expected=${workspaceId}, actual=${actual}, identity=${identityPath}`);
  }
}
