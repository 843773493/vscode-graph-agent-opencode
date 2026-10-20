import path from "node:path";
import { readdir, readFile } from "node:fs/promises";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const testsRoot = path.join(root, "src/workspace-services");
const transpiler = new Bun.Transpiler({ loader: "js" });
const groups = new Map([["node:test", []], ["bun:test", []]]);

async function discover(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory() && !["node_modules", "generated"].includes(entry.name)) {
      await discover(target);
    } else if (entry.isFile() && entry.name.endsWith(".test.js")) {
      const imports = transpiler.scanImports(await readFile(target, "utf8"));
      const frameworks = [...new Set(imports.map(item => item.path).filter(name => groups.has(name)))];
      if (frameworks.length !== 1) {
        throw new Error(`辅助服务测试必须且只能导入node:test或bun:test之一: ${target}`);
      }
      groups.get(frameworks[0]).push(target);
    }
  }
}

await discover(testsRoot);
if (![...groups.values()].some(files => files.length > 0)) throw new Error(`未发现辅助服务测试: ${testsRoot}`);
let failed = false;
for (const [framework, files] of groups) {
  if (files.length === 0) continue;
  const command = framework === "node:test" ? ["node", "--test", ...files] : ["bun", "test", ...files];
  console.log(`[workspace-services] ${framework}: ${files.length}个文件`);
  const result = Bun.spawnSync(command, { cwd: root, stdout: "inherit", stderr: "inherit" });
  if (result.exitCode !== 0 || result.signalCode) {
    failed = true;
    console.error(`[workspace-services] ${framework}失败: exit=${result.exitCode}, signal=${result.signalCode ?? "none"}`);
  }
}
process.exitCode = failed ? 1 : 0;
