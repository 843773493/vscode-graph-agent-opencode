import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createDefaultWebUiSettings } from "../../../state/uiSettings/preferences";
import type { GatewayHealth } from "../../../types/backend";
import {
  apiSuccessResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../../tests/fixtures/session/gatewayEnvironment";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
import GatewayControlCenter from "./GatewayControlCenter";

afterEach(restoreGatewayTestEnvironment);

function installHealthyGateway(): void {
  const health: GatewayHealth = {
    status: "ok",
    active_workspace_id: "",
    process_id: 10,
    development_restart_available: false,
    gateway_id: "gateway_local",
  };
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/health") return apiSuccessResponse(health);
    if (path === "/api/gateway/inbound-access") {
      return apiSuccessResponse({ gateway_id: "gateway_local", peers: [], items: [] });
    }
    if (path === "/api/gateway/device-connections") {
      return apiSuccessResponse({ items: [] });
    }
    return undefined;
  });
}

function gatewayControlCenter(
  onRefresh: () => Promise<void>,
  gatewayError: string | null = null,
) {
  const noop = async () => {};
  return (
    <WarmConfirmProvider>
      <GatewayControlCenter
        apiPort={8014}
        workspaces={[]}
        gatewayError={gatewayError}
        onAddSsh={noop}
        onRefresh={onRefresh}
        onReconnect={noop}
        onStart={noop}
        onStop={noop}
        onSafeRestart={noop}
        onForceRestart={noop}
        uiSettings={createDefaultWebUiSettings()}
        onUpdateUiSettings={noop}
      />
    </WarmConfirmProvider>
  );
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe("Gateway 连接页反馈槽", () => {
  test("刷新失败进入固定状态槽，连接卡片仍位于其后", async () => {
    installHealthyGateway();
    let renderer!: ReactTestRenderer;
    try {
      await act(async () => {
        renderer = create(gatewayControlCenter(async () => {
          throw new Error("远程 Gateway 连接失败");
        }));
        await flush();
      });

      const slot = renderer.root.findByProps({ className: "gateway-console-feedback-slot" });
      expect(slot.children).toHaveLength(0);
      const connectionCard = renderer.root.findByProps({
        className: "gateway-local-connection-card",
      });

      await act(async () => {
        const refreshButton = renderer.root.findByProps({ "aria-label": "刷新连接" });
        (refreshButton.props.onClick as () => void)();
        await flush();
      });

      const error = renderer.root.findByProps({
        className: "gateway-console-feedback error",
      });
      expect(error.props.role).toBe("alert");
      expect(error.props.title).toBe("连接操作失败：远程 Gateway 连接失败");
      expect(renderer.root.findByProps({ className: "gateway-local-connection-card" }))
        .toBeDefined();
      expect(renderer.root.findByProps({ className: "gateway-console-feedback-slot" }).children)
        .toHaveLength(1);
      expect(connectionCard.type).toBe("section");
    } finally {
      if (renderer) await act(async () => renderer.unmount());
    }
  });

  test("成功提示与错误提示共用同一状态槽", async () => {
    installHealthyGateway();
    let renderer!: ReactTestRenderer;
    try {
      await act(async () => {
        renderer = create(gatewayControlCenter(async () => {}, "外部 Gateway 错误"));
        await flush();
      });
      const slot = renderer.root.findByProps({ className: "gateway-console-feedback-slot" });
      expect(slot.children).toHaveLength(1);

      await act(async () => {
        const refreshButton = renderer.root.findByProps({ "aria-label": "刷新连接" });
        (refreshButton.props.onClick as () => void)();
        await flush();
      });

      const notice = renderer.root.findByProps({
        className: "gateway-console-feedback notice",
      });
      expect(notice.props.role).toBe("status");
      expect(renderer.root.findByProps({ className: "gateway-console-feedback error" }))
        .toBeDefined();
      const updatedSlot = renderer.root.findByProps({
        className: "gateway-console-feedback-slot",
      });
      expect(updatedSlot.props.className).toBe("gateway-console-feedback-slot");
      expect(updatedSlot.children).toHaveLength(2);
    } finally {
      if (renderer) await act(async () => renderer.unmount());
    }
  });
});
