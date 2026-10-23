// PTY文本的UTF-8字节边界：只负责保留尾部和事件切块，不处理命令标记。
const MAX_TERMINAL_BUFFER_BYTES = 256 * 1024;
const MAX_TERMINAL_OUTPUT_EVENT_BYTES = 64 * 1024;

export function trimTerminalBuffer(value) {
  if (value.length <= MAX_TERMINAL_BUFFER_BYTES
    && Buffer.byteLength(value, "utf8") <= MAX_TERMINAL_BUFFER_BYTES) {
    return value;
  }
  // UTF-8每个UTF-16单元至少占1字节，最终尾部只可能来自最后这么多个单元。
  // 切入代理对时，开头孤立代理项的3字节也必超限，会被下面的边界对齐丢弃。
  const buffer = Buffer.from(value.slice(-MAX_TERMINAL_BUFFER_BYTES), "utf8");
  let start = buffer.length - MAX_TERMINAL_BUFFER_BYTES;
  // 从下一完整字符开始，避免解码时引入替换字符并再次超过字节上限。
  while ((buffer[start] & 0xc0) === 0x80) {
    start += 1;
  }
  return buffer.toString("utf8", start);
}

export function splitTerminalOutputChunks(value) {
  if (value.length === 0) {
    return [];
  }
  if (value.length <= MAX_TERMINAL_OUTPUT_EVENT_BYTES
    && Buffer.byteLength(value, "utf8") <= MAX_TERMINAL_OUTPUT_EVENT_BYTES) {
    return [value];
  }
  const chunks = [];
  let chunkStart = 0;
  let chunkBytes = 0;
  for (let offset = 0; offset < value.length;) {
    const codePoint = value.codePointAt(offset);
    // 孤立代理项按UTF-8的3字节计费，正文仍从原字符串切片保留。
    const characterBytes = codePoint < 0x80 ? 1
      : codePoint < 0x800 ? 2 : codePoint < 0x10000 ? 3 : 4;
    if (chunkBytes + characterBytes > MAX_TERMINAL_OUTPUT_EVENT_BYTES) {
      chunks.push(value.slice(chunkStart, offset));
      chunkStart = offset;
      chunkBytes = 0;
    }
    chunkBytes += characterBytes;
    offset += codePoint > 0xffff ? 2 : 1;
  }
  chunks.push(value.slice(chunkStart));
  return chunks;
}
