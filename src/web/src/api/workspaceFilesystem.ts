import type {
  APIResponse,
  FileTreeShortcutRequest,
  SessionFileTreeSettings,
  WorkspaceFileContent,
  WorkspaceFileCreateRequest,
  WorkspaceFileList,
  WorkspaceFileNode,
  WorkspaceFilePasteRequest,
  WorkspaceFileReveal,
  WorkspaceFileUpdateRequest,
} from "../types/backend";
import {
  getApiBaseUrl,
  getGatewayToken,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

const FILESYSTEM_PATH_PREFIX = "filesystem:";

export function filesystemFileTreePath(absolutePath: string): string {
  if (!absolutePath.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(absolutePath)) {
    throw new Error(`文件系统快捷路径必须是绝对路径: ${absolutePath}`);
  }
  return `${FILESYSTEM_PATH_PREFIX}${absolutePath}`;
}

export function decodeFileTreePath(path: string): {
  path: string;
  scope: "workspace" | "filesystem";
} {
  if (path.startsWith(FILESYSTEM_PATH_PREFIX)) {
    return { path: path.slice(FILESYSTEM_PATH_PREFIX.length), scope: "filesystem" };
  }
  return { path, scope: "workspace" };
}

function encodeFileTreeResultPath(
  path: string,
  scope: "workspace" | "filesystem",
): string {
  return scope === "filesystem" ? filesystemFileTreePath(path) : path;
}

function encodeWorkspaceFileList(
  result: WorkspaceFileList,
  scope: "workspace" | "filesystem",
): WorkspaceFileList {
  return {
    ...result,
    path: encodeFileTreeResultPath(result.path, scope),
    items: (result.items ?? []).map((node): WorkspaceFileNode => ({
      ...node,
      path: encodeFileTreeResultPath(node.path, scope),
    })),
  };
}

export async function getWorkspaceFiles(
  port: number,
  path = "",
  workspaceId?: string | null,
  signal?: AbortSignal,
  cursor?: string | null,
): Promise<WorkspaceFileList> {
  const location = decodeFileTreePath(path);
  const query = new URLSearchParams();
  if (location.path) query.set("path", location.path);
  query.set("scope", location.scope);
  if (cursor) query.set("cursor", cursor);
  const suffix = query.toString();
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files${suffix ? `?${suffix}` : ""}`,
    { headers: workspaceHeader(workspaceId), signal },
  ));
  return encodeWorkspaceFileList(result, location.scope);
}

export async function createWorkspaceFileEntry(
  port: number,
  directoryPath: string,
  payload: WorkspaceFileCreateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  const location = decodeFileTreePath(directoryPath);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files/entries?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(payload),
    },
  ));
  return encodeWorkspaceFileList(result, location.scope);
}

export async function pasteWorkspaceFileEntries(
  port: number,
  directoryPath: string,
  payload: WorkspaceFilePasteRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  const location = decodeFileTreePath(directoryPath);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files/paste?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(payload),
    },
  ));
  return encodeWorkspaceFileList(result, location.scope);
}

export async function revealWorkspaceFileEntry(
  port: number,
  path: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileReveal> {
  const location = decodeFileTreePath(path);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileReveal>>(
    port,
    `/api/v1/workspace/files/reveal?${query.toString()}`,
    { method: "POST", headers: workspaceHeader(workspaceId) },
  ));
}

export async function getWorkspaceFileContent(
  port: number,
  path: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const location = decodeFileTreePath(path);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileContent>>(
    port,
    `/api/v1/workspace/files/content?${query.toString()}`,
    workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
  ));
  return { ...result, path: encodeFileTreeResultPath(result.path, location.scope) };
}

export async function getWorkspaceRawFileBlob(
  port: number,
  path: string,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<Blob> {
  const localToken = await getGatewayToken(port);
  const location = decodeFileTreePath(path);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  const response = await fetch(
    `${getApiBaseUrl(port)}/api/v1/workspace/files/raw?${query.toString()}`,
    {
      headers: { "X-Local-Token": localToken, ...workspaceHeader(workspaceId) },
      signal,
    },
  );
  if (!response.ok) {
    const payload = await response.clone().json().catch(() => null) as {
      detail?: string;
    } | null;
    throw new Error(`读取工作区原始文件失败: ${payload?.detail ?? `HTTP ${response.status}`}`);
  }
  return response.blob();
}

export async function updateWorkspaceFileContent(
  port: number,
  path: string,
  payload: WorkspaceFileUpdateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const location = decodeFileTreePath(path);
  const query = new URLSearchParams({ path: location.path, scope: location.scope });
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileContent>>(
    port,
    `/api/v1/workspace/files/content?${query.toString()}`,
    {
      method: "PUT",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(payload),
    },
  ));
  return { ...result, path: encodeFileTreeResultPath(result.path, location.scope) };
}

export async function getSessionFileTreeSettings(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionFileTreeSettings> {
  return unwrapApiData(await requestJson<APIResponse<SessionFileTreeSettings>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/file-tree-settings`,
    { headers: workspaceHeader(workspaceId) },
  ));
}

export async function addSessionFileTreeShortcut(
  port: number,
  sessionId: string,
  payload: FileTreeShortcutRequest,
  workspaceId?: string | null,
): Promise<SessionFileTreeSettings> {
  return unwrapApiData(await requestJson<APIResponse<SessionFileTreeSettings>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/file-tree-shortcuts`,
    { method: "POST", headers: workspaceHeader(workspaceId), body: JSON.stringify(payload) },
  ));
}

export async function removeSessionFileTreeShortcut(
  port: number,
  sessionId: string,
  path: string,
  source: "session" | "workspace",
  workspaceId?: string | null,
): Promise<SessionFileTreeSettings> {
  const query = new URLSearchParams({ path });
  const route = source === "workspace" ? "workspace-file-tree-shortcuts" : "file-tree-shortcuts";
  return unwrapApiData(await requestJson<APIResponse<SessionFileTreeSettings>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/${route}?${query.toString()}`,
    { method: "DELETE", headers: workspaceHeader(workspaceId) },
  ));
}

export async function applyFileTreeShortcutToWorkspace(
  port: number,
  sessionId: string,
  path: string,
  label?: string,
  workspaceId?: string | null,
): Promise<SessionFileTreeSettings> {
  return unwrapApiData(await requestJson<APIResponse<SessionFileTreeSettings>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/file-tree-shortcuts/apply-to-workspace`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ path, label }),
    },
  ));
}
