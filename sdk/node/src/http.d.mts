import type { IncomingMessage, Server, ServerResponse } from "node:http";

export function createJsonRequestReader(logger: { error(...values: unknown[]): void }): (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
export function parseJsonInput(input: string, options: { transport: string; stage: string;
  logger: { error(...values: unknown[]): void }; reviver?: (this: unknown, key: string, value: unknown) => unknown }): unknown;
export function closeHttpServer(server: Server): Promise<void>;
