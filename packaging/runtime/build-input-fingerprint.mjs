import { createHash } from "node:crypto";
import {
  lstatSync,
  readFileSync,
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

function collectBuildInputPaths(projectRoot, inputPath) {
  const absolutePath = path.join(projectRoot, inputPath);
  const entry = lstatSync(absolutePath);
  if (entry.isSymbolicLink()) {
    return [{ path: inputPath, linkTarget: readlinkSync(absolutePath) }];
  }
  if (entry.isFile()) return [{ path: inputPath }];
  if (!entry.isDirectory()) {
    throw new Error(`不支持的打包输入类型: ${inputPath}`);
  }

  return readdirSync(absolutePath, { withFileTypes: true })
    .filter(
      (child) => !GENERATED_DIRECTORY_NAMES.has(child.name),
    )
    .sort((left, right) =>
      left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
    )
    .flatMap((child) =>
      collectBuildInputPaths(
        projectRoot,
        path.join(inputPath, child.name),
      ),
    );
}

export function computeBuildInputFingerprint({
  projectRoot = process.cwd(),
  targetPlatform,
} = {}) {
  if (!new Set(["linux-x64", "windows-x64"]).has(targetPlatform)) {
    throw new Error(`不支持的打包目标: ${String(targetPlatform)}`);
  }

  const root = path.resolve(projectRoot);
  const inputPaths = [
    ...FIXED_BUILD_INPUTS,
    `packages/runtime-${targetPlatform}/package.json`,
    `scripts/release/package-${targetPlatform}.mjs`,
  ]
    .flatMap((inputPath) => collectBuildInputPaths(root, inputPath))
    .sort((left, right) =>
      left.path < right.path ? -1 : left.path > right.path ? 1 : 0,
    );
  const fingerprint = createHash("sha256");

  for (const input of inputPaths) {
    const relativePath = input.path.split(path.sep).join("/");
    fingerprint.update(relativePath);
    fingerprint.update("\0");
    if (input.linkTarget !== undefined) {
      fingerprint.update("symlink\0");
      fingerprint.update(input.linkTarget);
    } else {
      fingerprint.update(readFileSync(path.join(root, input.path)));
    }
    fingerprint.update("\0");
  }

  return fingerprint.digest("hex");
}
