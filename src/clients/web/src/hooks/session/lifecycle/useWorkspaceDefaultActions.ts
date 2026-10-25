import { useCallback } from "react";
import {
  listAgents as apiListAgents,
  setWorkspaceDefaultAgent as apiSetWorkspaceDefaultAgent,
  setWorkspaceDefaultProvider as apiSetWorkspaceDefaultProvider,
} from "../../../api";
import { errorMessage } from "../../../utils/errorMessage";
import type { SessionLifecycleContext } from "./sessionLifecycleContext";

export function useWorkspaceDefaultActions(context: SessionLifecycleContext) {
  const {
    apiPort, currentSessionGatewayWorkspaceId, activeGatewayWorkspaceId,
    defaultGatewayWorkspaceId, setState,
  } = context;

  const setWorkspaceDefaultAgent = useCallback(
    async (agentId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认 Agent 的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认 Agent: ${agentId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultAgent(
          apiPort,
          agentId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${agentId} 设为工作区默认 Agent，仅影响新会话`,
        }));
      } catch (error) {
        const message = errorMessage(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认 Agent 失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认 Agent 失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const setWorkspaceDefaultProvider = useCallback(
    async (agentId: string, providerId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认模型的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认模型: ${providerId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultProvider(
          apiPort,
          agentId,
          providerId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${providerId} 设为 ${agentId} 的工作区默认模型，仅影响新会话`,
        }));
      } catch (error) {
        const message = errorMessage(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认模型失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认模型失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  return { setWorkspaceDefaultAgent, setWorkspaceDefaultProvider };
}
