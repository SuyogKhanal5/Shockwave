import React from 'react'
import {Link} from 'react-router-dom';

export default function MainNavigation() {
  return (
    <header>
      <div>Shockwave</div>
      <nav>
        <ul>
          <li>
              <Link to='/'>Home</Link>
          </li>
          <li>
              <Link to='/reference'>Reference</Link>
          </li>
        </ul>
      </nav>
    </header>
  )
}
