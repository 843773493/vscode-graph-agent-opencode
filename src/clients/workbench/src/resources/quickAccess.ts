import { formatNumber, t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { Limiter } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { matchesFuzzy } from '../../upstream/src/vs/base/common/filters.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { PickerQuickAccessProvider, TriggerAction, type IPickerQuickAccessItem } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService, type IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { IEditorGroupsService, preferredSideBySideGroupDirection } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { HttpRequestError, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { ResourceNavigationResultDTO, SessionChangesetListDTO, ThreadAttachmentListDTO, ThreadToolResultListDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { LocalSkillsMetadataDTO, SkillSourcesMetadataDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/skill_sources.js';
import type { GatewaySkillSourcesMetadataDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/skill_sources.js';
import { listWorkspaceResources, type ResourceAction, type WorkspaceResourceDTO } from '../../../web/src/api/workspaceResources.js';
import { resourceActionLabels, workspaceResourceName } from '../../../web/src/hooks/workspace/resourceActions.js';
import { getMcpCatalog } from '../../../web/src/api/mcpConnections.js';
import { gateway } from '../agentApi.js';
import { ThreadPane, type ThreadEditorOptions } from '../threadPane.js';
import type { ResourceOpenOptions } from '../resourceOpener.js';
import { chooseSearchOwners } from '../search/quickAccessScope.js';
import type { SearchInvocationOptions } from '../search/target.js';
import { workspaceContext } from '../workspaceContext.js';
import { ThreadInput } from '../threadInput.js';
import { ATTACH_RESOURCE_URI, COPY_RESOURCE_URI, type ResourceReferenceOwner } from '../resourceActions.js';
import type { ComposerResourceTarget } from '../composerActions.js';
import { sourceDocumentUri } from '../sourceDocuments.js';
import { resourceActionCommand, resourceFocusCommand } from './runtimeActions.js';
import { openWorkspaceMcpServerCommand } from '../mcp/commands.js';
import { ConfigurationSourceOwner, type SourceTarget } from '../settings/sourceOwner.js';
import { openConfigurationSourceCommand, type ConfigurationSourceOpenRequest } from '../settings/commands.js';

interface SourcePick extends IPickerQuickAccessItem { id: string; filterLabel?: string; resourceId?: string; resource?: URI; resourceOwner?: ResourceReferenceOwner }
export interface ResourceAccessOptions extends SearchInvocationOptions { resourceGroup?: number; resourceThread?: SourceRun['thread']; resourceRecipient?: ComposerResourceTarget | null; resourceReferencesOnly?: boolean; resourceReferenceBack?: () => void }
interface SourceRun {
  picker: IQuickPick<SourcePick, { useSeparators: true }>;
  owners: readonly WorkspaceOwner[];
  gatewayId: string | null;
  group: number;
  ownerError?: string;
  invalidated: boolean;
  signal: AbortSignal;
  discovery?: Promise<void>;
  picks: SourcePick[][];
  loading: boolean;
  refresh?: () => void;
  thread?: { owner: WorkspaceOwner; session_id: string; thread_id: string };
  recipient?: ComposerResourceTarget | null;
  referencesOnly: boolean;
  hasFocusedPick: boolean;
}

/** 来源候选沿固定根导航发现本地文件，配置 Skill 只消费 accepted metadata。 */
export class ResourceQuickAccess extends PickerQuickAccessProvider<SourcePick> {
  static readonly PREFIX = 'resource ';
  private run?: SourceRun;
  constructor(@IQuickInputService private readonly quick: IQuickInputService,
    @IOpenerService private readonly opener: IOpenerService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
    @IEditorService private readonly editors: IEditorService,
  ) { super(ResourceQuickAccess.PREFIX, { canAcceptInBackground: true,
    noResultsPick: { id: 'no-sources', label: t('quickAccess.sources.noResults') } }); }

  override provide(picker: SourceRun['picker'], token: CancellationToken, options: ResourceAccessOptions = {}) {
    const lifetime = new DisposableStore(), access = new CancellationTokenSource(token), controller = new AbortController();
    const run: SourceRun = { picker, owners: (options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : [])).map(owner => ({ ...owner })),
      gatewayId: workspaceContext.getSnapshot().gatewayId, group: options.resourceGroup ?? this.groups.activeGroup.id,
      ownerError: options.searchOwnerError, invalidated: false, signal: controller.signal, referencesOnly: options.resourceReferencesOnly === true,
      picks: [], loading: true, hasFocusedPick: false };
    this.run = run;
    const input = this.editors.activeEditor;
    const pane = this.editors.activeEditorPane;
    run.recipient = options.resourceRecipient !== undefined ? options.resourceRecipient
      : pane instanceof ThreadPane ? { pane, target: pane.threadTarget } : null;
    run.thread = options.resourceThread && { ...options.resourceThread, owner: { ...options.resourceThread.owner } };
    if (!run.thread && input instanceof ThreadInput) run.thread = { owner: { ...input.owner }, session_id: input.owner.sessionId, thread_id: input.owner.threadId };
    const invalidate = () => {
      run.invalidated = true;
      access.cancel();
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'source-access-invalidated', label: t('quickAccess.sources.identityChanged') }];
      picker.busy = false;
    };
    lifetime.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidate(); })));
    const scope = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: t('quickAccess.sources.chooseWorkspace') };
    const back = run.referencesOnly && options.resourceReferenceBack ? this.quick.backButton : undefined;
    picker.buttons = [...picker.buttons, ...(back ? [back] : run.referencesOnly ? [] : [scope])];
    picker.placeholder = run.referencesOnly ? t('quickAccess.sources.referencesPlaceholder') : t('quickAccess.sources.placeholder');
    lifetime.add(picker.onDidTriggerButton(button => {
      if (back && button === back) {
        this.assertAccess(run);
        options.resourceReferenceBack!();
        picker.hide();
        return;
      }
      if (button !== scope) return;
      const value = picker.value;
      this.report((async () => {
        let invalidated = false;
        const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
        const releaseGateway = workspaceContext.subscribe(() => {
          if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidated = true;
        });
        try {
          picker.hide();
          const owners = await chooseSearchOwners(this.quick, run.owners);
          if (invalidated) throw new Error(t('quickAccess.sources.workspaceCredentialsChanged'));
          this.assertAccess(run);
          const providerOptions: ResourceAccessOptions = { ...options, searchOwners: owners ?? run.owners, resourceGroup: run.group, resourceThread: run.thread, resourceRecipient: run.recipient };
          this.quick.quickAccess.show(value, { providerOptions });
        } finally { releaseToken(); releaseGateway(); }
      })());
    }));
    lifetime.add(access.token.onCancellationRequested(() => controller.abort()));
    // 发现只属于本次 provide；逐键取消只阻止旧筛选发布，结束时才取消实际读取。
    lifetime.add(toDisposable(() => { controller.abort(); access.dispose(true); picker.busy = false; }));
    lifetime.add(super.provide(picker, access.token, options));
    lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(button => button !== scope && button !== back); if (this.run === run) this.run = undefined; }));
    return lifetime;
  }

  protected override _getPicks(filter: string, _disposables: DisposableStore, token: CancellationToken): SourcePick[] | null {
    const run = this.run!;
    if (run.invalidated) return [{ id: 'source-access-invalidated', label: t('quickAccess.sources.identityChanged') }];
    if (token.isCancellationRequested || run.signal.aborted) return null;
    run.refresh = () => {
      if (token.isCancellationRequested || run.signal.aborted || run.invalidated || this.run !== run) return;
      const currentActive = run.picker.activeItems[0];
      const currentActiveIsPick = !!currentActive && !currentActive.disabled
        && (typeof currentActive.accept === 'function' || typeof currentActive.trigger === 'function');
      if (currentActiveIsPick) run.hasFocusedPick = true;
      const selected = new Set(run.picker.selectedItems.map(item => item.id));
      const keep = run.picker.keepScrollPosition;
      try {
        run.picker.keepScrollPosition = true;
        const picks = this.sourcePicks(run, filter);
        const active = currentActiveIsPick
          ? picks.find(item => item.id === currentActive.id && !item.disabled && (item.accept || item.trigger))
          : undefined;
        const nextActive = active ?? (!run.hasFocusedPick ? picks.find(item => typeof item.accept === 'function' && !item.disabled) : undefined);
        if (nextActive) run.hasFocusedPick = true;
        run.picker.items = picks;
        const selectedPicks = picks.filter(item => selected.has(item.id) && typeof item.accept === 'function' && !item.disabled);
        run.picker.selectedItems = selectedPicks;
        run.picker.activeItems = nextActive ? [nextActive] : [];
        run.picker.busy = run.loading;
      } finally { run.picker.keepScrollPosition = keep; }
    };
    run.discovery ??= this.discover(run).finally(() => { run.loading = false; run.refresh?.(); });
    run.picker.busy = run.loading;
    return this.sourcePicks(run, filter);
  }

  private sourcePicks(run: SourceRun, filter: string): SourcePick[] {
    const picks = run.picks.flat().flatMap(item => {
      const candidate = run.referencesOnly ? item.resource ? { ...item, buttons: [], trigger: undefined } : { ...item, disabled: true } : item;
      if (item.filterLabel === undefined) return [candidate];
      const highlights = matchesFuzzy(filter, item.filterLabel);
      return filter && !highlights && !item.resourceId?.includes(filter) ? [] : [{ ...candidate, highlights: { label: highlights ?? undefined } }];
    });
    if (run.loading) picks.push({ id: 'source-pending', label: t('quickAccess.sources.loading'), disabled: true });
    return picks.length ? picks : [{ id: 'no-sources', label: t('quickAccess.sources.noResults') }];
  }

  private async discover(run: SourceRun): Promise<void> {
    const collect = async (pending: Promise<SourcePick[]>): Promise<void> => {
      // 槽位按原来源顺序分配，完成顺序只影响何时显示，不改变分组顺序。
      const index = run.picks.length;
      run.picks.push([]);
      const picks = await pending;
      if (run.signal.aborted || run.invalidated) return;
      run.picks[index] = picks;
      if (picks.length) run.refresh?.();
    };
    if (!run.owners.length) {
      run.picks = [[{ id: 'source-owner-required', label: run.ownerError ?? t('quickAccess.sources.workspaceRequired'),
        detail: t('quickAccess.sources.workspaceHint') }]];
      return;
    }
    const limiter = new Limiter<SourcePick[]>(3);
    // kind 同时属于已有候选身份；只翻译展示标签，不改变稳定 id。
    const sourceKindLabels = {
      '': '', 'AGENTS ': 'AGENTS ', 'Skill ': 'Skill ', 'Gateway Skill ': 'Gateway Skill ', 'MCP ': 'MCP ',
      'Gateway 配置 ': t('quickAccess.sources.gatewayConfigurationKind'), '配置 ': t('quickAccess.sources.configurationKind'),
      '附件 ': t('quickAccess.sources.attachmentKind'), '工具结果 ': t('quickAccess.sources.toolKind'),
      '变更 ': t('quickAccess.sources.changesKind'), '本地 Skill ': t('quickAccess.sources.localSkillKind'),
      '终端': t('quickAccess.sources.terminalKind'), '浏览器': t('quickAccess.sources.browserKind'),
    };
    const failed = (owner: WorkspaceOwner, kind: keyof typeof sourceKindLabels, cause: unknown): SourcePick[] => [{
      id: `${gatewayWorkspaceKey(owner)}/${kind}/failed`, label: t('quickAccess.sources.unavailable', sourceKindLabels[kind], cause instanceof Error ? cause.message : String(cause)),
      detail: cause instanceof HttpRequestError && cause.requestId ? `request_id: ${cause.requestId}` : gatewayWorkspaceKey(owner),
    }];
    const pick = (owner: WorkspaceOwner, uri: string, label: string, description: string, available = true, errorCode?: string): SourcePick => {
      const item: SourcePick = { id: `${gatewayWorkspaceKey(owner)}/${uri}`, label: available ? label : t('quickAccess.sources.unavailableLabel', label),
        filterLabel: label, description, detail: `${owner.gatewayId} · ${owner.workspaceId} · ${uri}${errorCode ? ` · ${errorCode}` : ''}` };
      if (available) {
        item.resource = sourceDocumentUri(owner, uri);
        item.resourceOwner = { ...owner };
        item.buttons = [{ iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('quickAccess.copyUri') },
          { iconClass: ThemeIcon.asClassName(Codicon.attach), tooltip: t('quickAccess.sources.attachUri') }];
        item.trigger = index => {
          this.assertAccess(run);
          this.report(this.commands.executeCommand(index === 0 ? COPY_RESOURCE_URI : ATTACH_RESOURCE_URI, item.resource, { ...owner }, run.recipient));
          return TriggerAction.NO_ACTION;
        };
        item.accept = (mods, event) => {
          this.assertAccess(run);
          const pinned = this.configuration.getValue<boolean>('workbench.editor.enablePreviewFromQuickOpen') === false
            || this.configuration.getValue<boolean>('workbench.editor.enablePreview') === false;
          const openOptions: ResourceOpenOptions = { fromUserGesture: true, allowContributedOpeners: true,
            boxteamResourceOwner: { ...owner }, boxteamTargetGroup: run.group, openToSide: mods.alt || pinned && mods.ctrlCmd,
            editorOptions: { pinned: mods.ctrlCmd || event.inBackground || pinned, preserveFocus: event.inBackground } };
          this.report(this.opener.open(uri, openOptions));
        };
      }
      return item;
    };
    const continuePage = (id: string, label: string, description: string, read: () => Promise<SourcePick[]>): SourcePick => {
      let nextPage: Promise<TriggerAction> | undefined;
      return { id, label, description,
        buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: label }],
        trigger: () => nextPage ??= (async () => {
          try {
            this.assertAccess(run);
            const next = await read();
            run.signal.throwIfAborted();
            this.assertAccess(run);
            run.picks = run.picks.map(picks => picks.flatMap(item => item.id === id ? next : [item]));
            return TriggerAction.REFRESH_PICKER;
          } catch (cause) {
            nextPage = undefined;
            if (!run.signal.aborted) this.notifications.error(cause instanceof Error ? cause.message : String(cause));
            return TriggerAction.NO_ACTION;
          }
        })(),
      };
    };
    const threadResourceAccept = (owner: WorkspaceOwner, viewState: Partial<ReturnType<ThreadInput['getViewState']>>, navigation?: ThreadEditorOptions['boxteamMessage']): SourcePick['accept'] => (mods, event) => {
      this.report((async () => {
        this.assertAccess(run);
        const source = this.groups.getGroup(run.group), thread = run.thread!;
        if (!source) throw new Error(t('quickAccess.resource.invokingGroupClosed'));
        const pinned = this.configuration.getValue<boolean>('workbench.editor.enablePreviewFromQuickOpen') === false
          || this.configuration.getValue<boolean>('workbench.editor.enablePreview') === false;
        const direction = preferredSideBySideGroupDirection(this.configuration);
        const destination = mods.alt || pinned && mods.ctrlCmd
          ? this.groups.findGroup({ direction }, source) ?? this.groups.addGroup(source, direction) : source;
        await this.editors.openEditor(new ThreadInput({ ...owner, workspaceKey: gatewayWorkspaceKey(owner),
          sessionId: thread.session_id, threadId: thread.thread_id, title: thread.thread_id }), {
          pinned: mods.ctrlCmd || event.inBackground || pinned, preserveFocus: event.inBackground,
          ...(navigation ? { boxteamMessage: navigation } : {}),
          viewState: { resourcesVisible: !navigation, ...viewState },
        }, destination.id);
      })());
    };
    const configurationPicks = async (target: SourceTarget, name: string): Promise<SourcePick[]> => {
      run.signal.throwIfAborted();
      const owner = new ConfigurationSourceOwner(target), result = await owner.list(run.signal);
      return result.sources.flatMap(source => {
        if (!source.source_key) return [];
        const label = source.source_key + '.jsonc';
        return [{ id: JSON.stringify([target.gateway_id, target.workspace_id, 'configuration', source.source_key]),
          label, filterLabel: label, description: t('quickAccess.sources.configuration', name, source.layer, source.presence),
          detail: `${target.gateway_id}${target.workspace_id ? ` · ${target.workspace_id}` : ' · Gateway'}${source.uri ? ` · ${source.uri}` : ''}`,
          ...(source.uri ? {
            resource: URI.parse(source.uri), resourceOwner: { gatewayId: target.gateway_id, workspaceId: target.workspace_id },
            buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('quickAccess.copyUri') },
              { iconClass: ThemeIcon.asClassName(Codicon.attach), tooltip: t('quickAccess.sources.attachUri') }],
            trigger: (index: number) => {
              this.assertAccess(run);
              this.report(this.commands.executeCommand(index === 0 ? COPY_RESOURCE_URI : ATTACH_RESOURCE_URI, URI.parse(source.uri!), {
                gatewayId: target.gateway_id, workspaceId: target.workspace_id,
              }, run.recipient));
              return TriggerAction.NO_ACTION;
            },
          } : {}),
          accept: (mods, event) => {
            this.assertAccess(run);
            const pinned = this.configuration.getValue<boolean>('workbench.editor.enablePreviewFromQuickOpen') === false
              || this.configuration.getValue<boolean>('workbench.editor.enablePreview') === false;
            const request: ConfigurationSourceOpenRequest = {
              location: { ...owner.target, version: 2, source_key: source.source_key!, name },
              groupId: run.group, openToSide: mods.alt || pinned && mods.ctrlCmd,
              editorOptions: { pinned: mods.ctrlCmd || event.inBackground || pinned, preserveFocus: event.inBackground },
            };
            this.report(this.commands.executeCommand(openConfigurationSourceCommand, request));
          },
        } satisfies SourcePick];
      });
    };
    try {
      this.assertAccess(run);
      const routes = await gateway.workspaces(false, run.signal);
      const registeredOwners = run.owners.filter(owner => routes.items.some(target => target.gateway_id === owner.gatewayId && target.workspace_id === owner.workspaceId));
      const gatewayConfigurations = [...new Map(registeredOwners.map(owner => [owner.gatewayId, owner])).values()].map(owner =>
        limiter.queue(() => configurationPicks({ gateway_id: owner.gatewayId }, 'Gateway'))
          .catch(cause => failed(owner, 'Gateway 配置 ', cause)));
      const results = run.owners.map(async owner => {
        try {
          run.signal.throwIfAborted();
          const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
          if (target.status !== 'ready') throw new Error(t('quickAccess.sources.workspaceUnavailable', target.name, target.status));
          const key = gatewayWorkspaceKey(target);
          const configuration = limiter.queue(() => configurationPicks({ gateway_id: target.gateway_id, workspace_id: target.workspace_id }, target.name))
            .catch(cause => failed(owner, '配置 ', cause));
          const attachmentPicks = async (after = ''): Promise<SourcePick[]> => {
            const thread = run.thread!;
            run.signal.throwIfAborted();
            const result = await gateway.request<ThreadAttachmentListDTO>(
              `/api/v1/sessions/${encodeURIComponent(thread.session_id)}/threads/${encodeURIComponent(thread.thread_id)}/attachments?${new URLSearchParams({ limit: '100', after })}`,
              { signal: run.signal }, key);
            if (result.session_id !== thread.session_id || result.thread_id !== thread.thread_id) throw new Error(t('quickAccess.sources.attachmentOwnerMismatch'));
            const picks: SourcePick[] = result.items.map(attachment => {
              const label = attachment.name ?? attachment.file_id, frozen = { ...attachment };
              return { id: JSON.stringify([owner.gatewayId, owner.workspaceId, thread.session_id, thread.thread_id, 'attachment', attachment.file_id]),
                label: attachment.availability === 'unavailable' ? t('quickAccess.sources.unavailableLabel', label) : label, filterLabel: label, resourceId: attachment.file_id,
                description: t('quickAccess.sources.attachment', target.name, attachment.content_type ?? 'application/octet-stream'),
                ...(attachment.availability === 'unavailable' ? {} : { resource: URI.parse(attachment.file_id), resourceOwner: { ...owner } }),
                detail: `${owner.gatewayId} · ${owner.workspaceId} · ${thread.session_id} · ${thread.thread_id} · ${attachment.file_id}`,
                accept: attachment.availability === 'unavailable' ? undefined : threadResourceAccept(owner, { resourceTab: 'files', selectedAttachment: frozen }),
                buttons: attachment.availability === 'unavailable' ? undefined
                  : [{ iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('quickAccess.copyUri') },
                    { iconClass: ThemeIcon.asClassName(Codicon.attach), tooltip: t('quickAccess.sources.attachUri') }],
                trigger: index => {
                  this.assertAccess(run);
                  this.report(this.commands.executeCommand(index === 0 ? COPY_RESOURCE_URI : ATTACH_RESOURCE_URI,
                    URI.parse(frozen.file_id), { ...owner }, run.recipient));
                  return TriggerAction.NO_ACTION;
                },
              };
            });
            if (result.next_after) picks.push(continuePage(`${key}/attachments/after/${result.next_after}`, t('quickAccess.sources.moreAttachments'),
              t('quickAccess.sources.moreAttachmentsHint'), () => attachmentPicks(result.next_after!)));
            return picks;
          };
          const attachments = run.thread?.owner.gatewayId === owner.gatewayId && run.thread.owner.workspaceId === owner.workspaceId
            ? limiter.queue(() => attachmentPicks()).catch(cause => failed(owner, '附件 ', cause)) : Promise.resolve([]);
          const toolPicks = async (page?: ThreadToolResultListDTO): Promise<SourcePick[]> => {
            const thread = run.thread!;
            run.signal.throwIfAborted();
            const query = new URLSearchParams({ limit: '100' });
            if (page?.next_after != null) {
              query.set('after', String(page.next_after));
              if (!page.source_view_id) throw new Error(t('quickAccess.sources.toolViewMissing'));
              query.set('view_id', page.source_view_id);
              query.set('view_revision', String(page.source_view_revision));
            }
            const result = await gateway.request<ThreadToolResultListDTO>(
              `/api/v1/sessions/${encodeURIComponent(thread.session_id)}/threads/${encodeURIComponent(thread.thread_id)}/tool-results?${query}`,
              { signal: run.signal }, key);
            if (result.session_id !== thread.session_id || result.thread_id !== thread.thread_id) throw new Error(t('quickAccess.sources.toolOwnerMismatch'));
            const picks: SourcePick[] = result.items.map(tool => ({
              id: JSON.stringify([owner.gatewayId, owner.workspaceId, thread.session_id, thread.thread_id, 'tool-result', tool.item_id]),
              label: tool.tool_name, filterLabel: tool.tool_name, resourceId: tool.item_id,
              description: t('quickAccess.sources.tool', target.name, tool.status),
              detail: `${owner.gatewayId} · ${owner.workspaceId} · ${thread.session_id} · ${thread.thread_id} · ${tool.turn_id} · ${tool.tool_call_id}`,
              accept: threadResourceAccept(owner, {}, { ...tool }),
            }));
            if (result.next_after != null) picks.push(continuePage(`${key}/tool-results/after/${result.next_after}`,
              t('quickAccess.sources.moreTools'), t('quickAccess.sources.moreToolsHint'), () => toolPicks(result)));
            return picks;
          };
          const tools = !run.referencesOnly && run.thread?.owner.gatewayId === owner.gatewayId && run.thread.owner.workspaceId === owner.workspaceId
            ? limiter.queue(() => toolPicks()).catch(cause => failed(owner, '工具结果 ', cause)) : Promise.resolve([]);
          const changes = !run.referencesOnly && run.thread?.owner.gatewayId === owner.gatewayId && run.thread.owner.workspaceId === owner.workspaceId
            ? limiter.queue(async () => {
              const thread = run.thread!;
              run.signal.throwIfAborted();
              const result = await gateway.request<SessionChangesetListDTO>(`/api/v1/sessions/${encodeURIComponent(thread.session_id)}/changesets`,
                { signal: run.signal }, key);
              if (result.session_id !== thread.session_id) throw new Error(t('quickAccess.sources.changesOwnerMismatch'));
              return result.items.map(changeset => ({
                id: JSON.stringify([owner.gatewayId, owner.workspaceId, thread.session_id, 'changeset', changeset.changeset_id]),
                label: changeset.label, filterLabel: changeset.label, resourceId: changeset.changeset_id,
                description: t('quickAccess.sources.changes', target.name, formatNumber(changeset.summary?.files ?? 0)),
                detail: `${owner.gatewayId} · ${owner.workspaceId} · ${thread.session_id} · ${changeset.changeset_id}`,
                accept: threadResourceAccept(owner, { resourceTab: 'changes', selectedChangesetId: changeset.changeset_id }),
              } satisfies SourcePick));
            }).catch(cause => failed(owner, '变更 ', cause)) : Promise.resolve([]);
          const agents = limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await gateway.request<ResourceNavigationResultDTO>('/api/v1/resources/agent-spec', { signal: run.signal }, key);
            const node = result.node;
            if (!node || node.resource_shape !== 'file' || !node.operations.includes('read')) throw new Error(t('quickAccess.sources.agentReadUnavailable'));
            return [pick(owner, node.uri, 'AGENTS.md', t('quickAccess.sources.workspaceRoot', target.name))];
          }).catch(cause => failed(owner, 'AGENTS ', cause));
          const skills = limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await gateway.request<SkillSourcesMetadataDTO>('/api/v1/resources/skill-sources', { signal: run.signal }, key);
            if (result.workspace_id !== owner.workspaceId) throw new Error(t('quickAccess.sources.skillOwnerMismatch'));
            return result.entries.map(entry => pick(owner, entry.display_uri, entry.name, t('quickAccess.sources.configuredSkill', target.name, formatNumber(entry.priority)),
              entry.available, entry.error_code));
          }).catch(cause => failed(owner, 'Skill ', cause));
          const gatewaySkills = limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await gateway.request<GatewaySkillSourcesMetadataDTO>(
              '/api/v1/resources/gateway-skills',
              { signal: run.signal }, key);
            if (result.gateway_id !== owner.gatewayId || result.workspace_id !== owner.workspaceId) {
              throw new Error(t('quickAccess.sources.gatewaySkillOwnerMismatch'));
            }
            const entries = result.entries.map(entry => pick(owner, entry.display_uri, entry.name,
              t('quickAccess.sources.skill', entry.plugin ? t('quickAccess.sources.pluginSkill', entry.plugin.package_id, entry.plugin.plugin_id, entry.plugin.package_version) : 'Gateway Skill', target.name, formatNumber(entry.priority)),
              entry.available, entry.error_code));
            return result.available ? entries : [...entries, ...failed(owner, 'Gateway Skill ', new Error(result.error_code ?? 'source-unavailable'))];
          }).catch(cause => failed(owner, 'Gateway Skill ', cause));
          const localSkills = limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await gateway.request<LocalSkillsMetadataDTO>('/api/v1/resources/local-skills?limit=1000', { signal: run.signal }, key);
            if (result.workspace_id !== owner.workspaceId) throw new Error(t('quickAccess.sources.localSkillOwnerMismatch'));
            return result.entries.map(entry => {
              if (entry.node?.resource_shape !== 'file' || !entry.node.operations.includes('read')) throw new Error(t('quickAccess.sources.localSkillReadUnavailable'));
              return pick(owner, entry.node.uri, entry.name, t('quickAccess.sources.localSkill', target.name));
            });
          }).catch(cause => failed(owner, '本地 Skill ', cause));
          const runtime = (run.referencesOnly ? [] : ['terminal', 'browser'] as const).map(kind => limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await listWorkspaceResources(0, key, kind, run.signal);
            if (result.workspace_id !== owner.workspaceId || result.items.some(item => item.workspace_id !== owner.workspaceId || item.kind !== kind)) {
              throw new Error(t('quickAccess.sources.runtimeOwnerMismatch'));
            }
            return result.items.map(resource => {
              const frozen = structuredClone(resource), label = workspaceResourceName(resource);
              return { id: `${key}/${kind}/${resource.resource_id}`, label, filterLabel: label, resourceId: resource.resource_id,
                description: `${kind === 'terminal' ? t('resources.kind.terminal') : t('resources.kind.browser')} · ${resource.status} · ${target.name}`,
                detail: `${owner.gatewayId} · ${owner.workspaceId} · ${resource.resource_id} · revision ${resource.owner_revision}`,
                accept: () => {
                  this.assertAccess(run);
                  this.report(this.commands.executeCommand(resourceFocusCommand, { owner: { ...owner }, resource: frozen }));
                },
                buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.gear), tooltip: t('quickAccess.sources.moreActions') }],
                trigger: () => { this.report(this.chooseRuntimeAction(run, { ...owner }, frozen)); return TriggerAction.CLOSE_PICKER; },
              };
            });
          }).catch(cause => failed(owner, kind === 'terminal' ? '终端' : '浏览器', cause)));
          const mcp = run.referencesOnly ? Promise.resolve([]) : limiter.queue(async () => {
            run.signal.throwIfAborted();
            const result = await getMcpCatalog(0, target, { refresh: false, signal: run.signal });
            const frozenTarget = { gateway_id: target.gateway_id, workspace_id: target.workspace_id, backend_id: target.backend_id };
            return result.data.map(server => {
              const commandTarget = { target: frozenTarget, serverId: server.server_id };
              return {
                id: `${key}/mcp/${server.server_id}`, label: server.server_id, filterLabel: server.server_id,
                description: `MCP · ${target.name} · ${server.status}`,
                detail: `${owner.gatewayId} · ${owner.workspaceId}${server.unavailable_reason ? ` · ${server.unavailable_reason}` : ''}`,
                accept: () => {
                  this.assertAccess(run);
                  this.report(this.commands.executeCommand(openWorkspaceMcpServerCommand, commandTarget));
                },
                buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.gear), tooltip: t('quickAccess.sources.moreMcpActions') }],
                trigger: () => {
                  this.assertAccess(run);
                  this.report(this.commands.executeCommand(openWorkspaceMcpServerCommand, { ...commandTarget, showOptions: true }));
                  return TriggerAction.CLOSE_PICKER;
                },
              };
            });
          }).catch(cause => failed(owner, 'MCP ', cause));
          await Promise.all([agents, skills, gatewaySkills, localSkills, configuration, attachments, tools, changes, ...runtime, mcp].map(collect));
        } catch (cause) { await collect(Promise.resolve(failed(owner, '', cause))); }
      });
      await Promise.all([...results, ...gatewayConfigurations.map(collect)]);
    } catch (cause) {
      if (run.signal.aborted || run.invalidated) return;
      run.picks.push([{ id: 'source-read-failed', label: t('quickAccess.sources.failed', cause instanceof Error ? cause.message : String(cause)) }]);
    } finally { limiter.dispose(); }
  }
  private assertAccess(run: SourceRun): void {
    if (run.invalidated || workspaceContext.getSnapshot().gatewayId !== run.gatewayId) throw new Error(t('quickAccess.sources.identityChanged'));
  }
  private async chooseRuntimeAction(run: SourceRun, owner: WorkspaceOwner, resource: WorkspaceResourceDTO): Promise<void> {
    const value = run.picker.value;
    let invalidated = false;
    const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    const releaseGateway = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidated = true;
    });
    try {
      this.assertAccess(run);
      const actions = (Object.keys(resourceActionLabels) as ResourceAction[]).filter(action => resource.available_actions.includes(action));
      if (!actions.length) throw new Error(t('quickAccess.sources.noActions'));
      const selected = await this.quick.pick(actions.map(action => ({ action, label: resourceActionLabels[action] })),
        { title: t('quickAccess.sources.actionsTitle', workspaceResourceName(resource)), placeHolder: `${owner.gatewayId} · ${owner.workspaceId} · revision ${resource.owner_revision}` });
      if (invalidated) throw new Error(t('quickAccess.sources.credentialsChanged'));
      this.assertAccess(run);
      if (!selected) {
        const providerOptions: ResourceAccessOptions = { searchOwners: run.owners, resourceGroup: run.group, resourceThread: run.thread };
        this.quick.quickAccess.show(value, { providerOptions });
        return;
      }
      const thread = selected.action === 'attach' && run.thread?.owner.gatewayId === owner.gatewayId && run.thread.owner.workspaceId === owner.workspaceId
        ? { session_id: run.thread.session_id, thread_id: run.thread.thread_id } : undefined;
      await this.commands.executeCommand(resourceActionCommand(selected.action), { owner, resource, thread });
    } finally { releaseToken(); releaseGateway(); }
  }
  private report(operation: Promise<unknown>): void { void operation.catch(cause => this.notifications.error(cause)); }
}

Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
  ctor: ResourceQuickAccess, prefix: ResourceQuickAccess.PREFIX, placeholder: t('quickAccess.sources.registeredPlaceholder'),
  helpEntries: [{ description: t('quickAccess.sources.help'), commandId: 'boxteam.quickAccess.resources', commandCenterOrder: 44 }],
});
registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.quickAccess.resources', title: { value: t('quickAccess.sources.actionTitle'), original: actionTitles['quickAccess.sources.actionTitle'] }, f1: true }); }
  run(accessor: ServicesAccessor, owner?: WorkspaceOwner): void {
    const providerOptions: SearchInvocationOptions | undefined = owner ? { searchOwner: owner } : undefined;
    accessor.get(IQuickInputService).quickAccess.show(ResourceQuickAccess.PREFIX, { providerOptions });
  }
});
