export type DisplayLocale = 'zh-cn' | 'en';
export type MessageCatalog = Readonly<Record<string, string>>;

let activeLocale: DisplayLocale = 'zh-cn';
let activeMessages: MessageCatalog = {};

/** bootstrap 在业务模块求值前调用；入口本身不读取任何运行面全局。 */
export function setLocaleResources(locale: DisplayLocale, messages: MessageCatalog): void {
  activeLocale = locale;
  activeMessages = messages;
}

export function getLocale(): DisplayLocale {
  return activeLocale;
}

export function t(key: string, ...args: readonly (string | number | boolean | null | undefined)[]): string {
  const message = activeMessages[key];
  if (message === undefined) throw new Error(`缺少当前语言资源：${key}`);
  return message.replace(/\{(\d+)\}/g, (placeholder, index: string) => {
    const value = args[Number(index)];
    if (value === undefined || value === null) return placeholder;
    return String(value);
  });
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(activeLocale === 'zh-cn' ? 'zh-CN' : 'en', options).format(value);
}

export function formatDate(value: Date | number, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(activeLocale === 'zh-cn' ? 'zh-CN' : 'en', options).format(value);
}

export function formatRelativeTime(value: number, unit: Intl.RelativeTimeFormatUnit, options?: Intl.RelativeTimeFormatOptions): string {
  return new Intl.RelativeTimeFormat(activeLocale === 'zh-cn' ? 'zh-CN' : 'en', options).format(value, unit);
}
