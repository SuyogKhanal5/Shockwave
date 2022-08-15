import React from 'react'
import ParticleBackground from './ParticleBackground'
import './Reference.scss'


export default function Reference() {
  return (
    <>
     <ParticleBackground/>
    <div class="container">
	
      <div class="table">
        <div class="table-header">
          <div class="header__item"><p id="name" class="filter__link" href="#">Command</p></div>
          <div class="header__item"><p id="wins" class="filter__link filter__link--number" href="#">Description</p></div>
          <div class="header__item"><p id="draws" class="filter__link filter__link--number" href="#">Inputs</p></div>
        </div>
        <div class="table-content">	
          <div class="table-row">		
            <div class="table-data">.setTeamSize</div>
            <div class="table-data">Sets the size of each team</div>
            <div class="table-data">integer</div>
          </div>
          <div class="table-row">
            <div class="table-data">.fullRandom</div>
            <div class="table-data">Randomizes teams and roles for League of Legends</div>
            <div class="table-data">n/A</div>
          </div>
          <div class="table-row">
            <div class="table-data">.fullRandomAll</div>
            <div class="table-data">Randomizes teams and roles for League of Legends while setting the team voice channels</div>
            <div class="table-data">Two voice channels</div>
          </div>
          <div class="table-row">
            <div class="table-data">.move</div>
            <div class="table-data">Moves the players in the call to their respective teams once they are set (.setTeamChannels is a prerequiste)</div>
            <div class="table-data">n/A</div>
          </div>
          <div class="table-row">
            <div class="table-data">.setTeamChannels</div>
            <div class="table-data">Sets the voice channels for each team to be moved to</div>
            <div class="table-data">Two voice channels</div>
          </div>
          <div class="table-row">
            <div class="table-data">.random</div>
            <div class="table-data">Randomizes teams</div>
            <div class="table-data">n/A</div>
          </div>
          <div class="table-row">
            <div class="table-data">.randomAll</div>
            <div class="table-data">Randomizes teams and sets the team voice channels</div>
            <div class="table-data">Two Voice Channels</div>
          </div>
          <div class="table-row">
            <div class="table-data">.returnAll</div>
            <div class="table-data">Returns all players who have been moved to the original voice channel</div>
            <div class="table-data">n/A</div>
          </div>
          <div class="table-row">
            <div class="table-data">.captains</div>
            <div class="table-data">Sets team captains and allows them to choose players</div>
            <div class="table-data">Two player ids</div>
          </div>
          <div class="table-row">
            <div class="table-data">.choose</div>
            <div class="table-data">Command for team captains to choose their teammates</div>
            <div class="table-data">One player id</div>
          </div>
          <div class="table-row">
            <div class="table-data">.randomCaptains</div>
            <div class="table-data">Chooses two team captains at random</div>
            <div class="table-data">n/A</div>
          </div>
        </div>	
      </div>
    </div>
    </>
  )
}
