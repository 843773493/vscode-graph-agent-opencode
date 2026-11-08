import type { IncomingMessage, ServerResponse } from "node:http";
import type {
  PluginInstanceStatus as GeneratedPluginInstanceStatus,
  PluginServiceContribution,
} from "../clients/web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js";

export type PluginServiceDeclaration = PluginServiceContribution;
export type PluginRuntimeIdentity = Omit<Pick<GeneratedPluginInstanceStatus,
  "gateway_id" | "package_id" | "plugin_id" | "package_version" | "workspace_id" |
  "instance_scope" | "instance_id" | "management_protocol_version" | "extension_contract_version" | "services"
>, "workspace_id" | "services"> & {
  workspace_id: string | null;
  services: readonly PluginServiceDeclaration[];
};

export interface PluginRuntimeEnvironment {
  port: number;
  token: string;
  identity: PluginRuntimeIdentity;
}

export interface PluginRuntimeManagement {
  readonly port: number;
  readonly token: string;
  readonly identity: PluginRuntimeIdentity;
  handleRequest(request: IncomingMessage, response: ServerResponse, pathname: string): Promise<boolean>;
}

export function readPluginRuntimeEnvironment(): PluginRuntimeEnvironment;
export function createPluginRuntimeManagement(options: {
  service: string;
  logger: { error(...values: unknown[]): void };
  readJson: (request: AsyncIterable<Uint8Array>) => Promise<unknown>;
  shutdown: () => void | Promise<void>;
}): PluginRuntimeManagement;
