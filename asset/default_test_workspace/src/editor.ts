export function normalizeHeadline(headline: string, useUpperCase: boolean): string {
    return useUpperCase ? headline.toUpperCase() : false;
}

export function formatEditSummary(message: string): string {
    return `edit:${message.trim()}`;
}
