const NODE_DEBUG_SYNC_CHANNEL = "boxteam-node-debug-state";

export interface NodeDebugSyncMessage {
  workspaceId: string | null;
  sessionId: string;
}

export interface NodeDebugSyncChannel {
  publish: () => void;
  close: () => void;
}

export function createNodeDebugSyncChannel(
  workspaceId: string | null,
  sessionId: string | null,
  onRemoteChange: () => void,
): NodeDebugSyncChannel {
  if (!sessionId || typeof BroadcastChannel === "undefined") {
    return { publish: () => undefined, close: () => undefined };
  }
  const channel = new BroadcastChannel(NODE_DEBUG_SYNC_CHANNEL);
  channel.onmessage = (event: MessageEvent<NodeDebugSyncMessage>) => {
    if (
      event.data?.sessionId === sessionId
      && event.data.workspaceId === workspaceId
    ) {
      onRemoteChange();
    }
  };
  return {
    publish: () => channel.postMessage({ workspaceId, sessionId }),
    close: () => channel.close(),
  };
}

