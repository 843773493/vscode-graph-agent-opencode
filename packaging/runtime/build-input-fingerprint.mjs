import { createHash } from "node:crypto";
import {
  closeSync,
  lstatSync,
  openSync,
  readSync,
  readdirSync,
  readlinkSync,
} from "node:fs";
import path from "node:path";

const FIXED_BUILD_INPUTS = Object.freeze([
  "package.json",
  "bun.lock",
  "uv.lock",
  "pyproject.toml",
  "app",
  "configs",
  "resources",
  "src/shared",
  "src/clients/web",
  "src/workspace-services",
  "packages/launcher",
  "packaging/runtime",
  "scripts/install/runtime-postinstall.mjs",
]);

const GENERATED_DIRECTORY_NAMES = new Set([
  ".git",
  ".venv",
  "dist",
  "node_modules",
  "out",
  "__pycache__",
]);

// 只忽略构建工具及Launcher中不进入发行包的普通说明和测试文件。
function isUnshippedInstructionOrTest(inputPath) {
  if (!inputPath.startsWith("packaging/runtime/") && !inputPath.startsWith("packages/launcher/")) return false;
  const fileName = path.posix.basename(inputPath);
  return fileName === "AGENTS.md" || fileName.endsWith(".test.mjs");
}

function collectBuildInputPaths(projectRoot, inputPath, entry, inputPaths) {
  // 构建相对路径始终使用/；只有实际磁盘定位遵循宿主路径规则。
  const absolutePath = path.join(projectRoot, inputPath);
  if (entry.isSymbolicLink()) {
    inputPaths.push({ path: inputPath, linkTarget: readlinkSync(absolutePath) });
    return;
  }
  if (entry.isFile()) {
    inputPaths.push({ path: inputPath });
    return;
  }
  if (!entry.isDirectory()) {
    throw new Error(`不支持的打包输入类型: ${inputPath}`);
  }

  for (const child of readdirSync(absolutePath, { withFileTypes: true })) {
    if (
      GENERATED_DIRECTORY_NAMES.has(child.name) ||
      (child.isFile() &&
        isUnshippedInstructionOrTest(path.posix.join(inputPath, child.name)))
    ) {
      continue;
    }
    collectBuildInputPaths(
      projectRoot,
      path.posix.join(inputPath, child.name),
      child,
      inputPaths,
    );
  }
}

export function computeBuildInputFingerprint({
  projectRoot = process.cwd(),
  targetPlatform,
} = {}) {
  if (!new Set(["linux-x64", "windows-x64"]).has(targetPlatform)) {
    throw new Error(`不支持的打包目标: ${String(targetPlatform)}`);
  }

  const root = path.resolve(projectRoot);
  const inputPaths = [];
  for (const inputPath of [
    ...FIXED_BUILD_INPUTS,
    `packages/runtime-${targetPlatform}/package.json`,
    `scripts/release/package-${targetPlatform}.mjs`,
  ]) {
    // 顶层输入需要查类型；后代复用readdir返回的Dirent，不再逐项lstat。
    collectBuildInputPaths(
      root,
      inputPath,
      lstatSync(path.join(root, inputPath)),
      inputPaths,
    );
  }
  inputPaths.sort((left, right) =>
    left.path < right.path ? -1 : left.path > right.path ? 1 : 0,
  );
  const fingerprint = createHash("sha256");
  const fileBuffer = Buffer.allocUnsafe(64 * 1024);

  for (const input of inputPaths) {
    fingerprint.update(input.path);
    fingerprint.update("\0");
    if (input.linkTarget !== undefined) {
      fingerprint.update("symlink\0");
      fingerprint.update(input.linkTarget);
    } else {
      const descriptor = openSync(path.join(root, input.path), "r");
      try {
        while (true) {
          const bytesRead = readSync(descriptor, fileBuffer, 0, fileBuffer.length, null);
          if (bytesRead === 0) break;
          fingerprint.update(fileBuffer.subarray(0, bytesRead));
        }
      } finally {
        closeSync(descriptor);
      }
    }
    fingerprint.update("\0");
  }

  return fingerprint.digest("hex");
}
