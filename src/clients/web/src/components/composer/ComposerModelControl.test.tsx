import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { act, create } from "react-test-renderer";
import ComposerModelControl from "./ComposerModelControl";

const main = { provider_id: "shared", model_id: "main" };
const backup = { provider_id: "shared", model_id: "backup" };
const models = [main, backup].map(model_ref => ({ model_ref, model: `server-${model_ref.model_id}`,
  custom_llm_provider: "openai", available: true }));

describe("ComposerModelControl", () => {
  test("同 Provider 的当前模型按完整引用展示，选择交给宿主", () => {
    let opened = 0;
    const renderer = create(<ComposerModelControl models={models} currentModel={backup} onOpen={() => opened++} />);
    try {
      expect(renderer.root.findByProps({ className: "composer-model-label" }).children).toEqual(["server-backup"]);
      act(() => renderer.root.findByType("button").props.onClick());
      expect(opened).toBe(1);
      expect(renderer.root.findAllByProps({ role: "menuitemradio" })).toHaveLength(0);
    } finally { act(() => renderer.unmount()); }
  });
  test("失效绑定保留身份与错误，仍可打开宿主重新选择", () => {
    const html = renderToStaticMarkup(<ComposerModelControl models={models}
      currentModel={{ provider_id: "shared", model_id: "removed" }} onOpen={() => {}} />);
    expect(html).toContain("shared / removed");
    expect(html).toContain("composer-model-pill configuration-error");
    expect(html).not.toContain('disabled=""');
  });
  test("显示名不替换服务端模型名和完整身份", () => {
    const html = renderToStaticMarkup(<ComposerModelControl models={[{ ...models[0], name: "通用助手" }]}
      currentModel={main} onOpen={() => {}} />);
    expect(html).toContain('class="composer-model-label">通用助手</span>');
    expect(html).toContain("server-main");
    expect(html).toContain("shared / main");
  });
  test("配置故障保留原始原因", () => {
    const html = renderToStaticMarkup(<ComposerModelControl models={[{ ...models[0], available: false,
      configuration_error: "Cannot find module '/home/alice/missing.js'" }]} currentModel={main} onOpen={() => {}} />);
    expect(html).toContain("Cannot find module");
    expect(html).toContain("/home/alice/missing.js");
  });
});
