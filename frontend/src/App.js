import './App.css';
import { Route, Routes } from 'react-router-dom';

import HomePage from './pages/HomePage';
import Reference from './pages/Reference';
import Layout from './components/Layout';

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/reference" element={<Reference />} />
      </Routes>
    </Layout>
  );
}

export default App;
