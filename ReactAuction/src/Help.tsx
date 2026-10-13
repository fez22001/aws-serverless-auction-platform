import { localMock } from './api';

export default function Help() {
  return <section className="card"><h2>Using the auction demo</h2>
    <p>All bids and balances are fictional credits. This demo does not sell real goods or process payments.</p>
    <h3>Get started</h3>
    <p>{localMock ? 'Create synthetic users with balances on the Users page.' : 'Sign in with your invitation, then open My account and create your profile. You receive 1,000 fictional credits once.'}</p>
    <h3>Create and bid</h3>
    <p>Open Auctions to create an item or select an existing one. Enter a bid greater than the current highest bid. Your available balance must cover it.</p>
    <p>{localMock ? 'The local simulator lets you select different synthetic bidders.' : 'Bids always use your signed-in identity. Other users cannot spend your balance or change your profile.'}</p>
    <h3>Close an auction</h3>
    <p>{localMock ? 'Use Close Auction after a bid meets the reserve.' : 'Only the auction creator can close it. The highest bid must meet the reserve. The winner is charged once.'} Closed auctions do not accept more bids.</p>
    <h3>If a request times out</h3>
    <p>Retry the same action with the same amount. Repeated submissions use the same request ID to avoid duplicate charges. Use Refresh to check the latest result.</p>
    <p>Balances are checked at bid time and again at closing; bids do not reserve credits across other auctions.</p>
  </section>;
}
