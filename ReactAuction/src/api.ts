import axios from 'axios';
import { auth } from './auth';

export interface User { userId: string; name: string; acctBalance: number }
export interface Auction {
  auctionId: string; itemName: string; reserve: number; description: string;
  auctionStatus: 'OPEN' | 'CLOSED'; winningUserId?: string; ownerId?: string;
}
export interface Bid { auctionId: string; userId: string; bidAmt: number; date: string }
export interface Result { accepted: boolean; message: string; reason?: string }

export const localMock = import.meta.env.VITE_LOCAL_MOCK === 'true';
const baseURL = import.meta.env.VITE_REACT_APP_API_BASE;
if (!baseURL) throw new Error('Missing VITE_REACT_APP_API_BASE. Copy .env.example to .env.local.');
if (localMock) {
  const url = new URL(baseURL);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)) {
    throw new Error('Local mock mode requires a loopback API URL.');
  }
}
export const api = axios.create({ baseURL, timeout: localMock ? 8000 : 30000 });
api.interceptors.request.use(async config => {
  if (!localMock) {
    const user = await auth?.getUser();
    if (!user || user.expired || !user.id_token) throw new Error('Your session has expired. Sign in again.');
    config.headers.Authorization = user.id_token;
  }
  return config;
});
export function errorMessage(error: unknown): string {
  if (axios.isAxiosError<{ message?: string; error?: string }>(error)) {
    return error.response?.data?.message || error.response?.data?.error ||
      (error.response ? `Request failed (${error.response.status}).` : (localMock ? 'Cannot reach the API. Check that the local mock is running.' : 'The API did not respond. For bids or closing, retry the same request to safely check its result.'));
  }
  return error instanceof Error ? error.message : 'Request failed. Please try again.';
}
