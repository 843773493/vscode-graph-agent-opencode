import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

const jsFiles = [
  "src/server/config.js",
  "src/server/fileStore.js",
  "src/server/http.js",
  "src/server/server.js",
  "src/server/staticFiles.js",
  "src/server/targetFileClient.js",
  "src/server/tunnel.js",
  "src/client/main.js",
  "scripts/check.mjs",
  "scripts/docker-up.mjs",
  "scripts/roles-up.mjs",
  "tests/config.test.js",
  "tests/fileStore.test.js",
];

const jsonFiles = [
  "config/targets.json",
  "package.json",
];

for (const file of jsFiles) {
  const result = spawnSync(process.execPath, ["--check", file], {
    stdio: "inherit",
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

for (const file of jsonFiles) {
  JSON.parse(readFileSync(file, "utf8"));
}
