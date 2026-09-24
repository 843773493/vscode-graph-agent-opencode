import { ItemizedDemoStore } from "./storage.js";

const reset = process.argv.includes("--reset");
const json = process.argv.includes("--json");
const store = new ItemizedDemoStore("demo");

await store.initialize({ reset });
const inspected = await store.inspect();
store.close();

if (json) {
  console.log(JSON.stringify(inspected, null, 2));
} else {
  console.log("Itemized Context Storage Demo");
  console.log(`运行时根目录: ${inspected.runtimeRoot}`);
  console.log(`canonical items: ${inspected.catalog.length}`);
  console.log(`active view: ${inspected.view.filter((item) => item.included).length} included / ${inspected.view.length} traced`);
  console.log(`model wire messages: ${inspected.plan.wire_request.messages.length}`);
  console.log(`transcript entries: ${inspected.transcript.entries.length}`);
  console.log("\n可以观察的文件:");
  for (const file of inspected.files) {
    console.log(`  ${file.path} (${file.bytes} bytes)`);
  }
  console.log("\n关键事实:");
  console.log("  - rollout/rollout.jsonl 保存每个 canonical item 的完整 payload，一行一个 item。");
  console.log("  - rollout/index.sqlite 只保存 item_catalog、projection 和 view 定位/状态。");
  console.log("  - request-plan.json 的 request_only 不会写入 rollout.jsonl。");
  console.log("  - transcript.json 是用户界面投影，不等于模型请求上下文。");
}
