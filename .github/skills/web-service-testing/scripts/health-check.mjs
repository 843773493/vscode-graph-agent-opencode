// 用于 Skill 的健康检查和端口状态脚本
// 在 .github/skills/web-service-testing/scripts/ 中运行

import { $ } from "bun";

const host = "127.0.0.1";
const ports = [8000, 8001, 8002];
const services = {
  8000: "后端 FastAPI",
  8001: "前端 Web",
  8002: "Debug 调试",
};

async function checkHealth() {
  console.log("=== 服务健康检查 ===\n");

  for (const port of ports) {
    const url = `http://${host}:${port === 8002 ? 8000 : port}${port === 8000 ? "/api/v1/health" : port === 8001 ? "/health" : ""}`;
    const serviceName = services[port];

    try {
      const response = await fetch(url, { method: "GET" });
      if (response.ok) {
        console.log(`✅ ${serviceName} (端口 ${port}): 运行正常`);
      } else {
        console.log(`⚠️ ${serviceName} (端口 ${port}): HTTP ${response.status}`);
      }
    } catch (error) {
      console.log(`❌ ${serviceName} (端口 ${port}): 无法连接`);
    }
  }

  console.log("\n=== 端口占用情况 ===\n");

  // 检查端口占用
  for (const port of ports) {
    try {
      const conn = await Bun.connect({ hostname: host, port: port });
      conn.end();
      console.log(`🔌 端口 ${port}: 已被占用`);
    } catch {
      console.log(`🟢 端口 ${port}: 空闲`);
    }
  }
}

await checkHealth();
