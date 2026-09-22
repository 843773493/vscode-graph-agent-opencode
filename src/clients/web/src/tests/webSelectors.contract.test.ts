import { describe, expect, test } from "bun:test";

/**
 * tests/clients/selectors/web-selectors.mjs 是跨客户端测试共享的选择器唯一来源。
 * 一个选择器如果在 Web 源码里找不到任何对应属性，场景会在 locator().waitFor()
 * 处超时失败，却不说明是选择器失效还是页面没渲染，属于静默漂移。本测试把每个
 * 选择器钉到实际承载它的源码文件，缺失或改名都会明确报出。
 */
const webSourceRoot = new URL("..", import.meta.url);
const selectorFile = new URL("../../../../tests/clients/selectors/web-selectors.mjs", webSourceRoot);

/** 每个选择器对应的 Web 源码文件；新增选择器必须在此登记，否则用例失败。 */
const selectorAnchors: Record<string, string> = {
  appRoot: "../index.html",
  composer: "./components/composer/Composer.tsx",
  sendButton: "./components/composer/ComposerActionButtons.tsx",
  sessionTree: "./components/agentSessions/AgentSessionsSessionTree.tsx",
};

async function readSource(relativePath: string): Promise<string> {
  return await Bun.file(new URL(relativePath, webSourceRoot)).text();
}

async function readSelectorEntries(): Promise<Array<[string, string]>> {
  const source = await Bun.file(selectorFile).text();
  // 值可能用单引号包裹（内部含 data-testid 的双引号），也可能用双引号。
  return [...source.matchAll(/^\s*(\w+):\s*(?:"([^"]*)"|'([^']*)'),?$/gm)]
    .map((match) => [match[1], match[2] ?? match[3]] as [string, string]);
}

describe("跨客户端 Web 选择器与源码属性一致", () => {
  test("web-selectors.mjs 的每个选择器都在源码中真实存在", async () => {
    const entries = await readSelectorEntries();
    expect(entries.length).toBeGreaterThan(0);

    for (const [name, selector] of entries) {
      const anchor = selectorAnchors[name];
      expect(anchor, `选择器 ${name} 未在 selectorAnchors 登记源码锚点`).toBeDefined();
      const source = await readSource(anchor);
      // 只支持 id 与 data-testid 两类稳定选择器；CSS 层级选择器不应进入共享选择器表。
      const match = /^#([\w-]+)$/.exec(selector) ?? /^\[data-testid="([^"]+)"\]$/.exec(selector);
      expect(match, `选择器 ${name} 不是受支持的稳定形式: ${selector}`).not.toBeNull();
      const isId = selector.startsWith("#");
      const expected = isId ? `id="${match![1]}"` : `data-testid="${match![1]}"`;
      expect(source, `${name} (${selector}) 在 ${anchor} 中找不到`).toContain(expected);
    }
  });

  test("selectorAnchors 不保留已失效的僵尸登记", async () => {
    const names = new Set((await readSelectorEntries()).map(([name]) => name));
    for (const registered of Object.keys(selectorAnchors)) {
      expect(names, `selectorAnchors 中的 ${registered} 已不在 web-selectors.mjs 中`).toContain(registered);
    }
  });
});
