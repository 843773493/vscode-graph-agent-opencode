#!/usr/bin/env node

import { main } from "../src/cli.mjs";

try {
  await main(process.argv.slice(2));
} catch (error) {
  process.stderr.write(
    `boxteam: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`,
  );
  process.exitCode = 1;
}
