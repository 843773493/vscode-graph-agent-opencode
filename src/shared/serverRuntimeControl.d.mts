import type { IncomingMessage, ServerResponse } from 'node:http';

export function verifyRuntimeControlToken(token: unknown, expected: string | undefined): void;
export function verifyLocalRuntimeControlRequest(request: IncomingMessage): void;
export function handleRuntimeLoggingRequest(request: IncomingMessage, response: ServerResponse, options: {
  pathname: string | undefined;
  service: string;
  readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
  token?: string;
}): Promise<boolean>;

export function handleRuntimeShutdownRequest(request: IncomingMessage, response: ServerResponse, options: {
  pathname: string | undefined;
  workspaceId: string;
  readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
  shutdown: () => Promise<void>;
  logger: { error(...values: unknown[]): void };
}): Promise<boolean>;
