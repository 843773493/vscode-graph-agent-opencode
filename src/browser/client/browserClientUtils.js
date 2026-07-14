export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function shortUrlLabel(value) {
  if (!value) {
    return "about:blank";
  }
  if (value.startsWith("data:")) {
    return "data:...";
  }
  try {
    const url = new URL(value);
    const path = `${url.pathname}${url.search}`;
    return `${url.origin}${path.length > 48 ? `${path.slice(0, 48)}...` : path}`;
  } catch {
    return value.length > 64 ? `${value.slice(0, 64)}...` : value;
  }
}

export function statusLabel(status) {
  const labels = {
    created: "已创建",
    running: "运行中",
    closed: "已关闭",
    deleted: "已删除",
    failed: "失败",
    lost: "已断开",
  };
  return labels[status] || status || "未知";
}

export function backendWsUrl(backendBaseUrl) {
  const url = new URL(backendBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/browser";
  url.search = "";
  return url.toString();
}

export function modifiersFromEvent(event) {
  return {
    alt: event.altKey,
    ctrl: event.ctrlKey,
    meta: event.metaKey,
    shift: event.shiftKey,
  };
}

export function pointerButtonName(button) {
  if (button === 0) return "left";
  if (button === 1) return "middle";
  if (button === 2) return "right";
  return "none";
}

export function remotePointFromEvent(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    throw new Error("canvas 尺寸无效，无法映射指针坐标");
  }
  return {
    x: clamp(((event.clientX - rect.left) / rect.width) * canvas.width, 0, canvas.width),
    y: clamp(((event.clientY - rect.top) / rect.height) * canvas.height, 0, canvas.height),
  };
}

export function sameViewport(left, right) {
  return left && right && left.width === right.width && left.height === right.height;
}
