import type { IncomingMessage, Server, ServerResponse } from "node:http";
/** 辅助服务的 HTTP／WebSocket 共用 JSON 解析，原文只进入原服务诊断。 */
export function parseJsonInput(raw: string, { logger, transport, stage, reviver }: { transport: string; stage: string;
  logger: { error(...values: unknown[]): void }; reviver?: (this: unknown, key: string, value: unknown) => unknown }): unknown {
  try {
    return JSON.parse(raw, reviver);
  } catch (cause) {
    if (!(cause instanceof SyntaxError)) throw cause;
    const label = `${transport} JSON 请求解析失败`;
    const error = new SyntaxError(`${label}: stage=${stage}, error=SyntaxError`, { cause });
    try {
      logger.error(label, { stage }, cause);
    } catch (diagnosticError) {
      error.cause = new AggregateError([cause, diagnosticError], "请求解析与诊断输出均失败", { cause });
    }
    throw error;
  }
}

export function createJsonRequestReader(logger: { error(...values: unknown[]): void }): (request: AsyncIterable<Uint8Array>) => Promise<unknown> {
  return async function readJson(request: AsyncIterable<Uint8Array>) {
    const chunks: Uint8Array[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString("utf8");
    if (!raw.trim()) return {};
    return parseJsonInput(raw, { logger, transport: "HTTP", stage: "request_json" });
  };
}

export async function closeHttpServer(server: Server): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await new Promise<void>((resolve, reject) => {
      timer = setTimeout(() => {
        reject(Object.assign(new Error("HTTP 服务排空超过 5000 毫秒，关闭未完成的连接"), {
          code: "http_server_drain_timeout",
        }));
        server.closeAllConnections();
      }, 5000);
      server.close(error => error ? reject(error) : resolve());
    });
  } finally {
    clearTimeout(timer);
  }
}
