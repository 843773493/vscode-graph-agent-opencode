import { t } from '../../../../shared/core/i18n/index.js';

type ProxyDefinition = Record<string, unknown>;

/** proxy 是 schema 声明的原子块，模式切换和字段编辑都提交完整本地块。 */
export function ConfigurationProxyFields({ local, inherited, disabled, allowInherit = true, onChange }: {
  local?: ProxyDefinition; inherited?: ProxyDefinition; disabled: boolean; allowInherit?: boolean;
  onChange: (value: ProxyDefinition | undefined) => void;
}) {
  const value = local ?? inherited;
  const mode = typeof local?.mode === 'string' ? local.mode : '';
  const auth = value?.auth && typeof value.auth === 'object' ? value.auth as ProxyDefinition : undefined;
  const field = (label: string, key: string, current: unknown, change: (text: string) => void) => <label className="configuration-entity-field">
    {label}<input disabled={disabled} value={typeof current === 'string' ? current : ''} data-proxy-field={key}
      onChange={event => change(event.target.value)} />
  </label>;
  return <section className="configuration-proxy-fields">
    <label className="configuration-entity-field">{t('configuration.node.proxy')}
      <select disabled={disabled} value={mode} onChange={event => onChange(event.target.value
        ? { mode: event.target.value, ...(event.target.value === 'explicit' ? { url: '' } : {}) } : undefined)}>
        {allowInherit ? <option value="" disabled={local === undefined}>{t('configuration.entities.defaultValue')}</option> : null}
        {(['environment', 'direct', 'explicit'] as const).map(mode => <option key={mode} value={mode}>{t(`configuration.node.proxy.${mode}`)}</option>)}
      </select>
    </label>
    {!local && inherited ? <p>{t('configuration.entities.inheritedValue', JSON.stringify(inherited))}</p> : null}
    {value?.mode === 'explicit' ? <>
      {field(t('configuration.node.proxyUrl'), 'url', value.url, url => onChange({ ...value, url }))}
      {field(t('configuration.node.proxyBypass'), 'bypass', Array.isArray(value.bypass) ? value.bypass.join(', ') : '',
        input => onChange({ ...value, bypass: input.split(',').map(host => host.trim()).filter(Boolean) }))}
      <label className="configuration-entity-field"><input type="checkbox" disabled={disabled} checked={Boolean(auth)}
        onChange={event => { const { auth: _auth, ...rest } = value; onChange(event.target.checked ? { ...rest, auth: { username: '', password: '' } } : rest); }} />
        {t('configuration.node.proxyAuth')}</label>
      {auth ? <>
        {field(t('configuration.node.proxyUsername'), 'username', auth.username, username => onChange({ ...value, auth: { ...auth, username } }))}
        {field(t('configuration.node.proxyPasswordReference'), 'password', auth.password, password => onChange({ ...value, auth: { ...auth, password } }))}
      </> : null}
    </> : null}
    <p>{t('configuration.node.networkScope')}</p>
  </section>;
}
