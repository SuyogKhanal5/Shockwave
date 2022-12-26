import React from 'react'
import MainNavigation from './MainNavigation'

export default function Layout(props) {
  return (
    <div>
      <MainNavigation/>
      <main>
        {props.children}
      </main>
    </div>
  )
}