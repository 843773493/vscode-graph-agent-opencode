import { expect, test } from 'bun:test';
import { spawnSync } from 'node:child_process';

for (const locale of ['zh-cn', 'en'] as const) {
  test(`${locale} 首次加载输入控件、命令与状态时使用启动语言，保留名称及原始错误`, () => {
    // 每个子进程只初始化一次语言，覆盖模块级文案求值；不以热切换模拟重载。
    const child = spawnSync(process.execPath, ['--eval', `
const { setLocaleResources } = await import(${JSON.stringify(new URL('../../../../shared/core/i18n/index.ts', import.meta.url).href)});
const messages = (await import(${JSON.stringify(new URL(`../../../../shared/core/i18n/messages/${locale}.json`, import.meta.url).href)})).default;
setLocaleResources(${JSON.stringify(locale)}, messages);
const React = await import(${JSON.stringify(import.meta.resolve('react'))});
const { renderToStaticMarkup } = await import(${JSON.stringify(import.meta.resolve('react-dom/server'))});
const { default: Actions } = await import(${JSON.stringify(new URL('./ComposerActionButtons.tsx', import.meta.url).href)});
const { default: Model } = await import(${JSON.stringify(new URL('./ComposerModelControl.tsx', import.meta.url).href)});
const { default: Tools } = await import(${JSON.stringify(new URL('./ComposerToolTree.tsx', import.meta.url).href)});
const { COMPOSER_SLASH_COMMANDS } = await import(${JSON.stringify(new URL('../../state/slashCommands.ts', import.meta.url).href)});
const { GOAL_STATUS_LABELS } = await import(${JSON.stringify(new URL('../../state/sessionGoal.ts', import.meta.url).href)});
const { default: Goal } = await import(${JSON.stringify(new URL('../cards/GoalManagerCard.tsx', import.meta.url).href)});
const { default: Usage } = await import(${JSON.stringify(new URL('../chat/turn/ResponseUsage.tsx', import.meta.url).href)});
const { default: WarmConfirm } = await import(${JSON.stringify(new URL('../shell/WarmConfirmProvider.tsx', import.meta.url).href)});
const { default: TaskEditor } = await import(${JSON.stringify(new URL('../workspace/persistentTasks/TaskEditor.tsx', import.meta.url).href)});
const { default: TaskHistory } = await import(${JSON.stringify(new URL('../workspace/persistentTasks/TaskRunHistory.tsx', import.meta.url).href)});
const { taskSchedule, taskLabel } = await import(${JSON.stringify(new URL('../workspace/persistentTasks/taskPresentation.ts', import.meta.url).href)});
const { default: DebugConsole } = await import(${JSON.stringify(new URL('../nodeDebug/NodeDebugConsoleView.tsx', import.meta.url).href)});
const { default: RequestLog } = await import(${JSON.stringify(new URL('../panels/inspectionPanels/RequestLogCard.tsx', import.meta.url).href)});
const { default: Changes } = await import(${JSON.stringify(new URL('../workspace/sessionChanges/SessionChangesTree.tsx', import.meta.url).href)});
const resourceDisplay = await import(${JSON.stringify(new URL('../../state/display/resourceDisplay.ts', import.meta.url).href)});
const { default: ChildConversation } = await import(${JSON.stringify(new URL('../panels/sessionPanels/ChildThreadConversation.tsx', import.meta.url).href)});
const { parsePortLiteral } = await import(${JSON.stringify(new URL('../workspace/portLiteral.ts', import.meta.url).href)});
const { default: Bootstrap } = await import(${JSON.stringify(new URL('../shell/BootstrapState.tsx', import.meta.url).href)});
const noop = () => {};
const tool = {tool_id:'read_file', name:'用户工具', description:'原始工具说明', execution_enabled:true,
  model_visible:false, test_supported:false, execution_locked:false, model_visibility_locked:false};
const actions = renderToStaticMarkup(React.createElement(Actions, {hasContent:true, showInterrupt:true,
  defaultDeliveryPolicy:'after_turn', onClear:noop, onInterrupt:noop, onSend:noop, onAlternate:noop, onToggleDefault:noop}));
const model = renderToStaticMarkup(React.createElement(Model, {onOpen:noop,
  models:[{model_ref: { provider_id: 'provider-raw', model_id: "default" }, model:'模型原名', custom_llm_provider:'openai', available:false,
    configuration_error:"Cannot find module '/home/alice/missing.js'"}], currentModel:{provider_id:'provider-raw',model_id:'default'},
  }));
const tools = renderToStaticMarkup(React.createElement(Tools, {groups:[{id:'default', name:'用户工具组', kind:'default', items:[tool]}],
  loading:false, savingToolIds:new Set(), runs:new Map(), testingTools:new Set(), collapsedGroups:new Set(),
  canRunTests:true,
  onToggleCollapsed:noop, onToggleGroupCapability:noop, onToggleToolExecution:noop, onToggleToolModelVisibility:noop, onRunTest:noop}));
const goal = renderToStaticMarkup(React.createElement(WarmConfirm, null, React.createElement(Goal, {
  sessionId:'session-raw', goal:{objective:'用户目标 {0}', status:'active', tokens_used:12345,
    token_budget:null, time_used_seconds:3661}, loading:false, error:null, onRefresh:noop, onUpdate:noop, onClear:noop})));
const task = {task_id:'task-raw', application_type:'automation', configuration:{name:'用户任务',
  trigger:{type_id:'builtin.interval',version:'1', interval_seconds:12345, timezone:'UTC'},
  condition:{type_id:'builtin.script',version:'1', script:{command:['/home/alice/程序.js'], timeout_seconds:30}},
  context:{type_id:'builtin.literal',version:'1',content:'用户提示 {0}'},
  action:{type_id:'builtin.runtime_source',version:'1',target_uri:'boxteam://workspace/raw/resources/session/raw/threads/raw'},
  policies:{completion:'continuous',match_mode:'each_match',misfire:'skip'}}};
const editor = renderToStaticMarkup(React.createElement(TaskEditor, {apiPort:0,
  target:{gateway_id:'gateway-raw',workspace_id:'workspace-raw'}, configuration:task.configuration, busy:false, onSave:noop, onCancel:noop}));
const history = renderToStaticMarkup(React.createElement(TaskHistory, {apiPort:0, target:{gateway_id:'gateway-raw',workspace_id:'workspace-raw'},
  runs:[{task_id:'task-raw',run_id:'run-raw',trigger_type:'manual',phase:'delivered',created_at:'2026-11-12T18:07:06Z',
    definition_revision:12345,decision:{decision:'proceed',reason:'原始判断原因 {0}'},delivery:{state:'saved',source_id:'source-raw'}}],
  onOpenSession:noop, onOpenThread:noop}));
const debug = renderToStaticMarkup(React.createElement(DebugConsole, {state:{output:["Cannot find module '/home/alice/missing.js' {0}"],
  actions:[],evaluations:[]},status:'paused',actionBusy:false,extensionWindow:false,expression:'用户表达式 {0}',setExpression:noop,onEvaluate:noop}));
const request = renderToStaticMarkup(React.createElement(RequestLog, {initiallyOpen:true,log:{session_id:'session-raw',timestamp:0,
  file_name:'request-original.json',request:{model_name:'模型原名',messages:[]},response:{result:[{role:'assistant',content:'',
    tool_calls:[{id:'raw-call',name:'用户工具',args:{path:'/home/alice/missing.js'}}]}]},upstream:{}}}));
const changes = renderToStaticMarkup(React.createElement(Changes, {changesets:[],selectedChangesetId:'set-raw',loading:false,
  error:null,loadedAt:null,activeChangeset:{summary:{files:12345,additions:12345,deletions:2},files:[{file_path:'/home/alice/用户文件 {0}.ts',
    kind:'create',additions:12345,deletions:2,reviewed:false}]},onSelectChangeset:noop,onRefresh:noop,onOpenFile:noop,onReviewFile:noop}));
const resource = {kind:'browser',status:'lost',resource_id:'resource-original',name:'用户浏览器',metadata:{resource_source:'历史记录'}};
const child = renderToStaticMarkup(React.createElement(ChildConversation, {thread:{thread_id:'child-original',title:'用户子线程 {0}'},
  conversation:{snapshot:{view:'history',history:null,loading:true,sending:false,draft:'子线程草稿 {0}',accepted:{job_id:'job-original'}},
    close:noop,refresh:noop,openReport:noop,loadEarlier:noop,send:noop,setDraft:noop}}));
const bootstrap = renderToStaticMarkup(React.createElement(Bootstrap, {onRetry:async () => {}}));
let portError;
try { parsePortLiteral('1e3', '目标端口 {0}'); } catch (error) { portError = error.message; }
const usageUnknown = renderToStaticMarkup(React.createElement(Usage, {tokenUsage:null, modelUsage:null}));
const usageKnown = renderToStaticMarkup(React.createElement(Usage, {modelUsage:{finalModelId:'模型原名'},
  tokenUsage:{totalTokens:0,inputTokens:0,outputTokens:0,cacheReadInputTokens:null,modelCalls:1,reportedModelCalls:1}}));
process.stdout.write(JSON.stringify({actions, model, tools, goal, usageUnknown, usageKnown, editor, history, schedule:taskSchedule(task),
  debug, request, changes, child, bootstrap, portError, resourceStatus:resourceDisplay.statusLabel('lost'),
  resourceGroup:resourceDisplay.resourceAttentionGroup(resource,null), unknownResourceStatus:resourceDisplay.statusLabel('原始未知状态'),
  unknownTaskLabel:taskLabel('未识别的原始状态'), commands:COMPOSER_SLASH_COMMANDS, activeGoal:GOAL_STATUS_LABELS.active}));
`], { encoding: 'utf8', timeout: 10_000 });
    expect({ status: child.status, stderr: child.stderr }).toEqual({ status: 0, stderr: '' });
    const output = JSON.parse(child.stdout);
    expect(output.actions).toContain(locale === 'en' ? 'Send message' : '发送消息');
    expect(output.actions).toContain(locale === 'en' ? 'Deliver after this turn' : '本轮结束后投递');
    expect(output.actions).not.toContain('${');
    expect(output.actions).not.toMatch(/\{\d+\}/);
    expect(output.model).toContain(locale === 'en' ? 'Current model configuration error:' : '当前模型配置错误：');
    expect(output.model).toContain('模型原名');
    expect(output.model).toContain('/home/alice/missing.js');
    expect(output.tools).toContain(locale === 'en' ? 'show tool definitions' : '显示工具说明');
    expect(output.tools).toContain('用户工具组');
    expect(output.tools).toContain('原始工具说明');
    expect(output.activeGoal).toBe(locale === 'en' ? 'In progress' : '进行中');
    expect(output.goal).toContain(locale === 'en' ? '>Pause</button>' : '>暂停</button>');
    expect(output.goal).toContain('12,345');
    expect(output.goal).toContain(locale === 'en' ? 'Only usage recorded by this Goal' : '仅含本 Goal 已记录的用量');
    expect(output.usageUnknown).toBe('');
    for (const usage of [output.usageKnown]) {
      expect(usage).toContain(locale === 'en' ? 'not a Thread total' : '非整个 Thread 总计');
      expect(usage).toContain(locale === 'en' ? 'Cost unknown' : '成本未知');
    }
    expect(output.usageKnown).toContain('模型原名');
    expect(output.usageKnown).toContain('0');
    expect(output.usageKnown).not.toContain(locale === 'en' ? 'Usage not reported' : '用量未报告');
    expect(output.goal).toContain('用户目标 {0}');
    expect(output.editor).toContain(locale === 'en' ? 'Edit persistent task' : '编辑持久任务');
    expect(output.editor).toContain(locale === 'en' ? 'Execution timeout (seconds)' : '执行期限（秒）');
    expect(output.editor).toContain('/home/alice/程序.js');
    expect(output.editor).toContain('用户任务');
    expect(output.editor).toContain('用户提示 {0}');
    expect(output.history).toContain(locale === 'en' ? 'Definition revision 12,345' : '定义版本 12,345');
    expect(output.history).toContain(locale === 'en' ? 'Content saved; this does not mean the model has consumed it' : '内容已保存，尚不代表模型已消费');
    expect(output.history).toContain('原始判断原因 {0}');
    expect(output.schedule).toBe(locale === 'en' ? 'Check every 12,345 seconds' : '每 12,345 秒检查');
    expect(output.unknownTaskLabel).toBe('未识别的原始状态');
    expect(output.debug).toContain(locale === 'en' ? 'Expression console' : '表达式控制台');
    expect(output.debug).toContain(locale === 'en' ? 'Debug console' : '调试控制台');
    expect(output.debug).toContain('/home/alice/missing.js');
    expect(output.debug).toContain('用户表达式 {0}');
    expect(output.debug).toContain('{0}');
    expect(output.request).toContain(locale === 'en' ? 'Tool call request' : '工具调用请求');
    expect(output.request).toContain(locale === 'en' ? 'The model chose to call tools in this request, so there is no natural-language content.' : '模型在本次请求中选择调用工具，因此没有自然语言正文。');
    expect(output.request).toContain('模型原名');
    expect(output.request).not.toContain(locale === 'en' ? 'This is an intermediate request in the chain with no readable natural-language content.' : '本次是链路中的中间请求，没有可读自然语言正文。');
    expect(output.changes).toContain(locale === 'en' ? 'Session file changes' : '会话文件变更');
    expect(output.changes).toContain(locale === 'en' ? 'Mark as reviewed' : '标记已审查');
    expect(output.changes).toContain('12,345');
    expect(output.changes).toContain('/home/alice/用户文件 {0}.ts');
    expect(output.resourceStatus).toBe(locale === 'en' ? 'Disconnected (lost)' : '已断开 (lost)');
    expect(output.resourceGroup).toBe('attention');
    expect(output.unknownResourceStatus).toBe('原始未知状态');
    expect(output.child).toContain(locale === 'en' ? 'Loading child Thread history…' : '正在读取子线程历史…');
    expect(output.child).toContain(locale === 'en' ? 'Send to child Thread' : '发送到子线程');
    expect(output.child).toContain('用户子线程 {0}');
    expect(output.child).toContain('子线程草稿 {0}');
    expect(output.child).toContain('job-original');
    expect(output.bootstrap).toContain(locale === 'en' ? 'Loading workspace and sessions...' : '正在加载工作区与会话...');
    expect(output.bootstrap).toContain(locale === 'en' ? 'Connecting to the local backend' : '正在连接本地后端');
    expect(output.portError).toBe(locale === 'en' ? '目标端口 {0} must be a decimal integer from 1 to 65535' : '目标端口 {0}必须是 1–65535 之间的十进制整数');
    expect(output.commands.find((command: { id: string }) => command.id === 'new')).toMatchObject({
      command: '/new', title: locale === 'en' ? 'New session' : '新建会话',
    });
    expect(output.commands.find((command: { id: string }) => command.id === 'goal').command).toBe('/goal');
  });
}
