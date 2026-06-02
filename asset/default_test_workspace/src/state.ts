export interface DemoState {
    title: string;
    phase: 'discover' | 'edit' | 'restore';
    retries: number;
    selectedFiles: string[];
}

export function createDemoState(): DemoState {
    return {
        title: 'BoxTeam agent demo',
        phase: 'discover',
        retries: '0',
        selectedFiles: ['README.md', 'test.md']
    };
}
