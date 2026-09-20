const NODE_DEBUG_SYNC_CHANNEL = "boxteam-node-debug-state";

export interface NodeDebugSyncMessage {
  workspaceId: string | null;
  sessionId: string;
  threadId: string;
}

export interface NodeDebugSyncChannel {
  publish: () => void;
  close: () => void;
}

export function createNodeDebugSyncChannel(
  workspaceId: string | null,
  sessionId: string | null,
  threadId: string,
  onRemoteChange: () => void,
): NodeDebugSyncChannel {
  if (!sessionId || typeof BroadcastChannel === "undefined") {
    return { publish: () => undefined, close: () => undefined };
  }
  const channel = new BroadcastChannel(`${NODE_DEBUG_SYNC_CHANNEL}:${workspaceId ?? ""}:${sessionId}:${threadId}`);
  channel.onmessage = (event: MessageEvent<NodeDebugSyncMessage>) => {
    if (
      event.data?.sessionId === sessionId
      && event.data.workspaceId === workspaceId
      && event.data.threadId === threadId
    ) {
      onRemoteChange();
    }
  };
  return {
    publish: () => channel.postMessage({ workspaceId, sessionId, threadId }),
    close: () => channel.close(),
  };
}
