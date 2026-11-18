import type { Agent, ModelReference } from "../types/backend";
import { sameModel } from "../utils/modelReference";

export type PickerModel = Agent["models"][number];
export interface ModelPreferences {
  favorites: string[];
  hidden: string[];
  recent: string[];
}
export const EMPTY_MODEL_PREFERENCES: ModelPreferences = { favorites: [], hidden: [], recent: [] };
export const modelPreferenceId = (model: ModelReference): string => JSON.stringify([model.provider_id, model.model_id]);

export interface ModelPickerRequest {
  loadModels(): Promise<PickerModel[]>;
  currentModel: ModelReference | null;
  onSelect(model: ModelReference): Promise<void>;
  onSetWorkspaceDefault(model: ModelReference): Promise<void>;
  onManage?(model?: ModelReference): void;
}

export function updateModelPreference(current: ModelPreferences, model: ModelReference,
  action: "favorite" | "hide" | "use"): ModelPreferences {
  const id = modelPreferenceId(model);
  if (action === "use") return { ...current, recent: [id, ...current.recent.filter(item => item !== id)].slice(0, 10) };
  const field = action === "favorite" ? "favorites" : "hidden";
  return { ...current, [field]: current[field].includes(id)
    ? current[field].filter(item => item !== id) : [...current[field], id] };
}

/** 偏好只排列当前 Agent 已公布的候选；隐藏当前项仍保留其选择入口。 */
export function modelPickerGroups(models: PickerModel[], currentModel: ModelReference | null,
  preferences: ModelPreferences, showHidden: boolean): { kind: "current" | "favorites" | "recent" | "provider"; providerId?: string; models: PickerModel[] }[] {
  const remaining = new Map(models.filter(model => showHidden || sameModel(model.model_ref, currentModel)
    || !preferences.hidden.includes(modelPreferenceId(model.model_ref))).map(model => [modelPreferenceId(model.model_ref), model]));
  const groups: ReturnType<typeof modelPickerGroups> = [];
  const take = (kind: "current" | "favorites" | "recent", ids: string[]) => {
    const selected: PickerModel[] = [];
    for (const id of ids) {
      const model = remaining.get(id);
      if (model) { selected.push(model); remaining.delete(id); }
    }
    if (selected.length) groups.push({ kind, models: selected });
  };
  take("current", currentModel ? [modelPreferenceId(currentModel)] : []);
  take("favorites", preferences.favorites);
  take("recent", preferences.recent);
  for (const [providerId, candidates] of Map.groupBy([...remaining.values()], model => model.model_ref.provider_id)) {
    groups.push({ kind: "provider", providerId, models: candidates });
  }
  return groups;
}
