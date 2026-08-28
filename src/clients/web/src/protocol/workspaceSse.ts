import { fromJson, type JsonObject } from "@bufbuild/protobuf";
import type { SseError } from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";
import {
  SseErrorSchema,
  WorkspaceFileChangeBatchSchema,
} from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";
import type { TraceEvent } from "../types/protocol_buf_generated/boxteam/workspace/v2/trace_pb";
import { TraceEventSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/trace_pb";
import type { WorkspaceFileChangeBatch } from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown, path: string): JsonRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`SSE Protobuf 校验失败: ${path} 必须是对象`);
  }
  return value as JsonRecord;
}

function requiredString(
  record: JsonRecord,
  key: string,
  path: string,
  name = "SSE Protobuf",
): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${name} 校验失败: ${path}/${key} 必须是非空字符串`);
  }
  return value;
}

function optionalString(
  record: JsonRecord,
  key: string,
  path: string,
): string | undefined {
  const value = record[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") {
    throw new Error(`SSE Protobuf 校验失败: ${path}/${key} 必须是字符串或 null`);
  }
  return value;
}

function parseProto<ProtoMessage>(
  name: string,
  parse: () => ProtoMessage,
): ProtoMessage {
  try {
    return parse();
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`${name} Protobuf 校验失败: ${detail}`);
  }
}

export function parseTraceEvent(value: unknown): TraceEvent {
  const source = asRecord(value, "/");
  const timestamp = requiredString(source, "timestamp", "/", "TraceEventDTO");
  if (Number.isNaN(Date.parse(timestamp))) {
    throw new Error("TraceEventDTO 校验失败: /timestamp 不是有效日期时间");
  }
  const proto: JsonObject = {
    eventId: requiredString(source, "event_id", "/", "TraceEventDTO"),
    sessionId: requiredString(source, "session_id", "/", "TraceEventDTO"),
    jobId: requiredString(source, "job_id", "/", "TraceEventDTO"),
    type: requiredString(source, "type", "/", "TraceEventDTO"),
    phase: requiredString(source, "phase", "/", "TraceEventDTO"),
    title: requiredString(source, "title", "/", "TraceEventDTO"),
    content: requiredString(source, "content", "/", "TraceEventDTO"),
    timestamp,
    skillNames: Array.isArray(source.skill_names) ? source.skill_names : [],
    raw: asRecord(source.raw ?? {}, "/raw") as JsonObject,
  };
  for (const [sourceKey, protoKey] of [
    ["part_id", "partId"],
    ["status", "status"],
    ["tool_name", "toolName"],
    ["step_id", "stepId"],
  ] as const) {
    const item = optionalString(source, sourceKey, "/");
    if (item !== undefined) proto[protoKey] = item;
  }
  return parseProto("TraceEventDTO", () =>
    fromJson(TraceEventSchema, proto, { ignoreUnknownFields: false }),
  );
}

export function parseWorkspaceFileChangeBatch(
  value: unknown,
): WorkspaceFileChangeBatch {
  const source = asRecord(value, "/");
  if (typeof source.overflow !== "boolean") {
    throw new Error("WorkspaceFileChangeBatchDTO 校验失败: /overflow 必须是布尔值");
  }
  const overflow = source.overflow;
  if (!Array.isArray(source.changes)) {
    throw new Error("WorkspaceFileChangeBatchDTO 校验失败: /changes 必须是数组");
  }
  const changes = source.changes.map((item, index) => {
    const change = asRecord(item, `/changes/${index}`);
    const kind = requiredString(
      change,
      "kind",
      `/changes/${index}`,
      "WorkspaceFileChangeBatchDTO",
    );
    if (!["create", "edit", "delete"].includes(kind)) {
      throw new Error(`WorkspaceFileChangeBatchDTO 校验失败: /changes/${index}/kind 不支持 ${kind}`);
    }
    return {
      kind,
      path: requiredString(
        change,
        "path",
        `/changes/${index}`,
        "WorkspaceFileChangeBatchDTO",
      ),
    };
  });
  return parseProto("WorkspaceFileChangeBatchDTO", () =>
    fromJson(
      WorkspaceFileChangeBatchSchema,
      { changes, overflow },
      { ignoreUnknownFields: false },
    ),
  );
}

export function parseSseError(value: unknown): SseError {
  const source = asRecord(value, "/");
  const message = requiredString(source, "message", "/", "SseErrorDTO");
  return parseProto("SseErrorDTO", () =>
    fromJson(SseErrorSchema, { message }, { ignoreUnknownFields: false }),
  );
}
