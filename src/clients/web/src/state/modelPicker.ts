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
  /** 点击输入区时锚定原按钮；快捷入口省略此项。 */
  anchor?: HTMLElement;
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

/** 仅收藏改变分组；切换当前模型和最近使用不移动目录项。 */
export function modelPickerGroups(models: PickerModel[], currentModel: ModelReference | null,
  preferences: ModelPreferences, showHidden: boolean): { kind: "favorites" | "provider"; providerId?: string; models: PickerModel[] }[] {
  const remaining = new Map(models.filter(model => showHidden || sameModel(model.model_ref, currentModel)
    || !preferences.hidden.includes(modelPreferenceId(model.model_ref))).map(model => [modelPreferenceId(model.model_ref), model]));
  const groups: ReturnType<typeof modelPickerGroups> = [];
  const favorites = [...remaining.values()].filter(model => preferences.favorites.includes(modelPreferenceId(model.model_ref)));
  if (favorites.length) {
    groups.push({ kind: "favorites", models: favorites });
    for (const model of favorites) remaining.delete(modelPreferenceId(model.model_ref));
  }
  for (const [providerId, candidates] of Map.groupBy([...remaining.values()], model => model.model_ref.provider_id)) {
    groups.push({ kind: "provider", providerId, models: candidates });
  }
  return groups;
}
