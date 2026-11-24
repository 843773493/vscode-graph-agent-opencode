import { boundedFailureText } from '@boxteam/plugin-sdk/failures';

/** 只投影默认可见文本；原原因及诊断继续由各 owner 保存。 */
export function failureSummary(message: string, operation?: string): string {
  const text = boundedFailureText(message).replace(/\s+/gu, ' ').trim();
  const full = operation ? `${operation}：${text}` : text;
  const points = Array.from(full);
  return points.length > 160 ? `${points.slice(0, 159).join('')}…` : full;
}
