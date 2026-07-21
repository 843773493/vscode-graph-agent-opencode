export const BOXTEAM_VERSION = "0.1.0";

export const PYTHON_RUNTIME = Object.freeze({
  version: "3.12.13",
  release: "20260510",
  archive:
    "cpython-3.12.13+20260510-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
  url:
    "https://github.com/astral-sh/python-build-standalone/releases/download/" +
    "20260510/cpython-3.12.13%2B20260510-x86_64-unknown-linux-gnu-" +
    "install_only_stripped.tar.gz",
  sha256: "d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88",
  license: "MPL-2.0（构建系统）与 Python-2.0（CPython）",
});

export const NODE_RUNTIME_DEPENDENCIES = Object.freeze({
  "node-pty": "1.1.0",
  playwright: "1.61.1",
  ws: "8.18.0",
});
