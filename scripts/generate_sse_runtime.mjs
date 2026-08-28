// 该脚本生成 SSE JSON schema 与无运行时依赖的校验器。
// SSE 的 Python Pydantic 模型仍用于后端运行时校验，但不再生成公开 TypeScript DTO。
import { access, readFile, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import path from "node:path";

const workspaceRoot = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const pythonExecutable = process.platform === "win32"
  ? path.join(workspaceRoot, ".venv", "Scripts", "python.exe")
  : path.join(workspaceRoot, ".venv", "bin", "python");
const schemaPath = path.join(
  workspaceRoot,
  "src",
  "clients",
  "web",
  "src",
  "types",
  "protocol_generated",
  "sse_runtime_schemas.json",
);

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: workspaceRoot,
      env: { ...process.env, PYTHONPATH: workspaceRoot },
      stdio: "inherit",
    });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`SSE runtime 生成失败: ${command} ${args.join(" ")}，code=${code} signal=${signal ?? ""}`));
    });
  });
}

async function generateValidators() {
  const webRequire = createRequire(path.join(workspaceRoot, "src", "clients", "web", "package.json"));
  const Ajv = webRequire("ajv").default;
  const standaloneCode = webRequire("ajv/dist/standalone").default;
  const payload = JSON.parse(await readFile(schemaPath, "utf8"));
  if (!payload || typeof payload !== "object" || !payload.schemas) {
    throw new Error(`SSE runtime schema 文件结构无效: ${schemaPath}`);
  }
  const ajv = new Ajv({
    allErrors: true,
    strict: false,
    validateFormats: false,
    code: { esm: true, source: true },
  });
  const exportsByName = {};
  for (const [name, schema] of Object.entries(payload.schemas)) {
    const schemaId = schema?.$id;
    if (typeof schemaId !== "string" || !schemaId) {
      throw new Error(`SSE runtime schema 缺少 $id: ${name}`);
    }
    ajv.addSchema(schema, schemaId);
    exportsByName[`validate${name}`] = schemaId;
  }
  const outputPath = path.join(workspaceRoot, "src", "shared", "sseRuntimeValidators.js");
  const declarationPath = path.join(workspaceRoot, "src", "shared", "sseRuntimeValidators.d.ts");
  const validatorSource = standaloneCode(ajv, exportsByName)
    .replace(
      /const (\w+) = require\("([^"]+)"\)\.default;/g,
      'import $1 from "$2";',
    )
    .replace(
      /import (\w+) from "ajv\/dist\/runtime\/ucs2length";/g,
      `const $1 = (value) => {
\tlet length = 0;
\tlet position = 0;
\twhile (position < value.length) {
\t\tlength += 1;
\t\tconst first = value.charCodeAt(position++);
\t\tif (first >= 0xd800 && first <= 0xdbff && position < value.length) {
\t\t\tconst second = value.charCodeAt(position);
\t\t\tif (second >= 0xdc00 && second <= 0xdfff) position += 1;
\t\t}
\t}
\treturn length;
};`,
    );
  if (validatorSource.includes("require(") || validatorSource.includes('from "ajv/')) {
    throw new Error("生成的 SSE runtime validator 仍依赖 Ajv runtime");
  }
  await writeFile(outputPath, `// 该文件由程序生成，请勿手写。\n${validatorSource}\n`, "utf8");
  await writeFile(
    declarationPath,
    `// 该文件由程序生成，请勿手写。\n${Object.keys(exportsByName)
      .map((name) => `export const ${name}: { (value: unknown): boolean; errors?: Array<{ instancePath: string; keyword: string; message?: string }> | null };`)
      .join("\n")}\n`,
    "utf8",
  );
}

await access(schemaPath, constants.F_OK).catch(async (error) => {
  if (error.code !== "ENOENT") throw error;
  await run(pythonExecutable, ["-m", "scripts.export_sse_runtime_schemas"]);
});
await generateValidators();
