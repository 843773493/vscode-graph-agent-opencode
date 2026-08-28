import {
  parseSseError,
  parseTraceEvent,
  parseWorkspaceFileChangeBatch,
} from "./protocol";
import type {
  SessionExecutionSseDTO,
  SseErrorDTO,
  TraceEventDTO,
  WorkspaceFileChangeBatchDTO,
} from "./protocol/jsonTypes";
import { parseSessionExecutionSse } from "./protocol/sessionSse";

export function validateTraceEvent(value: unknown): TraceEventDTO {
  parseTraceEvent(value);
  return value as TraceEventDTO;
}

export function validateSessionExecutionSse(
  value: unknown,
): SessionExecutionSseDTO {
  parseSessionExecutionSse(value);
  return value as SessionExecutionSseDTO;
}

export function validateWorkspaceFileChangeBatch(
  value: unknown,
): WorkspaceFileChangeBatchDTO {
  parseWorkspaceFileChangeBatch(value);
  return value as WorkspaceFileChangeBatchDTO;
}

export function validateSseError(value: unknown): SseErrorDTO {
  parseSseError(value);
  return value as SseErrorDTO;
}
