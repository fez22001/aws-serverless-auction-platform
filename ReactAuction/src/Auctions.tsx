import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, errorMessage, type Auction } from './api';

const empty = { auctionId: '', itemName: '', reserve: '', description: '' };
export default function Auctions() {
  const [auctions, setAuctions] = useState<Auction[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [form, setForm] = useState(empty);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  useEffect(() => {
    const controller = new AbortController();
    api.get<Auction[]>('/auction', { signal: controller.signal }).then(res => setAuctions(res.data))
      .catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); });
    return () => controller.abort();
  }, []);

  const create = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await api.post('/auction', { ...form, reserve: Number(form.reserve), auctionStatus: 'OPEN' });
      setMessage('Auction created.'); setForm(empty);
      setAuctions((await api.get<Auction[]>('/auction')).data);
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  };

  return <div className="card">
    <h2>Auctions</h2>
    {error && <p role="alert" className="error">{error}</p>}
    {message && <p role="status">{message}</p>}
    <table><thead><tr><th>Auction ID</th><th>Item Name</th><th>Reserve</th><th>Description</th><th>Auction Status</th><th>Winning User ID</th></tr></thead>
      <tbody>{auctions.map(a => <tr key={a.auctionId}>
        <td><label><input type="radio" name="selectedAuction" checked={selectedId === a.auctionId} onChange={() => setSelectedId(a.auctionId)} />{a.auctionId}</label></td>
        <td>{a.itemName}</td><td>${a.reserve.toFixed(2)}</td><td>{a.description}</td><td>{a.auctionStatus}</td><td>{a.winningUserId || 'N/A'}</td>
      </tr>)}</tbody>
    </table>
    {!auctions.length && <p>No auctions found</p>}
    <button onClick={() => navigate(`/bidding/${encodeURIComponent(selectedId)}`)} disabled={!selectedId}>Go to Bidding</button>
    <h3>Create Auction</h3>
    <form onSubmit={create}>
      <label>Auction ID (optional)<input name="auctionId" placeholder="Auction ID" maxLength={64} pattern="[A-Za-z0-9_-]+" value={form.auctionId} onChange={e => setForm({ ...form, auctionId: e.target.value })} /></label>
      <label>Item Name<input name="itemName" placeholder="Item Name" required maxLength={200} value={form.itemName} onChange={e => setForm({ ...form, itemName: e.target.value })} /></label>
      <label>Reserve<input name="reserve" placeholder="Reserve" type="number" min="0" max="1000000000" step="0.01" required value={form.reserve} onChange={e => setForm({ ...form, reserve: e.target.value })} /></label>
      <label>Description<input name="description" placeholder="Description" maxLength={2000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
      <button disabled={busy}>{busy ? 'Creating…' : 'Create Auction'}</button>
    </form>
  </div>;
}
