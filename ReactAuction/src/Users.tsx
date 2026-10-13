import { useEffect, useState } from 'react';
import { api, errorMessage, localMock, type User } from './api';

export default function Users() {
  const [users, setUsers] = useState<User[]>([]);
  const [form, setForm] = useState({ userId: '', name: '', acctBalance: '' });
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api.get<User[]>('/user', { signal: controller.signal }).then(res => setUsers(res.data))
      .catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); });
    return () => controller.abort();
  }, []);

  const create = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await api.post('/user', localMock ? { ...form, acctBalance: Number(form.acctBalance) } : { name: form.name });
      setMessage('User created.'); setForm({ userId: '', name: '', acctBalance: '' });
      setUsers((await api.get<User[]>('/user')).data);
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  };

  return <div className="card">
    <h2>{localMock ? 'User List' : 'My demo account'}</h2>{!localMock && <p>Your account starts with 1,000 fictional credits. Balances cannot be edited. Create your profile before bidding.</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {message && <p role="status">{message}</p>}
    <table><thead><tr><th>User ID</th><th>Name</th><th>Account Balance</th></tr></thead>
      <tbody>{users.length ? users.map(user => <tr key={user.userId}>
        <td>{user.userId}</td><td>{user.name}</td><td>${user.acctBalance.toFixed(2)}</td>
      </tr>) : <tr><td colSpan={3}>No users found</td></tr>}</tbody>
    </table>
    {(localMock || users.length === 0) && <><h3>{localMock ? 'Create User' : 'Create my profile'}</h3>
    <form onSubmit={create}>
      {localMock && <label>User ID (optional)<input name="userId" placeholder="User ID" maxLength={64} pattern="[A-Za-z0-9_-]+" value={form.userId} onChange={e => setForm({ ...form, userId: e.target.value })} /></label>}
      <label>Name<input name="name" placeholder="Name" required maxLength={200} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
      {localMock && <label>Balance<input name="acctBalance" placeholder="Balance" type="number" min="0" max="1000000000" step="0.01" required value={form.acctBalance} onChange={e => setForm({ ...form, acctBalance: e.target.value })} /></label>}
      <button disabled={busy}>{busy ? 'Creating…' : 'Create'}</button>
    </form></>}
  </div>;
}
