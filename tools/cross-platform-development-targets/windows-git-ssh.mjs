import crossSpawn from "cross-spawn";

export function windowsGitSshArguments([settingsJson, destination, remoteCommand]) {
  const { options, destination: expectedDestination, repository } = JSON.parse(settingsJson);
  const gitQuotedPath = `'${repository.replace(/['!]/g, (character) => `'\\${character}'`)}'`;
  if (destination !== expectedDestination || remoteCommand !== `git-receive-pack ${gitQuotedPath}`) {
    throw new Error("Windows Git SSH 只接受当前目标的快照 receive-pack 命令");
  }
  // TODO: Windows cmd 的百分号展开尚不支持；显式拒绝，避免访问另一个仓库。
  if (/["%\r\n]/.test(repository)) {
    throw new Error("Windows Git SSH 仓库路径不能含双引号、百分号或换行");
  }
  return ["ssh", ...options, destination, `git-receive-pack "${repository}"`];
}

if (import.meta.main) {
  // 原样继承二进制 Git 协议流，不能通过文本解码或 PowerShell pipeline 中转。
  const [command, ...args] = windowsGitSshArguments(process.argv.slice(2));
  const result = crossSpawn.sync(command, args, { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.signal) throw new Error(`Windows Git SSH 被信号终止: ${result.signal}`);
  process.exit(result.status);
}
