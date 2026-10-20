import { create, fromBinary, toBinary } from "@bufbuild/protobuf";
import { test, expect } from "bun:test";
import {
  SessionExecutionEventSchema,
} from "../../src/workspace-services/protocol/generated/boxteam/workspace/v2/session_interaction_pb.js";

test("Node Protobuf 生成结果保留跨文件 import 与 oneof", () => {
  const event = create(SessionExecutionEventSchema, {
    type: "job.updated",
    header: {
      eventId: "event_123",
      sessionId: "session_123",
    },
    payload: {
      case: "jobUpdated",
      value: {
        jobId: "job_123",
        status: 2,
        progress: 42,
      },
    },
  });

  expect(event.payload.case).toBe("jobUpdated");
  expect(event.payload.value.progress).toBe(42);
});


test("公开诊断字段没有真实定位，通信目标的三种 destination 均可往返", async () => {
  const [workspaceBinding, webBinding] = await Promise.all([
    import("../../src/workspace-services/protocol/generated/boxteam/workspace/v2/public_pb.js"),
    import("../../src/clients/web/src/types/protocol_buf_generated/boxteam/workspace/v2/public_pb.js"),
  ]);
  const {
    SessionInformationSnapshotDTOSchema,
    SessionInformationWorkspaceDTOSchema,
  } = workspaceBinding;
  expect(SessionInformationSnapshotDTOSchema.fields.map((field) => field.name)).not.toContain("storage_path");
  expect(SessionInformationWorkspaceDTOSchema.fields.map((field) => field.name)).not.toContain("root_path");

  for (const binding of [workspaceBinding, webBinding]) {
    for (const schema of [binding.WorkspaceDTOSchema, binding.WorkspaceContextDTOSchema]) {
      expect(schema.fields.map((field) => field.name)).not.toContain("root_path");
      const value = create(schema, { workspaceId: "workspace-stable-identity" });
      expect(fromBinary(schema, toBinary(schema, value)).workspaceId).toBe("workspace-stable-identity");
    }
    const schema = binding.CommunicationGatewayRequestSchema;
    expect(schema.fields.find((field) => field.name === "fork_target")?.number).toBe(5);
    const threadTarget = create(schema, {
      operation: "send",
      destination: {
        case: "target",
        value: {
          gatewayId: "gateway-thread", workspaceId: "workspace-thread",
          sessionId: "session-thread", threadId: "thread-target",
        },
      },
    });
    const decodedThread = fromBinary(schema, toBinary(schema, threadTarget));
    expect(decodedThread.destination.case).toBe("target");
    expect(decodedThread.destination.value.gatewayId).toBe("gateway-thread");
    expect(decodedThread.destination.value.threadId).toBe("thread-target");

    const resourceTarget = create(schema, {
      operation: "read",
      destination: {
        case: "resourceTarget",
        value: { gatewayId: "gateway-c", resourceIdentity: "resource", uri: "resource-uri", scope: "workspace" },
      },
    });
    const decodedResource = fromBinary(schema, toBinary(schema, resourceTarget));
    expect(decodedResource.destination.case).toBe("resourceTarget");
    expect(decodedResource.destination.value.gatewayId).toBe("gateway-c");
    expect(decodedResource.destination.value.resourceIdentity).toBe("resource");
    expect(decodedResource.destination.value.uri).toBe("resource-uri");
    expect(decodedResource.destination.value.scope).toBe("workspace");

    const forkTarget = create(schema, {
      operation: "fork.transfer",
      destination: {
        case: "forkTarget",
        value: { gatewayId: "gateway-fork", uri: "boxteam://forks/fork_123/source", forkRole: "source" },
      },
    });
    const decodedFork = fromBinary(schema, toBinary(schema, forkTarget));
    expect(decodedFork.destination.case).toBe("forkTarget");
    expect(decodedFork.destination.value.gatewayId).toBe("gateway-fork");
    expect(decodedFork.destination.value.uri).toBe("boxteam://forks/fork_123/source");
    expect(decodedFork.destination.value.forkRole).toBe("source");
  }

  const published = create(workspaceBinding.SessionForkResultDTOSchema, {
    targetGatewayId: "gateway-published-target",
    session: { workspaceId: "workspace-published", sessionId: "session-published" },
    threadId: "thread-published",
  });
  const decodedPublished = fromBinary(webBinding.SessionForkResultDTOSchema,
    toBinary(workspaceBinding.SessionForkResultDTOSchema, published));
  expect(decodedPublished.targetGatewayId).toBe("gateway-published-target");
  expect(decodedPublished.session.workspaceId).toBe("workspace-published");
  expect(decodedPublished.threadId).toBe("thread-published");

  const workspaceSchema = workspaceBinding.CommunicationGatewayRequestSchema;
  const webSchema = webBinding.CommunicationGatewayRequestSchema;
  const workspaceFork = create(workspaceSchema, {
    operation: "fork.transfer",
    destination: {
      case: "forkTarget",
      value: { gatewayId: "gateway-workspace", uri: "boxteam://forks/workspace/source", forkRole: "source" },
    },
  });
  const decodedWorkspaceForkByWeb = fromBinary(
    webSchema,
    toBinary(workspaceSchema, workspaceFork),
  );
  expect(decodedWorkspaceForkByWeb.destination.case).toBe("forkTarget");
  expect(decodedWorkspaceForkByWeb.destination.value.gatewayId).toBe("gateway-workspace");
  expect(decodedWorkspaceForkByWeb.destination.value.uri).toBe("boxteam://forks/workspace/source");
  expect(decodedWorkspaceForkByWeb.destination.value.forkRole).toBe("source");

  const webFork = create(webSchema, {
    operation: "fork.transfer",
    destination: {
      case: "forkTarget",
      value: { gatewayId: "gateway-web", uri: "boxteam://forks/web/target", forkRole: "target" },
    },
  });
  const decodedWebForkByWorkspace = fromBinary(
    workspaceSchema,
    toBinary(webSchema, webFork),
  );
  expect(decodedWebForkByWorkspace.destination.case).toBe("forkTarget");
  expect(decodedWebForkByWorkspace.destination.value.gatewayId).toBe("gateway-web");
  expect(decodedWebForkByWorkspace.destination.value.uri).toBe("boxteam://forks/web/target");
  expect(decodedWebForkByWorkspace.destination.value.forkRole).toBe("target");
});


