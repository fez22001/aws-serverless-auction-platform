import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { requestIdentity } from './requestIdentity';
import { api, errorMessage, localMock, type Auction, type User, type Bid, type Result } from './api';

export default function Bidding() {
  const { auctionId = '' } = useParams<{ auctionId: string }>();
  return <AuctionBidding key={auctionId} auctionId={auctionId} />;
}

function AuctionBidding({ auctionId }: { auctionId: string }) {
  const [auction, setAuction] = useState<Auction | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [bids, setBids] = useState<Bid[]>([]);
  const [selectedUserId, setSelectedUserId] = useState('');
  const [bidAmt, setBidAmt] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [a, u, b] = await Promise.all([
      api.get<Auction>(`/auction/${encodeURIComponent(auctionId)}`, { signal }),
      api.get<User[]>('/user', { signal }),
      api.get<Bid[]>('/bid', { params: { auctionId }, signal }),
    ]);
    if (!signal?.aborted) { setAuction(a.data); setUsers(u.data); setBids(b.data); if (!localMock) setSelectedUserId(u.data[0]?.userId || ''); }
  }, [auctionId]);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal)
      .catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);

  const action = async (kind: 'bid' | 'close') => {
    setBusy(true); setError(''); setResult(null);
    try {
      const payload: Record<string, string | number> = kind === 'bid'
        ? { auctionId, userId: selectedUserId, bidAmt: Number(bidAmt) } : { auctionId };
      const body = { ...payload, requestId: requestIdentity(kind, payload) };
      const response = kind === 'bid'
        ? await api.post<Result>('/bid', body)
        : await api.put<Result>('/auction', body);
      setResult(response.data);
      await refresh();
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  };
  const reload = async () => {
    setBusy(true); setError('');
    try { await refresh(); } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  };
  const disabled = busy || loading || auction?.auctionStatus !== 'OPEN';

  return <div className="bidding-container">
    {error && <p role="alert" className="error">{error}</p>}
    {result && <p role={result.accepted ? 'status' : 'alert'} className={result.accepted ? 'success' : 'error'}>{result.message}</p>}
    <div className="auction-details card">
      <h2>Auction Item: {auction?.itemName || (loading ? 'Loading…' : 'Unavailable')}</h2>
      <p>Auction ID: {auction?.auctionId}</p><p>Description: {auction?.description}</p>
      <p>Reserve: ${auction?.reserve.toFixed(2)}</p><p>Auction Status: {auction?.auctionStatus}</p>
      <p>Winning User: {auction?.winningUserId || 'N/A'}</p>
    </div>
    <div className="bidding-main">
      <div className="left card"><h3>{localMock ? 'Users' : 'My bidding account'}</h3>{!localMock && !users.length && <p>Create your profile in My account before bidding.</p>}
        <form onSubmit={event => { event.preventDefault(); void action('bid'); }}>
          {users.map(user => <label key={user.userId}>
            <input type="radio" name="user" value={user.userId} checked={selectedUserId === user.userId} onChange={() => setSelectedUserId(user.userId)} disabled={disabled || !localMock} />
            {user.userId}: {user.name} (Acct Balance: ${user.acctBalance.toFixed(2)})
          </label>)}
          <label>Bid Amount<input type="number" placeholder="Bid Amount" min="0.01" max="1000000000" step="0.01" required value={bidAmt} onChange={e => setBidAmt(e.target.value)} disabled={disabled} /></label>
          <button disabled={disabled || !selectedUserId}>Place Bid</button>
        </form>
      </div>
      <div className="right card"><h3>Bids</h3>
        <button onClick={() => void reload()} disabled={busy || loading}>Refresh</button>
        {bids.length ? <ul>{bids.map(bid => <li key={`${bid.auctionId}-${bid.bidAmt}`}>{bid.userId}: ${bid.bidAmt.toFixed(2)} ({new Date(bid.date).toLocaleString()})</li>)}</ul> : <p>No bids placed yet.</p>}
      </div>
    </div>
    {(localMock || auction?.ownerId === users[0]?.userId) && <button onClick={() => void action('close')} disabled={disabled}>Close Auction</button>}
  </div>;
}
