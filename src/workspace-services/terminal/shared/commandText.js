// 命令协议的纯文本解析；不持有Session、PTY、输出缓冲或运行状态。
function normalizeTerminalLines(value) {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

// 只有同一命令的开始/结束标记同时存在时，才交付完整标记对。
export function commandMarkersFromInput(data) {
  const startMatch = data.match(/__BOXTEAM_CMD_START_([a-f0-9]+)__/);
  const doneMatch = data.match(/__BOXTEAM_CMD_DONE_([a-f0-9]+)__/);
  if (!startMatch || !doneMatch || startMatch[1] !== doneMatch[1]) {
    return { startMarker: null, doneMarker: null };
  }
  return {
    startMarker: startMatch[0],
    doneMarker: doneMatch[0],
  };
}

// 输入标签取最后一个非空行并去掉首尾空白，不解析或执行shell语法。
export function lastNonEmptyInputLine(data) {
  // 不为被丢弃的行构造数组；长单行先走原生查询，避免两次逆向扫描。
  const trimmedInput = data.trimEnd();
  if (!trimmedInput.includes("\n") && !trimmedInput.includes("\r")) return trimmedInput.trimStart() || null;
  const lineStart = Math.max(trimmedInput.lastIndexOf("\n"), trimmedInput.lastIndexOf("\r")) + 1;
  return trimmedInput.slice(lineStart).trim() || null;
}

function stripAnsi(value) {
  // OSC止于BEL或首个ESC反斜杠；不跨下一条转义吞正文或反复回溯未闭合前缀。
  return value
    .replace(/\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)/g, "")
    .replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "");
}

// 在归一换行并去除ANSI的副本中取最后匹配的退出码，不修改原始输出。
// doneMarker限定当前命令；缺省时按协议匹配，用于恢复记录中的状态推导。
export function latestCommandExitCode(buffer, doneMarker = null) {
  const markerPattern = doneMarker
    ? doneMarker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    : "__BOXTEAM_CMD_DONE_[a-f0-9]+__";
  const normalizedBuffer = stripAnsi(normalizeTerminalLines(buffer));
  const pattern = new RegExp(`${markerPattern}:(\\d+)`, "g");
  let latestExitCodeText = null;
  let match;
  while ((match = pattern.exec(normalizedBuffer)) !== null) {
    latestExitCodeText = match[1];
  }
  return latestExitCodeText === null ? null : Number(latestExitCodeText);
}

// 展示态删除包含协议标记的整行；其余行保留原换行与ANSI，原始buffer不变。
export function stripCommandMarkerLines(buffer) {
  const markers = /__BOXTEAM_CMD_(?:START|DONE)_/g;
  const retained = [];
  let retainedStart = 0;
  let match;
  while ((match = markers.exec(buffer)) !== null) {
    const lineStart = buffer.lastIndexOf("\n", match.index) + 1;
    retained.push(buffer.slice(retainedStart, lineStart));
    const lineEnd = buffer.indexOf("\n", match.index);
    retainedStart = lineEnd === -1 ? buffer.length : lineEnd + 1;
    // 跳过整条标记行及其换行；同一行多个标记只处理一次，单独的CR仍属于行正文。
    markers.lastIndex = retainedStart;
  }
  // 无标记时不拆行；有标记时只拼接保留区间，避免为所有普通行分配数组项。
  if (retainedStart === 0) return buffer;
  retained.push(buffer.slice(retainedStart));
  return retained.join("");
}
