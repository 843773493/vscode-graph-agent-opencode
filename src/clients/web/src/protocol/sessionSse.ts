import {
  fromJson,
  type JsonObject,
  type JsonValue,
} from "@bufbuild/protobuf";
import type { SessionExecutionSse } from "../types/protocol_buf_generated/boxteam/workspace/v2/session_stream_pb";
import { SessionExecutionSseSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/session_stream_pb";

const EVENT_PAYLOAD_FIELDS: Record<string, string> = {
  "message.updated": "messageUpdated",
  "job.updated": "jobUpdated",
  "job.step.updated": "jobStepUpdated",
  "job.status.changed": "jobStatusChanged",
  "session.status.changed": "sessionStatusChanged",
  "session.completed": "sessionCompleted",
  "session.error": "sessionError",
  "trace.observed": "traceObserved",
};

const JOB_STATUSES = new Set([
  "accepted",
  "queued",
  "running",
  "streaming",
  "waiting_input",
  "paused",
  "interrupt_pending",
  "cancelling",
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown, path: string): JsonRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path} 必须是对象`);
  }
  return value as JsonRecord;
}

function requiredString(record: JsonRecord, key: string, path: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path}/${key} 必须是非空字符串`);
  }
  return value;
}

function optionalString(
  record: JsonRecord,
  key: string,
  path: string,
): string | undefined {
  const value = record[key];
  if (value === undefined || value === null) {
    return undefined;
  }
  if (typeof value !== "string") {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path}/${key} 必须是字符串或 null`);
  }
  return value;
}

function numberWithDefault(
  record: JsonRecord,
  key: string,
  defaultValue: number,
  path: string,
): number {
  const value = record[key];
  if (value === undefined || value === null) {
    return defaultValue;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path}/${key} 必须是有限数字`);
  }
  return value;
}

function booleanWithDefault(
  record: JsonRecord,
  key: string,
  defaultValue: boolean,
  path: string,
): boolean {
  const value = record[key];
  if (value === undefined || value === null) {
    return defaultValue;
  }
  if (typeof value !== "boolean") {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path}/${key} 必须是布尔值`);
  }
  return value;
}

function jsonObjectOrDefault(
  record: JsonRecord,
  key: string,
  path: string,
): JsonObject {
  const value = record[key];
  if (value === undefined || value === null) {
    return {};
  }
  return asRecord(value, `${path}/${key}`) as JsonObject;
}

function optionalJsonObject(
  record: JsonRecord,
  key: string,
  path: string,
): JsonObject | undefined {
  const value = record[key];
  if (value === undefined || value === null) {
    return undefined;
  }
  return asRecord(value, `${path}/${key}`) as JsonObject;
}

function optionalJsonValue(
  record: JsonRecord,
  key: string,
  path: string,
): JsonValue | undefined {
  const value = record[key];
  if (value === undefined || value === null) {
    return undefined;
  }
  return value as JsonValue;
}

function mapHeader(event: JsonRecord): JsonObject {
  const path = "/event";
  const header: JsonObject = {
    eventId: requiredString(event, "event_id", path),
    sessionId: requiredString(event, "session_id", path),
    time: requiredString(event, "time", path),
  };
  const jobId = optionalString(event, "job_id", path);
  if (jobId !== undefined) {
    header.jobId = jobId;
  }
  return header;
}

function mapJobProgress(payload: JsonRecord, path: string): JsonObject {
  const status = requiredString(payload, "status", path);
  if (!JOB_STATUSES.has(status)) {
    throw new Error(`SessionExecutionSseDTO 校验失败: ${path}/status 不支持 ${status}`);
  }
  const result: JsonObject = {
    jobId: requiredString(payload, "job_id", path),
    status: `JOB_STATUS_${status.toUpperCase()}`,
    progress: numberWithDefault(payload, "progress", 0, path),
  };
  const currentStepId = optionalString(payload, "current_step_id", path);
  const message = optionalString(payload, "message", path);
  if (currentStepId !== undefined) {
    result.currentStepId = currentStepId;
  }
  if (message !== undefined) {
    result.message = message;
  }
  return result;
}

function mapPayload(type: string, payload: JsonRecord): JsonObject {
  const path = "/event/payload";
  switch (type) {
    case "message.updated": {
      const result: JsonObject = {
        messageId: requiredString(payload, "message_id", path),
        sessionId: requiredString(payload, "session_id", path),
        role: requiredString(payload, "role", path),
        content: requiredString(payload, "content", path),
        attachments: payload.attachments === undefined ? [] : (payload.attachments as JsonValue),
        metadata: jsonObjectOrDefault(payload, "metadata", path),
        createdAt: requiredString(payload, "created_at", path),
      };
      return result;
    }
    case "job.updated":
    case "job.status.changed":
    case "session.completed":
      return mapJobProgress(payload, path);
    case "job.step.updated": {
      const result: JsonObject = {};
      const agentId = optionalString(payload, "agent_id", path);
      const message = optionalString(payload, "message", path);
      const phase = optionalString(payload, "phase", path);
      if (agentId !== undefined) result.agentId = agentId;
      if (message !== undefined) result.message = message;
      if (phase !== undefined) result.phase = phase;
      return result;
    }
    case "session.status.changed": {
      const sessionId = requiredString(payload, "session_id", path);
      const result: JsonObject = { sessionId };
      const status = optionalString(payload, "status", path);
      const message = optionalString(payload, "message", path);
      const activeJobId = optionalString(payload, "active_job_id", path);
      if (status !== undefined) result.status = status;
      if (message !== undefined) result.message = message;
      if (activeJobId !== undefined) result.activeJobId = activeJobId;
      if (status === undefined) {
        result.observationState = payload.is_streaming === true ? "streaming" : "idle";
        result.isStreaming = booleanWithDefault(payload, "is_streaming", false, path);
        result.isIdle = booleanWithDefault(payload, "is_idle", true, path);
      } else {
        const waiting = optionalJsonObject(payload, "waiting", path);
        if (waiting !== undefined) result.waiting = waiting;
      }
      return result;
    }
    case "session.error":
      return { error: requiredString(payload, "error", path) };
    case "trace.observed":
      return { rawType: requiredString(payload, "raw_type", path) };
    default:
      throw new Error(`SessionExecutionSseDTO 校验失败: /event/type 不支持 ${type}`);
  }
}

export function parseSessionExecutionSse(value: unknown): SessionExecutionSse {
  const envelope = asRecord(value, "/");
  const event = asRecord(envelope.event, "/event");
  const type = requiredString(event, "type", "/event");
  const payloadField = EVENT_PAYLOAD_FIELDS[type];
  if (payloadField === undefined) {
    throw new Error(`SessionExecutionSseDTO 校验失败: /event/type 不支持 ${type}`);
  }

  const protoEnvelope: JsonObject = {
    event: {
      type,
      header: mapHeader(event),
      [payloadField]: mapPayload(type, asRecord(event.payload, "/event/payload")),
    },
    rawType: requiredString(envelope, "raw_type", "/"),
  };
  const rawPayload = optionalJsonObject(envelope, "raw_payload", "/");
  if (rawPayload !== undefined) {
    protoEnvelope.rawPayload = rawPayload;
  }

  try {
    return fromJson(SessionExecutionSseSchema, protoEnvelope, {
      ignoreUnknownFields: false,
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`SessionExecutionSseDTO Protobuf 校验失败: ${detail}`);
  }
}
