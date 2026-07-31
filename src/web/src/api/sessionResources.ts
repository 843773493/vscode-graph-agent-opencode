import type {
  APIResponse,
  SessionChangesSummary,
  SessionChangeset,
  SessionChangesetList,
  SessionFileChange,
  SessionFileReviewResult,
  SessionResourceAction,
  SessionResourceControlResult,
  SessionResourceKind,
  SessionResourceList,
} from "../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";

function normalizeSessionChangesSummary(
  summary: Partial<SessionChangesSummary> | null | undefined,
): SessionChangesSummary {
  return {
    files: summary?.files ?? 0,
    additions: summary?.additions ?? 0,
    deletions: summary?.deletions ?? 0,
  };
}

function normalizeSessionFileChange(file: SessionFileChange): SessionFileChange {
  return {
    ...file,
    additions: file.additions ?? 0,
    deletions: file.deletions ?? 0,
    reviewed: file.reviewed ?? false,
    tool_call_ids: file.tool_call_ids ?? [],
    turn_ids: file.turn_ids ?? [],
  };
}

function normalizeSessionChangesetList(
  value: SessionChangesetList,
): SessionChangesetList {
  return {
    ...value,
    items: value.items.map((item) => ({
      ...item,
      is_default: item.is_default ?? false,
      summary: normalizeSessionChangesSummary(item.summary),
    })),
  };
}

function normalizeSessionChangeset(value: SessionChangeset): SessionChangeset {
  return {
    ...value,
    status: value.status ?? "ready",
    summary: normalizeSessionChangesSummary(value.summary),
    files: (value.files ?? []).map(normalizeSessionFileChange),
  };
}

export async function getSessionResources(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionResourceList> {
  const data = await requestJson<APIResponse<SessionResourceList>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources`,
    { headers: workspaceHeader(workspaceId) },
  );
  return unwrapApiData(data);
}

export async function getSessionChangesets(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionChangesetList> {
  const data = await requestJson<APIResponse<SessionChangesetList>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets`,
    { headers: workspaceHeader(workspaceId) },
  );
  return normalizeSessionChangesetList(unwrapApiData(data));
}

export async function getSessionChangeset(
  port: number,
  sessionId: string,
  changesetId: string,
  workspaceId?: string | null,
): Promise<SessionChangeset> {
  const data = await requestJson<APIResponse<SessionChangeset>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets/${encodeURIComponent(changesetId)}`,
    { headers: workspaceHeader(workspaceId) },
  );
  return normalizeSessionChangeset(unwrapApiData(data));
}

export async function reviewSessionChangeFile(
  port: number,
  sessionId: string,
  changesetId: string,
  filePath: string,
  reviewed: boolean,
  workspaceId?: string | null,
): Promise<SessionFileReviewResult> {
  const data = await requestJson<APIResponse<SessionFileReviewResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets/${encodeURIComponent(changesetId)}/review`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ file_path: filePath, reviewed }),
    },
  );
  return unwrapApiData(data);
}

export async function controlSessionResource(
  port: number,
  sessionId: string,
  kind: SessionResourceKind,
  resourceId: string,
  action: SessionResourceAction,
  workspaceId?: string | null,
): Promise<SessionResourceControlResult> {
  const data = await requestJson<APIResponse<SessionResourceControlResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(resourceId)}/control`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ action }),
    },
  );
  return unwrapApiData(data);
}
