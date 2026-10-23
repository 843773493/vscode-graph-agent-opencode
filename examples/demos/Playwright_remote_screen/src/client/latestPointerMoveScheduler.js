export function createLatestPointerMoveScheduler({ requestFrame, cancelFrame, send }) {
  let pendingMessage = null;
  let frameId = null;

  function flush() {
    if (frameId !== null) {
      cancelFrame(frameId);
      frameId = null;
    }
    const message = pendingMessage;
    pendingMessage = null;
    if (message) {
      send(message);
    }
  }

  function schedule(message) {
    pendingMessage = message;
    if (frameId !== null) {
      return;
    }
    frameId = requestFrame(() => {
      frameId = null;
      const latestMessage = pendingMessage;
      pendingMessage = null;
      if (latestMessage) {
        send(latestMessage);
      }
    });
  }

  function clear() {
    if (frameId !== null) {
      cancelFrame(frameId);
      frameId = null;
    }
    pendingMessage = null;
  }

  return { schedule, flush, clear };
}
