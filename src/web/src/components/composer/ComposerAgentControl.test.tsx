import React from "react";
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import ComposerAgentControl from "./ComposerAgentControl";

describe("ComposerAgentControl", () => {
  test("为每个 Agent 渲染独立的工作区默认按钮", () => {
    const html = renderToStaticMarkup(
      <ComposerAgentControl
        controlRef={React.createRef<HTMLDivElement>()}
        agents={[
          {
            agent_id: "coder",
            name: "Coding Assistant",
            description: "实现代码",
            model: "primary",
            tools: [],
            capabilities: [],
            providers: [],
            workspace_default: true,
          },
          {
            agent_id: "reviewer",
            name: "Code Reviewer",
            description: "审查代码",
            model: "primary",
            tools: [],
            capabilities: [],
            providers: [],
            workspace_default: false,
          },
        ]}
        currentAgent="reviewer"
        open
        onToggle={() => undefined}
        onClose={() => undefined}
        onSelect={() => undefined}
        onSetWorkspaceDefault={() => undefined}
        onKeyDown={() => undefined}
      />,
    );

    expect(html).toContain("Coding Assistant 已是工作区默认 Agent");
    expect(html).toContain("将 Code Reviewer 设为工作区默认 Agent");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-checked="true"');
  });
});
