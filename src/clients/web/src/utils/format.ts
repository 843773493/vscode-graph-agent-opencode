const TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZoneName: 'short',
});

// 保留浏览器默认语言与时区，等价于无选项的 Date 本地时间格式化。
const LOCAL_DATE_TIME_FORMATTER = new Intl.DateTimeFormat();
const LOCAL_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: 'numeric',
  second: 'numeric',
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
    ? date.toLocaleString()
    : LOCAL_DATE_TIME_FORMATTER.format(date);
}

export function formatLocalTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? date.toLocaleTimeString()
    : LOCAL_TIME_FORMATTER.format(date);
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
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
