interface VsCodeApi {
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

export function getVsCodeState<T>(): T | null {
  return vscode?.getState<T>() ?? null;
}

export function setVsCodeState(state: unknown): void {
  vscode?.setState(state);
}

export function getWebviewHtmlSnapshot(): string {
  if (typeof document === 'undefined') {
    return '';
  }
  return document.documentElement.outerHTML;
}

export function formatLocalLogBlock(kind: string, body: string): string {
  return [`========== ${kind} ==========`,'timestamp=' + new Date().toISOString(), body, ''].join('\n');
}

// 保存原始 console 引用，避免 interceptConsoleToMessageSink 造成无限递归
const _originalConsoleLog = typeof console !== 'undefined' ? console.log.bind(console) : () => {};
const _originalConsoleError = typeof console !== 'undefined' ? console.error.bind(console) : () => {};
const _originalConsoleWarn = typeof console !== 'undefined' ? console.warn.bind(console) : () => {};

export function writeRuntimeLog(message: string): void {
  _originalConsoleLog(message);
}

export function clearRuntimeLog(): void {
  if (typeof console !== 'undefined') {
    _originalConsoleLog('\x1b[2J\x1b[H');
  }
}

export function interceptConsoleToMessageSink(sink: (line: string) => void): void {
  const format = (level: string, args: unknown[]) => {
    const text = args.map((item) => {
      if (typeof item === 'string') {
        return item;
      }
      try {
        return JSON.stringify(item);
      } catch {
        return String(item);
      }
    }).join(' ');
    return formatLocalLogBlock(`console ${level}`, text);
  };

  console.log = (...args: unknown[]) => {
    sink(format('log', args));
    _originalConsoleLog(...args);
  };
  console.warn = (...args: unknown[]) => {
    sink(format('warn', args));
    _originalConsoleWarn(...args);
  };
  console.error = (...args: unknown[]) => {
    sink(format('error', args));
    _originalConsoleError(...args);
  };
}
