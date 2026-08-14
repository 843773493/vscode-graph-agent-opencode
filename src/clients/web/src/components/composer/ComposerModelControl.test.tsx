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
          },
          {
            provider_id: "backup",
            model: "model-backup",
            custom_llm_provider: "openrouter",
            workspace_default: false,
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
});
