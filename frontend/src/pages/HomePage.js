import React from 'react'
import { useNavigate } from 'react-router-dom';

export default function HomePage() {

    const navigate = useNavigate();
    
    function buttonHandler() {
        navigate('/reference')
    }

  return (
    <div>
      <h1>Shockwave</h1>
      <button onClick={buttonHandler}>Go to Reference</button>
    </div>
  )
}
