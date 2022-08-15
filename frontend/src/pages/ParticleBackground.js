import React from 'react'
import Particles from 'react-tsparticles'
import {loadFull} from 'tsparticles'
import layers from './ParticleBackground.module.css'

const particlesInit = async (main) => {
    console.log(main);
    await loadFull(main);
};

const particlesLoaded = (container) => {
    console.log(container);
  };

const ParticleBackground = () => {
  return (
    <div >
    <body className={layers.main}>
      <Particles 
        id="tsparticles"
        init={particlesInit}
        loaded={particlesLoaded}
        options={{
            background: {
                color: '#131313',
            },
            fpsLimit: 60,
            interactivity: {
                detectsOn: "canvas",
                events: {
                   resize: true 
                },
            },
            particles: {
                color: {
                    value: "ffffff"
                },
                number: {
                    density: {
                        enable: true,
                        area: 1080
                    },
                    limit: 0,
                    value: 400,
                },
                opacity: {
                    animation: {
                        enable: true,
                        minimumValue: 0.05,
                        speed: 1,
                        sync: false
                    },
                    random: {
                        enable: true,
                        minimumValue: 0.01
                    },
                    value: 1
                },
                shape: {
                    type: "polygon",
                },
                size: {
                    random: {
                        enable: true,
                        minimumValue: 0.5
                    },
                    value: 2
                }
            }
        }}
      />
      </body>
    </div>
  )
}
export default ParticleBackground