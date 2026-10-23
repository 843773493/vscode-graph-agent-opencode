import { readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

import { PYTHON_RUNTIME_WINDOWS_X64 } from "./versions.mjs";

// TODO: 依赖锁文件变化后重新确认这些包仍是纯 Python 且没有 Windows wheel。
const CROSS_BUILD_SOURCE_PACKAGES = Object.freeze([
  { distributionName: "commentjson", importRoot: "commentjson" },
  { distributionName: "lark-parser", importRoot: "lark" },
  { distributionName: "litellm", importRoot: "litellm" },
]);

function parseLockedRequirements(requirementsPath) {
  const blocks = [];
  let header = [];
  let current = null;
  for (const line of readFileSync(requirementsPath, "utf8").split(/\r?\n/)) {
    const match = line.match(
      /^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\;]+)/,
    );
    if (match) {
      if (current !== null) blocks.push(current);
      current = {
        name: match[1].toLowerCase().replaceAll("_", "-"),
        specifier: `${match[1]}==${match[2]}`,
        lines: [line],
      };
      continue;
    }
    if (current === null) {
      header.push(line);
    } else {
      current.lines.push(line);
    }
  }
  if (current !== null) blocks.push(current);
  return { blocks, header };
}

function writeCrossRequirements(requirementsPath, outputRoot) {
  const { blocks, header } = parseLockedRequirements(requirementsPath);
  const sourcePackageNames = new Set(
    CROSS_BUILD_SOURCE_PACKAGES.map(({ distributionName }) => distributionName),
  );
  const sourcePackages = blocks.filter((block) =>
    sourcePackageNames.has(block.name),
  );
  const targetBlocks = blocks.filter(
    (block) => !sourcePackageNames.has(block.name),
  );
  const crossRequirementsPath = path.join(
    outputRoot,
    "requirements.cross.locked.txt",
  );
  writeFileSync(
    crossRequirementsPath,
    `${[...header, ...targetBlocks.flatMap((block) => block.lines)].join("\n")}\n`,
  );
  return {
    crossRequirementsPath,
    sourceSpecifiers: sourcePackages.map((block) => block.specifier),
  };
}

function removeInstalledPythonPackageArtifacts(sitePackagesRoot, packages) {
  for (const entry of readdirSync(sitePackagesRoot)) {
    const lowerEntry = entry.toLowerCase();
    const matchesPackage = packages.some(({ distributionName, importRoot }) => {
      if (entry === importRoot) return true;
      const metadataName = distributionName.toLowerCase().replaceAll("-", "_");
      return [".dist-info", ".egg-info"].some((suffix) => {
        if (!lowerEntry.endsWith(suffix)) return false;
        const nameAndVersion = lowerEntry.slice(0, -suffix.length);
        return nameAndVersion === metadataName ||
          nameAndVersion.startsWith(`${metadataName}-`);
      });
    });
    if (matchesPackage) {
      rmSync(path.join(sitePackagesRoot, entry), {
        recursive: true,
        force: true,
      });
    }
  }
}

function collectPythonSharedObjectPaths(root, result = []) {
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) {
      collectPythonSharedObjectPaths(target, result);
    } else if (target.endsWith(".so")) {
      result.push(target);
    }
  }
  return result;
}

function removeSourcePackageLinuxExtensionsAndRejectOthers(sitePackagesRoot) {
  const packageRoots = CROSS_BUILD_SOURCE_PACKAGES.map(({ importRoot }) =>
    path.join(sitePackagesRoot, importRoot),
  );
  const unexpectedExtensions = [];
  // 清理与残留校验共用一次枚举；不能再为校验扫描整个 site-packages。
  for (const file of collectPythonSharedObjectPaths(sitePackagesRoot)) {
    if (packageRoots.some((root) => file.startsWith(`${root}${path.sep}`))) {
      rmSync(file, { force: true });
    } else {
      unexpectedExtensions.push(file);
    }
  }
  if (unexpectedExtensions.length > 0) {
    throw new Error(
      `Windows 目标 Python 包含 Linux 原生扩展: ${unexpectedExtensions.join(", ")}`,
    );
  }
}

export function installWindowsPythonDependencies({ pythonExecutable, outputRoot, runtimePackageRoot, crossBuild, run }) {
  const requirementsPath = path.join(outputRoot, "requirements.locked.txt");
  run("uv", [
    "export",
    "--locked",
    "--no-dev",
    "--format",
    "requirements-txt",
    "--output-file",
    requirementsPath,
  ]);
  if (crossBuild) {
    const sitePackagesRoot = path.join(
      runtimePackageRoot,
      "python",
      "Lib",
      "site-packages",
    );
    const { crossRequirementsPath, sourceSpecifiers } =
      writeCrossRequirements(requirementsPath, outputRoot);
    run("uv", [
      "pip",
      "install",
      "--target",
      sitePackagesRoot,
      "--python-version",
      PYTHON_RUNTIME_WINDOWS_X64.version,
      "--python-platform",
      "x86_64-pc-windows-msvc",
      "--no-build",
      "--requirements",
      crossRequirementsPath,
    ]);
    removeInstalledPythonPackageArtifacts(
      sitePackagesRoot,
      CROSS_BUILD_SOURCE_PACKAGES,
    );
    run("uv", [
      "pip",
      "install",
      "--target",
      sitePackagesRoot,
      "--no-deps",
      ...sourceSpecifiers,
    ]);
    removeSourcePackageLinuxExtensionsAndRejectOthers(sitePackagesRoot);
    return;
  }
  const installArguments = [
      "pip",
      "install",
      "--python",
      pythonExecutable,
      "--requirements",
      requirementsPath,
  ];
  run("uv", installArguments);
}
