import type {
  ToolDTO,
  ToolSelectionChange as ToolSelectionChangeDTO,
  ToolTestAttemptDTO,
  ToolTestProviderResultDTO,
  ToolTestRunDTO,
  ToolTestRunListDTO,
} from "./protocol_generated/boxteam/workspace/v2/public";

export type ToolKind = "default" | "collaboration" | "extension" | "debugging";
export type ToolOrigin = "builtin" | "custom" | "mcp";
export type ToolTestStatus = ToolTestRunDTO["status"];

export type ToolCatalogItem = Omit<
  ToolDTO,
  | "parameters"
  | "origin"
  | "group_id"
  | "group_name"
  | "kind"
  | "execution_enabled"
  | "model_visible"
  | "test_supported"
> & {
  parameters: Record<string, unknown>;
  origin: ToolOrigin;
  group_id: string;
  group_name: string;
  kind: ToolKind;
  execution_enabled: boolean;
  model_visible: boolean;
  test_supported: boolean;
};

export type ToolSelectionChange = ToolSelectionChangeDTO;
export type ToolTestAttempt = ToolTestAttemptDTO;
export type ToolTestProviderResult = Required<ToolTestProviderResultDTO>;
export type ToolTestRun = Omit<ToolTestRunDTO, "progress" | "providers" | "attempts"> & {
  progress: number;
  providers: ToolTestProviderResult[];
  attempts: ToolTestAttempt[];
};
export type ToolTestRunList = Omit<ToolTestRunListDTO, "items"> & {
  items: ToolTestRun[];
};
