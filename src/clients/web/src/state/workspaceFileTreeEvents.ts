import { rawTracePayload } from "./traceEvents";
import type { TraceEvent } from "../types/backend";

export const WORKSPACE_FILE_CHANGES_EVENT = "boxteam:workspace-file-changes";

export interface WorkspaceFileChangeNotice {
  path: string;
  kind: string;
}

export interface WorkspaceFileChangesEventDetail {
  workspaceId: string | null;
  changes: WorkspaceFileChangeNotice[];
}

function fileChangeNotice(value: unknown): WorkspaceFileChangeNotice | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.file_path !== "string" || !record.file_path.trim()) {
    return null;
  }
  return {
    path: record.file_path,
    kind: typeof record.kind === "string" ? record.kind : "edit",
  };
}

export function fileChangesFromTraceEvents(
  events: readonly TraceEvent[],
): WorkspaceFileChangeNotice[] {
  const changesByKey = new Map<string, WorkspaceFileChangeNotice>();
  for (const event of events) {
    if (event.type !== "tool_call_end") {
      continue;
    }
    const payload = rawTracePayload(event);
    const candidates = [
      payload.file_edit,
      ...(Array.isArray(payload.file_edits) ? payload.file_edits : []),
    ];
    for (const candidate of candidates) {
      const change = fileChangeNotice(candidate);
      if (change) {
        changesByKey.set(`${change.kind}:${change.path}`, change);
      }
    }
  }
  return [...changesByKey.values()];
}

export function dispatchWorkspaceFileChanges(
  workspaceId: string | null,
  changes: readonly WorkspaceFileChangeNotice[],
): void {
  if (changes.length === 0) {
    return;
  }
  window.dispatchEvent(new CustomEvent<WorkspaceFileChangesEventDetail>(
    WORKSPACE_FILE_CHANGES_EVENT,
    { detail: { workspaceId, changes: [...changes] } },
  ));
}
