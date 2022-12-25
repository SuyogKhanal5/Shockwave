import { Route, Routes } from 'react-router-dom';

import HomePage from './pages/HomePage';
import Reference from './pages/Reference';

function App() {
  return (
    <>
    <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/reference" element={<Reference />} />
    </Routes>
    </>
  );
}

export default App;
