import { escapeHtml } from './format';

export { escapeHtml };

function linkifyEscapedText(value: string): string {
  const links: string[] = [];
  const withMarkdownLinks = value.replace(
    /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_match, label: string, url: string) => {
      const index = links.length;
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return `@@LINK_${index}@@`;
    },
  );
  const withBareLinks = withMarkdownLinks.replace(
    /\bhttps?:\/\/[^\s<]+/g,
    (url: string) => {
      const cleanUrl = url.replace(/(?:&lt;\/strong&gt;|<\/strong>|[),.;!?，。！？；）*])+$/u, '');
      const suffix = url.slice(cleanUrl.length);
      return `<a href="${cleanUrl}" target="_blank" rel="noopener noreferrer">${cleanUrl}</a>${suffix}`;
    },
  );
  return withBareLinks.replace(/@@LINK_(\d+)@@/g, (_match, index: string) => {
    return links[Number(index)] ?? '';
  });
}

function parseTableRow(line: string): string[] {
  const trimmed = line.trim();
  const normalized = trimmed.startsWith('|') ? trimmed.slice(1) : trimmed;
  const withoutTail = normalized.endsWith('|')
    ? normalized.slice(0, -1)
    : normalized;
  return withoutTail.split('|').map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = parseTableRow(line);
  return (
    cells.length > 1 &&
    cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, '')))
  );
}

function isTableRow(line: string): boolean {
  return parseTableRow(line).length > 1 && line.includes('|');
}

function renderTable(headers: string[], rows: string[][]): string {
  const headerHtml = headers
    .map((cell) => `<th>${cell}</th>`)
    .join('');
  const bodyHtml = rows
    .map((row) => {
      const cells = headers.map((_header, index) => `<td>${row[index] ?? ''}</td>`);
      return `<tr>${cells.join('')}</tr>`;
    })
    .join('');
  return `<table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`;
}

function extractMarkdownTables(value: string): { text: string; tables: string[] } {
  const lines = value.split('\n');
  const tables: string[] = [];
  const output: string[] = [];
  let index = 0;

  while (index < lines.length) {
    const headerLine = lines[index] ?? '';
    const separatorLine = lines[index + 1] ?? '';
    if (isTableRow(headerLine) && isTableSeparator(separatorLine)) {
      const headers = parseTableRow(headerLine);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && isTableRow(lines[index] ?? '')) {
        rows.push(parseTableRow(lines[index] ?? ''));
        index += 1;
      }
      const tableIndex = tables.length;
      tables.push(renderTable(headers, rows));
      output.push(`@@TABLE_${tableIndex}@@`);
      continue;
    }

    output.push(headerLine);
    index += 1;
  }

  return { text: output.join('\n'), tables };
}

export function renderMarkdown(value: string): string {
  const escaped = escapeHtml(String(value ?? ''));
  const codeBlocks: string[] = [];
  const inlineCodes: string[] = [];
  const withCodeBlocks = escaped.replace(
    /```(?:[a-zA-Z0-9_-]+)?\n?([\s\S]*?)```/g,
    (_match, code: string) => {
      const index = codeBlocks.length;
      const normalizedCode = code.replace(/^\n/, '').replace(/\n$/, '');
      codeBlocks.push(`<pre><code>${normalizedCode}</code></pre>`);
      return `@@CODE_BLOCK_${index}@@`;
    },
  );
  const withInlineCodes = withCodeBlocks.replace(/`(.+?)`/g, (_match, code: string) => {
    const index = inlineCodes.length;
    inlineCodes.push(`<code>${linkifyEscapedText(code)}</code>`);
    return `@@INLINE_CODE_${index}@@`;
  });
  const withLinks = linkifyEscapedText(withInlineCodes);
  const withStrong = withLinks.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  const { text: withTablePlaceholders, tables } =
    extractMarkdownTables(withStrong);
  const withLineBreaks = withTablePlaceholders.replace(/\n/g, '<br />');
  return withLineBreaks
    .replace(/@@TABLE_(\d+)@@/g, (_match, index: string) => {
      return tables[Number(index)] ?? '';
    })
    .replace(/@@INLINE_CODE_(\d+)@@/g, (_match, index: string) => {
      return inlineCodes[Number(index)] ?? '';
    })
    .replace(/@@CODE_BLOCK_(\d+)@@/g, (_match, index: string) => {
      return codeBlocks[Number(index)] ?? '';
    });
}
