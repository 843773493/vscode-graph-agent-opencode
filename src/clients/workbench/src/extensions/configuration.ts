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
import { registerTargetConfigurationFiles } from './configurationFiles.js';
import { TargetWorkspaceResources } from './targetUris.js';
import { targetProfileId, targetSettings, targetWorkspace, type TargetConfigurationContext } from './targetContext.js';

/** 原 WorkspaceService 同时持有配置与 context；所有 MainThread customer 从同一 child 取得它。 */
export async function targetConfigurationServices(parent: IInstantiationService, context: TargetConfigurationContext,
  lifetime: DisposableStore, services = new ServiceCollection()): Promise<IInstantiationService> {
  const workspace = targetWorkspace(context), settings = targetSettings(context), binding = context.connection;
  const resources = new TargetWorkspaceResources(context);
  const owner = parent.invokeFunction(accessor => {
    const files = accessor.get(IFileService), environment = accessor.get(IBrowserWorkbenchEnvironmentService);
    lifetime.add(registerTargetConfigurationFiles(files, context));
    const cache = new ConfigurationCache(['boxteam', 'boxteam-file', 'vscode-userdata'], environment, files);
    return lifetime.add(new WorkspaceService({ remoteAuthority: JSON.stringify([binding.gateway_id, binding.backend_id, targetProfileId(context)]),
      configurationCache: cache, remoteSettingsResource: URI.parse(settings.resource_uri),
      resolveWorkspaceFolders: (folders, configuration) => resources.workspaceFolders(folders, configuration),
    }, environment, accessor.get(IUserDataProfileService), accessor.get(IUserDataProfilesService), files,
    accessor.get(IRemoteAgentService), accessor.get(IUriIdentityService), accessor.get(ILogService), accessor.get(IPolicyService)));
  });
  services.set(IConfigurationService, owner);
  services.set(IWorkspaceContextService, owner);
  const child = parent.createChild(services, lifetime);
  owner.acquireInstantiationService(child);
  await owner.initialize(workspace.configuration ? { id: workspace.id, configPath: URI.parse(workspace.configuration.resource_uri) }
    : resources.folders.length ? { id: workspace.id, uri: resources.folders[0].uri } : { id: workspace.id });
  const trust = child.invokeFunction(accessor => accessor.get(IWorkspaceTrustManagementService));
  owner.updateWorkspaceTrust(trust.isWorkspaceTrusted());
  lifetime.add(trust.onDidChangeTrust(trusted => owner.updateWorkspaceTrust(trusted)));
  return child;
}
