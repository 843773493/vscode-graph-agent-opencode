import { expect, test } from 'bun:test';
import { failureSummary } from './failurePresentation';

test('摘要只投影单行安全原因，不改变原公开文本或切坏非 BMP 字符', () => {
  const message = `原原因\n api_key=private-key ${'😀'.repeat(200)}`;
  const summary = failureSummary(message, '读取');
  expect(summary.startsWith('读取：原原因 api_key=[redacted]')).toBe(true);
  expect(Array.from(summary)).toHaveLength(160);
  expect(summary).not.toContain('\n');
  expect(summary).not.toMatch(/[\uD800-\uDFFF]/u);
  expect(message).toContain('private-key');
  expect(message).toContain('\n');
});
