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
  let result;
  try {
    result = Bun.spawnSync(command, {
      cwd,
      env: environment,
      stdout,
      stderr,
    });
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${label}启动失败: ${detail}`, { cause });
  }
  const signaled = result.signalCode !== null && result.signalCode !== undefined;
  if (result.exitCode !== 0 || signaled) {
    const errorOutput = decodeOutput(result.stderr);
    const standardOutput = decodeOutput(result.stdout);
    const termination = signaled
      ? `exit=${result.exitCode}, signal=${result.signalCode}`
      : `exit=${result.exitCode}`;
    throw new Error(
      `${label}失败(${termination})` +
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
  let child;
  try {
    child = Bun.spawn(command, {
      cwd,
      env: environment,
      stdin: Buffer.from(input, "utf8"),
      stdout: "pipe",
      stderr: "pipe",
    });
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${label}启动失败: ${detail}`, { cause });
  }

  const [exitCode, standardOutput, errorOutput] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  const signaled = child.signalCode !== null && child.signalCode !== undefined;
  if (exitCode !== 0 || signaled) {
    const termination = signaled
      ? `exit=${exitCode}, signal=${child.signalCode}`
      : `exit=${exitCode}`;
    throw new Error(
      `${label}失败(${termination})` +
        `${standardOutput.trim() ? `\nstdout:\n${standardOutput.trim()}` : ""}` +
        `${errorOutput.trim() ? `\nstderr:\n${errorOutput.trim()}` : ""}`,
    );
  }
  return { exitCode, stdout: standardOutput.trim(), stderr: errorOutput.trim() };
}

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}
