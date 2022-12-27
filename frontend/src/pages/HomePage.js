import React from 'react'
import logo from '../components/logo.png'
import { Image, Container, Center, Button } from '@mantine/core'
import classes from './HomePage.module.css'
import { IconBrandDiscord } from '@tabler/icons';


export default function HomePage() {

  function invHandler() {
    window.location.replace('https://tinyurl.com/AddShockwave')
  }

  return (
    <>
    <div className={classes.div}>
      <Container size="sm" className={classes.starth1}>
        <Center>
          <Image src={logo} className={classes.pic}></Image>
        </Center>
      </Container>
      <Container>
        <Center>
          <h1 className={classes.text}>Create Custom Teams In Your Own Discord Server</h1>
        </Center>
      </Container>
      <Container>
        <Center>
          <Button color="violet" radius="md" size="lg" leftIcon={<IconBrandDiscord />} onClick={invHandler} className={classes.btn}>
            Invite To Server
          </Button>
        </Center>
      </Container>
    </div>
    </>
  )
}
