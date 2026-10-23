import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(fileURLToPath(new URL("..", import.meta.url)));

async function collectJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nestedFiles = await Promise.all(entries.map((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return collectJavaScriptFiles(entryPath);
    }
    return entry.isFile() && /\.(?:m?js)$/.test(entry.name) ? [entryPath] : [];
  }));
  return nestedFiles.flat();
}

const files = (await Promise.all(["src", "scripts", "tests"].map((directory) => (
  collectJavaScriptFiles(path.join(packageRoot, directory))
)))).flat().sort();

const transpiler = new Bun.Transpiler({ loader: "js", target: "node" });
for (const file of files) {
  try {
    transpiler.transformSync(await readFile(file, "utf8"));
  } catch (error) {
    const details = error instanceof Error ? error.message : String(error);
    throw new Error(`JavaScript 语法校验失败 ${file}: ${details}`, { cause: error });
  }
}
