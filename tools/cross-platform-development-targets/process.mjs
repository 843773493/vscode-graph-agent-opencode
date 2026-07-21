export function decodeOutput(value) {
  return value ? new TextDecoder().decode(value).trim() : "";
}

export function runChecked(command, {
  cwd = process.cwd(),
  environment = process.env,
  stdout = "pipe",
  stderr = "pipe",
  label = command[0],
} = {}) {
  const result = Bun.spawnSync(command, {
    cwd,
    env: environment,
    stdout,
    stderr,
  });
  if (result.exitCode !== 0) {
    const errorOutput = decodeOutput(result.stderr);
    const standardOutput = decodeOutput(result.stdout);
    throw new Error(
      `${label}失败(exit=${result.exitCode})` +
        `${standardOutput ? `\nstdout:\n${standardOutput}` : ""}` +
        `${errorOutput ? `\nstderr:\n${errorOutput}` : ""}`,
    );
  }
  return {
    stdout: decodeOutput(result.stdout),
    stderr: decodeOutput(result.stderr),
    exitCode: result.exitCode,
  };
}

export async function runCheckedWithInput(command, input, {
  cwd = process.cwd(),
  environment = process.env,
  label = command[0],
} = {}) {
  const child = Bun.spawn(command, {
    cwd,
    env: environment,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });
  child.stdin.write(input);
  child.stdin.end();
  const [exitCode, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  if (exitCode !== 0) {
    throw new Error(
      `${label}失败(exit=${exitCode})` +
        `${stdout.trim() ? `\nstdout:\n${stdout.trim()}` : ""}` +
        `${stderr.trim() ? `\nstderr:\n${stderr.trim()}` : ""}`,
    );
  }
  return { exitCode, stdout: stdout.trim(), stderr: stderr.trim() };
}

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}
