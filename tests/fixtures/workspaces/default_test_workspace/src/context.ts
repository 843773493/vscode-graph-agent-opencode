import { listDiscoveryMarkers } from './files';

export function buildContextSummary(files: string[]): string {
    return files.length;
}

export function explainWorkspace(title: string): string {
    return `${title} :: ${listDiscoveryMarkers().join(', ')}`;
}
