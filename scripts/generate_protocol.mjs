// 该脚本负责从 proto/ 生成各运行时的公开协议绑定。
import { access, cp, mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

const projectRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const scriptArguments = process.argv.slice(2);
const checkOnly = scriptArguments.includes("--check");
const breakingAgainstIndex = scriptArguments.indexOf("--breaking-against");
const breakingAgainst = breakingAgainstIndex >= 0
  ? scriptArguments[breakingAgainstIndex + 1]
  : null;
if (breakingAgainstIndex >= 0 && (!breakingAgainst || breakingAgainst.startsWith("-"))) {
  console.error("协议参数错误：--breaking-against 后必须提供基线 image/path。\n用法：--breaking-against <image-or-path>");
  process.exit(2);
}
const stagingRootIndex = scriptArguments.indexOf("--staging-root");
const stagingRoot = path.resolve(
  projectRoot,
  stagingRootIndex >= 0
    ? scriptArguments[stagingRootIndex + 1]
    : path.join("out", "protocol-generation"),
);
const bufExecutable = path.join(
  projectRoot,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "buf.exe" : "buf",
);
const pythonOutputRoot = path.join(projectRoot, "app", "protocol", "generated");
const generatedRoots = [
  {
    directory: pythonOutputRoot,
    extensions: [".py"],
  },
  {
    directory: path.join(projectRoot, "src", "workspace-services", "protocol", "generated"),
    extensions: [".js", ".d.ts"],
  },
  {
    directory: path.join(projectRoot, "src", "clients", "web", "src", "types", "protocol_buf_generated"),
    extensions: [".js", ".d.ts"],
  },
  {
    directory: path.join(projectRoot, "src", "clients", "web", "src", "types", "protocol_generated"),
    extensions: [".ts"],
  },
];

async function requirePathExists(filePath) {
  await access(filePath, constants.F_OK);
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: projectRoot,
      env: { ...process.env },
      stdio: "inherit",
    });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`协议命令失败: ${command} ${args.join(" ")}，code=${code} signal=${signal ?? ""}`));
    });
  });
}

async function removeGeneratedFiles(directory, extensions) {
  const entries = await readdir(directory, { withFileTypes: true }).catch((error) => {
    if (error.code === "ENOENT") {
      return [];
    }
    throw error;
  });
  for (const entry of entries) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      await removeGeneratedFiles(entryPath, extensions);
      continue;
    }
    if (extensions.some((extension) => entry.name.endsWith(extension))) {
      await rm(entryPath, { force: true });
    }
  }
}

async function addPythonPackageMarkers(directory) {
  await mkdir(directory, { recursive: true });
  const entries = await readdir(directory, { withFileTypes: true });
  const hasPythonFile = entries.some((entry) => entry.isFile() && entry.name.endsWith(".py"));
  if (hasPythonFile || entries.some((entry) => entry.isDirectory())) {
    const initPath = path.join(directory, "__init__.py");
    await writeFile(initPath, "# 该文件由协议生成脚本创建。\n", "utf8");
  }
  for (const entry of entries) {
    if (entry.isDirectory()) {
      await addPythonPackageMarkers(path.join(directory, entry.name));
    }
  }
}

async function main() {
  await requirePathExists(path.join(projectRoot, "buf.yaml"));
  await requirePathExists(path.join(projectRoot, "buf.gen.yaml"));
  await requirePathExists(path.join(projectRoot, "proto"));
  await requirePathExists(bufExecutable);

  await run(bufExecutable, ["lint"]);
  await run(bufExecutable, ["build"]);
  if (breakingAgainst) {
    await run(bufExecutable, ["breaking", "--against", breakingAgainst]);
  }

  if (checkOnly) {
    return;
  }

  await mkdir(stagingRoot, { recursive: true });
  const stagingDirectory = await mkdtemp(path.join(stagingRoot, "protocol-"));
  try {
    // 原 Buf 模板和插件版本不变；远端生成全部成功前，不移除现有绑定。
    await run(bufExecutable, ["generate", "--output", stagingDirectory]);
    await addPythonPackageMarkers(
      path.join(stagingDirectory, path.relative(projectRoot, pythonOutputRoot)),
    );
    await run(process.execPath, [path.join(projectRoot, "scripts", "generate_sse_runtime.mjs")]);
    const stagedRoots = generatedRoots.map((generatedRoot) => ({
      ...generatedRoot,
      generated: path.join(stagingDirectory, path.relative(projectRoot, generatedRoot.directory)),
    }));
    for (const { generated } of stagedRoots) {
      await access(generated, constants.R_OK);
    }
    for (const { directory, extensions, generated } of stagedRoots) {
      await mkdir(directory, { recursive: true });
      await removeGeneratedFiles(directory, extensions);
      await cp(generated, directory, { recursive: true });
    }
  } finally {
    await rm(stagingDirectory, { recursive: true, force: true });
  }
}

await main();
