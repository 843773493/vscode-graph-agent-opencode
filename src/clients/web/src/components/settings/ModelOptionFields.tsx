import { useRef, type FormEvent, type ReactNode } from 'react';
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

const capacityNumber = new Intl.NumberFormat('en-US');
const capacityCompact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 });

/** 千位分隔只属于展示；原来源仍接收整数，按数字位置恢复编辑光标。 */
function ContextCapacityInput({ value, minimum, maximum, disabled, onEdit }: {
  value: unknown; minimum?: number; maximum?: number; disabled: boolean; onEdit: (value: number | undefined) => void;
}) {
  const selection = useRef<[number, number]>();
  const formatted = typeof value === 'number' ? capacityNumber.format(value) : '';
  const restoreSelection = (element: HTMLInputElement) => {
    if (!selection.current) return;
    const position = (digits: number) => {
      let offset = 0, seen = 0;
      while (offset < element.value.length && seen < digits) {
        if (element.value[offset] !== ',') seen++;
        offset++;
      }
      return offset;
    };
    element.setSelectionRange(position(selection.current[0]), position(selection.current[1]));
  };
  return <span className="configuration-capacity-input">
    <input ref={element => { if (element) { restoreSelection(element); selection.current = undefined; } }} type="text" inputMode="numeric" value={formatted} disabled={disabled}
      onBlur={() => { selection.current = undefined; }} onKeyDown={event => {
        const element = event.currentTarget, caret = element.selectionStart;
        if (caret === null || caret !== element.selectionEnd || event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === 'Backspace' && element.value[caret - 1] === ',') element.setSelectionRange(caret - 1, caret - 1);
        if (event.key === 'Delete' && element.value[caret] === ',') element.setSelectionRange(caret + 1, caret + 1);
      }} onChange={event => {
        const element = event.currentTarget, raw = element.value, digits = raw.replaceAll(',', '');
        const next = digits === '' ? undefined : Number(digits);
        if (!/^[0-9,]*$/.test(raw) || next !== undefined && (!Number.isSafeInteger(next) || next < (minimum ?? 1) || maximum !== undefined && next > maximum)) {
          element.setCustomValidity(t('configuration.options.capacityInvalid')); element.reportValidity(); return;
        }
        element.setCustomValidity('');
        selection.current = [element.selectionStart ?? raw.length, element.selectionEnd ?? raw.length]
          .map(offset => raw.slice(0, offset).replaceAll(',', '').length) as [number, number];
        element.value = next === undefined ? '' : capacityNumber.format(next);
        restoreSelection(element);
        onEdit(next);
      }} />
    {typeof value === 'number' && value >= 1000 ? <small>{t('configuration.options.capacityApprox', capacityCompact.format(value))}</small> : null}
  </span>;
}

