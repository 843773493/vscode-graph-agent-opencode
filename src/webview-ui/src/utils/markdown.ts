import { escapeHtml } from './format';

// inline markdown: bold/italic/links/inline-code（仅处理行内格式）
export function applyInlineMarkdown(text: string): string {
  let value = escapeHtml(text);
  value = value.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, href) =>
    `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${label}</a>`
  );
  value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
  value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return value;
}

function renderCodeBlock(codeLang: string, code: string): string {
  const language = codeLang.trim() || 'text';
  return `
<div class="markdown-code-block" data-code-block="true">
  <div class="markdown-code-header">
    <span class="markdown-code-language">${escapeHtml(language)}</span>
    <button class="markdown-code-copy" type="button" title="复制代码" data-code-action="copy">复制</button>
  </div>
  <pre><code data-code-language="${escapeHtml(language)}">${escapeHtml(code)}</code></pre>
</div>`;
}

// 完整 Markdown 渲染：代码块 + 标题 + 无序/有序列表 + 引用
export function renderMarkdown(source: string): string {
  const lines = (source ?? '').replace(/\r\n/g, '\n').split('\n');
  const output: string[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let listType: 'ul' | 'ol' | null = null;
  let codeLines: string[] = [];
  let codeLang = '';
  let inCode = false;

  const flushCode = () => {
    if (!codeLines.length) return;
    output.push(renderCodeBlock(codeLang, codeLines.join('\n')));
    codeLines = [];
    codeLang = '';
  };

  const flushList = () => {
    if (!listItems.length) return;
    const tag = listType === 'ol' ? 'ol' : 'ul';
    output.push(`<${tag}>${listItems.map(item => `<li>${applyInlineMarkdown(item)}</li>`).join('')}</${tag}>`);
    listItems = [];
    listType = null;
  };

  const flushParagraph = () => {
    if (!paragraph.length) return;
    output.push(`<p>${applyInlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  };

  const flushAll = () => {
    flushParagraph();
    flushList();
    flushCode();
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('```')) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushAll();
        inCode = true;
        codeLang = trimmed.slice(3).trim();
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (!trimmed) {
      flushAll();
      continue;
    }
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      flushAll();
      const level = Math.min(headingMatch[1].length, 6);
      output.push(`<h${level}>${applyInlineMarkdown(headingMatch[2])}</h${level}>`);
      continue;
    }
    const unorderedMatch = trimmed.match(/^[-*+]\s+(.*)$/);
    const orderedMatch = trimmed.match(/^\d+\.\s+(.*)$/);
    if (unorderedMatch || orderedMatch) {
      flushParagraph();
      const nextType = unorderedMatch ? 'ul' : 'ol';
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((unorderedMatch ?? orderedMatch)![1]);
      continue;
    }
    if (trimmed.startsWith('>')) {
      flushAll();
      output.push(`<blockquote>${applyInlineMarkdown(trimmed.slice(1).trim())}</blockquote>`);
      continue;
    }
    if (listItems.length) flushList();
    paragraph.push(trimmed);
  }

  flushAll();
  return output.join('');
}
