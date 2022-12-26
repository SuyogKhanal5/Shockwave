import React from 'react'
import { Table, Container, Center } from "@mantine/core";
import classes from './Reference.module.css'

export default function Reference() {

  const elements = [
    { command: ".setTeamSize", desc: "Sets the size of each team", input: 'Integer'},
    { command: ".fullRandom", desc: "Randomizes teams and roles for League of Legends", input: 'N/A'},
    { command: ".fullRandomAll", desc: "Randomizes teams and roles for League of Legends while setting the team voice channels", input: 'Two voice channels'},
    { command: ".move", desc: "Moves the players in the call to their respective teams once they are set (.setTeamChannels is a prerequiste)", input: 'N/A'},
    { command: ".help", desc: "Links this webpage", input: 'N/A'},
    { command: ".setTeamChannels", desc: "Sets the voice channels for each team to be moved to", input: 'Two voice channels'},
    { command: ".randomTeams", desc: "Randomizes teams", input: 'N/A'},
    { command: ".randomAll", desc: "Randomizes teams and sets the team voice channels", input: 'Two Voice Channels'},
    { command: ".returnAll", desc: "Returns all users in the current voice channel back to the original voice channel", input: 'N/A'},
    { command: ".returnTeams", desc: "Returns just the players back to the original voice channel", input: 'N/A'},
    { command: ".captains", desc: "Sets team captains and allows them to choose players", input: 'Two player ids (mentions, i.e @user#91420 @other#21930'},
    { command: ".captainsAll", desc: "Sets team captains and allows them to choose players and sets the team voice channels", input: 'Two player ids (mentions, i.e @user#91420 @other#21930) and Two Voice Channels'},
    { command: ".choose", desc: "Command for team captains to choose their teammates", input: 'One player id (mention, i.e @user#91420)'},
    { command: ".notify", desc: "Notify and send an Direct Message/Invite to the server and voice channel. (.invite is an alias)", input: 'One player id (mention, i.e @user#91420)'},
    { command: ".randomCaptains", desc: "Chooses two team captains at random", input: 'N/A'},
    { command: ".chooseRandom", desc: "Chooses a random player to add to your team. Used in place of .choose", input: 'N/A'},
    { command: ".chooseFrom", desc: "Chooses a random player from a few given players. Used in place of .choose", input: 'A variable number of player ids (mentions, i.e @user#91420 @other#21930)'},
    { command: ".roll", desc: "Rolls a die with the number of sides depending on input given", input: 'Integer'},
    { command: ".randomizeRoles", desc: "Randomizes roles for each team. Teams must have been created before using this command.", input: 'N/A'},
  ];

  const rows = elements.map((element) => (
    <tr key={element.name} >
      <td>{element.command}</td>
      <td>{element.desc}</td>
      <td>{element.input}</td>
    </tr>
  ));

  return (
    <div className={classes.div}>
      <Container>
        <Center>
          <Table withColumnBorders className={classes.entry}>
            <thead>
              <tr>
                <th className={classes.th}>Command</th>
                <th className={classes.th}>Description</th>
                <th className={classes.th}>Input(s)</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </Table>
        </Center>
      </Container>
    </div>
  )
}
