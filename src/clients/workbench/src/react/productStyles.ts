import product from '../../../web/src/index.css?text';
import panel from '../../../web/src/styles/foundation/panelShared.css?text';
import agentState from '../../../web/src/styles/session/agentState.css?text';
import eventQueue from '../../../web/src/styles/session/eventQueue.css?text';
import requestLog from '../../../web/src/styles/session/requestLog.css?text';
import resources from '../../../web/src/styles/workspace/resourcePanel.css?text';
import tasks from '../../../web/src/styles/workspace/persistentTasks.css?text';
import childThread from '../../../web/src/styles/session/childThreadPanel.css?text';
import workspace from '../../../web/src/styles/workspace/workspace.css?text';
import sessions from '../../../web/src/styles/session/agentSessionsPanel.css?text';
import explorer from '../../../web/src/styles/workspace/sessionResourceExplorer.css?text';
import layout from '../../../web/src/styles/foundation/workbenchLayout.css?text';
import tools from '../../../web/src/styles/session/toolControl.css?text';
import chat from '../../../web/src/styles/session/chatMessages.css?text';
import diagnostics from '../../../web/src/styles/workspace/gatewayDiagnostics.css?text';
import connections from '../../../web/src/styles/workspace/gatewayConnections.css?text';
import gateway from '../../../web/src/styles/workspace/gatewayConsole.css?text';
import debug from '../../../web/src/styles/inspection/debugPanel.css?text';
import contextInspection from '../../../web/src/styles/inspection/contextInspection.css?text';
import surfaces from '../../../web/src/styles/foundation/themeSurfaces.css?text';

/** 原 Web 的完整产品样式，统一限制在各个 React ShadowRoot 内。 */
export const productStyles = [product, panel, agentState, eventQueue, requestLog, resources, tasks, childThread,
  workspace, sessions, explorer, layout, tools, chat, diagnostics, connections, gateway, debug, surfaces, contextInspection];
