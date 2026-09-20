import { afterEach, describe, expect, test } from "bun:test";
import { createNodeDebugSyncChannel } from "./nodeDebugSync";

interface FakeChannelInstance {
  onmessage: ((event: MessageEvent) => void) | null;
  posted: unknown[];
  closed: boolean;
}

const originalBroadcastChannel = globalThis.BroadcastChannel;

afterEach(() => {
  globalThis.BroadcastChannel = originalBroadcastChannel;
});

describe("跨窗口调试状态同步", () => {
  test("只刷新同一 workspace 与 session，并广播本地调试动作", () => {
    const instances: FakeChannelInstance[] = [];
    class FakeBroadcastChannel {
      onmessage: ((event: MessageEvent) => void) | null = null;
      posted: unknown[] = [];
      closed = false;

      constructor(_name: string) {
        instances.push(this);
      }

      postMessage(value: unknown): void {
        this.posted.push(value);
      }

      close(): void {
        this.closed = true;
      }
    }
    globalThis.BroadcastChannel = FakeBroadcastChannel as unknown as typeof BroadcastChannel;
    let refreshCount = 0;
    const sync = createNodeDebugSyncChannel("workspace-a", "session-a", "main", () => {
      refreshCount += 1;
    });

    sync.publish();
    expect(instances[0].posted).toEqual([{
      workspaceId: "workspace-a",
      sessionId: "session-a",
      threadId: "main",
    }]);
    instances[0].onmessage?.({
      data: { workspaceId: "workspace-a", sessionId: "session-b", threadId: "main" },
    } as MessageEvent);
    // 同一 session 的其它调试 owner 不能触发当前窗口刷新。
    instances[0].onmessage?.({
      data: { workspaceId: "workspace-a", sessionId: "session-a", threadId: "child-1" },
    } as MessageEvent);
    instances[0].onmessage?.({
      data: { workspaceId: "workspace-a", sessionId: "session-a", threadId: "main" },
    } as MessageEvent);
    expect(refreshCount).toBe(1);

    sync.close();
    expect(instances[0].closed).toBe(true);
  });
});
