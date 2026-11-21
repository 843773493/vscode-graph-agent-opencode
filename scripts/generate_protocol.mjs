// 该脚本负责从 proto/ 生成各运行时的公开协议绑定。
import { access, cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
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
const bufExecutable = createRequire(path.join(projectRoot, "package.json"))
  .resolve("@bufbuild/buf/bin/buf");
const pythonOutputRoot = path.join(projectRoot, "sdk", "python", "src", "boxteam");
const generatedRoots = [
  {
    directory: pythonOutputRoot,
    extensions: [".py", ".pyi"],
    guidance: true,
  },
  {
    directory: path.join(projectRoot, "sdk", "node", "src", "generated"),
    extensions: [".js", ".d.ts"],
    guidance: true,
  },
  {
    directory: path.join(projectRoot, "sdk", "node", "src", "generated-json"),
    extensions: [".ts"],
    guidance: true,
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

async function generatedFiles(directory, root, relative = "") {
  const result = new Map();
  const entries = await readdir(directory, { withFileTypes: true }).catch((error) => {
    if (error.code === "ENOENT") {
      return [];
    }
    throw error;
  });
  for (const entry of entries) {
    const name = path.join(relative, entry.name);
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      for (const [key, value] of await generatedFiles(entryPath, root, name)) result.set(key, value);
      continue;
    }
    if ((root.guidance && entry.name === "AGENTS.md") || (root.files ? root.files.includes(name) : root.extensions.some((extension) => entry.name.endsWith(extension)))) {
      result.set(name, await readFile(entryPath));
    }
  }
  return result;
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

async function addGeneratedGuidance(directory) {
  await writeFile(path.join(directory, "AGENTS.md"), `# 目录用途

存放插件 SDK 的协议生成产物及其子包。

# 可修改内容

仅由 bun run gen:protocol 从 proto/boxteam/ 及固定生成配置更新。

# 不可修改内容

不得手写 wire 类型、修改生成正文或导入产品业务实现。

# 规范

协议源码只有 proto/boxteam/ 一份；使用 bun run check:protocol 核对产物，包内不得建立平行协议。
`);
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isDirectory()) await addGeneratedGuidance(path.join(directory, entry.name));
  }
}

async function main() {
  await requirePathExists(path.join(projectRoot, "buf.yaml"));
  await requirePathExists(path.join(projectRoot, "buf.gen.yaml"));
  await requirePathExists(path.join(projectRoot, "proto"));
  // Buf 的包入口负责选择平台二进制，无需猜测包管理器的 .bin shim。
  await run(process.execPath, [bufExecutable, "lint"]);
  if (breakingAgainst) {
    await run(process.execPath, [bufExecutable, "breaking", "--against", breakingAgainst]);
  }

  await mkdir(stagingRoot, { recursive: true });
  const stagingDirectory = await mkdtemp(path.join(stagingRoot, "protocol-"));
  try {
    const descriptorSet = path.join(stagingDirectory, "protocol.binpb");
    await run(process.execPath, [bufExecutable, "build", "--as-file-descriptor-set", "--output", descriptorSet]);
    // 原 Buf 模板和插件版本不变；远端生成全部成功前，不移除现有绑定。
    await run(process.execPath, [bufExecutable, "generate", "--output", stagingDirectory]);
    await addPythonPackageMarkers(
      path.join(stagingDirectory, path.relative(projectRoot, pythonOutputRoot)),
    );
    const stagedRoots = generatedRoots.map((generatedRoot) => ({
      ...generatedRoot,
      generated: path.join(stagingDirectory, path.relative(projectRoot, generatedRoot.directory)),
    }));
    for (const root of stagedRoots) {
      if (root.guidance) await addGeneratedGuidance(root.generated);
    }
    for (const { generated } of stagedRoots) {
      await access(generated, constants.R_OK);
    }
    const differences = [];
    for (const root of stagedRoots) {
      const expected = await generatedFiles(root.generated, root);
      const actual = await generatedFiles(root.directory, root);
      for (const name of new Set([...expected.keys(), ...actual.keys()])) {
        const target = path.join(root.directory, name);
        const content = expected.get(name);
        if (content !== undefined && actual.has(name) && content.equals(actual.get(name))) continue;
        differences.push(path.relative(projectRoot, target));
        if (!checkOnly) {
          if (content === undefined) await rm(target);
          else {
            await mkdir(path.dirname(target), { recursive: true });
            await cp(path.join(root.generated, name), target);
          }
        }
      }
    }
    if (checkOnly && differences.length) {
      throw new Error(`协议生成产物不一致，请运行 bun run gen:protocol：\n${differences.join("\n")}`);
    }
  } finally {
    await rm(stagingDirectory, { recursive: true, force: true });
  }
}

await main();
