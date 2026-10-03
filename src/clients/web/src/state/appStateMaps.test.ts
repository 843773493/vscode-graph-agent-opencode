import { describe, expect, test } from "bun:test";

import { INITIAL_APP_STATE } from "../hooks/app/appStateSeed";
import type { AppState } from "../types/frontend";
import { cloneMaps } from "./appStateMaps";

function seededState(): AppState {
  return {
    ...INITIAL_APP_STATE,
    activeGatewayWorkspaceId: "ws-a",
    removingGatewayWorkspaceIds: new Set(["ws-a"]),
  };
}

describe("cloneMaps 集合快照", () => {
  test("removingGatewayWorkspaceIds 也必须是独立克隆", () => {
    const source = seededState();
    const clone = cloneMaps(source);

    expect(clone.removingGatewayWorkspaceIds).not.toBe(
      source.removingGatewayWorkspaceIds,
    );
    clone.removingGatewayWorkspaceIds.add("ws-b");
    expect([...source.removingGatewayWorkspaceIds]).toEqual(["ws-a"]);
  });

  test("AppState 的每个 Map/Set 字段都必须脱离源实例", () => {
    const source = seededState();
    const clone = cloneMaps(source);
    const collectionFields = Object.keys(source).filter((key) => {
      const value = (source as unknown as Record<string, unknown>)[key];
      return value instanceof Map || value instanceof Set;
    });
    expect(collectionFields.length).toBeGreaterThan(0);
    for (const field of collectionFields) {
      expect((clone as unknown as Record<string, unknown>)[field]).not.toBe(
        (source as unknown as Record<string, unknown>)[field],
      );
    }
  });
});
