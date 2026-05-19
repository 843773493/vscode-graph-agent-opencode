import type { WebviewToHostMessage } from './types';

interface VsCodeApi {
  postMessage: (msg: WebviewToHostMessage) => void;
  getState: <T>() => T | undefined;
  setState: (state: unknown) => void;
}

function getVsCodeApi(): VsCodeApi | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const acquire = (window as Window & { acquireVsCodeApi?: () => VsCodeApi }).acquireVsCodeApi;
  if (!acquire) {
    return null;
  }
  try {
    return acquire();
  } catch {
    return null;
  }
}

const vscode = getVsCodeApi();

export function postMessage(msg: WebviewToHostMessage): void {
  if (vscode) {
    vscode.postMessage(msg);
  } else {
    // dev fallback
    window.dispatchEvent(new MessageEvent('message', { data: msg }));
  }
}

export function getVsCodeState<T>(): T | null {
  return vscode?.getState<T>() ?? null;
}

export function postDebug(detail: string): void {
  postMessage({ type: 'debug', detail });
}

export function postError(message: string): void {
  postMessage({ type: 'error', message });
}

export function setVsCodeState(state: unknown): void {
  vscode?.setState(state);
}

export function getPersistedState<T>(): T | undefined {
  return vscode?.getState<T>();
}
