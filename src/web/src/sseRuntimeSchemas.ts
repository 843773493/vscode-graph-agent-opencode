import type { SseErrorDTO } from "./types/gen/sse";
import type { SessionExecutionSseDTO } from "./types/gen/session_interaction";
import type { TraceEventDTO } from "./types/gen/trace";
import type { WorkspaceFileChangeBatchDTO } from "./types/gen/workspace";
import {
  validateSessionExecutionSse as validateSharedSessionExecutionSse,
  validateSseError as validateSharedSseError,
  validateTraceEvent as validateSharedTraceEvent,
  validateWorkspaceFileChangeBatch as validateSharedWorkspaceFileChangeBatch,
} from "../../shared/sseRuntime.js";

export function validateTraceEvent(value: unknown): TraceEventDTO {
  return validateSharedTraceEvent(value) as TraceEventDTO;
}

export function validateSessionExecutionSse(
  value: unknown,
): SessionExecutionSseDTO {
  return validateSharedSessionExecutionSse(value) as SessionExecutionSseDTO;
}

export function validateWorkspaceFileChangeBatch(
  value: unknown,
): WorkspaceFileChangeBatchDTO {
  return validateSharedWorkspaceFileChangeBatch(
    value,
  ) as WorkspaceFileChangeBatchDTO;
}

export function validateSseError(value: unknown): SseErrorDTO {
  return validateSharedSseError(value) as SseErrorDTO;
}
