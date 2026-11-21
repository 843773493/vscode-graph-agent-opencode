import { t } from '../../shared/core/i18n/index.js';
import { EMPTY_MODEL_PREFERENCES, modelPickerGroups, modelPreferenceId, updateModelPreference,
  type ModelPickerRequest, type ModelPreferences, type PickerModel } from '../../web/src/state/modelPicker.js';
import type { ModelReference } from '../../web/src/types/backend.js';
import { sameModel } from '../../web/src/utils/modelReference.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { Action, toAction } from '../upstream/src/vs/base/common/actions.js';
import { AnchorPosition } from '../upstream/src/vs/base/browser/ui/contextview/contextview.js';
import { ActionListItemKind, type IActionListItem } from '../upstream/src/vs/platform/actionWidget/browser/actionList.js';
import { IActionWidgetService } from '../upstream/src/vs/platform/actionWidget/browser/actionWidget.js';
import { Disposable, DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../upstream/src/vs/base/common/themables.js';
import { IQuickInputService, type IQuickInputButton, type IQuickPickItem } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IStorageService, StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { createManagementTargetLifecycle } from './managementTarget.js';
import { workspaceContext } from './workspaceContext.js';

interface ModelPick extends IQuickPickItem { model?: PickerModel }
type Choice = { action: 'select' | 'default'; model: ModelReference; checked?: boolean; id?: string } | { action: 'manage'; model?: ModelReference } | { action: 'quick' };
const button = (icon: ThemeIcon, tooltip: string): IQuickInputButton => ({ iconClass: ThemeIcon.asClassName(icon), tooltip });

/** 候选和写入回调属于发起 Thread；原版控件及存储仅管理展示偏好。 */
export class ComposerModelPicker extends Disposable {
  private current?: { hide(): void };
  constructor(@IQuickInputService private readonly quick: IQuickInputService,
    @IStorageService private readonly storage: IStorageService,
    @IActionWidgetService private readonly widget: IActionWidgetService) { super(); }

  async show(owner: { gatewayId: string; workspaceId: string }, request: ModelPickerRequest): Promise<void> {
    await workspaceContext.ensureReady();
    if (this._store.isDisposed || request.anchor && !request.anchor.checkVisibility()) throw new Error(t('composer.model.sourceGone'));
    this.hide();
    const key = 'boxteam.modelPicker.' + JSON.stringify([owner.gatewayId, owner.workspaceId]);
    const lifetime = createManagementTargetLifecycle(t('composer.model.sourceGone'));
    const bindings = new DisposableStore();
    const picker = request.anchor ? undefined : this.quick.createQuickPick<ModelPick>({ useSeparators: true });
    let shown = false;
    let loading = true;
    let open = true;
    let choose: (choice: Choice) => void;
    const current = this.current = { hide: () => {
      if (!open) return;
      if (picker) picker.hide(); else this.widget.hide();
    } };
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
    const hiddenAction = bindings.add(new Action('hidden', t('composer.model.showHidden'), ThemeIcon.asClassName(Codicon.eye), true, async () => {
      showHidden = !showHidden;
      hiddenAction.label = t(showHidden ? 'composer.model.hideHidden' : 'composer.model.showHidden');
      render();
    }));
    const render = () => {
      const preferences = load();
      const groups = modelPickerGroups(models, request.currentModel, preferences, showHidden);
      if (!picker) {
        const action = (id: string, label: string, run: () => void, icon?: ThemeIcon) => toAction({ id, label,
          class: icon ? ThemeIcon.asClassName(icon) : undefined, run });
        const items: IActionListItem<Choice>[] = groups.flatMap(group => [
          { kind: ActionListItemKind.Header, label: group.kind === 'provider'
            ? group.models[0].provider_name ?? group.providerId : t('composer.model.favorites') },
          ...group.models.map(model => {
            const reference = model.model_ref, id = modelPreferenceId(reference);
            const checked = sameModel(reference, request.currentModel);
            const selectable = model.available && !model.configuration_error;
            const favorite = preferences.favorites.includes(id), hidden = preferences.hidden.includes(id);
            return { kind: ActionListItemKind.Action,
              item: { action: 'select', model: reference, checked, id } as Choice,
              label: model.name ?? model.model, disabled: !selectable,
              group: { title: '', icon: checked ? Codicon.check : Codicon.blank },
              description: model.workspace_default ? t('composer.model.default') : undefined,
              filterText: `${reference.provider_id} / ${reference.model_id} ${model.model} ${model.provider_name ?? ''}`,
              tooltip: model.configuration_error || `${model.name ?? model.model}\n${reference.provider_id} / ${reference.model_id}`,
              toolbarActions: [
                action('favorite', t(favorite ? 'composer.model.unfavorite' : 'composer.model.favorite'), () => save(reference, 'favorite'), favorite ? Codicon.starFull : Codicon.starEmpty),
                action('hide', t(hidden ? 'composer.model.unhide' : 'composer.model.hide'), () => save(reference, 'hide'), hidden ? Codicon.eye : Codicon.eyeClosed),
                ...(selectable && !model.workspace_default ? [action('default', t('composer.model.makeDefault'), () => choose({ action: 'default', model: reference }), Codicon.pin)] : []),
                ...(request.onManage ? [action('settings', t('workbench.settings.modelsProviders'), () => choose({ action: 'manage', model: reference }), Codicon.settingsGear)] : []),
              ],
            };
          }),
        ]);
        if (!models.length) items.push({ kind: ActionListItemKind.Action, label: t(loading ? 'composer.model.loading' : 'composer.model.unavailable'), disabled: true });
        if (request.onManage) items.push({ kind: ActionListItemKind.Separator }, { kind: ActionListItemKind.Action,
          item: { action: 'manage' }, label: t('workbench.settings.modelsProviders'), group: { title: '', icon: Codicon.settingsGear } });
        if (shown) {
          const focused = this.widget.getFocusedElement<Choice>()?.item;
          const id = focused && 'id' in focused ? focused.id : undefined;
          // 列表重建会移除行内按钮；沿原接口恢复搜索焦点，避免更新被误判为失焦关闭。
          const next = items.find(item => id !== undefined && item.item && 'id' in item.item && item.item.id === id)
            ?? items.find(item => !item.disabled && item.item && 'id' in item.item);
          this.widget.updateItems(items, next?.item && 'id' in next.item ? next.item.id : undefined,
            { preserveScrollPosition: true, focusFilter: true });
        }
        else {
          // 原生列表异步更新不重设外框宽度；两层使用同一有界宽度，避免候选超出可点击区域。
          const width = Math.min(360, request.anchor!.ownerDocument.documentElement.clientWidth * 0.8 - 12);
          this.widget.show('boxteam-model-picker', false, items, {
            onSelect: choice => choose(choice),
            onHide: () => { open = false; finish(); if (request.anchor?.isConnected) request.anchor.focus(); },
          }, request.anchor!, undefined, undefined, { getWidgetAriaLabel: () => t('composer.model.search') }, {
            showFilter: true, filterAsCombobox: true, filterPlaceholder: t('composer.model.search'), focusFilterOnOpen: true,
            filterActions: [hiddenAction],
            footerText: t('composer.model.quickPick'),
            footerAction: action('quick', t('composer.model.quickPick'), () => choose({ action: 'quick' })),
            preferredAnchorPosition: AnchorPosition.ABOVE, hideDefaultKeybindingTooltip: true,
            minWidth: width, maxWidth: width, maxVisibleItems: 10,
            stopToolbarPointerPropagation: true, tabThroughItemActions: true, multipleSelectionSupport: false,
          });
          shown = true;
        }
        return;
      }
      const active = picker.activeItems[0]?.id;
      picker.buttons = [showHidden ? visibleToggle : hiddenToggle];
      picker.items = groups.flatMap(group => [
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
    if (picker) {
      picker.title = t('composer.model.search');
      picker.placeholder = t('composer.model.search');
      picker.matchOnDescription = true;
      picker.matchOnDetail = true;
      picker.sortByLabel = false;
      picker.keepScrollPosition = true;
    }
    let finish: () => void;
    try {
      const choice = await new Promise<Choice | undefined>((resolve, reject) => {
        finish = () => resolve(undefined);
        choose = (choice: Choice) => { resolve(choice); current.hide(); };
        bindings.add(lifetime.token.onCancellationRequested(() => current.hide()));
        bindings.add(this.storage.onDidChangeValue(StorageScope.WORKSPACE, key, bindings)(render));
        if (picker) {
          bindings.add(picker.onDidHide(() => { open = false; resolve(undefined); }));
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
        } else render();
        void (async () => {
          try {
            const loaded = await request.loadModels();
            if (!open) return;
            lifetime.check();
            models = loaded; loading = false; if (picker) picker.busy = false; render();
          } catch (error) { if (open) { reject(error); current.hide(); } }
        })();
      });
      if (!choice) return;
      lifetime.check();
      if (choice.action === 'quick') await this.show(owner, { ...request, anchor: undefined });
      else if (choice.action === 'manage') request.onManage?.(choice.model);
      else if (choice.action === 'default') await request.onSetWorkspaceDefault(choice.model);
      else { await request.onSelect(choice.model); save(choice.model, 'use'); }
    } finally {
      bindings.dispose(); picker?.dispose(); lifetime.dispose();
      if (this.current === current) this.current = undefined;
    }
  }
  hide(): void { this.current?.hide(); }
  override dispose(): void { this.hide(); super.dispose(); }
}
