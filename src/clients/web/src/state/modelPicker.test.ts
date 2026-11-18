import { describe, expect, test } from "bun:test";
import { EMPTY_MODEL_PREFERENCES, modelPickerGroups, modelPreferenceId, updateModelPreference } from "./modelPicker";
import type { ModelReference } from "../types/backend";

const ref = (model_id: string, provider_id = "shared"): ModelReference => ({ provider_id, model_id });
const models = [ref("main"), ref("backup"), ref("other", "second")].map(model_ref => ({ model_ref,
  model: `server-${model_ref.model_id}`, custom_llm_provider: "openai", available: true }));
const visible = (groups: ReturnType<typeof modelPickerGroups>) => groups.flatMap(group => group.models.map(model => model.model_ref));

describe("模型选择展示偏好", () => {
  test("收藏与最近不扩大 Agent 候选，同 Provider 的模型身份独立", () => {
    let preferences = updateModelPreference(EMPTY_MODEL_PREFERENCES, ref("catalog-only"), "favorite");
    preferences = updateModelPreference(preferences, ref("backup"), "favorite");
    preferences = updateModelPreference(preferences, ref("removed"), "use");
    const groups = modelPickerGroups(models, ref("main"), preferences, false);
    expect(groups.map(group => group.kind)).toEqual(["current", "favorites", "provider"]);
    expect(visible(groups)).toEqual(models.map(model => model.model_ref));
    expect(modelPreferenceId(ref("main"))).not.toBe(modelPreferenceId(ref("backup")));
    expect(modelPreferenceId(ref("main"))).not.toBe(modelPreferenceId(ref("main", "second")));
  });
  test("隐藏当前模型保持选中，隐藏其他模型可显式恢复且不更改业务对象", () => {
    let preferences = updateModelPreference(EMPTY_MODEL_PREFERENCES, ref("main"), "hide");
    preferences = updateModelPreference(preferences, ref("backup"), "hide");
    expect(visible(modelPickerGroups(models, ref("main"), preferences, false))).toEqual([ref("main"), ref("other", "second")]);
    expect(visible(modelPickerGroups(models, ref("main"), preferences, true))).toEqual(models.map(model => model.model_ref));
    preferences = updateModelPreference(preferences, ref("backup"), "hide");
    expect(visible(modelPickerGroups(models, ref("main"), preferences, false))).toEqual(models.map(model => model.model_ref));
    expect(models).toHaveLength(3);
  });
  test("最近记录按明确使用顺序去重，切换 Agent 后与新候选取交集", () => {
    let preferences = updateModelPreference(EMPTY_MODEL_PREFERENCES, ref("main"), "use");
    preferences = updateModelPreference(preferences, ref("backup"), "use");
    preferences = updateModelPreference(preferences, ref("main"), "use");
    expect(preferences.recent).toEqual([modelPreferenceId(ref("main")), modelPreferenceId(ref("backup"))]);
    expect(visible(modelPickerGroups(models.slice(1), ref("main"), preferences, false))).toEqual([ref("backup"), ref("other", "second")]);
    expect(EMPTY_MODEL_PREFERENCES.recent).toEqual([]);
  });
  test("失效当前绑定不会借偏好恢复候选", () => {
    const preferences = updateModelPreference(EMPTY_MODEL_PREFERENCES, ref("removed"), "favorite");
    expect(visible(modelPickerGroups(models, ref("removed"), preferences, false))).toEqual(models.map(model => model.model_ref));
  });
});
