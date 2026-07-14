from __future__ import annotations

import subprocess
from pathlib import Path


def test_browser_url_normalization_accepts_bare_domain_and_local_address() -> None:
    script = """
import { normalizeBrowserUrl } from './src/browser/server/url.js';
const cases = [
  ['www.baidu.com', 'https://www.baidu.com/'],
  ['127.0.0.1:8016/health', 'http://127.0.0.1:8016/health'],
  ['data:text/html,ok', 'data:text/html,ok'],
];
for (const [input, expected] of cases) {
  const actual = normalizeBrowserUrl(input);
  if (actual !== expected) {
    throw new Error(`${input} => ${actual}, expected ${expected}`);
  }
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
