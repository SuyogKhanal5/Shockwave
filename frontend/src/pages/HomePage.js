import React from 'react'
import { useNavigate } from 'react-router-dom';
import logo from '../components/logo.png'
import classes from './HomePage.module.css'

export default function HomePage() {

    const navigate = useNavigate();
    
    function buttonHandler() {
        navigate('/reference')
    }

  return (
    <div>
      <img src={logo} className={classes.img}></img>
      <h1 className={classes.h1}>Create custom teams in your own Discord server</h1>
      <button onClick={buttonHandler}>Go to Reference</button>
    </div>
  )
}
