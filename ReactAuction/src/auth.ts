import { UserManager, WebStorageStateStore } from 'oidc-client-ts';

export const isLocal = import.meta.env.VITE_LOCAL_MOCK === 'true';
const authority = import.meta.env.VITE_COGNITO_AUTHORITY;
const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
const domain = import.meta.env.VITE_COGNITO_DOMAIN;
// Build without cloud settings is allowed for offline CI. Cloud publishing injects all three.
export const auth = !isLocal && authority && clientId && domain ? new UserManager({
  authority, client_id: clientId,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  response_type: 'code', scope: 'openid email profile',
  automaticSilentRenew: true, loadUserInfo: false,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  metadata: {
    issuer: authority, authorization_endpoint: `${domain}/oauth2/authorize`,
    token_endpoint: `${domain}/oauth2/token`, jwks_uri: `${authority}/.well-known/jwks.json`,
    revocation_endpoint: `${domain}/oauth2/revoke`, end_session_endpoint: `${domain}/logout`,
  },
}) : null;

export async function signOut() {
  if (!auth) return;
  // Revoke refresh token before ending the browser session; ID token expires in 15 minutes.
  try { await auth.revokeTokens(['refresh_token']); } finally {
    await auth.removeUser();
    window.location.assign(`${domain}/logout?${new URLSearchParams({ client_id: clientId, logout_uri: `${window.location.origin}/` })}`);
  }
}
