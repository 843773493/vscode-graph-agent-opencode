import type { DisplayLocale } from '../../shared/core/i18n/index.js';

type NlsAssetManifest = { buildId: string; assets: Record<DisplayLocale, string> };
type NlsResource = {
  buildId: string;
  contents: Record<string, Record<string, string>>;
  extensions: Record<string, Record<string, string>>;
};

export async function loadWorkbenchNlsMessages(locale: DisplayLocale, fetcher: typeof fetch, manifestPath: string): Promise<Record<string, unknown>> {
  const manifestResponse = await fetcher(manifestPath, { cache: 'no-cache' });
  if (!manifestResponse.ok) throw new Error(`${manifestPath}: HTTP ${manifestResponse.status}`);
  const manifest = await manifestResponse.json() as NlsAssetManifest;
  if (typeof manifest.buildId !== 'string' || !manifest.buildId || !manifest.assets || typeof manifest.assets !== 'object') {
    throw new Error('Workbench NLS manifest is malformed');
  }
  if (manifest.buildId !== manifestPath.match(/nls-assets-([a-f0-9]+)\.json$/)?.[1]) {
    throw new Error(`Workbench NLS manifest identity mismatch: ${manifestPath}`);
  }
  const resourcePath = manifest.assets[locale];
  if (typeof resourcePath !== 'string' || !resourcePath.startsWith('/out/nls-')) throw new Error(`No valid NLS asset for ${locale}`);
  const response = await fetcher(resourcePath);
  if (!response.ok) throw new Error(`${resourcePath}: HTTP ${response.status}`);
  const resource = await response.json() as NlsResource;
  if (resource.buildId !== manifest.buildId) throw new Error(`Workbench NLS asset build mismatch: ${resourcePath}`);
  if (!resource.contents || typeof resource.contents !== 'object' || Array.isArray(resource.contents)
    || !resource.extensions || typeof resource.extensions !== 'object' || Array.isArray(resource.extensions)) {
    throw new Error(`Workbench NLS asset has invalid contents: ${resourcePath}`);
  }
  return locale === 'zh-cn' ? { ...resource.contents, extensions: resource.extensions } : {};
}
