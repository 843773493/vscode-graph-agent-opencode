import { t } from '../../shared/core/i18n/index.js';
import { EMPTY_MODEL_PREFERENCES, modelPickerGroups, modelPreferenceId, updateModelPreference,
  type ModelPickerRequest, type ModelPreferences, type PickerModel } from '../../web/src/state/modelPicker.js';
import type { ModelReference } from '../../web/src/types/backend.js';
import { sameModel } from '../../web/src/utils/modelReference.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { Disposable, DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../upstream/src/vs/base/common/themables.js';
import { IQuickInputService, type IQuickInputButton, type IQuickPick, type IQuickPickItem } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IStorageService, StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { createManagementTargetLifecycle } from './managementTarget.js';

interface ModelPick extends IQuickPickItem { model?: PickerModel }
type Choice = { action: 'select' | 'default'; model: ModelReference } | { action: 'manage'; model?: ModelReference };
const button = (icon: ThemeIcon, tooltip: string): IQuickInputButton => ({ iconClass: ThemeIcon.asClassName(icon), tooltip });

/** 候选和写入回调属于发起 Thread；原版控件及存储仅管理展示偏好。 */
export class ComposerModelPicker extends Disposable {
  private current?: IQuickPick<ModelPick, { useSeparators: true }>;
  constructor(@IQuickInputService private readonly quick: IQuickInputService,
    @IStorageService private readonly storage: IStorageService) { super(); }

  async show(owner: { gatewayId: string; workspaceId: string }, request: ModelPickerRequest): Promise<void> {
    if (this._store.isDisposed) throw new Error(t('composer.model.sourceGone'));
    this.hide();
    const key = 'boxteam.modelPicker.' + JSON.stringify([owner.gatewayId, owner.workspaceId]);
    const lifetime = createManagementTargetLifecycle(t('composer.model.sourceGone'));
    const bindings = new DisposableStore();
    const picker = this.current = this.quick.createQuickPick<ModelPick>({ useSeparators: true });
    let showHidden = false;
    let models: PickerModel[] = [];
    const load = () => this.storage.getObject<ModelPreferences>(key, StorageScope.WORKSPACE, EMPTY_MODEL_PREFERENCES);
    const save = (model: ModelReference, action: 'favorite' | 'hide' | 'use') => {
      lifetime.check();
      this.storage.store(key, updateModelPreference(load(), model, action), StorageScope.WORKSPACE, StorageTarget.MACHINE);
    };
    const favorite = button(Codicon.starEmpty, t('composer.model.favorite'));
    const unfavorite = button(Codicon.starFull, t('composer.model.unfavorite'));
    const hide = button(Codicon.eyeClosed, t('composer.model.hide'));
    const unhide = button(Codicon.eye, t('composer.model.unhide'));
    const defaults = button(Codicon.pin, t('composer.model.makeDefault'));
    const settings = button(Codicon.settingsGear, t('workbench.settings.modelsProviders'));
    const hiddenToggle = button(Codicon.eye, t('composer.model.showHidden'));
    const visibleToggle = button(Codicon.eyeClosed, t('composer.model.hideHidden'));
    const render = () => {
      const preferences = load(), active = picker.activeItems[0]?.id;
      picker.buttons = [showHidden ? visibleToggle : hiddenToggle];
      picker.items = modelPickerGroups(models, request.currentModel, preferences, showHidden).flatMap(group => [
        { type: 'separator' as const, label: group.kind === 'provider'
          ? group.models[0].provider_name ?? group.providerId : t(`composer.model.${group.kind}`) },
        ...group.models.map(model => ({ id: modelPreferenceId(model.model_ref), label: model.name ?? model.model,
          description: [`${model.model_ref.provider_id} / ${model.model_ref.model_id}`,
            model.name ? model.model : '', model.provider_name ?? '', model.custom_llm_provider].filter(Boolean).join(' · '),
          detail: [sameModel(model.model_ref, request.currentModel) ? t('composer.model.current') : '',
            model.workspace_default ? t('composer.model.default') : '',
            preferences.hidden.includes(modelPreferenceId(model.model_ref)) ? t('composer.model.hidden') : '',
            model.configuration_error ?? ''].filter(Boolean).join(' · '),
          model, buttons: [preferences.favorites.includes(modelPreferenceId(model.model_ref)) ? unfavorite : favorite,
            preferences.hidden.includes(modelPreferenceId(model.model_ref)) ? unhide : hide,
            ...(model.available && !model.configuration_error && !model.workspace_default ? [defaults] : []),
            ...(request.onManage ? [settings] : [])],
        })),
      ]);
      if (request.onManage) picker.items = [...picker.items, { label: t('workbench.settings.modelsProviders'), id: 'manage' }];
      const focused = picker.items.find((item): item is ModelPick => item.type !== 'separator' && item.id === active);
      if (focused) picker.activeItems = [focused];
    };
    picker.title = t('composer.model.search');
    picker.placeholder = t('composer.model.search');
    picker.matchOnDescription = true;
    picker.matchOnDetail = true;
    picker.sortByLabel = false;
    picker.keepScrollPosition = true;
    try {
      const choice = await new Promise<Choice | undefined>((resolve, reject) => {
        let open = true;
        const choose = (choice: Choice) => { resolve(choice); picker.hide(); };
        bindings.add(lifetime.token.onCancellationRequested(() => picker.hide()));
        bindings.add(picker.onDidHide(() => { open = false; resolve(undefined); }));
        bindings.add(this.storage.onDidChangeValue(StorageScope.WORKSPACE, key, bindings)(render));
        bindings.add(picker.onDidTriggerButton(() => { showHidden = !showHidden; render(); }));
        bindings.add(picker.onDidTriggerItemButton(event => {
          const reference = event.item.model?.model_ref;
          if (!reference) return;
          try {
            if (event.button === favorite || event.button === unfavorite) save(reference, 'favorite');
            else if (event.button === hide || event.button === unhide) save(reference, 'hide');
            else choose({ action: event.button === defaults ? 'default' : 'manage', model: reference });
          } catch (error) { reject(error); picker.hide(); }
        }));
        bindings.add(picker.onDidAccept(() => {
          const item = picker.selectedItems[0];
          if (!item) return;
          if (!item.model) { choose({ action: 'manage' }); return; }
          if (!item.model.available || item.model.configuration_error) {
            picker.validationMessage = item.model.configuration_error || t('composer.model.unavailable'); return;
          }
          choose({ action: 'select', model: item.model.model_ref });
        }));
        // 先建立原生控件生命周期，隐藏后不再呈现迟到的候选。
        picker.busy = true;
        picker.show();
        void (async () => {
          try {
            const loaded = await request.loadModels();
            if (!open) return;
            lifetime.check();
            models = loaded; picker.busy = false; render();
          } catch (error) { if (open) { reject(error); picker.hide(); } }
        })();
      });
      if (!choice) return;
      lifetime.check();
      if (choice.action === 'manage') request.onManage?.(choice.model);
      else if (choice.action === 'default') await request.onSetWorkspaceDefault(choice.model);
      else { await request.onSelect(choice.model); save(choice.model, 'use'); }
    } finally {
      bindings.dispose(); picker.dispose(); lifetime.dispose();
      if (this.current === picker) this.current = undefined;
    }
  }
  hide(): void { this.current?.hide(); }
  override dispose(): void { this.hide(); super.dispose(); }
}
