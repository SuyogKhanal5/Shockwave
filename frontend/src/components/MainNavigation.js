import React from 'react'
import {Link} from 'react-router-dom';
import classes from './MainNavigation.module.css'
import logo from './logo.png'

export default function MainNavigation() {
  return (
    <header>
        <div className={classes.nav_wrapper}>
            <div className={classes.logo_container}>
                <img className={classes.logo} src={logo} alt="Logo" />
                <h1>Shockwave</h1>
            </div>
            <nav>
                <div className={classes.nav_container}>
                    <ul className={classes.nav_tabs}>
                        <li className={classes.nav_tab}>
                          <Link to='/'>Home</Link>
                        </li>
                        <li className={classes.nav_tab}>
                          <Link to='/reference'>Reference</Link>
                        </li>
                    </ul>
                </div>
            </nav>
        </div>
    </header>
  )
}
