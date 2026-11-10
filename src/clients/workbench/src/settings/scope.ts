import { createDecorator } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { InstantiationType, registerSingleton } from '../../upstream/src/vs/platform/instantiation/common/extensions.js';
import type { SourceSettings } from './editor.js';
import type { SourceTarget } from './sourceOwner.js';

export interface TargetSettingsLocation extends SourceTarget {
  readonly version: 2;
  readonly source_key: string;
  readonly name: string;
}

/** 每个原 SettingsEditor 只消费自己的来源投影，不修改窗口配置 registry。 */
export const ITargetSettingsContext = createDecorator<ITargetSettingsContext>('boxteamTargetSettingsContext');
export interface ITargetSettingsContext {
  readonly _serviceBrand: undefined;
  readonly target: SourceSettings | undefined;
}
class WindowSettingsContext implements ITargetSettingsContext {
  declare readonly _serviceBrand: undefined;
  readonly target = undefined;
}
registerSingleton(ITargetSettingsContext, WindowSettingsContext, InstantiationType.Delayed);
