import { describe, expect, test } from "bun:test";
import { spawnSync } from 'node:child_process';

import { formatByteSize } from "./format";

for (const locale of ['zh-cn', 'en'] as const) {
  test(`${locale} 启动时统一历史和本地时间显示，保留时间点及既有格式选项`, () => {
      const instant = '2026-11-12T18:07:06Z', date = new Date(instant);
      // 独立进程对应真实 bootstrap 顺序；不在同一次启动中切换模块级格式器。
      const child = spawnSync(process.execPath, ['--eval', `
const { setLocaleResources } = await import(${JSON.stringify(new URL('../../../shared/core/i18n/index.ts', import.meta.url).href)});
setLocaleResources(${JSON.stringify(locale)}, {});
const m = await import(${JSON.stringify(new URL('./format.ts', import.meta.url).href)});
process.stdout.write(JSON.stringify([m.formatTime('${instant}'), m.formatDateTime('${instant}'),
  m.formatLocalDateTime('${instant}'), m.formatLocalTime('${instant}'), m.formatDateTime('原始错误时间'), m.formatTime(null),
  m.formatLocalTimestamp('${instant}')]));
`], { encoding: 'utf8', timeout: 10_000 });
      expect({ status: child.status, stderr: child.stderr }).toEqual({ status: 0, stderr: '' });
      const [time, dateTime, localDateTime, localTime, invalid, empty, timestamp] = JSON.parse(child.stdout);
      expect(time).toBe(new Intl.DateTimeFormat(locale,
        { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date));
      expect(dateTime).toBe(new Intl.DateTimeFormat(locale,
        { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }).format(date));
      expect(localDateTime).toBe(new Intl.DateTimeFormat(locale).format(date));
      expect(localTime).toBe(new Intl.DateTimeFormat(locale,
        { hour: 'numeric', minute: 'numeric', second: 'numeric' }).format(date));
      expect(invalid).toBe('原始错误时间');
      expect(empty).toBe('');
      expect(timestamp).toBe(date.toLocaleString(locale));
  });
}

describe("formatByteSize", () => {
  test("非数字输入返回空串，交由调用方决定占位", () => {
    expect(formatByteSize(null)).toBe("");
    expect(formatByteSize(undefined)).toBe("");
  });

  test("按 B / KB / MB 分段并保留一位小数", () => {
    expect(formatByteSize(0)).toBe("0 B");
    expect(formatByteSize(1023)).toBe("1023 B");
    expect(formatByteSize(1024)).toBe("1.0 KB");
    expect(formatByteSize(1536)).toBe("1.5 KB");
    expect(formatByteSize(1024 * 1024 - 1)).toBe("1024.0 KB");
    expect(formatByteSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatByteSize(3 * 1024 * 1024 + 512 * 1024)).toBe("3.5 MB");
  });
});
