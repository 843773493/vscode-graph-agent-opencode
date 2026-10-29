/** Terminal 和 Browser 共用 HTTP JSON 读取，解析原文只进入原服务诊断。 */
/** @param {{ error(...values: unknown[]): void }} logger */
export function createJsonRequestReader(logger) {
  /** @param {AsyncIterable<Uint8Array>} request */
  return async function readJson(request) {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString("utf8");
    if (!raw.trim()) return {};
    try {
      return JSON.parse(raw);
    } catch (cause) {
      if (!(cause instanceof SyntaxError)) throw cause;
      const error = new SyntaxError("HTTP JSON 请求解析失败: stage=request_json, error=SyntaxError", { cause });
      try {
        logger.error("HTTP JSON 请求解析失败", { stage: "request_json" }, cause);
      } catch (diagnosticError) {
        // 日志失败不能覆盖解析失败；两份原始原因仍可由宿主诊断核对。
        error.cause = new AggregateError([cause, diagnosticError], "请求解析与诊断输出均失败", { cause });
      }
      throw error;
    }
  };
}
