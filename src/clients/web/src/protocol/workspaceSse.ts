import { fromJson, type JsonObject } from "@bufbuild/protobuf";
import type { SseError } from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";
import {
  SseErrorSchema,
  WorkspaceFileChangeBatchSchema,
} from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";
import type { TraceEvent } from "../types/protocol_buf_generated/boxteam/workspace/v2/trace_pb";
import { TraceEventSchema } from "../types/protocol_buf_generated/boxteam/workspace/v2/trace_pb";
import type { WorkspaceFileChangeBatch } from "../types/protocol_buf_generated/boxteam/workspace/v2/file_events_pb";
import { errorMessage } from "../utils/errorMessage";

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
  allowEmpty = false,
): string {
  const value = record[key];
  if (typeof value !== "string" || (!allowEmpty && value.length === 0)) {
    throw new Error(`${name} 校验失败: ${path}/${key} 必须是${allowEmpty ? "字符串" : "非空字符串"}`);
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
    const detail = errorMessage(error);
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
    threadId: requiredString(source, "thread_id", "/", "TraceEventDTO"),
    jobId: requiredString(source, "job_id", "/", "TraceEventDTO"),
    type: requiredString(source, "type", "/", "TraceEventDTO"),
    phase: requiredString(source, "phase", "/", "TraceEventDTO"),
    title: requiredString(source, "title", "/", "TraceEventDTO"),
    content: requiredString(source, "content", "/", "TraceEventDTO", true),
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
  if (!Array.isArray(source.directories) || !Array.isArray(source.invalidated_roots) || !Array.isArray(source.snapshots)
    || !Number.isSafeInteger(source.event_id) || (source.event_id as number) < 0) {
    throw new Error("WorkspaceFileChangeBatchDTO 校验失败: 缺少目录版本、事件序号或失效根");
  }
  const directories = source.directories.map((item, index) => {
    const revision = asRecord(item, `/directories/${index}`);
    const key = asRecord(revision.directory_key, `/directories/${index}/directory_key`);
    if (!Number.isSafeInteger(revision.directory_revision) || (revision.directory_revision as number) < 1
      || typeof revision.exists !== "boolean" || typeof key.path !== "string"
      || !["workspace", "filesystem"].includes(String(key.scope))) {
      throw new Error("WorkspaceFileChangeBatchDTO 校验失败: 目录观察格式无效");
    }
    return { directoryKey: {
      workspaceId: requiredString(key, "workspace_id", "/directories", "WorkspaceFileChangeBatchDTO"),
      root: requiredString(key, "root", "/directories", "WorkspaceFileChangeBatchDTO"),
      scope: key.scope as string, path: key.path,
    }, directoryRevision: revision.directory_revision as number, exists: revision.exists };
  });
  if (!source.invalidated_roots.every((root) => typeof root === "string" && root.length > 0)) {
    throw new Error("WorkspaceFileChangeBatchDTO 校验失败: 失效根必须为非空路径");
  }
  const snapshots = source.snapshots.map((item, index) => {
    const path = `/snapshots/${index}`;
    const snapshot = asRecord(item, path);
    const key = asRecord(snapshot.directory_key, `${path}/directory_key`);
    const revision = directories.find((value) => value.directoryKey.workspaceId === key.workspace_id
      && value.directoryKey.scope === key.scope && value.directoryKey.root === key.root
      && value.directoryKey.path === key.path);
    if (!revision?.exists || !Number.isSafeInteger(snapshot.directory_revision)
      || snapshot.directory_revision !== revision.directoryRevision
      || typeof snapshot.path !== "string" || snapshot.path !== key.path
      || snapshot.root_path !== key.root || !Array.isArray(snapshot.items)) {
      throw new Error(`WorkspaceFileChangeBatchDTO 校验失败: ${path} 缺少同次目录键/revision 配对`);
    }
    return snapshot as JsonObject;
  });
  return parseProto("WorkspaceFileChangeBatchDTO", () =>
    fromJson(
      WorkspaceFileChangeBatchSchema,
      { changes, overflow, directories, eventId: source.event_id as number,
        invalidatedRoots: source.invalidated_roots as string[], snapshots },
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
