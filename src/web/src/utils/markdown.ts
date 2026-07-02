import { escapeHtml } from './format';

export { escapeHtml };

export function renderMarkdown(value: string): string {
  const escaped = escapeHtml(String(value ?? ''));
  const codeBlocks: string[] = [];
  const withCodeBlocks = escaped.replace(
    /```(?:[a-zA-Z0-9_-]+)?\n?([\s\S]*?)```/g,
    (_match, code: string) => {
      const index = codeBlocks.length;
      const normalizedCode = code.replace(/^\n/, '').replace(/\n$/, '');
      codeBlocks.push(`<pre><code>${normalizedCode}</code></pre>`);
      return `@@CODE_BLOCK_${index}@@`;
    },
  );
  const withLineBreaks = withCodeBlocks.replace(/\n/g, '<br />');
  return withLineBreaks
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/@@CODE_BLOCK_(\d+)@@/g, (_match, index: string) => {
      return codeBlocks[Number(index)] ?? '';
    });
}
