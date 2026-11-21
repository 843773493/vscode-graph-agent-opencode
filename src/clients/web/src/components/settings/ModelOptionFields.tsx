import type { FormEvent, ReactNode } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';

interface OptionSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
  enum?: unknown[];
  properties?: Record<string, OptionSchema>;
}

type OptionPath = string[];

function shown(value: unknown): string {
  return value === undefined ? '' : JSON.stringify(value, null, 2);
}

/** JSON 输入只是待确认的单个字段；应用后只改原来源文本，不持有另一份配置。 */
function JsonOption({ label, value, inherited, disabled, objectOnly = false, onEdit }: {
  label: string; value: unknown; inherited: unknown; disabled: boolean; objectOnly?: boolean;
  onEdit: (value: unknown) => void;
}) {
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem('value') as HTMLTextAreaElement;
    let parsed: unknown;
    try {
      parsed = JSON.parse(input.value);
      if (objectOnly && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))) throw new Error(t('configuration.options.objectRequired'));
    } catch (error) {
      input.setCustomValidity(error instanceof Error ? error.message : String(error)); input.reportValidity(); return;
    }
    onEdit(parsed);
  };
  return <div className="configuration-option-json">
    <form onSubmit={submit}>
      <label className="configuration-entity-field">{label}
        <textarea key={shown(value)} name="value" aria-label={label} defaultValue={shown(value)} placeholder={shown(inherited)} rows={3} required disabled={disabled}
          onInput={event => event.currentTarget.setCustomValidity('')} />
      </label>
      <button type="submit" disabled={disabled}>{t('configuration.options.applyValue')}</button>
    </form>
    {inherited !== undefined ? <small>{t('configuration.options.lowerDefinition', shown(inherited))}</small> : null}
    {value !== undefined ? <button type="button" disabled={disabled} onClick={() => onEdit(undefined)}>{t(inherited === undefined ? 'configuration.options.clear' : 'configuration.entities.restoreField')}</button> : null}
  </div>;
}

