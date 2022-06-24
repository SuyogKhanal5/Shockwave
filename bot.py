from glob import glob
from queue import Empty
import random
from unittest import result
import discord
from discord.ext import commands
import numpy as np
import os

intents = discord.Intents.default()
intents.members = True
client = commands.Bot(command_prefix = '.', intents=intents, help_command=None)

token = ''
with open('token.txt') as f:
    token = f.read()

original_channel, teamList1, teamList2 = "", "", ""
captainNum, drafted = 1, 2

team_size = 5
team1, team2 = [], []
players, members = [], []
team1ids, team2ids = [], []

channel1, channel2 = None, None
captain1, captain2 = None, None

using_captains = False

global roles
roles = {0 : "Top - ", 1 : "Jungle - ", 2 : "Mid - ", 3 : "Bottom - ", 4 : "Support - "}

async def movefunc(ctx):
    global ids, channel1, channel2, team1, team2, original_channel
    original_channel = ctx.message.author.voice.channel
    
    for i in team1:
        await i.move_to(channel1)

    for i in team2:
        await i.move_to(channel2)

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('Use .commands for a list of commands.'))
    print('Bot is online')

@client.command(aliases = ['size'])
async def setTeamSize(ctx, *, sizeChange):
    global team_size
    team_size = sizeChange

    await ctx.send("Set team size!")

@client.command(aliases = ['james', 'hashmap'])
async def fullRandom(ctx):
    global members, ids
    channel = ctx.message.author.voice.channel
    names, ids = [], []
    result1, result2 = "", ""

    for i in channel.members:
        members.append(i)
    
    m = np.array(members)
    np.random.shuffle(m)
    
    for i in m:
        names.append(i.name)
        ids.append(i.id)

    for i in range(team_size*2):
        if(i < team_size):
            result1 += roles[i%5] + " " + str(m[i]) + "\n"
        else:
            result2 += roles[i%5] + " " + str(m[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

@client.command()
async def setTeamChannels(ctx, *, teams="Team-1 Team-2"):
    teamsList = teams.split()

    global channel1
    guild = ctx.guild

    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])
    if (channel1 is None):
        newChannel1 = await guild.create_voice_channel(name=teamsList[0])
        channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])
    if (channel2 is None):
        newChannel2 = await guild.create_voice_channel(name=teamsList[1])
        channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

@client.command()
async def move(ctx):
    await movefunc(ctx)

