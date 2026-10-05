/** 生成符合后端 UUIDv7 profile 的目录 operation identity。 */
export function createSessionCatalogOperationId(): string {
  const source = globalThis.crypto;
  if (!source?.getRandomValues) {
    throw new Error("当前浏览器没有安全随机源，无法生成会话目录 operation ID");
  }
  const timestamp = Date.now();
  if (!Number.isSafeInteger(timestamp) || timestamp < 0 || timestamp > 0xffffffffffff) {
    throw new Error(`UUIDv7 时间戳超出 48 位范围: ${timestamp}`);
  }

  const bytes = source.getRandomValues(new Uint8Array(16));
  let remaining = timestamp;
  for (let index = 5; index >= 0; index -= 1) {
    bytes[index] = remaining & 0xff;
    remaining = Math.floor(remaining / 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return `op_${[...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