/** JSON 输入只是待确认的单个字段；应用后只改原来源文本，不持有另一份配置。 */
function JsonOption({ label, value, inherited, disabled, objectOnly = false, onEdit, actions }: {
  label: string; value: unknown; inherited: unknown; disabled: boolean; objectOnly?: boolean;
  onEdit: (value: unknown) => void; actions?: ReactNode;
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
  const scalar = !objectOnly && (value === undefined || value === null || typeof value !== 'object') && (inherited === undefined || inherited === null || typeof inherited !== 'object');
  return <div className={scalar ? 'configuration-option-json configuration-option-scalar' : 'configuration-option-json'}>
    <form onSubmit={submit}>
      <label className="configuration-entity-field"><span>{label}</span>
        {scalar ? <input type={typeof (value ?? inherited) === 'number' ? 'number' : 'text'} step="any" key={shown(value)} name="value" aria-label={label} defaultValue={shown(value)} placeholder={shown(inherited)} required disabled={disabled}
          onInput={event => event.currentTarget.setCustomValidity('')} />
          : <textarea key={shown(value)} name="value" aria-label={label} defaultValue={shown(value)} placeholder={shown(inherited)} rows={3} required disabled={disabled}
            onInput={event => event.currentTarget.setCustomValidity('')} />}
      </label>
      <div className="configuration-option-actions">
        <button type="submit" disabled={disabled}>{t('configuration.options.applyValue')}</button>
        {value !== undefined ? <button type="button" disabled={disabled} onClick={() => onEdit(undefined)}>{t(inherited === undefined ? 'configuration.options.clear' : 'configuration.entities.restoreField')}</button> : null}
        {actions}
      </div>
    </form>
    {inherited !== undefined ? <small>{t('configuration.options.lowerDefinition', shown(inherited))}</small> : null}
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
    const value = read(path), label = t(`configuration.capability.${path.at(-1)}`);
    return <label key={name} className="configuration-entity-field" data-search-key={path.join('.')} title={description}>{label}
      <select aria-label={label} value={typeof value === 'boolean' ? String(value) : ''} disabled={disabled}
        onChange={event => onEdit(path, event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t('configuration.entities.defaultValue')}</option>
        <option value="true">{t('configuration.options.enabled')}</option><option value="false">{t('configuration.options.disabled')}</option>
      </select>

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
    <div className="configuration-form-grid">
    <label className="configuration-entity-field">{t('configuration.entities.protocolLabel')}
      <select aria-label={t('configuration.entities.protocolLabel')} value={typeof protocol === 'string' ? protocol : ''} disabled={disabled} onChange={event => {
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
      <select aria-label={t('configuration.entities.streaming')} value={typeof stream === 'boolean' ? String(stream) : ''} disabled={disabled}
        onChange={event => onEdit(['api_mode', 'streaming'], event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t(scope === 'model' ? 'configuration.entities.useServiceDefault'
          : inherited(['api_mode', 'streaming']) === undefined ? 'configuration.entities.defaultStreaming' : 'configuration.entities.defaultValue')}</option>
        <option value="true">{t('configuration.entities.streamEnabled')}</option>
        <option value="false">{t('configuration.entities.streamDisabled')}</option>
      </select>
    </label>
    </div>
    <div className="configuration-model-options">
      {advanced}
      <details className="configuration-option-section"><summary>{t('configuration.options.capabilities')}</summary>
      <p>{t('configuration.catalog.autoCapabilities')}</p>
      <div className="configuration-form-grid">
      {Object.entries(apiMode?.model_info.properties ?? {}).map(([key, field]) => {
        const path = ['api_mode', 'model_info', key], value = read(path);
        if (field.type === 'boolean') return booleanField(field.title ?? key, path, field.description);
        return <label key={key} className="configuration-entity-field" data-search-key={path.join('.')} title={field.description}>{t(`configuration.capability.${key}`)}
          {key === 'max_input_tokens' ? <ContextCapacityInput value={value} minimum={field.minimum} maximum={field.maximum}
            disabled={disabled} onEdit={next => onEdit(path, next)} /> : <input type="number" value={typeof value === 'number' ? value : ''} step={field.type === 'integer' ? 1 : 'any'}
            min={field.minimum} max={field.maximum} disabled={disabled} onChange={event => {
              if (event.target.value === '') onEdit(path, undefined);
              else if (event.target.validity.valid) onEdit(path, event.target.valueAsNumber);
            }} />}

        </label>;
      })}
      </div></details>
      <details className="configuration-option-section"><summary>{t('configuration.options.reasoning')}</summary>
      <p>{t('configuration.options.reasoningHint')}</p>
      <JsonOption label={t('configuration.options.reasoning')} value={read(['api_mode', 'supports_reasoning'])} inherited={inherited(['api_mode', 'supports_reasoning'])}
        objectOnly disabled={disabled} onEdit={value => onEdit(['api_mode', 'supports_reasoning'], value)}
        actions={<button type="button" disabled={disabled} onClick={() => onEdit(['api_mode', 'supports_reasoning'], {})}>{t('configuration.options.emptyReasoning')}</button>} />
      </details>
      <details className="configuration-option-section"><summary>{t('configuration.options.parameters')}</summary>
      <p>{t('configuration.options.parametersHint')}</p>
      {[...parameterNames].map(name => {
        const path = ['request_options', 'parameters', name], value = read(path);
        return <section key={name} className="configuration-request-parameter" data-parameter={name}>
          <strong>{name}</strong><small>{t(value === undefined ? 'configuration.entities.inherit' : value === null ? 'configuration.options.omitted' : 'configuration.options.explicit')}</small>
          <JsonOption label={t('configuration.options.parameterValue', name)} value={value} inherited={inherited(path)} disabled={disabled}
            onEdit={next => onEdit(path, next)}
            actions={<button type="button" disabled={disabled} onClick={() => onEdit(path, null)}>{t('configuration.options.omit')}</button>} />
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
      </details>
      <details className="configuration-option-section"><summary>{t('configuration.options.network')}</summary>
      <JsonOption label={t('configuration.options.headers')} value={read(['request_options', 'default_headers'])}
        inherited={inherited(['request_options', 'default_headers'])} objectOnly disabled={disabled}
        onEdit={value => onEdit(['request_options', 'default_headers'], value)} />
      </details>
    </div>
  </>;
}
