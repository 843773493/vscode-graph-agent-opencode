import { t } from "../../../../shared/core/i18n/index.js";
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
  if (kind === "condition") return t("debug.breakpoint.conditionLabel", breakpoint?.condition);
  if (kind === "hit") return t("debug.breakpoint.hitLabel", breakpoint?.hit_condition);
  if (kind === "log") return t("debug.breakpoint.logLabel", breakpoint?.log_message);
  return kind === "ordinary" ? t("debug.breakpoint.ordinary") : t("debug.breakpoint.none");
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
      setValidationError(t("debug.breakpoint.invalidHits"));
      return;
    }
    if (editorKind === "condition" && !normalizedCondition) {
      setValidationError(t("debug.breakpoint.conditionRequired"));
      return;
    }
    if (editorKind === "hit" && normalizedHitCondition === null) {
      setValidationError(t("debug.breakpoint.hitsRequired"));
      return;
    }
    if (editorKind === "log" && !normalizedLogMessage) {
      setValidationError(t("debug.breakpoint.logRequired"));
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
      aria-label={editorKind ? t("debug.breakpoint.editor") : t("debug.breakpoint.menu", line)}
    >
      {editorKind ? (
        <form onSubmit={saveSpecialBreakpoint}>
          <header>
            <strong>
              {editorKind === "condition" ? t("debug.breakpoint.condition") : null}
              {editorKind === "hit" ? t("debug.breakpoint.hit") : null}
              {editorKind === "log" ? t("debug.breakpoint.log") : null}
            </strong>
            <span>{path}:{line}</span>
          </header>
          {editorKind === "log" ? (
            <label>

              {t("debug.breakpoint.logMessage")}
              <input
                autoFocus
                value={logMessage}
                onChange={(event) => setLogMessage(event.target.value)}
                placeholder={t("debug.breakpoint.logExample")}
              />
            </label>
          ) : null}
          {editorKind === "condition" || editorKind === "hit" ? (
            <label>
              {t(editorKind === "hit" ? "debug.breakpoint.expressionOptional" : "debug.breakpoint.expression")}
              <input
                autoFocus={editorKind === "condition"}
                value={condition}
                onChange={(event) => setCondition(event.target.value)}
                placeholder={t("debug.breakpoint.expressionExample")}
              />
            </label>
          ) : null}
          {editorKind === "hit" ? (
            <label>

              {t("debug.breakpoint.hitCount")}
              <input
                autoFocus
                type="number"
                min="1"
                step="1"
                value={hitCondition}
                onChange={(event) => setHitCondition(event.target.value)}
                placeholder={t("debug.breakpoint.hitExample")}
              />
            </label>
          ) : null}
          {editorKind === "log" ? (
            <details>
              <summary>{t("debug.breakpoint.triggerOptional")}</summary>
              <label>

                {t("debug.breakpoint.expression")}
                <input
                  value={condition}
                  onChange={(event) => setCondition(event.target.value)}
                  placeholder={t("debug.breakpoint.expressionExample")}
                />
              </label>
              <label>

                {t("debug.breakpoint.hitCount")}
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={hitCondition}
                  onChange={(event) => setHitCondition(event.target.value)}
                  placeholder={t("debug.breakpoint.everyHit")}
                />
              </label>
            </details>
          ) : null}
          {validationError ? <p role="alert">{validationError}</p> : null}
          <footer>
            <button type="button" onClick={() => setEditorKind(null)}>{t("debug.breakpoint.back")}</button>
            <button type="submit" className="primary">{t("debug.breakpoint.save")}</button>
          </footer>
        </form>
      ) : (
        <>
          <header><strong>{t("debug.breakpoint.line", line)}</strong><span>{markerTitle}</span></header>
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
            {breakpoint ? t("debug.breakpoint.convert") : t("debug.breakpoint.addOrdinary")}
          </button>
          <button type="button" role="menuitem" className={kind === "condition" ? "active" : ""} onClick={() => setEditorKind("condition")}>
            <span className="codicon codicon-debug-breakpoint-conditional" aria-hidden="true" />
            {kind === "condition" ? t("debug.breakpoint.editCondition") : t("debug.breakpoint.addCondition")}
          </button>
          <button type="button" role="menuitem" className={kind === "hit" ? "active" : ""} onClick={() => setEditorKind("hit")}>
            <span className="codicon codicon-debug-breakpoint-data" aria-hidden="true" />
            {kind === "hit" ? t("debug.breakpoint.editHits") : t("debug.breakpoint.addHits")}
          </button>
          <button type="button" role="menuitem" className={kind === "log" ? "active" : ""} onClick={() => setEditorKind("log")}>
            <span className="codicon codicon-debug-breakpoint-log" aria-hidden="true" />
            {kind === "log" ? t("debug.breakpoint.editLog") : t("debug.breakpoint.addLog")}
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

              {t("debug.breakpoint.delete")}
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
        aria-label={t(breakpoint ? "debug.breakpoint.clearAria" : "debug.breakpoint.setAria", line)}
        aria-haspopup="menu"
        aria-expanded={menuPosition !== null}
        title={t(breakpoint ? "debug.breakpoint.clearHint" : "debug.breakpoint.addHint", markerTitle)}
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
