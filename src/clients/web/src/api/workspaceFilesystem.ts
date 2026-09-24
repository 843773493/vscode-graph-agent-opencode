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
  BULK_FILE_OPERATION_TIMEOUT_MS,
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  getApiBaseUrl,
  getGatewayToken,
  requestGatewayResponse,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "./http";

const FILESYSTEM_PATH_PREFIX = "filesystem:";

/**
 * 后端 /files/upload 与 /files/paste 的批量上限（File/Form/Field 的 max_length=100）。
 * 超出时 FastAPI 返回 422 且 detail 是英文 Pydantic 校验原文，用户无法据此判断
 * 是文件数量问题，因此必须在发请求前拦住并给出可执行的中文提示。
 */
const FILE_BATCH_LIMIT = 100;

function assertBatchWithinLimit(count: number, action: string): void {
  if (count > FILE_BATCH_LIMIT) {
    throw new Error(`${action}一次最多 ${FILE_BATCH_LIMIT} 项，当前 ${count} 项，请分批操作`);
  }
}

export function filesystemFileTreePath(absolutePath: string): string {
  if (!absolutePath.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(absolutePath)) {
    throw new Error(`文件系统快捷路径必须是绝对路径: ${absolutePath}`);
  }
  return `${FILESYSTEM_PATH_PREFIX}${absolutePath}`;
}

export interface WorkspaceFileLocation {
  path: string;
  scope: "workspace" | "filesystem";
}

export function decodeFileTreePath(path: string): WorkspaceFileLocation {
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

/**
 * decodeFileTreePath 与 path/scope 查询参数的唯一组合实现。
 * 多个文件接口此前各写一遍同样的两行，收敛后调用点只保留一行。
 */
function fileTreeLocationQuery(path: string): {
  location: WorkspaceFileLocation;
  query: URLSearchParams;
} {
  const location = decodeFileTreePath(path);
  return {
    location,
    query: new URLSearchParams({ path: location.path, scope: location.scope }),
  };
}

function encodeWorkspaceFileList(
  result: WorkspaceFileList,
  scope: "workspace" | "filesystem",
): WorkspaceFileList {
  // 后端 WorkspaceFileListDTO.items 是 default_factory=list，序列化必为数组；
  // 非数组即契约被破坏，不得静默收敛成空列表把损坏伪装成合法结果。
  if (!Array.isArray(result.items)) {
    throw new Error("工作区文件列表响应 items 必须是数组");
  }
  return {
    ...result,
    path: encodeFileTreeResultPath(result.path, scope),
    items: result.items.map((node): WorkspaceFileNode => ({
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
    {
      headers: workspaceHeader(workspaceId),
      signal,
      timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS,
    },
  ));
  return encodeWorkspaceFileList(result, location.scope);
}

export async function createWorkspaceFileEntry(
  port: number,
  directoryPath: string,
  payload: WorkspaceFileCreateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  const { location, query } = fileTreeLocationQuery(directoryPath);
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
  assertBatchWithinLimit(payload.source_paths.length, "粘贴");
  const { location, query } = fileTreeLocationQuery(directoryPath);
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files/paste?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      timeoutMs: BULK_FILE_OPERATION_TIMEOUT_MS,
      body: JSON.stringify(payload),
    },
  ));
  return encodeWorkspaceFileList(result, location.scope);
}

export async function copyWorkspaceFileEntry(
  port: number,
  directoryPath: string,
  source: WorkspaceFileLocation,
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  const { location: destination, query } = fileTreeLocationQuery(directoryPath);
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files/copy?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      timeoutMs: BULK_FILE_OPERATION_TIMEOUT_MS,
      body: JSON.stringify({
        source_path: source.path,
        source_scope: source.scope,
      }),
    },
  ));
  return encodeWorkspaceFileList(result, destination.scope);
}

export async function uploadWorkspaceFileEntries(
  port: number,
  directoryPath: string,
  files: readonly File[],
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  if (files.length === 0) {
    throw new Error("没有需要上传的本地文件");
  }
  assertBatchWithinLimit(files.length, "上传");
  const { location: destination, query } = fileTreeLocationQuery(directoryPath);
  const body = new FormData();
  for (const file of files) {
    body.append("files", file, file.name);
    body.append("relative_paths", file.webkitRelativePath || file.name);
  }
  // multipart 上传自行携带 FormData，只共享统一凭据与刷新重试。
  const response = await requestGatewayResponse(
    port,
    `/api/v1/workspace/files/upload?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body,
      skipGatewayUserSession: true,
    },
  );
  const result = unwrapApiData(await response.json() as APIResponse<WorkspaceFileList>);
  return encodeWorkspaceFileList(result, destination.scope);
}

export interface WorkspaceFileDownloadRequest {
  url: string;
  headers: Record<string, string>;
  suggestedName: string;
}

export async function createWorkspaceFileDownloadRequest(
  port: number,
  path: string,
  suggestedName: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileDownloadRequest> {
  // TODO: 下载由 FileTransferHost 以 anchor 触发浏览器原生导航，无法在本层包装成
  // 带刷新重试的 fetch，只能沿用 getGatewayToken 取一次凭据并烘焙进 headers。降级
  // 行为：若 Gateway 在本调用与 host 发起 fetch 之间轮换本地凭据，下载会以 host 的
  // 普通 Error 失败且不会重试（非 HttpRequestError，调用方无法按状态码降级）；待把
  // 下载改为经 requestGatewayResponse 由页面发起后即可与其余入口共享同一屏障。
  const localToken = await getGatewayToken(port);
  const { location, query } = fileTreeLocationQuery(path);
  return {
    url: `${getApiBaseUrl(port)}/api/v1/workspace/files/download?${query.toString()}`,
    headers: { "X-Local-Token": localToken, ...workspaceHeader(workspaceId) },
    suggestedName,
  };
}

export async function revealWorkspaceFileEntry(
  port: number,
  path: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileReveal> {
  const { location, query } = fileTreeLocationQuery(path);
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
  const { location, query } = fileTreeLocationQuery(path);
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
  const { location, query } = fileTreeLocationQuery(path);
  // 二进制下载只共享统一凭据与刷新重试，不建立 Gateway 用户会话屏障。
  const response = await requestGatewayResponse(
    port,
    `/api/v1/workspace/files/raw?${query.toString()}`,
    {
      headers: workspaceHeader(workspaceId),
      signal,
      skipGatewayUserSession: true,
    },
  );
  return await response.blob();
}

export async function updateWorkspaceFileContent(
  port: number,
  path: string,
  payload: WorkspaceFileUpdateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const { location, query } = fileTreeLocationQuery(path);
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
