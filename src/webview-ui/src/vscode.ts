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

export function writeRuntimeLog(message: string): void {
  if (typeof console !== 'undefined') {
    console.log(message);
  }
}

export function clearRuntimeLog(): void {
  if (typeof console !== 'undefined') {
    console.clear();
  }
}

export function interceptConsoleToMessageSink(sink: (line: string) => void): void {
  if (typeof console === 'undefined') {
    return;
  }

  const originalLog = console.log.bind(console);
  const originalWarn = console.warn.bind(console);
  const originalError = console.error.bind(console);

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
    originalLog(...args);
  };
  console.warn = (...args: unknown[]) => {
    sink(format('warn', args));
    originalWarn(...args);
  };
  console.error = (...args: unknown[]) => {
    sink(format('error', args));
    originalError(...args);
  };
}
