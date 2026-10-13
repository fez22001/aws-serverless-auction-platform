// Reuse identity after an uncertain response, including a page reload in this tab.
// Only a changed semantic payload generates a new identity. Successful receipts
// remain replayable; repeated clicks cannot turn a retry into a second operation.
const memory = new Map<string, { fingerprint: string; requestId: string }>();
export function requestIdentity(kind: 'bid' | 'close', payload: Record<string, string | number>): string {
  const key = `auction-request:${kind}:${payload.auctionId}`;
  const fingerprint = JSON.stringify(payload);
  let previous = memory.get(key);
  try {
    const stored = sessionStorage.getItem(key);
    if (stored) previous = JSON.parse(stored);
  } catch { /* In-memory retry protection remains available if storage is blocked. */ }
  if (previous?.fingerprint === fingerprint && typeof previous.requestId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(previous.requestId)) return previous.requestId;
  const value = { fingerprint, requestId: crypto.randomUUID() };
  memory.set(key, value);
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* See fallback above. */ }
  return value.requestId;
}
