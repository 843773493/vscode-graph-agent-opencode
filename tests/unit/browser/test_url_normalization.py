from __future__ import annotations

import subprocess
from pathlib import Path


def test_browser_url_targets_keep_defaults_and_reject_unsupported_protocols() -> None:
    script = """
import assert from 'node:assert/strict';
import { normalizeBrowserUrl } from './src/workspace-services/browser/server/url.js';
import { BrowserSession } from './src/workspace-services/browser/server/browserSession.js';
const cases = [
  ['www.baidu.com', 'https://www.baidu.com/'],
  ['127.0.0.1:8016/health', 'http://127.0.0.1:8016/health'],
  ['data:text/html,ok', 'data:text/html,ok'],
  ['localhost:8016/health', 'http://localhost:8016/health'],
  ['example.test:8443/path?q=1#part', 'https://example.test:8443/path?q=1#part'],
  ['intranet:8080/path', 'https://intranet:8080/path'],
  ['[::1]:8016/health', 'http://[::1]:8016/health'],
  ['http:80', 'http://0.0.0.80/'],
  ['https://user:password@example.test/path', 'https://user:password@example.test/path'],
  ['file:///workspace/file.txt', 'file:///workspace/file.txt'],
  ['about:blank', 'about:blank'],
  ['localhost?ready=1', 'http://localhost/?ready=1'],
  ['127.0.0.1#status', 'http://127.0.0.1/#status'],
  ['[::1]?ready=1', 'http://[::1]/?ready=1'],
];
for (const [input, expected] of cases) {
  const actual = normalizeBrowserUrl(input);
  if (actual !== expected) {
    throw new Error(`${input} => ${actual}, expected ${expected}`);
  }
}
for (const input of ['ftp://example.test/archive', 'ws://example.test/events',
  'wss://example.test/events', 'mailto:user@example.test', 'custom+tool://owner/path']) {
  let failure;
  try { normalizeBrowserUrl(input); } catch (error) { failure = error; }
  if (!failure || !failure.message.includes('不支持的 URL 协议')) {
    throw new Error(`${input} 必须明确拒绝不支持的协议，实际错误=${failure}`);
  }
}
const session = new BrowserSession({
  manager: { persist: async () => undefined },
  record: { browser_id: 'browser_url_test', status: 'running', url: 'https://example.test/current' },
});
let navigations = 0;
session.browser = {};
session.context = {};
session.cdpSession = {};
session.page = { goto: async () => { navigations += 1; } };
await assert.rejects(session.goto('ftp://example.test/archive'), /不支持的 URL 协议/);
assert.equal(navigations, 0);
assert.equal(session.record.url, 'https://example.test/current');
"""
    result = subprocess.run(
        ["bun", "--eval", script],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