test("Gateway 完整组请求、blocker 与两种客户端绑定往返保持成员和 generation", async () => {
  const bindings = await Promise.all([
    import("../../src/workspace-services/protocol/generated/boxteam/gateway/v1/public_pb.js"),
    import("../../src/clients/web/src/types/protocol_buf_generated/boxteam/gateway/v1/public_pb.js"),
  ]);
  for (const binding of bindings) {
    const schema = binding.GatewayRuntimeGroupRequestSchema;
    const request = create(schema, {
      expectedBackendGeneration: "generation-current",
      expectedWorkspaceIds: ["gateway-a", "gateway-b"],
      desiredWorkspaceIds: { workspaceIds: ["gateway-a", "gateway-c"] },
    });
    const decoded = fromBinary(schema, toBinary(schema, request));
    expect(decoded.expectedBackendGeneration).toBe("generation-current");
    expect(decoded.expectedWorkspaceIds).toEqual(["gateway-a", "gateway-b"]);
    expect(decoded.desiredWorkspaceIds.workspaceIds).toEqual(["gateway-a", "gateway-c"]);
    expect(create(schema).desiredWorkspaceIds).toBeUndefined();
    const empty = create(schema, {
      expectedBackendGeneration: "generation-current", expectedWorkspaceIds: ["gateway-a"],
      desiredWorkspaceIds: { workspaceIds: [] },
    });
    const decodedEmpty = fromBinary(schema, toBinary(schema, empty));
    expect(decodedEmpty.desiredWorkspaceIds).toBeDefined();
    expect(decodedEmpty.desiredWorkspaceIds.workspaceIds).toEqual([]);
    expect(create(schema).expectedBackendGeneration).toBeUndefined();
    for (const schema of [
      binding.GatewayRuntimeRestartResultDTOSchema,
      binding.GatewayRuntimeStateResultDTOSchema,
      binding.GatewayWorkspaceDTOSchema,
      binding.FederationWorkspaceDTOSchema,
    ]) {
      const value = create(schema, {
        backendGeneration: "generation-current",
        affectedWorkspaceIds: ["gateway-a", "gateway-b"],
      });
      const decoded = fromBinary(schema, toBinary(schema, value));
      expect(decoded.backendGeneration).toBe("generation-current");
      expect(decoded.affectedWorkspaceIds).toEqual(["gateway-a", "gateway-b"]);
      expect(create(schema).backendGeneration).toBeUndefined();
    }
    const value = create(binding.GatewayRuntimeRestartResultDTOSchema, {
      status: "blocked", backendGeneration: "generation-current",
      affectedWorkspaceIds: ["gateway-a", "gateway-b"],
      blockers: [{ kind: "proxy_stream", workspaceId: "gateway-b", resourceId: "route-b", status: "active" }],
    });
    const decodedResult = fromBinary(
      binding.GatewayRuntimeRestartResultDTOSchema,
      toBinary(binding.GatewayRuntimeRestartResultDTOSchema, value),
    );
    expect(decodedResult.blockers[0].workspaceId).toBe("gateway-b");
    expect(decodedResult.status).toBe("blocked");
  }
});


test("Node 与 Web 的独立文本写入 destination 保留固定 URI", async () => {
  for (const binding of [
    await import("../../src/workspace-services/protocol/generated/boxteam/workspace/v2/public_pb.js"),
    await import("../../src/clients/web/src/types/protocol_buf_generated/boxteam/workspace/v2/public_pb.js"),
  ]) {
    const schema = binding.CommunicationGatewayRequestSchema;
    expect(schema.fields.find((field) => field.name === "mutation_target")?.number).toBe(7);
    const value = create(schema, { operation: "file_write", destination: { case: "mutationTarget", value: {
      gatewayId: "gateway-target", uri: "boxteam://gateway-target/workspace/ws/resources/files/root", mutationKind: "text",
    } } });
    const decoded = fromBinary(schema, toBinary(schema, value));
    expect(decoded.destination.case).toBe("mutationTarget");
    expect(decoded.destination.value.mutationKind).toBe("text");
    expect(decoded.destination.value.uri).toBe(value.destination.value.uri);
    const mutation = create(binding.TextMutationRequestSchema, { command: { case: "create", value: {
      target: "boxteam://workspace/ws/resources/files/root/测试.txt", expectedVersion: "absent", content: "原件\r\n",
    } }, timeoutMs: 2700 });
    const roundtrip = fromBinary(binding.TextMutationRequestSchema, toBinary(binding.TextMutationRequestSchema, mutation));
    expect(roundtrip.command.case).toBe("create");
    expect(roundtrip.command.value.content).toBe("原件\r\n");
    expect(roundtrip.timeoutMs).toBe(2700);
  }
});
