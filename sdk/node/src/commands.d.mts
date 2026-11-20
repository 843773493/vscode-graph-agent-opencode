import type { PluginCommandCallRequest } from "./generated-json/boxteam/gateway/v2/plugin_commands.js";

export function createPluginCommands(
  identity: Readonly<Record<string, unknown>>,
  handlers?: Readonly<Record<string, (arguments_: Record<string, unknown>, context: {
    call: Readonly<PluginCommandCallRequest>;
    requestId: string;
    signal: AbortSignal;
    parameters: Readonly<Record<string, unknown>>;
    progress(completed: number, total?: number, message?: string): Promise<void>;
  }) => Promise<unknown>>>,
  parameters?: Readonly<Record<string, unknown>>,
): {
  registrations: { instance_id: string; commands: readonly { command_id: string; handler: string; registration_id: string }[] };
  handle(request: unknown, response: unknown, pathname: string, readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>, logger: { error(...values: unknown[]): void }): Promise<void>;
};
