import { describe, expect, test } from "bun:test";
import type { GatewayWorkspace } from "../../types/backend";
import {
  buildWorkspaceNavigationSyncKey,
} from "./resourceTreeSync";

describe("resourceTreeSync", () => {
  test("工作区状态变化不刷新导航，增删改名会刷新", () => {
    const ready = ([
      { gateway_id: "gateway-test", workspace_id: "00000000-0000-0000-0000-000000000001", name: "工作区", status: "ready" },
    ] as GatewayWorkspace[]);
    const offline = [{ ...ready[0], status: "offline" }] as GatewayWorkspace[];
    const renamed = [{ ...ready[0], name: "新名称" }] as GatewayWorkspace[];

    expect(buildWorkspaceNavigationSyncKey(ready)).toBe(
      buildWorkspaceNavigationSyncKey(offline),
    );
    expect(buildWorkspaceNavigationSyncKey(ready)).not.toBe(
      buildWorkspaceNavigationSyncKey(renamed),
    );
    expect(buildWorkspaceNavigationSyncKey(ready)).not.toBe(
      buildWorkspaceNavigationSyncKey([]),
    );
    const otherGateway = [{ ...ready[0], gateway_id: "gateway-other" }];
    expect(buildWorkspaceNavigationSyncKey(ready)).not.toBe(
      buildWorkspaceNavigationSyncKey(otherGateway),
    );
    expect(buildWorkspaceNavigationSyncKey([...ready, ...otherGateway])).not.toBe(
      buildWorkspaceNavigationSyncKey(ready),
    );
    expect(buildWorkspaceNavigationSyncKey([...ready, ...otherGateway])).toBe(
      buildWorkspaceNavigationSyncKey([...otherGateway, ...ready]),
    );
  });

});
