import type { IncomingMessage, ServerResponse } from 'node:http';

export function verifyRuntimeLoggingToken(token: unknown, expected: string | undefined): void;
export function handleRuntimeLoggingRequest(request: IncomingMessage, response: ServerResponse, options: {
  pathname: string | undefined;
  service: string;
  readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
}): Promise<boolean>;
