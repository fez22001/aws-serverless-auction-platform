// App.tsx
import React from 'react';
import { BrowserRouter as Router, Routes, Route, Link } from 'react-router-dom';
import AuthGate from './AuthGate';
import Users from './Users';
import Auctions from './Auctions';
import Bidding from './Bidding';
import Help from './Help';
import './App.css';
import { localMock } from './api';

const App: React.FC = () => {
  return (
    <AuthGate><Router>
      <nav className="navbar">
        <Link to="/users" className="nav-link">{localMock ? 'Users' : 'My account'}</Link>
        <Link to="/auctions" className="nav-link">Auctions</Link>
        <Link to="/help" className="nav-link">Help</Link>
      </nav>
      <div className="container">
        {localMock && <aside className="demo-banner">Local demo · Synthetic data only · Resets when the mock restarts · No AWS connection</aside>}
        <Routes>
          <Route path="/" element={<Users />} />
          <Route path="/users" element={<Users />} />
          <Route path="/auctions" element={<Auctions />} />
          <Route path="/bidding/:auctionId" element={<Bidding />} />
          <Route path="/help" element={<Help />} />
        </Routes>
      </div>
    </Router></AuthGate>
  );
};

export default App;
