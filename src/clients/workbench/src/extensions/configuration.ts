import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { IPolicyService } from '../../upstream/src/vs/platform/policy/common/policy.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IUserDataProfilesService } from '../../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { IWorkspaceTrustManagementService } from '../../upstream/src/vs/platform/workspace/common/workspaceTrust.js';
import { WorkspaceService } from '../../upstream/src/vs/workbench/services/configuration/browser/configurationService.js';
import { ConfigurationCache } from '../../upstream/src/vs/workbench/services/configuration/common/configurationCache.js';
import { IBrowserWorkbenchEnvironmentService } from '../../upstream/src/vs/workbench/services/environment/browser/environmentService.js';
import { IRemoteAgentService } from '../../upstream/src/vs/workbench/services/remote/common/remoteAgentService.js';
import { IUserDataProfileService } from '../../upstream/src/vs/workbench/services/userDataProfile/common/userDataProfile.js';
import type { ExtensionHostConnectionDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { registerTargetConfigurationFiles } from './configurationFiles.js';
import { TargetUriTransformer } from './targetUris.js';

/** 原 WorkspaceService 同时持有配置与 context；所有 MainThread customer 从同一 child 取得它。 */
export async function targetConfigurationServices(parent: IInstantiationService, binding: ExtensionHostConnectionDTO,
  transformer: TargetUriTransformer, lifetime: DisposableStore): Promise<IInstantiationService> {
  const initialization = binding.initialization;
  if (!initialization?.environment?.remote_settings || !initialization.workspace || !transformer.folders.length) {
    throw new Error('目标宿主缺少完整配置初始化环境');
  }
  const workspace = initialization.workspace;
  const owner = parent.invokeFunction(accessor => {
    const files = accessor.get(IFileService), environment = accessor.get(IBrowserWorkbenchEnvironmentService);
    lifetime.add(registerTargetConfigurationFiles(files, binding));
    const cache = new ConfigurationCache(['boxteam', 'boxteam-file', 'vscode-userdata'], environment, files);
    return lifetime.add(new WorkspaceService({ remoteAuthority: JSON.stringify([binding.gateway_id, binding.backend_id, initialization.environment!.profile_id]),
      configurationCache: cache, remoteSettingsResource: URI.parse(initialization.environment!.remote_settings!.resource_uri),
      resolveWorkspaceFolders: (folders, configuration) => transformer.workspaceFolders(folders, configuration),
    }, environment, accessor.get(IUserDataProfileService), accessor.get(IUserDataProfilesService), files,
    accessor.get(IRemoteAgentService), accessor.get(IUriIdentityService), accessor.get(ILogService), accessor.get(IPolicyService)));
  });
  const child = parent.createChild(new ServiceCollection([IConfigurationService, owner], [IWorkspaceContextService, owner]), lifetime);
  owner.acquireInstantiationService(child);
  await owner.initialize(workspace.configuration ? { id: workspace.id, configPath: URI.parse(workspace.configuration.resource_uri) }
    : { id: workspace.id, uri: transformer.folders[0].uri });
  const trust = child.invokeFunction(accessor => accessor.get(IWorkspaceTrustManagementService));
  owner.updateWorkspaceTrust(trust.isWorkspaceTrusted());
  lifetime.add(trust.onDidChangeTrust(trusted => owner.updateWorkspaceTrust(trusted)));
  return child;
}
