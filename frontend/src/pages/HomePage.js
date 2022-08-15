import React from 'react'
import logo from '../components/logo.png'
import classes from './HomePage.module.css'
import ParticleBackground from './ParticleBackground.js'

export default function HomePage() {

    
    function buttonHandler() {
      window.location.replace('https://tinyurl.com/AddShockwave')
    }

  return (
    <>
    <div>
      <ParticleBackground />
    </div>
    <img src={logo} className={classes.img} alt=""></img>
    <h1 className={classes.h1}>Create custom teams in your own Discord server</h1>
    <button onClick={buttonHandler} className={classes.button}>Invite To Server</button>
    
    </>
  )
}
