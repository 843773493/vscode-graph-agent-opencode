export const BOXTEAM_VERSION = "0.1.0";

export const BOXTEAM_GITHUB_REPOSITORY =
  "843773493/vscode-graph-agent-opencode";

export const RUNTIME_DOWNLOADER_DEPENDENCIES = Object.freeze({
  tar: "7.5.22",
});

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

export const PYTHON_RUNTIME_WINDOWS_X64 = Object.freeze({
  version: "3.12.13",
  release: "20260510",
  archive:
    "cpython-3.12.13+20260510-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
  url:
    "https://github.com/astral-sh/python-build-standalone/releases/download/" +
    "20260510/cpython-3.12.13%2B20260510-x86_64-pc-windows-msvc-" +
    "install_only_stripped.tar.gz",
  sha256: "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0",
  license: "MPL-2.0（构建系统）与 Python-2.0（CPython）",
});

export const NODE_RUNTIME_WINDOWS_X64 = Object.freeze({
  version: "22.17.0",
  archive: "node-v22.17.0-win-x64.zip",
  url: "https://nodejs.org/dist/v22.17.0/node-v22.17.0-win-x64.zip",
  sha256: "721ab118a3aac8584348b132767eadf51379e0616f0db802cc1e66d7f0d98f85",
  license: "Node.js 使用 MIT License",
});

export const NODE_RUNTIME_DEPENDENCIES = Object.freeze({
  "node-pty": "1.1.0",
  playwright: "1.61.1",
  ws: "8.18.0",
});
