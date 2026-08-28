// 该脚本负责从 proto/ 生成各运行时的公开协议绑定。
import { access, mkdir, readdir, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const scriptArguments = process.argv.slice(2);
const checkOnly = scriptArguments.includes("--check");
const breakingAgainstIndex = scriptArguments.indexOf("--breaking-against");
const breakingAgainst = breakingAgainstIndex >= 0
  ? scriptArguments[breakingAgainstIndex + 1]
  : null;
const bufExecutable = path.join(
  workspaceRoot,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "buf.exe" : "buf",
);
const pythonOutputRoot = path.join(workspaceRoot, "app", "protocol", "generated");
const generatedRoots = [
  {
    directory: pythonOutputRoot,
    extensions: [".py"],
  },
  {
    directory: path.join(workspaceRoot, "src", "workspace-services", "protocol", "generated"),
    extensions: [".js", ".d.ts"],
  },
  {
    directory: path.join(workspaceRoot, "src", "clients", "web", "src", "types", "protocol_buf_generated"),
    extensions: [".js", ".d.ts"],
  },
  {
    directory: path.join(workspaceRoot, "src", "clients", "web", "src", "types", "protocol_generated"),
    extensions: [".ts"],
  },
];

async function ensureFile(filePath, label) {
  await access(filePath, constants.F_OK);
  if (!filePath.startsWith(workspaceRoot)) {
    throw new Error(`${label} 不在项目根目录下: ${filePath}`);
  }
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: workspaceRoot,
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
  await ensureFile(path.join(workspaceRoot, "buf.yaml"), "Buf 配置");
  await ensureFile(path.join(workspaceRoot, "buf.gen.yaml"), "Buf 生成配置");
  await ensureFile(path.join(workspaceRoot, "proto"), "协议源目录");
  await ensureFile(bufExecutable, "Buf 可执行文件");

  await run(bufExecutable, ["lint"]);
  await run(bufExecutable, ["build"]);
  if (breakingAgainst) {
    await run(bufExecutable, ["breaking", "--against", breakingAgainst]);
  }

  if (checkOnly) {
    return;
  }

  for (const generatedRoot of generatedRoots) {
    await mkdir(generatedRoot.directory, { recursive: true });
    await removeGeneratedFiles(generatedRoot.directory, generatedRoot.extensions);
  }
  await run(bufExecutable, ["generate"]);
  await addPythonPackageMarkers(pythonOutputRoot);
  await run(process.execPath, [path.join(workspaceRoot, "scripts", "generate_sse_runtime.mjs")]);
}

await main();
