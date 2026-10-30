/** 辅助服务退出时的 HTTP 收尾；到期关闭连接并明确失败，不声称外部资源已停止。 */
export async function closeHttpServer(server) {
  let timer;
  try {
    await new Promise((resolve, reject) => {
      timer = setTimeout(() => {
        reject(Object.assign(new Error("HTTP 服务排空超过 5000 毫秒，关闭未完成的连接"), {
          code: "http_server_drain_timeout",
        }));
        // 普通关闭先核实 Manager；致命错误仍沿原退出1合同，不以断网代替停止证明。
        server.closeAllConnections();
      }, 5000);
      server.close((error) => {
        if (error) reject(error);
        else resolve();
      });
    });
  } finally {
    clearTimeout(timer);
  }
}
