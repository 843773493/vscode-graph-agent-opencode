import {
  parseSseError,
  parseTraceEvent,
  parseWorkspaceFileChangeBatch,
} from "../protocol";
import type {
  SseErrorDTO,
  TraceEventDTO,
  WorkspaceFileChangeBatchDTO,
} from "../protocol/jsonTypes";
import { validateFederatedSearchEvent as validateSearchEvent } from "../../../../shared/sseRuntime.js";
import type { FederatedSearchEventDTO } from "../types/protocol_generated/boxteam/search/v1/public";

export function validateFederatedSearchEvent(value: unknown): FederatedSearchEventDTO {
  return validateSearchEvent(value) as FederatedSearchEventDTO;
}

export function validateTraceEvent(value: unknown): TraceEventDTO {
  parseTraceEvent(value);
  return value as TraceEventDTO;
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
