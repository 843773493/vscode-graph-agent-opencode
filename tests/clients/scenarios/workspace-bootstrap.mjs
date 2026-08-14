import { webSelectors } from "../selectors/web-selectors.mjs";

export async function runWorkspaceBootstrapScenario(driver) {
  await driver.open("/");
  await driver.page.locator(webSelectors.appRoot).waitFor({ state: "visible" });
  const healthResponse = await driver.page.request.get(
    new URL("/api/gateway/health", driver.baseUrl).toString(),
  );
  if (!healthResponse.ok()) {
    throw new Error(
      `Gateway health 请求失败: HTTP ${healthResponse.status()} ${await healthResponse.text()}`,
    );
  }
  const workspacesResponse = await driver.page.request.get(
    new URL("/api/gateway/workspaces", driver.baseUrl).toString(),
  );
  if (!workspacesResponse.ok()) {
    throw new Error(
      `Gateway workspaces 请求失败: HTTP ${workspacesResponse.status()} ${await workspacesResponse.text()}`,
    );
  }
  return {
    health: await healthResponse.json(),
    workspaces: await workspacesResponse.json(),
  };
}
