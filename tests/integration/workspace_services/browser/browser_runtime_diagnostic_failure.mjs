import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 仅替换被测Node后端的Chromium启动参数和日志writer边界，不代填资源状态。
if (path.basename(process.argv[1] || "") === "backend.js") {
  const control = process.env.BOXTEAM_TEST_BROWSER_RUNTIME_CONTROL;
  const projectRoot = process.env.BOXTEAM_TEST_PROJECT_ROOT;
  if (!control || !projectRoot) throw new Error("Browser运行时故障测试缺少显式control/project root");
  const { chromium } = await import(pathToFileURL(path.join(projectRoot, "node_modules/playwright/index.mjs")));
  const launch = chromium.launch.bind(chromium);
  chromium.launch = options => launch(fs.existsSync(path.join(control, "fail-launch"))
    ? { ...options, executablePath: path.join(control, "missing-chromium") } : options);
  const write = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    if (typeof chunk === "string" && chunk.includes("[browser-runtime] 资源生命周期操作失败")) {
      fs.appendFileSync(path.join(control, "diagnostic-attempts.jsonl"), JSON.stringify({ chunk }) + "\n");
      if (fs.existsSync(path.join(control, "fail-sink"))) {
        // log-parent是真实普通文件，触发原生目录访问失败；观察记录不充当产品日志回退。
        fs.appendFileSync(path.join(control, "log-parent/diagnostic.log"), chunk);
      }
    }
    return write(chunk, ...args);
  };
}