@client.command()
async def help(ctx, *, specific):
    if (specific == "setTeamSize"):
        helpembed = discord.Embed(title = "setTeamSize", description = "Change the size of the team. \nWARNING: This command doesn't work on \".fullRandom\" or \".fullRandomAll\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "fullRandom"):
        helpembed = discord.Embed(title = "fullRandom", description = "Create random teams of random roles. Aliases are \"james\" and \"hashmap\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "fullRandomAll"):
        helpembed = discord.Embed(title = "fullRandomAll", description = "Create random teams of random roles, set team channels, and move members all in one command.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "move"):
        helpembed = discord.Embed(title = "move", description = "Move team members into set channels. Make sure channels are set already with .setTeamChannels.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "setTeamChannels"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Takes in two arguments seperated by space, two names of voice channels. \nWARNING: This command does not work with voice channels that have spaces in their name. Try hypenating voice channels that need spaces.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "random"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Randomize teams without assigning roles. Useful with setTeamSize for use in other games.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "randomAll"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Randomize teams without assigning roles and move into channels all in one command. Useful with setTeamSize for use in other games.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "returnAll"):
        helpembed = discord.Embed(title = "returnAll", description = "Return all players to their original channel.\n WARNING: \".move\", \"fullRandomAll\", or \"randomAll\" must be used before using this.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "captains"):
        helpembed = discord.Embed(title = "captains", description = "Start the process of creating new teams. Parameters are team captains.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "choose"):
        helpembed = discord.Embed(title = "choose", description = "Team captains can use this command to choose new team members in alternating order.\nWARNING: Use this command after using \".captains\" or \".randomCaptains\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "clearAll"):
        helpembed = discord.Embed(title = "clearAll", description = "Clear all stored teams.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "notify"):
        helpembed = discord.Embed(title = "notify", description = "Sends a direct message to who you mention in order to invite them to the 10 man.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    else:
        await ctx.send("Command not found!")

@client.command(aliases = ["commands"])
async def commandList(ctx):
    helpembed = discord.Embed(title = "Commands", description = "setTeamSize\nfullRandom\nfullRandomAll\nmove\nsetTeamChannels\nrandom\nrandomAll\nreturnAll\ncaptains\nchoose\nrandomCaptains", color = discord.Color.blue())
    await ctx.send(embed = helpembed)
    await ctx.send("Type \".help  ____\" in order to get info on a specific command.")

@client.command()
async def fullRandomAll(ctx, *, teams="Team-1 Team-2"):
    channel = ctx.message.author.voice.channel
    members = []
    await setTeamChannels(ctx, teams)

    for i in channel.members:
        members.append(i)

    if (len(members) < 10 or len(members) > 10):
        await ctx.send("You must have exactly 10 people in the call to use this command.")
    else:
        global ids, original_channel
        m = np.array(members)
        names, ids = [], []  
        result1, result2 = "", ""
        original_channel = ctx.message.author.voice.channel
        counter = 0

        np.random.shuffle(m)

        for i in m:
            names.append(i.name)
            ids.append(i.id)

        for i in range(team_size*2):
            if(i < team_size):
                result1 += roles[i%5] + " " + str(m[i]) + "\n"
            else:
                result2 += roles[i%5] + " " + str(m[i]) + "\n"

        team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
        team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

        await ctx.send(embed = team1_embed)
        await ctx.send(embed = team2_embed)
        
        for i in range(0,10):
            if counter <=4:
                id = ids[i]
                member = ctx.guild.get_member(id)
                await member.move_to(channel1)
            else:
                id = ids[i]
                member = ctx.guild.get_member(id)
                await member.move_to(channel2)
            counter += 1

@client.command()
async def randomTeams(ctx):
    global team1, team2, ids
    members, names, ids = [], [], []
    result1, result2 = "", ""
    channel = ctx.message.author.voice.channel
    for i in channel.members:
        members.append(i)

    m = np.array(members)
    np.random.shuffle(m)
    
    for i in m:
        names.append(i.name)    
        ids.append(i.id)

    for i in range(0, len(ids)):
        if(i < (len(ids)/2)):
            result1 += str(m[i]) + "\n"
            team1.append(m[i])
        else:
            result2 += str(m[i]) + "\n"
            team2.append(m[i])

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

@client.command()
async def randomAll(ctx, *, teams="Team-1 Team-2"):
    global channel1, channel2, ids, original_channel
    original_channel = ctx.message.author.voice.channel
    channel = ctx.message.author.voice.channel
    members, names, ids = [], [], []
    result1, result2 = "", ""
    counter = 0
    
    teamsList = teams.split()

    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])
    await ctx.send("Channels set!")

    for i in channel.members:
        members.append(i)

    m = np.array(members)
    np.random.shuffle(m)
    
    for i in m:
        names.append(i.name)
        ids.append(i.id)

    for i in range(0,len(ids)):
        if(i <= len(ids)/2):
            result1 += str(m[i]) + "\n"
        else:
            result2 += str(m[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)
    
    for i in range(0,len(ids)):
        if counter <=(len(ids)/2):
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel1)
        else:
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel2)
        counter += 1

@client.command()
async def returnTeams(ctx):
    global original_channel

    if (original_channel == ""):
        await ctx.send("You have not been seperated into team voice channels! Use \".move\" first.")
    else:
        if (using_captains):
            for i in team1ids:
                member = ctx.guild.get_member(i)
                await member.move_to(original_channel)
            for i in team2ids:
                member =  ctx.guild.get_member(i)
                await member.move_to(original_channel)
        else:
            for i in range(0,10):
                id = ids[i]
                if (id is None):
                    continue
                else:
                    member = ctx.guild.get_member(id)
                    if (member is not None and member.voice):
                        await member.move_to(original_channel)

@client.command()
async def returnAll(ctx):
    global original_channel

    if (original_channel == ""):
        await ctx.send("You have not been seperated into team voice channels! Use \".move\" first.")
    else:
        aggregate = channel1.members
        aggregate.extend(channel2.members)

        for i in aggregate:
            await i.move_to(original_channel)

@client.command()
async def captains(ctx, captain_1: discord.Member, captain_2: discord.Member):
    global captain1, captain2, members, players, using_captains, original_channel, teamList1, teamList2
    original_channel = ctx.message.author.voice.channel
    channel = ctx.message.author.voice.channel
    using_captains = True
    playersString = ""

    if (captain_1 == None or captain_2 == None):
        await ctx.send("Mention two team captains!")
    else:
        captain1 = captain_1     
        teamList1 += str(captain1.display_name)
        team1ids.append(captain1.id)
        team1.append(captain1)
        team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())

        captain2 = captain_2
        teamList2 += str(captain2.display_name)
        team2ids.append(captain2.id)
        team2.append(captain2)
        team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

        await ctx.send(embed = team1_embed)
        await ctx.send(embed = team2_embed)

        for player in channel.members:
            if(player.display_name != captain1.display_name and player.display_name != captain2.display_name):
                players.append(player.display_name)
                playersString += player.display_name + "\n"
         
        players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
        await ctx.send(embed = players_embed)
        await ctx.send("Captains selected!")
        await ctx.send(captain_1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def captainsAll(ctx, captain_1: discord.Member, captain_2: discord.Member, *, teams="Team-1 Team-2"):
    global captain1, captain2, members, players, using_captains, original_channel, channel1, channel2, teamList1, teamList2
    original_channel = ctx.message.author.voice.channel
    using_captains = True
    teamsList = teams.split()

    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

    if (captain_1 == None or captain_2 == None):
        await ctx.send("Mention two team captains!")
    else:
        channel = ctx.message.author.voice.channel
        playersString = ""

        captain1 = captain_1
        teamList1 += str(captain1.display_name)
        team1ids.append(captain1.id)
        team1.append(captain1)
        team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())

        captain2 = captain_2
        teamList2 += str(captain2.display_name)
        team2ids.append(captain2.id)
        team2.append(captain2)
        team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

        await ctx.send(embed = team1_embed)
        await ctx.send(embed = team2_embed)
        
        for player in channel.members:
            if(player.display_name != captain1.display_name and player.display_name != captain2.display_name):
                players.append(player.display_name)
                playersString += player.display_name + "\n"
         
        players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
        await ctx.send(embed = players_embed)
        await ctx.send("Captains selected!")
        await ctx.send(captain_1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def choose(ctx, member: discord.Member):
    global teamList1, teamList2, captainNum, captain1, captain2, players
    switch = True

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):
            
            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):

            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True): 
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")


@client.command()
async def clearAll(ctx):
    global original_channel, captainNum, drafted, team_size, team1, team2, teamList1, teamList2, channel1, channel2, captain1, captain2
    
    original_channel = ""
    captainNum, drafted = 1, 2
    team_size = 5
    team1, team2 = [], []
    teamList1, teamList2 = None, None
    channel1, channel2 = None, None
    captain1, captain2 = None, None

    await ctx.send("Cleared!")

@client.command()
async def clearTeams(ctx):
    global original_channel, captainNum, drafted, team_size, team1, team2, teamList1, teamList2, channel1, channel2, captain1, captain2
    
    original_channel = ""
    captainNum , drafted = 1, 2
    team_size = 5
    team1, team2 = [], []
    teamList1, teamList2 = None, None
    captain1,  captain2 = None, None

    await ctx.send("Cleared!")

@client.command(aliases = ['invite'])
async def notify(ctx, member: discord.Member):
    global team_size
    channel = await member.create_dm()
    invite_channel = ctx.message.author.voice.channel
    invite_link = await invite_channel.create_invite(max_uses=1,unique=True)
    content = ctx.message.author.name + " has invited you to a " + str(team_size * 2) + ' man!\n\n' + str(invite_link)
    await channel.send(content)
    await ctx.send('Sent an invite for the ' + str(team_size * 2) + ' man!')

@client.command()
async def randomCaptains(ctx):
    global members, captain1, captain2, members, players, using_captains, original_channel, teamList1, teamList2
    channel = ctx.message.author.voice.channel
    original_channel = ctx.message.author.voice.channel
    using_captains = True
    playersString = ""

    for i in channel.members:
        members.append(i)
    
    m = np.array(members)
    np.random.shuffle(m)

    captain1 = m[0]
    teamList1 += str(captain1.display_name)
    team1ids.append(captain1.id)
    team1.append(captain1)
    team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())

    captain2 = m[1]
    teamList2 += str(captain2.display_name)
    team2ids.append(captain2.id)
    team2.append(captain2)
    team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)
    
    for player in channel.members:
        if (player.display_name != captain1.display_name and player.display_name != captain2.display_name):
            players.append(player.display_name)
            playersString += player.display_name + "\n"
         
    players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
    await ctx.send(embed = players_embed)
    await ctx.send("The captains are <@{}>".format(captain1.id) + " and <@{}>".format(captain2.id))
    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def chooseRandom(ctx):
    global teamList1, teamList2, captainNum, captain1, captain2, players, player_members
    channel = ctx.message.author.voice.channel
    player_members = []
    switch = True

    if (len(players) == 0):
        await ctx.send("All players have been selected")

    for player in channel.members:
        if(player.display_name != captain1.display_name and player.display_name != captain2.display_name and players.__contains__(player.display_name) == True):
            player_members.append(player)
    
    m = np.array(player_members)
    np.random.shuffle(m)

    member = m[0]

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")

@client.command()
async def chooseFrom(ctx, *_available_players: discord.Member):
    global teamList1, teamList2, captainNum, captain1, captain2, players, player_members
    channel = ctx.message.author.voice.channel
    player_members, available_players = [], []
    switch = True

    for i in _available_players:
        available_players.append(i)

    print(available_players[0])

    if (len(players) == 0):
        await ctx.send("All players have been selected")
    
    m = np.array(available_players)
    np.random.shuffle(m)
    member = m[0]

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")

@client.command()
async def roll(ctx, *, num=6):
    if (int(num) > 1):
        rand = random.randint(1, int(num))
        await ctx.send("You rolled " + str(rand))
    else:
        await ctx.send("Please use a number greater than 1.")

@client.command()
async def randomRoles(ctx):
    roles = {0 : "Top - ", 1 : "Jungle - ", 2 : "Mid - ", 3 : "Bottom - ", 4 : "Support - "}
    random.shuffle(team1)
    random.shuffle(team2)

    result1, result2 = "", ""

    for i in range (10):
        if(i < 5):
            result1 += roles.get(i%5) + str(team1[i%5].display_name) + "\n"
        else:
            result2 += roles.get(i%5) + str(team2[i%5].display_name) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

client.run(token)