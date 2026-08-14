import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { createPortal } from "react-dom";

import type { NodeDebugBreakpoint } from "../../types/backend";

export interface NodeDebugBreakpointDefinition {
  condition: string | null;
  hit_condition: number | null;
  log_message: string | null;
}

interface NodeDebugBreakpointGutterProps {
  className: string;
  path: string;
  line: number;
  breakpoint: NodeDebugBreakpoint | null;
  current?: boolean;
  disabled: boolean;
  onChange: (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => void;
}

type BreakpointEditorKind = "condition" | "hit" | "log";

interface MenuPosition {
  x: number;
  y: number;
}

export function nodeDebugBreakpointKind(
  breakpoint: NodeDebugBreakpoint | null,
): "ordinary" | BreakpointEditorKind | null {
  if (!breakpoint) return null;
  if (breakpoint.log_message) return "log";
  if (breakpoint.hit_condition) return "hit";
  if (breakpoint.condition) return "condition";
  return "ordinary";
}

export function nodeDebugBreakpointLabel(
  breakpoint: NodeDebugBreakpoint | null,
): string {
  const kind = nodeDebugBreakpointKind(breakpoint);
  if (kind === "condition") return `条件断点：${breakpoint?.condition}`;
  if (kind === "hit") return `命中次数断点：第 ${breakpoint?.hit_condition} 次`;
  if (kind === "log") return `日志点：${breakpoint?.log_message}`;
  return kind === "ordinary" ? "普通断点" : "未设置断点";
}

function breakpointIcon(breakpoint: NodeDebugBreakpoint): string {
  const kind = nodeDebugBreakpointKind(breakpoint);
  const verified = breakpoint.verified === true;
  if (kind === "condition") {
    return verified
      ? "codicon-debug-breakpoint-conditional"
      : "codicon-debug-breakpoint-conditional-unverified";
  }
  if (kind === "hit") {
    return verified
      ? "codicon-debug-breakpoint-data"
      : "codicon-debug-breakpoint-data-unverified";
  }
  if (kind === "log") {
    return verified
      ? "codicon-debug-breakpoint-log"
      : "codicon-debug-breakpoint-log-unverified";
  }
  return verified ? "codicon-debug-breakpoint" : "codicon-debug-breakpoint-unverified";
}

export default function NodeDebugBreakpointGutter({
  className,
  path,
  line,
  breakpoint,
  current = false,
  disabled,
  onChange,
}: NodeDebugBreakpointGutterProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [menuPosition, setMenuPosition] = useState<MenuPosition | null>(null);
  const [editorKind, setEditorKind] = useState<BreakpointEditorKind | null>(null);
  const [condition, setCondition] = useState("");
  const [hitCondition, setHitCondition] = useState("");
  const [logMessage, setLogMessage] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const kind = nodeDebugBreakpointKind(breakpoint);
  const markerTitle = nodeDebugBreakpointLabel(breakpoint);

  const closeMenu = () => {
    setMenuPosition(null);
    setEditorKind(null);
    setValidationError(null);
  };

  useEffect(() => {
    if (!menuPosition) return undefined;
    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) closeMenu();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu();
    };
    const handleWindowChange = () => closeMenu();
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("blur", handleWindowChange);
    window.addEventListener("resize", handleWindowChange);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("blur", handleWindowChange);
      window.removeEventListener("resize", handleWindowChange);
    };
  }, [menuPosition]);

  const openMenu = (x: number, y: number) => {
    if (disabled) return;
    setCondition(breakpoint?.condition ?? "");
    setHitCondition(breakpoint?.hit_condition?.toString() ?? "");
    setLogMessage(breakpoint?.log_message ?? "");
    setValidationError(null);
    setEditorKind(null);
    setMenuPosition({ x, y });
  };

  const handleContextMenu = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    openMenu(event.clientX, event.clientY);
  };

  const handleKeyboardMenu = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (!(event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) return;
    event.preventDefault();
    const rectangle = event.currentTarget.getBoundingClientRect();
    openMenu(rectangle.left + rectangle.width, rectangle.top + rectangle.height);
  };

  const applyDefinition = (definition: NodeDebugBreakpointDefinition) => {
    onChange(path, line, breakpoint?.breakpoint_id ?? null, definition);
    closeMenu();
  };

  const saveSpecialBreakpoint = (event: FormEvent) => {
    event.preventDefault();
    const normalizedCondition = condition.trim() || null;
    const normalizedLogMessage = logMessage.trim() || null;
    const normalizedHitCondition = hitCondition.trim() ? Number(hitCondition) : null;
    if (normalizedHitCondition !== null && (
      !Number.isSafeInteger(normalizedHitCondition) || normalizedHitCondition < 1
    )) {
      setValidationError("命中次数必须是正整数");
      return;
    }
    if (editorKind === "condition" && !normalizedCondition) {
      setValidationError("请输入条件表达式");
      return;
    }
    if (editorKind === "hit" && normalizedHitCondition === null) {
      setValidationError("请输入命中次数");
      return;
    }
    if (editorKind === "log" && !normalizedLogMessage) {
      setValidationError("请输入日志消息");
      return;
    }
    applyDefinition({
      condition: normalizedCondition,
      hit_condition: editorKind === "condition" ? null : normalizedHitCondition,
      log_message: editorKind === "log" ? normalizedLogMessage : null,
    });
  };

  const menu = menuPosition ? createPortal(
    <div
      ref={menuRef}
      className={`node-debug-breakpoint-menu${editorKind ? " editing" : ""}`}
      style={{
        left: Math.max(8, Math.min(menuPosition.x, window.innerWidth - (editorKind ? 328 : 250))),
        top: Math.max(8, Math.min(menuPosition.y, window.innerHeight - (editorKind ? 330 : 230))),
      }}
      role={editorKind ? "dialog" : "menu"}
      aria-label={editorKind ? "编辑特殊断点" : `第 ${line} 行断点菜单`}
    >
      {editorKind ? (
        <form onSubmit={saveSpecialBreakpoint}>
          <header>
            <strong>
              {editorKind === "condition" ? "条件断点" : null}
              {editorKind === "hit" ? "命中次数断点" : null}
              {editorKind === "log" ? "日志点" : null}
            </strong>
            <span>{path}:{line}</span>
          </header>
          {editorKind === "log" ? (
            <label>
              日志消息
              <input
                autoFocus
                value={logMessage}
                onChange={(event) => setLogMessage(event.target.value)}
                placeholder="例如 count={count}"
              />
            </label>
          ) : null}
          {editorKind === "condition" || editorKind === "hit" ? (
            <label>
              条件表达式{editorKind === "hit" ? "（可选）" : ""}
              <input
                autoFocus={editorKind === "condition"}
                value={condition}
                onChange={(event) => setCondition(event.target.value)}
                placeholder="例如 count > 5"
              />
            </label>
          ) : null}
          {editorKind === "hit" ? (
            <label>
              命中次数
              <input
                autoFocus
                type="number"
                min="1"
                step="1"
                value={hitCondition}
                onChange={(event) => setHitCondition(event.target.value)}
                placeholder="例如 3"
              />
            </label>
          ) : null}
          {editorKind === "log" ? (
            <details>
              <summary>触发条件（可选）</summary>
              <label>
                条件表达式
                <input
                  value={condition}
                  onChange={(event) => setCondition(event.target.value)}
                  placeholder="例如 count > 5"
                />
              </label>
              <label>
                命中次数
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={hitCondition}
                  onChange={(event) => setHitCondition(event.target.value)}
                  placeholder="每次都输出"
                />
              </label>
            </details>
          ) : null}
          {validationError ? <p role="alert">{validationError}</p> : null}
          <footer>
            <button type="button" onClick={() => setEditorKind(null)}>返回</button>
            <button type="submit" className="primary">保存</button>
          </footer>
        </form>
      ) : (
        <>
          <header><strong>第 {line} 行断点</strong><span>{markerTitle}</span></header>
          <button
            type="button"
            role="menuitem"
            className={kind === "ordinary" ? "active" : ""}
            onClick={() => applyDefinition({
              condition: null,
              hit_condition: null,
              log_message: null,
            })}
          >
            <span className="codicon codicon-debug-breakpoint" aria-hidden="true" />
            {breakpoint ? "转换为普通断点" : "添加普通断点"}
          </button>
          <button type="button" role="menuitem" className={kind === "condition" ? "active" : ""} onClick={() => setEditorKind("condition")}>
            <span className="codicon codicon-debug-breakpoint-conditional" aria-hidden="true" />
            {kind === "condition" ? "编辑条件断点…" : "添加条件断点…"}
          </button>
          <button type="button" role="menuitem" className={kind === "hit" ? "active" : ""} onClick={() => setEditorKind("hit")}>
            <span className="codicon codicon-debug-breakpoint-data" aria-hidden="true" />
            {kind === "hit" ? "编辑命中次数断点…" : "添加命中次数断点…"}
          </button>
          <button type="button" role="menuitem" className={kind === "log" ? "active" : ""} onClick={() => setEditorKind("log")}>
            <span className="codicon codicon-debug-breakpoint-log" aria-hidden="true" />
            {kind === "log" ? "编辑日志点…" : "添加日志点…"}
          </button>
          {breakpoint ? (
            <button
              type="button"
              role="menuitem"
              className="danger separated"
              onClick={() => {
                onChange(path, line, breakpoint.breakpoint_id, null);
                closeMenu();
              }}
            >
              <span className="codicon codicon-trash" aria-hidden="true" />
              删除断点
            </button>
          ) : null}
        </>
      )}
    </div>,
    document.body,
  ) : null;

  return (
    <>
      <button
        type="button"
        className={`${className}${breakpoint ? ` has-breakpoint breakpoint-${kind}${breakpoint.verified === true ? "" : " breakpoint-unverified"}` : ""}`}
        aria-label={`${breakpoint ? "清除" : "设置"}第 ${line} 行断点；右键打开特殊断点菜单`}
        aria-haspopup="menu"
        aria-expanded={menuPosition !== null}
        title={`${markerTitle}；左键${breakpoint ? "清除" : "添加普通断点"}，右键编辑特殊断点`}
        disabled={disabled}
        onClick={() => onChange(
          path,
          line,
          breakpoint?.breakpoint_id ?? null,
          breakpoint ? null : { condition: null, hit_condition: null, log_message: null },
        )}
        onContextMenu={handleContextMenu}
        onKeyDown={handleKeyboardMenu}
      >
        {breakpoint ? (
          <span className={`codicon ${breakpointIcon(breakpoint)} node-debug-breakpoint-icon`} aria-hidden="true" />
        ) : current ? (
          <span className="codicon codicon-debug-stackframe-active node-debug-current-icon" aria-hidden="true" />
        ) : (
          <span className="node-debug-breakpoint-placeholder" aria-hidden="true" />
        )}
      </button>
      {menu}
    </>
  );
}
