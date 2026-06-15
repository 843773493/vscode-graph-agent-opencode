import { escapeHtml } from './format';

export { escapeHtml };

export function renderMarkdown(value: string): string {
  const escaped = escapeHtml(String(value ?? ''));
  const withLineBreaks = escaped.replace(/\n/g, '<br />');
  return withLineBreaks
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
}
