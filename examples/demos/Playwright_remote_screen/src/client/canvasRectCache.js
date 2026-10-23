export function createCanvasRectCache({ readRect, requestFrame, cancelFrame }) {
  let cachedRect = null;
  let refreshFrameId = null;

  function refresh() {
    if (refreshFrameId !== null) {
      cancelFrame(refreshFrameId);
      refreshFrameId = null;
    }
    cachedRect = null;
    cachedRect = readRect();
    return cachedRect;
  }

  function scheduleRefresh() {
    if (refreshFrameId !== null) {
      return;
    }
    refreshFrameId = requestFrame(() => {
      refreshFrameId = null;
      cachedRect = null;
      cachedRect = readRect();
    });
  }

  function getRect() {
    if (cachedRect === null || refreshFrameId !== null) {
      return refresh();
    }
    return cachedRect;
  }

  function dispose() {
    if (refreshFrameId !== null) {
      cancelFrame(refreshFrameId);
      refreshFrameId = null;
    }
    cachedRect = null;
  }

  return { dispose, getRect, refresh, scheduleRefresh };
}
