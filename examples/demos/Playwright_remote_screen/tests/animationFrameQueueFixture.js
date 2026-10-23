export function createAnimationFrameQueueFixture() {
  let nextFrameId = 0;
  const pendingCallbacks = new Map();
  const cancelledFrameIds = [];

  return {
    pendingCallbacks,
    cancelledFrameIds,
    requestFrame(callback) {
      const frameId = ++nextFrameId;
      pendingCallbacks.set(frameId, callback);
      return frameId;
    },
    cancelFrame(frameId) {
      cancelledFrameIds.push(frameId);
      pendingCallbacks.delete(frameId);
    },
    runNext() {
      const nextFrame = pendingCallbacks.entries().next().value;
      if (!nextFrame) {
        throw new Error("没有待执行的动画帧");
      }
      const [frameId, callback] = nextFrame;
      pendingCallbacks.delete(frameId);
      callback();
    },
  };
}
