import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import ComposerModelControl from "./ComposerModelControl";

describe("ComposerModelControl", () => {
  test("展示当前实际模型和可选 provider", () => {
    const html = renderToStaticMarkup(
      <ComposerModelControl
        controlRef={React.createRef<HTMLDivElement>()}
        providers={[
          {
            provider_id: "primary",
            model: "model-primary",
            custom_llm_provider: "openai",
            workspace_default: true,
            available: true,
          },
          {
            provider_id: "backup",
            model: "model-backup",
            custom_llm_provider: "openrouter",
            workspace_default: false,
            available: true,
          },
        ]}
        currentProviderId="backup"
        open
        disabled={false}
        onToggle={() => undefined}
        onClose={() => undefined}
        onSelect={() => undefined}
        onSetWorkspaceDefault={() => undefined}
        onKeyDown={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="选择模型，当前：model-backup"');
    expect(html).toContain("model-primary");
    expect(html).toContain("primary · openai");
    expect(html).toContain("model-primary 已是工作区默认模型");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-checked="true"');
    expect(html).toContain("backup · openrouter");
  });

  test("在对应模型按钮内展示配置错误并禁止选择和设为默认", () => {
    const html = renderToStaticMarkup(
      <ComposerModelControl
        controlRef={React.createRef<HTMLDivElement>()}
        providers={[
          {
            provider_id: "backup_4",
            model: "gpt-5.6-luna",
            custom_llm_provider: "chatgpt",
            workspace_default: false,
            available: false,
            configuration_error: "Codex 认证缺少可用账号信息",
          },
        ]}
        currentProviderId="backup_4"
        open
        disabled={false}
        onToggle={() => undefined}
        onClose={() => undefined}
        onSelect={() => undefined}
        onSetWorkspaceDefault={() => undefined}
        onKeyDown={() => undefined}
      />,
    );

    expect(html).toContain("配置错误：Codex 认证缺少可用账号信息");
    expect(html).toContain("composer-model-pill configuration-error");
    expect(html.match(/disabled=""/g)?.length).toBe(2);
    expect(html).toContain('role="alert"');
  });
});
