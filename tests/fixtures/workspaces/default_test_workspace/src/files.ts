export function listDiscoveryMarkers(): string[] {
    return ['read', 'grep', 'glob', 'lsp', 'edit', 'patch', 'multiedit', 'snapshot', 'watcher'];
}

export function collectWorkspaceHints(): string[] {
    return ['README.md', 'test.md', 'src/**/*.ts'];
}
