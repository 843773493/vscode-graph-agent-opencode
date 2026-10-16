import {
  parseSseError,
  parseTraceEvent,
  parseWorkspaceFileChangeBatch,
} from "../protocol";
import type {
  SessionExecutionSseDTO,
  SseErrorDTO,
  TraceEventDTO,
  WorkspaceFileChangeBatchDTO,
} from "../protocol/jsonTypes";
import { parseSessionExecutionSse } from "../protocol/sessionSse";
import { validateWorkspaceObservationFrameDTO } from "../../../../shared/sseRuntimeValidators.js";
import type { WorkspaceObservationFrameDTO } from "../types/backend";

export function validateWorkspaceObservationFrame(value: unknown): WorkspaceObservationFrameDTO {
  if (!validateWorkspaceObservationFrameDTO(value)) {
    throw new Error(`Workspace 观察者信封无效: ${JSON.stringify(validateWorkspaceObservationFrameDTO.errors)}`);
  }
  const frame = value as WorkspaceObservationFrameDTO;
  if (frame.state === "frame" && !frame.frame) {
    throw new Error("Workspace 观察者信封缺少原 SSE 帧");
  }
  if (frame.state === "error" && (typeof frame.status !== "number" || typeof frame.detail_json !== "string")) {
    throw new Error("Workspace 观察者错误信封缺少 status/detail_json");
  }
  return frame;
}

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
