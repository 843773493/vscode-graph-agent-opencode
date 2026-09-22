function stripReadFileLineNumber(line: string): string {
  return line.replace(/^\s*\d+\s*/, "").trim();
}

export function allowedToolsFromSkillMarkdownText(text: string): string[] {
  const normalizedText = text
    .split(/\r?\n/)
    .map(stripReadFileLineNumber)
    .join("\n");
  const frontmatter = normalizedText.match(/^\s*---\s*\n([\s\S]*?)\n---/);
  if (!frontmatter) {
    return [];
  }

  const tools: string[] = [];
  for (const line of frontmatter[1].split(/\r?\n/)) {
    const match = line.trim().match(/^allowed-tools:\s*(.+)$/);
    if (!match) {
      continue;
    }
    tools.push(
      ...match[1]
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    );
  }
  return tools;
}
