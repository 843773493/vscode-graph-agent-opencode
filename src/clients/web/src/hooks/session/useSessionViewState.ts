import { useCallback, useRef } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import type { SessionViewState } from "../../state/sessionViewState";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { errorMessage } from "../../utils/errorMessage";
import type { SetAppState } from "../contentViewLoaderTypes";
import { readSessionView, writeSessionView } from "./browserSessionViews";

export interface SessionViewStatePayload {
  turn_anchor: string | null;
  scroll_offset: number;
  follow_latest: boolean;
  tool_details_expanded?: boolean;
}
export interface SessionViewStateHost {
  apiPort: number | null;
  currentWorkspaceId: string | null;
  currentSessionId: string | null;
  sessionViewStates: Map<string, SessionViewState>;
  expandDetails: boolean;
  threadId?: string;
  scopeKey?: string;
}
export type SessionViewStateLoadOutcome =
  | { kind: "loaded"; viewState: SessionViewState | null }
  | { kind: "failed"; error: unknown }
  | { kind: "skipped" };
export interface SessionViewStateController {
  loadSessionViewState: (workspaceId: string | null, sessionId: string) => Promise<SessionViewStateLoadOutcome>;
  saveSessionViewState: (payload: SessionViewStatePayload) => void;
  toggleExpandDetails: (expand: boolean) => void;
}

export function useSessionViewState({ host, setState, onError }: {
  host: SessionViewStateHost; setState: SetAppState; onError: (message: string) => void;
}): SessionViewStateController {
  const latest = useRef(host); latest.current = host;
  const requests = useRef(new Map<string, Promise<SessionViewStateLoadOutcome>>());
  const versions = useRef(new Map<string, number>());
  const apply = useCallback((target: SessionViewStateHost, workspaceId: string, sessionId: string, view: SessionViewState | null, expand: boolean) => {
    const key = target.scopeKey ?? sessionScopeKey(workspaceId, sessionId);
    setState(previous => {
      const views = new Map(previous.sessionViewStates);
      if (view) views.set(key, view); else views.delete(key);
      const current = latest.current;
      return { ...previous, sessionViewStates: views,
        expandDetails: expand && current.currentWorkspaceId === workspaceId && current.currentSessionId === sessionId
          && current.threadId === target.threadId ? view?.tool_details_expanded ?? false : previous.expandDetails };
    });
  }, [setState]);
  const loadSessionViewState = useCallback(async (workspaceId: string | null, sessionId: string): Promise<SessionViewStateLoadOutcome> => {
    if (!workspaceId) return { kind: "skipped" };
    const target = latest.current;
    const key = JSON.stringify([target.apiPort, workspaceId, sessionId, target.threadId]);
    const pending = requests.current.get(key);
    if (pending) return pending;
    const version = versions.current.get(key) ?? 0;
    const request = readSessionView(target.apiPort ?? DEFAULT_BACKEND_PORT, workspaceId, sessionId, target.threadId).then(viewState => {
      if ((versions.current.get(key) ?? 0) === version) apply(target, workspaceId, sessionId, viewState, true);
      return { kind: "loaded", viewState } as const;
    }).catch((error: unknown) => {
      onError(`读取浏览器视图位置失败: ${errorMessage(error)}`);
      return { kind: "failed", error } as const;
    }).finally(() => { requests.current.delete(key); });
    requests.current.set(key, request);
    return request;
  }, [apply, onError]);
  const saveSessionViewState = useCallback((payload: SessionViewStatePayload) => {
    const target = latest.current;
    const workspaceId = target.currentWorkspaceId, sessionId = target.currentSessionId;
    if (!workspaceId || !sessionId) return;
    const key = JSON.stringify([target.apiPort, workspaceId, sessionId, target.threadId]);
    const version = (versions.current.get(key) ?? 0) + 1;
    versions.current.set(key, version);
    const existing = target.sessionViewStates.get(target.scopeKey ?? sessionScopeKey(workspaceId, sessionId));
    void writeSessionView(target.apiPort ?? DEFAULT_BACKEND_PORT, workspaceId, sessionId, {
      ...payload, projection_version: 1,
      tool_details_expanded: payload.tool_details_expanded ?? existing?.tool_details_expanded ?? target.expandDetails,
    }, target.threadId).then(view => {
      if (versions.current.get(key) === version) apply(target, workspaceId, sessionId, view, false);
    }).catch((error: unknown) => onError(`保存浏览器视图位置失败: ${errorMessage(error)}`));
  }, [apply, onError]);
  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState(previous => ({ ...previous, expandDetails: expand }));
    const current = latest.current;
    const existing = current.currentWorkspaceId && current.currentSessionId
      ? current.sessionViewStates.get(current.scopeKey ?? sessionScopeKey(current.currentWorkspaceId, current.currentSessionId)) : null;
    saveSessionViewState({ turn_anchor: existing?.turn_anchor ?? null, scroll_offset: existing?.scroll_offset ?? 0,
      follow_latest: existing?.follow_latest ?? true, tool_details_expanded: expand });
  }, [saveSessionViewState, setState]);
  return { loadSessionViewState, saveSessionViewState, toggleExpandDetails };
}
