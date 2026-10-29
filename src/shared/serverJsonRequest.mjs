/** 辅助服务的 HTTP／WebSocket 共用 JSON 解析，原文只进入原服务诊断。 */
/** @param {string} raw
 * @param {{ logger: { error(...values: unknown[]): void }, transport: string, stage: string,
 *   reviver?: (key: string, value: unknown) => unknown }} context
 */
export function parseJsonInput(raw, { logger, transport, stage, reviver }) {
  try {
    return JSON.parse(raw, reviver);
  } catch (cause) {
    if (!(cause instanceof SyntaxError)) throw cause;
    const label = `${transport} JSON 请求解析失败`;
    const error = new SyntaxError(`${label}: stage=${stage}, error=SyntaxError`, { cause });
    try {
      logger.error(label, { stage }, cause);
    } catch (diagnosticError) {
      // 日志失败不能覆盖解析失败；两份原始原因仍可由宿主诊断核对。
      error.cause = new AggregateError([cause, diagnosticError], "请求解析与诊断输出均失败", { cause });
    }
    throw error;
  }
}

/** @param {{ error(...values: unknown[]): void }} logger */
export function createJsonRequestReader(logger) {
  /** @param {AsyncIterable<Uint8Array>} request */
  return async function readJson(request) {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString("utf8");
    if (!raw.trim()) return {};
    return parseJsonInput(raw, { logger, transport: "HTTP", stage: "request_json" });
  };
}
