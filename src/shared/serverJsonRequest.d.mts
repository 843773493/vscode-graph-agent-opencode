export interface JsonRequestLogger {
  error(...values: unknown[]): void;
}

export function parseJsonInput(raw: string, context: {
  logger: JsonRequestLogger;
  transport: string;
  stage: string;
  reviver?: (key: string, value: unknown) => unknown;
}): unknown;
export function createJsonRequestReader(logger: JsonRequestLogger):
  (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
