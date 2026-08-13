import type {
  NodeDebugBreakpoint,
  NodeDebugStackFrame,
  NodeDebugState,
} from "../../types/backend";

export interface NodeDebugSourceSelection {
  path: string | null;
  focusLine: number | null;
}

interface ResolveNodeDebugSourceSelectionInput {
  state: NodeDebugState | null;
  selectedPath: string | null;
  selectedLine: number | null;
  draftScriptPath: string;
}

function latestBreakpoint(
  breakpoints: NodeDebugBreakpoint[],
): NodeDebugBreakpoint | null {
  return breakpoints.at(-1) ?? null;
}

function frameLocation(
  frame: NodeDebugStackFrame | null | undefined,
): NodeDebugSourceSelection | null {
  if (!frame?.path) return null;
  return { path: frame.path, focusLine: frame.line };
}

export function resolveNodeDebugSourceSelection({
  state,
  selectedPath,
  selectedLine,
  draftScriptPath,
}: ResolveNodeDebugSourceSelectionInput): NodeDebugSourceSelection {
  const activeFrame = frameLocation(state?.call_stack?.[0]);
  if (activeFrame) return activeFrame;

  const lastStoppedFrame = frameLocation(state?.last_stopped_frame);
  if (lastStoppedFrame) return lastStoppedFrame;

  if (selectedPath) return { path: selectedPath, focusLine: selectedLine };

  const breakpoint = latestBreakpoint(state?.breakpoints ?? []);
  if (breakpoint) return { path: breakpoint.path, focusLine: breakpoint.line };

  const savedScriptPath = state?.script_path?.trim();
  if (savedScriptPath) return { path: savedScriptPath, focusLine: null };

  const draft = draftScriptPath.trim();
  return { path: draft || null, focusLine: null };
}
