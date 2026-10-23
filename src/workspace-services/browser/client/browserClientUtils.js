export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function createOpaqueId(prefix, cryptoSource = globalThis.crypto) {
  if (!cryptoSource || typeof cryptoSource.getRandomValues !== "function") {
    throw new Error("当前浏览器不支持生成连接参与者 ID 所需的 crypto.getRandomValues");
  }
  const bytes = cryptoSource.getRandomValues(new Uint8Array(16));
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${prefix}_${value}`;
}

export function shortUrlLabel(value) {
  if (!value || value === "about:blank") {
    return "about:blank";
  }
  if (value.startsWith("data:")) {
    return "data:...";
  }
  try {
    const url = new URL(value);
    if (url.origin === "null") {
      return value.length > 64 ? `${value.slice(0, 64)}...` : value;
    }
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

export function backendWsUrl(backendBaseUrl, token = null) {
  const url = new URL(backendBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/$/, "")}/browser`;
  url.search = token ? `?token=${encodeURIComponent(token)}` : "";
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

/**
 * 从本地画布显示位置换算指针坐标，缩放和远端 DPR 不直接作为输入单位。
 * @param {HTMLCanvasElement} canvas width/height 是画布后备像素，不一定等于 CSS 视口。
 * @param {Pick<MouseEvent, "clientX" | "clientY">} event 当前 attach 页面的 CSS 像素坐标。
 * @param {{width: number, height: number}|null} [remoteViewportCss=null] 正式输入链传入远端 CSS 视口；省略时按画布后备像素返回。
 * @returns {{x: number, y: number}} 单位随目标尺寸，正式输入链为远端 CSS 像素。
 */
export function remotePointFromEvent(canvas, event, remoteViewportCss = null) {
  const canvasDisplayRect = canvas.getBoundingClientRect();
  if (canvasDisplayRect.width <= 0 || canvasDisplayRect.height <= 0) {
    throw new Error("canvas 尺寸无效，无法映射指针坐标");
  }
  const width = remoteViewportCss?.width || canvas.width;
  const height = remoteViewportCss?.height || canvas.height;
  return {
    x: clamp(((event.clientX - canvasDisplayRect.left) / canvasDisplayRect.width) * width, 0, width),
    y: clamp(((event.clientY - canvasDisplayRect.top) / canvasDisplayRect.height) * height, 0, height),
  };
}

export function sameViewport(left, right) {
  return left && right && left.width === right.width && left.height === right.height;
}
