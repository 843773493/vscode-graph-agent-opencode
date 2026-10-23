import type { WorkspaceFileReferencesDTO, WorkspaceFileRootsDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type {
  APIResponse,
  FileTreeShortcutRequest,
  SessionFileTreeSettings,
  WorkspaceFileContent,
  WorkspaceFileCopyRequest,
  WorkspaceFileCreateRequest,
  WorkspaceFileList,
  WorkspaceFileMutationIntent,
  WorkspaceFileOperationReceipt,
  WorkspaceFileOperationStatus,
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

export interface WorkspaceFileLocation {
  path: string;
  scope: "workspace" | "filesystem";
}

// 原生路径只作为输入原样交给固定目标；URI 的完整解析归后端唯一 grammar。
export function decodeFileTreePath(path: string): WorkspaceFileLocation {
  return { path, scope: path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) ? "filesystem" : "workspace" };
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

function validateWorkspaceFileList(
  workspaceFileList: WorkspaceFileList,
): WorkspaceFileList {
  // 后端 WorkspaceFileListDTO.items 是 default_factory=list，序列化必为数组；
  // 非数组即契约被破坏，不得静默收敛成空列表把损坏伪装成合法结果。
  if (!Array.isArray(workspaceFileList.items)) {
    throw new Error("工作区文件列表响应 items 必须是数组");
  }
  const directoryKey = workspaceFileList.directory_key;
  if (!directoryKey || typeof directoryKey.workspace_id !== "string" || !directoryKey.workspace_id
    || typeof directoryKey.root !== "string" || !directoryKey.root
    || typeof directoryKey.path !== "string" || directoryKey.path !== workspaceFileList.path
    || !Number.isSafeInteger(workspaceFileList.directory_revision)
    || workspaceFileList.directory_revision < 1) {
    throw new Error("工作区文件列表响应缺少配对的目录键和 revision");
  }
  if (!workspaceFileList.path.startsWith("boxteam://")
    || !directoryKey.root.startsWith("boxteam://")
    || workspaceFileList.items.some((node) => !node.path.startsWith("boxteam://"))) {
    throw new Error("文件列表响应必须使用资源 URI");
  }
  return workspaceFileList;
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
  const workspaceFileList = unwrapApiData(await requestJson<APIResponse<WorkspaceFileList>>(
    port,
    `/api/v1/workspace/files${suffix ? `?${suffix}` : ""}`,
    {
      headers: workspaceHeader(workspaceId),
      signal,
      timeoutMs: DEFAULT_API_REQUEST_TIMEOUT_MS,
    },
  ));
  return validateWorkspaceFileList(workspaceFileList);
}

export async function createWorkspaceFileEntry(
  port: number,
  directoryPath: string,
  payload: WorkspaceFileCreateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileOperationReceipt> {
  const { query } = fileTreeLocationQuery(directoryPath);
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileOperationReceipt>>(
    port,
    `/api/v1/workspace/files/entries?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(payload),
    },
  ));
}

export async function pasteWorkspaceFileEntries(
  port: number,
  directoryPath: string,
  payload: WorkspaceFilePasteRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileOperationReceipt> {
  assertBatchWithinLimit(payload.source_paths.length, "粘贴");
  const { query } = fileTreeLocationQuery(directoryPath);
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileOperationReceipt>>(
    port,
    `/api/v1/workspace/files/paste?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      timeoutMs: BULK_FILE_OPERATION_TIMEOUT_MS,
      body: JSON.stringify(payload),
    },
  ));
}

export async function copyWorkspaceFileEntry(
  port: number,
  directoryPath: string,
  payload: WorkspaceFileCopyRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileOperationReceipt> {
  const { query } = fileTreeLocationQuery(directoryPath);
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileOperationReceipt>>(
    port,
    `/api/v1/workspace/files/copy?${query.toString()}`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      timeoutMs: BULK_FILE_OPERATION_TIMEOUT_MS,
      body: JSON.stringify(payload),
    },
  ));
}

export interface WorkspaceUploadEntry {
  file: Blob;
  name: string;
  relativePath: string;
}

export async function uploadWorkspaceFileEntries(
  port: number,
  directoryPath: string,
  files: readonly WorkspaceUploadEntry[],
  intent: WorkspaceFileMutationIntent,
  workspaceId?: string | null,
): Promise<WorkspaceFileOperationReceipt> {
  if (files.length === 0) {
    throw new Error("没有需要上传的本地文件");
  }
  assertBatchWithinLimit(files.length, "上传");
  const { query } = fileTreeLocationQuery(directoryPath);
  const body = new FormData();
  for (const file of files) {
    body.append("files", file.file, file.name);
    body.append("relative_paths", file.relativePath);
  }
  body.append("intent", JSON.stringify(intent));
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
  return unwrapApiData(await response.json() as APIResponse<WorkspaceFileOperationReceipt>);
}

