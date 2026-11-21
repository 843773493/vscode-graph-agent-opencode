import { expect, test } from "bun:test";
import { isTaggedContextSources, parseTaggedContextSources } from "../support/context_sources.mjs";

test("v3 来源 envelope 必须由完整 source_defined JSON 标签包裹", () => {
  const envelope = { protocol: "boxteam-context-sources:v3", sources: [{ source_identity: "session:one" }] };
  const content = `<context_sources encoding="json" trust="source_defined">\n${JSON.stringify(envelope)}\n</context_sources>`;
  expect(isTaggedContextSources(content)).toBe(true);
  expect(parseTaggedContextSources(content)).toEqual(envelope);
});

test("裸 JSON、标签变体和标签外用户正文不作为来源消息解析", () => {
  const envelope = JSON.stringify({ protocol: "boxteam-context-sources:v3", sources: [] });
  const contents = [envelope,
    `用户正文\n<context_sources encoding="json" trust="source_defined">\n${envelope}\n</context_sources>`,
    `<context_sources encoding="json" trust="user">\n${envelope}\n</context_sources>`,
    `<context_sources encoding="json" trust="source_defined">\n${envelope}\n</context_sources>用户正文`,
    `<context_sources encoding="json" trust="source_defined">\n${envelope}`];
  for (const content of contents) {
    expect(isTaggedContextSources(content)).toBe(false);
    expect(() => parseTaggedContextSources(content)).toThrow();
  }
  const historical = `<context_sources encoding="json" trust="source_defined">\n${envelope.replace(":v3", ":v2")}\n</context_sources>`;
  expect(isTaggedContextSources(historical)).toBe(true);
  expect(() => parseTaggedContextSources(historical)).toThrow();
});
