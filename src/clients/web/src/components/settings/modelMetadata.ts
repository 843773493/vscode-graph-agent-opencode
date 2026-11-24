import { t } from '../../../../shared/core/i18n/index.js';
import type { LlmModelMetadataDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';

/** 选择目录与上游发现共用来源说明；不将参考值表达为当前渠道已验证的能力。 */
export function modelMetadataDetails(metadata: LlmModelMetadataDTO | undefined): string {
  if (!metadata) return t('configuration.catalog.unknown');
  const fields = [
    ['supports_vision', 'configuration.catalog.vision'],
    ['supports_video_input', 'configuration.catalog.video'],
    ['supports_audio_input', 'configuration.catalog.audioInput'],
    ['supports_audio_output', 'configuration.catalog.audioOutput'],
    ['supports_function_calling', 'configuration.catalog.tools'],
    ['supports_reasoning', 'configuration.catalog.reasoning'],
  ] as const;
  const groups = ['provider', 'upstream', 'litellm'].map(source => {
    const details: string[] = [];
    if (metadata.max_input_tokens !== undefined && metadata.sources.max_input_tokens === source) {
      details.push(t('configuration.catalog.capacity', metadata.max_input_tokens));
    }
    if (metadata.context_window !== undefined && metadata.sources.context_window === source) {
      details.push(t('configuration.catalog.contextWindow', metadata.context_window));
    }
    for (const [field, key] of fields) {
      if (metadata[field] !== undefined && metadata.sources[field] === source) {
        details.push(metadata[field] ? t(key) : t('configuration.catalog.unsupported', t(key)));
      }
    }
    if (!details.length) return '';
    const label = source === 'litellm' ? t('configuration.catalog.referenceSource', metadata.catalog_provider ?? '')
      : t(source === 'provider' ? 'configuration.catalog.providerSource' : 'configuration.catalog.upstreamSource');
    return `${label}：${details.join(' · ')}`;
  }).filter(Boolean);
  return groups.join('\n') || t('configuration.catalog.unknown');
}
