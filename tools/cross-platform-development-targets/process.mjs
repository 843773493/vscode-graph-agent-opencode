import { once } from "node:events";
import { Readable } from "node:stream";
import { text } from "node:stream/consumers";
import { pipeline } from "node:stream/promises";
import crossSpawn from "cross-spawn";

function checkedResult(label, { status, signal, stdout, stderr }, inputError) {
  const standardOutput = stdout?.toString("utf8").trim() ?? "";
  const errorOutput = stderr?.toString("utf8").trim() ?? "";
  if (status !== 0 || signal) {
    const termination = signal ? `exit=${status}, signal=${signal}` : `exit=${status}`;
    throw new Error(
      `${label}失败(${termination})` +
        `${standardOutput ? `\nstdout:\n${standardOutput}` : ""}` +
        `${errorOutput ? `\nstderr:\n${errorOutput}` : ""}`,
      inputError ? { cause: inputError } : undefined,
    );
  }
  if (inputError) throw new Error(`${label}输入失败: ${inputError.message}`, { cause: inputError });
  return { stdout: standardOutput, stderr: errorOutput, exitCode: status };
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
    result = crossSpawn.sync(command[0], command.slice(1), {
      cwd,
      env: environment,
      stdio: ["ignore", stdout, stderr],
      maxBuffer: Infinity,
    });
    if (result.error) throw result.error;
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${label}启动失败: ${detail}`, { cause });
  }
  return checkedResult(label, result);
}

export async function runCheckedWithInput(command, input, {
  cwd = process.cwd(),
  environment = process.env,
  label = command[0],
} = {}) {
  let result;
  try {
    const child = crossSpawn(command[0], command.slice(1), {
      cwd,
      env: environment,
      stdio: "pipe",
    });
    result = await Promise.all([
      once(child, "close"),
      text(child.stdout),
      text(child.stderr),
      // 先收齐退出码和诊断；提前关闭stdin时，写入错误不能覆盖命令的真实失败。
      pipeline(Readable.from([Buffer.from(input, "utf8")]), child.stdin)
        .then(() => undefined, (error) => error),
    ]);
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${label}启动失败: ${detail}`, { cause });
  }

  const [[status, signal], stdout, stderr, inputError] = result;
  return checkedResult(label, { status, signal, stdout, stderr }, inputError);
}

export function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}
