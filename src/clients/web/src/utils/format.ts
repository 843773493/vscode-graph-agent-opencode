import { formatNumber, getLocale } from '../../../shared/core/i18n/index.js';

const TIME_FORMATTER = new Intl.DateTimeFormat(getLocale(), {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat(getLocale(), {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZoneName: 'short',
});

// 显示语言在模块加载前确定；保留原选项和本地时区，格式器沿本次启动复用。
const LOCAL_DATE_TIME_FORMATTER = new Intl.DateTimeFormat(getLocale());
const LOCAL_TIME_FORMATTER = new Intl.DateTimeFormat(getLocale(), {
  hour: 'numeric',
  minute: 'numeric',
  second: 'numeric',
});
const LOCAL_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat(getLocale(), {
  year: 'numeric', month: 'numeric', day: 'numeric',
  hour: 'numeric', minute: 'numeric', second: 'numeric',
});

export function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatTime(value: unknown): string {
  if (value == null || value === '') {
    return '';
  }

  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return TIME_FORMATTER.format(date);
}

export function formatDateTime(value: unknown): string {
  if (value == null || value === '') {
    return '';
  }

  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return DATE_TIME_FORMATTER.format(date);
}

export function formatLocalDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? date.toString()
    : LOCAL_DATE_TIME_FORMATTER.format(date);
}

export function formatLocalTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? date.toString()
    : LOCAL_TIME_FORMATTER.format(date);
}

/** 与原生 toLocaleString 的完整日期时间字段相同，语言使用本次启动选择。 */
export function formatLocalTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? date.toString() : LOCAL_TIMESTAMP_FORMATTER.format(date);
}

/**
 * 把字节数归一成人类可读文案；非数字输入（如未加载的文件大小）返回空串，
 * 由调用方决定是否展示占位。文件树、文件预览与 Gateway 诊断共用同一口径。
 */
export function formatByteSize(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "";
  }
  if (value < 1024) {
    return `${formatNumber(value, { useGrouping: false })} B`;
  }
  if (value < 1024 * 1024) {
    return `${formatNumber(value / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1, useGrouping: false })} KB`;
  }
  return `${formatNumber(value / 1024 / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1, useGrouping: false })} MB`;
}
