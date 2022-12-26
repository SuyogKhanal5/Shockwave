import React from 'react'
import logo from '../components/logo.png'
import { Image } from '@mantine/core'
import classes from './HomePage.module.css'

export default function HomePage() {
  return (
    <>
    <div className={classes.div}>
      <Image src={logo}></Image>
      <h1>HOME</h1>
    </div>
    </>
  )
}
