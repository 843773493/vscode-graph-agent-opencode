import type { ToolCatalogItem, ToolSelectionChange } from "../../types/toolTesting";

export function applyToolSelectionChanges(
  tools: ToolCatalogItem[],
  changes: ToolSelectionChange[],
): ToolCatalogItem[] {
  const changesById = new Map(
    changes.map((change) => [change.tool_id, change]),
  );
  return tools.map((tool) => {
    const change = changesById.get(tool.tool_id);
    return change === undefined
      ? tool
      : {
          ...tool,
          execution_enabled: change.execution_enabled,
          model_visible: change.model_visible,
        };
  });
}

export function restoreToolSelectionAfterSaveFailure(
  previousTools: ToolCatalogItem[],
  refreshedTools: ToolCatalogItem[] | null,
): ToolCatalogItem[] {
  return refreshedTools ?? previousTools;
}