/** 只编辑当前来源的明确选项；有效选项和协议一致性仍由原 Backend resolver 决定。 */
export function ModelOptionFields({ schema, read, inherited, disabled, onEdit, scope, advanced }: {
  schema: OptionSchema; read: (path: OptionPath) => unknown; inherited: (path: OptionPath) => unknown;
  disabled: boolean; onEdit: (path: OptionPath, value: unknown) => void;
  scope: 'provider' | 'model'; advanced?: ReactNode;
}) {
  const apiMode = schema.properties?.api_mode.properties;
  const protocol = read(['api_mode', 'protocol']);
  const stream = read(['api_mode', 'streaming']);
  const booleanField = (name: string, path: OptionPath, description?: string) => {
    const value = read(path);
    return <label key={name} className="configuration-entity-field">{name}
      <select value={typeof value === 'boolean' ? String(value) : ''} disabled={disabled}
        onChange={event => onEdit(path, event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t('configuration.entities.defaultValue')}</option>
        <option value="true">true</option><option value="false">false</option>
      </select>
      {description ? <small>{description}</small> : null}
    </label>;
  };
  const rawParameters = read(['request_options', 'parameters']);
  const lowerParameters = inherited(['request_options', 'parameters']);
  const parameterNames = new Set([
    ...Object.keys(lowerParameters && typeof lowerParameters === 'object' ? lowerParameters : {}),
    ...Object.keys(rawParameters && typeof rawParameters === 'object' ? rawParameters : {}),
  ]);
  const addParameter = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const name = form.elements.namedItem('parameter') as HTMLInputElement;
    const input = form.elements.namedItem('value') as HTMLInputElement;
    let value: unknown;
    try { value = JSON.parse(input.value); }
    catch (error) { input.setCustomValidity(error instanceof Error ? error.message : String(error)); input.reportValidity(); return; }
    if (!name.value.trim()) { name.setCustomValidity(t('configuration.entities.required')); name.reportValidity(); return; }
    onEdit(['request_options', 'parameters', name.value.trim()], value);
    form.reset();
  };
  return <>
    <label className="configuration-entity-field">{t('configuration.entities.protocolLabel')}
      <select value={typeof protocol === 'string' ? protocol : ''} disabled={disabled} onChange={event => {
        const value = read(['api_mode']);
        const next = value && typeof value === 'object' && !Array.isArray(value) ? { ...value } as Record<string, unknown> : {};
        if (event.target.value) { next.protocol = event.target.value; next.supports_reasoning = {}; }
        else { delete next.protocol; delete next.supports_reasoning; }
        onEdit(['api_mode'], Object.keys(next).length ? next : undefined);
      }}>
        <option value="">{t(scope === 'model' ? 'configuration.entities.useServiceDefault' : 'configuration.entities.defaultValue')}</option>
        {apiMode?.protocol.enum?.filter((value): value is string => typeof value === 'string').map(value => <option key={value} value={value}>{t(`configuration.protocol.${value}`)}</option>)}
      </select>
    </label>
    <label className="configuration-entity-field">{t('configuration.entities.streaming')}
      <select value={typeof stream === 'boolean' ? String(stream) : ''} disabled={disabled}
        onChange={event => onEdit(['api_mode', 'streaming'], event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t(scope === 'model' ? 'configuration.entities.useServiceDefault'
          : inherited(['api_mode', 'streaming']) === undefined ? 'configuration.entities.defaultStreaming' : 'configuration.entities.defaultValue')}</option>
        <option value="true">{t('configuration.entities.streamEnabled')}</option>
        <option value="false">{t('configuration.entities.streamDisabled')}</option>
      </select>
    </label>
    <details className="configuration-model-options">
      <summary>{t('configuration.options.advanced')}</summary>
      {advanced}
      <h4>{t('configuration.options.capabilities')}</h4>
      {Object.entries(apiMode?.model_info.properties ?? {}).map(([key, field]) => {
        const path = ['api_mode', 'model_info', key], value = read(path);
        if (field.type === 'boolean') return booleanField(field.title ?? key, path, field.description);
        return <label key={key} className="configuration-entity-field">{field.title ?? key}
          <input type="number" value={typeof value === 'number' ? value : ''} step={field.type === 'integer' ? 1 : 'any'}
            min={field.minimum} max={field.maximum} disabled={disabled} onChange={event => {
              if (event.target.value === '') onEdit(path, undefined);
              else if (event.target.validity.valid) onEdit(path, event.target.valueAsNumber);
            }} />
          {field.description ? <small>{field.description}</small> : null}
        </label>;
      })}
      <h4>{t('configuration.options.reasoning')}</h4>
      <p>{t('configuration.options.reasoningHint')}</p>
      <JsonOption label="api_mode.supports_reasoning" value={read(['api_mode', 'supports_reasoning'])} inherited={inherited(['api_mode', 'supports_reasoning'])}
        objectOnly disabled={disabled} onEdit={value => onEdit(['api_mode', 'supports_reasoning'], value)} />
      <button type="button" disabled={disabled} onClick={() => onEdit(['api_mode', 'supports_reasoning'], {})}>{t('configuration.options.emptyReasoning')}</button>
      <h4>{t('configuration.options.parameters')}</h4>
      <p>{t('configuration.options.parametersHint')}</p>
      {[...parameterNames].map(name => {
        const path = ['request_options', 'parameters', name], value = read(path);
        return <section key={name} className="configuration-request-parameter" data-parameter={name}>
          <strong>{name}</strong>
          <p>{t(value === undefined ? 'configuration.entities.inherit' : value === null ? 'configuration.options.omitted' : 'configuration.options.explicit')}</p>
          <JsonOption label={t('configuration.options.parameterValue', name)} value={value} inherited={inherited(path)} disabled={disabled}
            onEdit={next => onEdit(path, next)} />
          <button type="button" disabled={disabled} onClick={() => onEdit(path, null)}>{t('configuration.options.omit')}</button>
        </section>;
      })}
      <form className="configuration-parameter-add" onSubmit={addParameter}>
        <label className="configuration-entity-field">{t('configuration.options.parameterName')}
          <input name="parameter" required disabled={disabled} onInput={event => event.currentTarget.setCustomValidity('')} />
        </label>
        <label className="configuration-entity-field">{t('configuration.options.newParameterValue')}
          <input name="value" required disabled={disabled} onInput={event => event.currentTarget.setCustomValidity('')} />
        </label>
        <button type="submit" disabled={disabled}>{t('configuration.options.addParameter')}</button>
      </form>
      <h4>{t('configuration.options.network')}</h4>
      <JsonOption label="request_options.default_headers" value={read(['request_options', 'default_headers'])}
        inherited={inherited(['request_options', 'default_headers'])} objectOnly disabled={disabled}
        onEdit={value => onEdit(['request_options', 'default_headers'], value)} />
      {booleanField('request_options.no_proxy', ['request_options', 'no_proxy'], schema.properties?.request_options.properties?.no_proxy.description)}
    </details>
  </>;
}
