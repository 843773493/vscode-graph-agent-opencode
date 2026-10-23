import path from "node:path";

const packageRoot = path.resolve(import.meta.dir, "..");
const sourceFiles = [
  "src/server/server.js",
  "src/server/terminalManager.js",
  "src/server/protocol.js",
  "src/client/main.js",
].map((filePath) => path.join(packageRoot, filePath));

const result = await Bun.build({
  entrypoints: sourceFiles,
  external: ["node-pty", "ws"],
  target: "bun",
  write: false,
});

if (!result.success) {
  for (const log of result.logs) {
    console.error(log);
  }
  process.exitCode = 1;
}