export async function getWorkspaceFileOperations(port: number, operationIds: readonly string[], workspaceId: string,
  backendWorkspaceId: string): Promise<WorkspaceFileOperationStatus> {
  const query = new URLSearchParams({ expected_workspace_id: backendWorkspaceId });
  for (const operationId of operationIds) query.append("operation_id", operationId);
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileOperationStatus>>(port,
    `/api/v1/workspace/files/operations?${query}`, { headers: workspaceHeader(workspaceId) }));
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
  const { query } = fileTreeLocationQuery(path);
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
  const { query } = fileTreeLocationQuery(path);
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileReveal>>(
    port,
    `/api/v1/workspace/files/reveal?${query.toString()}`,
    { method: "POST", headers: workspaceHeader(workspaceId) },
  ));
}

/**
 * 文件内容响应契约验证的唯一实现。
 *
 * 后端 WorkspaceFileContentDTO 的 content / path 均为必填字符串；非字符串即契约
 * 被破坏。此前只做信封校验（unwrapApiData），畸形 content（数字 / null / 缺失）
 * 会一路透传到预览组件，在 `content.replace` 处抛英文引擎错误并让整页被错误边界
 * 接管（真实浏览器审查实测白屏）；path 为数字时同理会落到 decodeFileTreePath 的
 * `path.startsWith` 抛 TypeError。这里与同文件 validateWorkspaceFileList 的数组校验
 * 保持同一标准：不符就带上下文响亮失败，由上层既有错误展示链路接管。
 */
function validateWorkspaceFileContent(
  workspaceFileContent: WorkspaceFileContent,
): WorkspaceFileContent {
  if (!workspaceFileContent || typeof workspaceFileContent !== "object"
    || Array.isArray(workspaceFileContent)) {
    throw new Error("工作区文件内容响应必须是对象");
  }
  if (typeof workspaceFileContent.content !== "string") {
    throw new Error("工作区文件内容响应 content 必须是字符串");
  }
  if (typeof workspaceFileContent.path !== "string") {
    throw new Error("工作区文件内容响应 path 必须是字符串");
  }
  if (!workspaceFileContent.path.startsWith("boxteam://")
    || !workspaceFileContent.root_path.startsWith("boxteam://")) {
    throw new Error("文件内容响应必须使用资源 URI");
  }
  return workspaceFileContent;
}

export async function getWorkspaceFileContent(
  port: number,
  path: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const { query } = fileTreeLocationQuery(path);
  const workspaceFileContent = unwrapApiData(await requestJson<APIResponse<WorkspaceFileContent>>(
    port,
    `/api/v1/workspace/files/content?${query.toString()}`,
    workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
  ));
  return validateWorkspaceFileContent(workspaceFileContent);
}

export async function getWorkspaceRawFileBlob(
  port: number,
  path: string,
  workspaceId?: string | null,
  signal?: AbortSignal,
): Promise<Blob> {
  const { query } = fileTreeLocationQuery(path);
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
  const { query } = fileTreeLocationQuery(path);
  const workspaceFileContent = unwrapApiData(await requestJson<APIResponse<WorkspaceFileContent>>(
    port,
    `/api/v1/workspace/files/content?${query.toString()}`,
    {
      method: "PUT",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify(payload),
    },
  ));
  return validateWorkspaceFileContent(workspaceFileContent);
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

export async function getWorkspaceFileRoots(port: number, workspaceId: string | null): Promise<WorkspaceFileRootsDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceFileRootsDTO>>(
    port, "/api/v1/workspace/files/roots", { headers: workspaceHeader(workspaceId) },
  ));
}

export async function resolveWorkspaceFileReferences(port: number, paths: string[], workspaceId: string): Promise<string[]> {
  assertBatchWithinLimit(paths.length, "解析文件引用");
  const result = unwrapApiData(await requestJson<APIResponse<WorkspaceFileReferencesDTO>>(
    port, "/api/v1/workspace/files/references", {
      method: "POST", headers: workspaceHeader(workspaceId), body: JSON.stringify({ paths }),
    },
  ));
  if (result.uris.length !== paths.length || result.uris.some((uri) => !uri.startsWith("boxteam://"))) {
    throw new Error("目标文件 owner 未返回配对的资源 URI");
  }
  return result.uris;
}
