const openingTag = '<context_sources encoding="json" trust="source_defined">\n';
const closingTag = '\n</context_sources>';

export function isTaggedContextSources(content) {
  return typeof content === "string" && content.startsWith(openingTag) && content.endsWith(closingTag);
}

export function parseTaggedContextSources(content) {
  if (!isTaggedContextSources(content)) {
    throw new Error("上下文来源消息必须完整包含 source_defined JSON 标签");
  }
  const envelope = JSON.parse(content.slice(openingTag.length, -closingTag.length));
  if (envelope?.protocol !== "boxteam-context-sources:v3") {
    throw new Error("source_defined JSON 标签必须包含 v3 来源 envelope");
  }
  return envelope;
}
