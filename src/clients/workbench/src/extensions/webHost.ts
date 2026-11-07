import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { joinPath } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ILabelService } from '../../upstream/src/vs/platform/label/common/label.js';
import { ILayoutService } from '../../upstream/src/vs/platform/layout/browser/layoutService.js';
import { ILogService, ILoggerService } from '../../upstream/src/vs/platform/log/common/log.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IProductService } from '../../upstream/src/vs/platform/product/common/productService.js';
import { IStorageService } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IUserDataProfilesService } from '../../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { IWebWorkerService } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { IBrowserWorkbenchEnvironmentService } from '../../upstream/src/vs/workbench/services/environment/browser/environmentService.js';
import { WebWorkerExtensionHost, type IWebWorkerExtensionHostDataProvider } from '../../upstream/src/vs/workbench/services/extensions/browser/webWorkerExtensionHost.js';
import type { LocalWebWorkerRunningLocation } from '../../upstream/src/vs/workbench/services/extensions/common/extensionRunningLocation.js';
import { ExtensionHostStartup } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { IDefaultLogLevelsService } from '../../upstream/src/vs/workbench/services/log/common/defaultLogLevels.js';
import { targetConfigurationServices } from './configuration.js';
import { targetProfileId, targetWorkspace, type ITargetWebHostConnection, type TargetExtensionContext } from './targetContext.js';

/** 仍由原 LocalWebWorker 执行、握手与 RPC；目标资格和业务 context 不借用 Remote Node。 */
export class TargetWebExtensionHost extends WebWorkerExtensionHost {
  readonly targetContext: TargetExtensionContext;
  customerInstantiationService: IInstantiationService | undefined;
  private readonly targetLifetime = this._register(new DisposableStore());
  private readonly targetCancellation = this._register(new CancellationTokenSource());
  private targetStart: ReturnType<WebWorkerExtensionHost['start']> | undefined;

  constructor(runningLocation: LocalWebWorkerRunningLocation, startup: ExtensionHostStartup, provider: IWebWorkerExtensionHostDataProvider,
    private readonly admission: ITargetWebHostConnection,
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @INotificationService private readonly notifications: INotificationService,
    @ITelemetryService telemetry: ITelemetryService,
    @IWorkspaceContextService context: IWorkspaceContextService,
    @ILabelService labels: ILabelService,
    @ILogService logs: ILogService,
    @ILoggerService loggers: ILoggerService,
    @IBrowserWorkbenchEnvironmentService environment: IBrowserWorkbenchEnvironmentService,
    @IUserDataProfilesService profiles: IUserDataProfilesService,
    @IProductService product: IProductService,
    @ILayoutService layout: ILayoutService,
    @IStorageService storage: IStorageService,
    @IWebWorkerService workers: IWebWorkerService,
    @IDefaultLogLevelsService defaultLogs: IDefaultLogLevelsService,
  ) {
    super(runningLocation, startup, provider, telemetry, context, labels, logs, loggers, environment, profiles, product, layout, storage, workers, defaultLogs);
    this.targetContext = { kind: 'web', connection: admission.connection, gatewayConnectionId: admission.request.gateway_connection_id };
    this._register(admission);
    this._register(admission.onDidClose(reason => { this._onDidExit.fire([1, reason]); this.dispose(); }));
  }

  override start(): ReturnType<WebWorkerExtensionHost['start']> {
    return this.targetStart ??= (async () => {
      this.admission.connection;
      this.customerInstantiationService = await raceCancellationError(
        targetConfigurationServices(this.instantiation, this.targetContext, this.targetLifetime), this.targetCancellation.token);
      this.admission.connection;
      return await raceCancellationError(super.start(), this.targetCancellation.token);
    })().catch(error => {
      this.dispose();
      if (!isCancellationError(error)) this.notifications.error(error instanceof Error ? error : String(error));
      throw error;
    });
  }

  protected override async _createExtHostInitData() {
    this.admission.connection;
    const data = await super._createExtHostInitData(), context = this.targetContext;
    this.admission.connection;
    const { gateway_id, backend_id } = context.connection, workspace = targetWorkspace(context);
    const profileNamespace = encodeURIComponent(JSON.stringify([gateway_id, backend_id, targetProfileId(context)]));
    return { ...data, isTargetExtensionHost: true,
      environment: { ...data.environment,
        globalStorageHome: joinPath(data.environment.globalStorageHome, profileNamespace),
        workspaceStorageHome: joinPath(data.environment.workspaceStorageHome, profileNamespace) },
      workspace: { id: workspace.id, name: workspace.name, configuration: workspace.configuration ? URI.parse(workspace.configuration.resource_uri) : undefined, transient: false },
      logsLocation: joinPath(data.logsLocation, profileNamespace, workspace.id),
    };
  }

  override dispose(): void { this.targetCancellation.cancel(); super.dispose(); }
}
