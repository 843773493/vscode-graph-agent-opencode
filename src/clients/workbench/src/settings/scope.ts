import { createDecorator } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { InstantiationType, registerSingleton } from '../../upstream/src/vs/platform/instantiation/common/extensions.js';
import type { TargetConfigurationContext } from '../extensions/targetContext.js';

export interface TargetSettingsLocation {
  readonly version: 1;
  readonly gateway_id: string;
  readonly backend_id: string;
  readonly profile_id: string;
  readonly view_id: string;
  readonly workspace_ids: readonly string[];
  readonly name: string;
  readonly gateway_connection_id?: string;
}

/** 仅承载当前设置输入的冻结归属；配置模型和文件仍由原 owner 持有。 */
export const ITargetSettingsContext = createDecorator<ITargetSettingsContext>('boxteamTargetSettingsContext');
export interface ITargetSettingsContext {
  readonly _serviceBrand: undefined;
  readonly target: { readonly location: TargetSettingsLocation; readonly configuration: TargetConfigurationContext } | undefined;
}

class WindowSettingsContext implements ITargetSettingsContext {
  declare readonly _serviceBrand: undefined;
  readonly target = undefined;
}
registerSingleton(ITargetSettingsContext, WindowSettingsContext, InstantiationType.Delayed);
