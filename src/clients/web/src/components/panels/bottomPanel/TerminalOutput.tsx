import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ITheme, Terminal } from "@xterm/xterm";
import { t } from "../../../../../shared/core/i18n/index.js";
import { getWorkspaceResource } from "../../../api/workspaceResources";
import { loadTerminalRuntime } from "../../../runtime/terminal";
import { errorMessage } from "../../../utils/errorMessage";

export const TerminalThemeContext = createContext<ITheme>({});

/** 只读回放原始终端快照，不连接 PTY，也不向后台发送输入或尺寸变更。 */
export function TerminalOutput({ apiPort, workspaceId, resourceId }: {
  apiPort: number; workspaceId: string; resourceId: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal>();
  const terminalTheme = useContext(TerminalThemeContext);
  const themeRef = useRef(terminalTheme);
  themeRef.current = terminalTheme;
  const viewport = useRef<HTMLElement>(null);
  const [state, setState] = useState<"loading" | "ready" | { error: string }>("loading");
  useEffect(() => {
    const abort = new AbortController();
    const container = host.current!;
    let terminal: Terminal | undefined;
    setState("loading");
    void getWorkspaceResource(apiPort, workspaceId, resourceId, abort.signal).then(async resource => {
      const text = resource.metadata?.display_buffer;
      if (typeof text !== "string") { if (!abort.signal.aborted) setState("ready"); return; }
      const runtime = await loadTerminalRuntime();
      if (abort.signal.aborted) return;
      const stylesheet = document.createElement("style");
      stylesheet.textContent = runtime.stylesheet;
      container.append(stylesheet);
      terminal = new runtime.Terminal({
        cols: typeof resource.metadata?.cols === "number" ? resource.metadata.cols : undefined,
        rows: typeof resource.metadata?.rows === "number" ? resource.metadata.rows : undefined,
        disableStdin: true, cursorInactiveStyle: "none", convertEol: true, allowProposedApi: true,
        fontFamily: getComputedStyle(container).fontFamily, fontSize: 14,
        theme: themeRef.current,
      });
      terminalRef.current = terminal;
      terminal.loadAddon(new runtime.Unicode11Addon());
      terminal.unicode.activeVersion = "11";
      terminal.open(container);
      terminal.write(text, () => {
        if (abort.signal.aborted) return;
        terminal!.scrollToBottom();
        setState("ready");
        if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
      });
    }).catch((error: unknown) => { if (!abort.signal.aborted) setState({ error: errorMessage(error) }); });
    return () => { abort.abort(); terminalRef.current = undefined; terminal?.dispose(); container.replaceChildren(); };
  }, [apiPort, workspaceId, resourceId]);
  useEffect(() => {
    // 沿原 xterm 的更新入口重绘已有缓冲，不因主题变化重新读取或回放输出。
    if (terminalRef.current) terminalRef.current.options.theme = terminalTheme;
  }, [terminalTheme]);
  return <section className="terminal-panel-final-output panel-feedback-host" ref={viewport} aria-label={t("bottom.terminal.content")} aria-busy={state === "loading"}>
    {typeof state === "object" ? <span className="panel-feedback" role="alert">{state.error}</span> : null}
    <div className="terminal-panel-output-screen" ref={host} />
  </section>;
}
