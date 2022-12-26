import React from 'react'
import classes from './MainNavigation.module.css'
import { useNavigate } from 'react-router-dom';
import logo from './logo.png'
import { Button } from '@mantine/core';

export default function MainNavigation() {

    const navigate = useNavigate();

    function homeHandler() {
        navigate('/');
    }

    function referenceHandler() {
        navigate('/reference');
    }

    function githubHandler() {
        window.location.replace('https://github.com/SuyogKhanal5/Shockwave/tree/main');
    }

  return (
    <>

    <header>
        <div className={classes.nav_wrapper}>
            <div className={classes.logo_container}>
                <img className={classes.logo} src={logo} alt="Logo" />
                <h1>Shockwave</h1>
            </div>
            <nav>
                <div className={classes.nav_container}>
                    <ul className={classes.nav_tabs}>
                        <li>
                            <Button variant="subtle" color="gray" onClick={homeHandler}>
                                Home
                            </Button>
                        </li>
                        <li>
                            <Button variant="subtle" color="gray" onClick={referenceHandler}>
                                Reference
                            </Button>
                        </li>
                        <li>
                            <Button variant="subtle" color="gray" onClick={githubHandler}>
                                Github
                            </Button>
                        </li>
                    </ul>
                </div>
            </nav>
        </div>
    </header>
    </>
  )
}