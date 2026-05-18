import type { WebviewToHostMessage, HostToWebviewMessage } from './types';

const vscode = (window as any).acquireVsCodeApi?.();

// 解构 acquireVsCodeApi（仅在 webview 环境可用）
let _postMessage: ((msg: WebviewToHostMessage) => void) | null = null;
let _getState: (() => unknown) | null = null;

try {
  if (typeof window !== 'undefined' && (window as any).acquireVsCodeApi) {
    _postMessage = (window as any).acquireVsCodeApi().postMessage;
    _getState = (window as any).acquireVsCodeApi().getState;
  }
} catch {
  // 非 webview 环境（如开发模式）忽略
}

export function postMessage(msg: WebviewToHostMessage): void {
  if (_postMessage) {
    _postMessage(msg);
  } else {
    // dev fallback
    window.dispatchEvent(new MessageEvent('message', { data: msg }));
  }
}

export function getVsCodeState<T>(): T | null {
  if (_getState) {
    return _getState() as T;
  }
  return null;
}

export function postDebug(detail: string): void {
  postMessage({ type: 'debug', detail });
}

export function postError(message: string): void {
  postMessage({ type: 'error', message });
}

// 创建 SSE job events（仅 Host 侧直接调用 SSE 流，webview 侧只收 jobEvent）
export type JobEventCallback = (eventType: string, payload: Record<string, unknown>) => void;
