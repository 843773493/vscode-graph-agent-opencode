import { createWriteStream, existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { finished } from "node:stream/promises";
import yazl from "yazl";
import { parse as parseJsonc } from "jsonc-parser";

const root = path.resolve(process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd());
const output = process.argv[2] ? path.resolve(process.argv[2]) : "";
if (!output.toLowerCase().endsWith(".zip")) {
  throw new Error("用法: bun run plugin:pack:mcp-stdio -- <输出 ZIP 绝对路径>");
}
const packageRoot = path.join(root, "resources/plugins/mcp-stdio");
const files = ["plugin.jsonc", "bridge.py"];
for (const file of files) {
  if (!existsSync(path.join(packageRoot, file))) {
    throw new Error(`MCP stdio 插件缺少 ${file}`);
  }
}
const errors = [];
const manifest = parseJsonc(readFileSync(path.join(packageRoot, "plugin.jsonc"), "utf8"), errors);
if (errors.length || manifest.package_id !== "boxteam.mcp-stdio" || manifest.plugins.length !== 1) {
  throw new Error("MCP stdio 插件 manifest 无效");
}

const archive = new yazl.ZipFile();
for (const file of files) archive.addFile(path.join(packageRoot, file), file, { compress: true });
const stream = createWriteStream(output, { flags: "wx" });
const closed = finished(stream);
archive.outputStream.pipe(stream);
archive.end();
await closed;
