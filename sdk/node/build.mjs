// 开发只转换运行 JS；类型检查与发行声明使用同一份 TypeScript 源码。
import { build, context } from "esbuild";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

function filesIn(directory, directories = []) {
  directories.push(directory);
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? filesIn(file, directories) : entry.name === "AGENTS.md" ? [] : [file];
  });
}

export async function buildNodeSdk({ projectRoot = process.cwd(), outputDirectory, watch = false,
  check = false, release = false, onBuild = () => {} } = {}) {
  if (watch && (check || release)) throw new Error("watch 只用于开发运行构建");
  if (check && release) throw new Error("check 与 release 不能同时使用");
  const root = path.resolve(projectRoot, "sdk/node");
  const source = path.join(root, "src");
  const output = path.resolve(outputDirectory ?? path.join(root, "dist"));
  const assets = () => {
    const directories = [];
    const files = [...filesIn(path.join(source, "generated"), directories), ...filesIn(path.join(source, "resources"), directories)];
    return { files, directories };
  };
  const assetContents = files => new Map(files.map(file => [path.join(output, path.relative(source, file)), readFileSync(file)]));
  let firstBuild = true;
  async function publish(outputs) {
    let changed = false;
    for (const [destination, content] of outputs) {
      if (existsSync(destination) && readFileSync(destination).equals(content)) continue;
      mkdirSync(path.dirname(destination), { recursive: true });
      writeFileSync(destination, content);
      changed = true;
    }
    for (const file of filesIn(output)) {
      if (outputs.has(file)) continue;
      rmSync(file);
      changed = true;
    }
    if (changed || firstBuild) await onBuild();
    firstBuild = false;
  }
  if (check || release) {
    const { default: ts } = await import("typescript");
    const configPath = path.join(root, "tsconfig.jsonc");
    const loaded = ts.readConfigFile(configPath, ts.sys.readFile);
    const host = { getCurrentDirectory: () => projectRoot, getCanonicalFileName: file => file, getNewLine: () => "\n" };
    const config = ts.parseJsonConfigFileContent(loaded.config, ts.sys, root,
      { outDir: output, noEmit: check, emitDeclarationOnly: false }, configPath);
    const outputs = release ? assetContents(assets().files) : null;
    const program = ts.createProgram(config.fileNames, config.options);
    const diagnostics = [...(loaded.error ? [loaded.error] : []), ...config.errors, ...ts.getPreEmitDiagnostics(program)];
    if (diagnostics.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, host));
    if (check) return;
    // JS 和声明由同一 Program 的源码快照生成，不在检查后重新读取 TS。
    const emitted = program.emit(undefined, (file, content) => outputs.set(path.resolve(file), Buffer.from(content)));
    if (emitted.emitSkipped || emitted.diagnostics.length) {
      throw new Error(ts.formatDiagnosticsWithColorAndContext(emitted.diagnostics, host) || "TypeScript 未生成 SDK 产物");
    }
    rmSync(output, { recursive: true, force: true });
    await publish(outputs);
    return;
  }
  const config = {
    absWorkingDir: root,
    entryPoints: ["src/*.ts", "src/generated-json/**/*.ts"],
    outbase: source, outdir: output,
    platform: "node", target: "node22", format: "esm", bundle: false, write: false,
    tsconfig: path.join(root, "tsconfig.jsonc"),
    plugins: [{ name: "sdk-assets", setup(builder) {
      builder.onLoad({ filter: /[/\\]index\.ts$/ }, args => {
        const tree = assets();
        return { contents: readFileSync(args.path, "utf8"), loader: "ts",
          watchFiles: tree.files, watchDirs: [source, ...tree.directories] };
      });
      builder.onEnd(async result => {
        if (result.errors.length) return;
        const outputs = assetContents(assets().files);
        for (const file of result.outputFiles) outputs.set(file.path, file.contents);
        await publish(outputs);
      });
    } }],
  };
  if (!watch) { await build(config); return; }
  const compiler = await context(config);
  try {
    await compiler.rebuild();
    await compiler.watch();
    return compiler;
  } catch (error) { await compiler.dispose(); throw error; }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: { watch: { type: "boolean" }, check: { type: "boolean" },
    release: { type: "boolean" }, "out-dir": { type: "string" } } });
  let compiler;
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    if (compiler) await compiler.dispose();
  };
  for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => {
    void stop().catch(error => { console.error(error); process.exitCode = 1; });
  });
  compiler = await buildNodeSdk({ projectRoot: process.cwd(), outputDirectory: values["out-dir"],
    watch: values.watch, check: values.check, release: values.release });
  if (stopping && compiler) await compiler.dispose();
}
