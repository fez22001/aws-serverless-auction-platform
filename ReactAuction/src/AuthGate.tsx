import { useEffect, useState, type ReactNode } from 'react';
import { auth, isLocal, signOut } from './auth';

let callback: Promise<unknown> | undefined;
export default function AuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(isLocal);
  const [loading, setLoading] = useState(!isLocal);
  const [error, setError] = useState('');
  useEffect(() => {
    if (isLocal) return;
    let active = true;
    const manager = auth;
    if (!manager) { setLoading(false); setError('Authentication is not configured for this deployment.'); return; }
    const expired = () => { if (active) setReady(false); };
    manager.events.addAccessTokenExpired(expired);
    manager.events.addSilentRenewError(expired);
    void (async () => {
      try {
        if (window.location.pathname === '/callback') {
          callback ??= manager.signinRedirectCallback();
          await callback;
          window.history.replaceState({}, '', '/auctions');
        }
        const user = await manager.getUser();
        if (active) setReady(!!user && !user.expired);
      } catch { if (active) setError('Sign-in could not be completed. Please try again.'); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; manager.events.removeAccessTokenExpired(expired); manager.events.removeSilentRenewError(expired); };
  }, []);
  if (loading) return <main className="container"><p>Checking your session…</p></main>;
  if (!ready) return <main className="container"><section className="card">
    <h1>Serverless Auction Demo</h1>
    <p>Create auctions, place bids, and explore an AWS serverless application.</p>
    <p>This is an invitation-only demonstration. All balances and bids use fictional credits. No payments or real goods are involved.</p>
    <p>Use the email address from your invitation to sign in. Ask the demo owner for an account.</p>
    {error && <p role="alert" className="error">{error}</p>}
    <button disabled={!auth} onClick={() => { void auth?.signinRedirect().catch(() => setError('Could not start sign-in. Try again.')); }}>Sign in</button>
  </section></main>;
  return <>{!isLocal && <aside className="demo-banner">Invitation-only demo · Fictional credits · <button onClick={() => { void signOut(); }}>Sign out</button></aside>}{children}</>;
}
