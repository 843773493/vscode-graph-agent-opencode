import path from "node:path";

const demoRoot = path.resolve(import.meta.dir, "..");
const buildResults = await Promise.all([
  Bun.build({
    entrypoints: [
      path.join(demoRoot, "src/cli.js"),
      path.join(demoRoot, "src/server.js"),
    ],
    target: "bun",
    write: false,
  }),
  Bun.build({
    entrypoints: [path.join(demoRoot, "src/web/main.js")],
    target: "browser",
    write: false,
  }),
]);

const failedResults = buildResults.filter((result) => !result.success);
for (const result of failedResults) {
  for (const log of result.logs) {
    console.error(log);
  }
}

if (failedResults.length > 0) {
  process.exitCode = 1;
}
