import type {
  ToolDTO,
  ToolSelectionChange as ToolSelectionChangeDTO,
} from "./gen/tool";
import type {
  ToolTestAttemptDTO,
  ToolTestProviderResultDTO,
  ToolTestRunDTO,
  ToolTestRunListDTO,
} from "./gen/tool_test";

export type ToolKind = "default" | "collaboration" | "extension";
export type ToolTestStatus = ToolTestRunDTO["status"];

export type ToolCatalogItem = Omit<
  ToolDTO,
  "parameters" | "group_id" | "group_name" | "kind" | "enabled" | "test_supported"
> & {
  parameters: Record<string, unknown>;
  group_id: string;
  group_name: string;
  kind: ToolKind;
  enabled: boolean;
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
