# Import Statements
import random
import discord
from discord.ext import commands
import numpy as np

# Set Intents

intents = discord.Intents.default()
intents.members = True

# Set command prefix

client = commands.Bot(command_prefix='.', intents=intents, help_command=None)

# Get token from text file

token = ''
with open('token.txt') as f:
    token = f.read()

# Global Variables

original_channel, teamList1, teamList2, result1, result2, commandPrefix = ""

captainNum, drafted, team_size = 1, 2, 5

team1, team2, players, members, team1ids, team2ids = []

channel1, channel2, captain1, captain2 = None

using_captains = False

roles = {0: "Top - ", 1: "Jungle - ",
         2: "Mid - ", 3: "Bottom - ", 4: "Support - "}

# Events


@client.event
async def on_ready():
    await client.change_presence(activity=discord.Game('Visit https://shockwave.lol for a list of commands.'))
    print('Bot is online')

# Helper Functions


async def movefunc(ctx):
    global ids, channel1, channel2, team1, team2, original_channel
    original_channel = ctx.message.author.voice.channel

    for i in team1:
        await i.move_to(channel1)

    for i in team2:
        await i.move_to(channel2)


async def randomizeTeamHelper(ctx):
    global members, ids, team1, team2, result1, result2
    channel = ctx.message.author.voice.channel
    names, ids = [], []

    for i in channel.members:
        members.append(i)

    m = np.array(members)
    np.random.shuffle(m)

    for i in m:
        names.append(i.name)
        ids.append(i.id)

    for i in range(team_size*2):
        if(i < team_size):
            team1.append(m[i])
            result1 += str(m[i]) + "\n"
        else:
            team2.append(m[i])
            result2 += str(m[i]) + "\n"


async def printEmbed(ctx, channel=None):
    global result1, result2, captain1, captain2

    team1_embed = discord.Embed(
        title="TEAM 1", description=result1, color=discord.Color.blue())
    team2_embed = discord.Embed(
        title="TEAM 2", description=result2, color=discord.Color.red())

    if (channel != None):
        for player in channel.members:
            if(player.display_name != captain1.display_name and player.display_name != captain2.display_name):
                players.append(player.display_name)
                playersString += player.display_name + "\n"

        players_embed = discord.Embed(
            title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
        await ctx.send(embed=players_embed)

    await ctx.send(embed=team1_embed)
    await ctx.send(embed=team2_embed)


async def setTeamHelper(ctx, teams="Team-1 Team-2"):
    teamsList = teams.split()

    global channel1
    guild = ctx.guild

    channel1 = discord.utils.get(ctx.guild.channels, name=teamsList[0])
    if (channel1 is None):
        await guild.create_voice_channel(name=teamsList[0])
        channel1 = discord.utils.get(ctx.guild.channels, name=teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name=teamsList[1])
    if (channel2 is None):
        await guild.create_voice_channel(name=teamsList[1])
        channel2 = discord.utils.get(ctx.guild.channels, name=teamsList[1])

    await ctx.send("Channels set!")


async def both(ctx):
    randomizeTeamHelper(ctx)
    randomRoleHelper(ctx)


async def randomRoleHelper(ctx):
    global roles, team1, team2, result1, result2

    random.shuffle(team1)
    random.shuffle(team2)

    for i in range(10):
        if(i < 5):
            result1 += roles.get(i % 5) + str(team1[i % 5].display_name) + "\n"
        else:
            result2 += roles.get(i % 5) + str(team2[i % 5].display_name) + "\n"


async def captainsHelper(ctx, captain_1, captain_2):
    global captain1, captain2, members, players, using_captains, original_channel, teamList1, teamList2
    original_channel = ctx.message.author.voice.channel
    channel = ctx.message.author.voice.channel
    using_captains = True

    if (captain_1 == None or captain_2 == None):
        await ctx.send("Mention two team captains!")
    else:
        captain1 = captain_1
        teamList1 += str(captain1.display_name)
        team1ids.append(captain1.id)
        team1.append(captain1)

        captain2 = captain_2
        teamList2 += str(captain2.display_name)
        team2ids.append(captain2.id)
        team2.append(captain2)

        await printEmbed(ctx, channel)

        await ctx.send("Captains selected!")
        await ctx.send(captain_1.mention + ", type \".choose  @_____\" to pick a player for your team")

# Commands


@client.command(aliases=['size'])
async def setTeamSize(ctx, *, sizeChange):
    global team_size
    team_size = sizeChange

    await ctx.send("Set team size!")


@client.command(aliases=['james', 'hashmap'])
async def fullRandom(ctx):
    await both(ctx)
    await printEmbed(ctx)


@client.command()
async def setTeamChannels(ctx, *, teams="Team-1 Team-2"):
    await setTeamHelper(ctx, teams)


async def all(ctx, teams):
    await printEmbed(ctx)
    await setTeamHelper(ctx, teams)
    await movefunc(ctx)


@client.command()
async def move(ctx):
    await movefunc(ctx)


@client.command()
async def help(ctx):
    await ctx.send('Visit https://shockwave.lol for a full list of commands')


@client.command()
async def fullRandomAll(ctx, *, teams="Team-1 Team-2"):
    await both(ctx)
    await all(ctx, teams)


@client.command()
async def randomTeams(ctx):
    await randomizeTeamHelper(ctx)
    await printEmbed(ctx)


@client.command()
async def randomAll(ctx, *, teams="Team-1 Team-2"):
    await randomizeTeamHelper(ctx)
    await all(ctx, teams)


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
                member = ctx.guild.get_member(i)
                await member.move_to(original_channel)
        else:
            for i in range(0, 10):
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
    await captainsHelper(ctx, captain_1, captain_2)


@client.command()
async def captainsAll(ctx, captain_1: discord.Member, captain_2: discord.Member, *, teams="Team-1 Team-2"):
    await setTeamHelper(ctx, teams)
    await captainsHelper(ctx, captain_1, captain_2)


@client.command()
async def choose(ctx, member: discord.Member):
    global teamList1, teamList2, captainNum, captain1, captain2, players
    switch = True

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)
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

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)

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
    captainNum, drafted = 1, 2
    team_size = 5
    team1, team2 = [], []
    teamList1, teamList2 = None, None
    captain1,  captain2 = None, None

    await ctx.send("Cleared!")


@client.command(aliases=['invite'])
async def notify(ctx, member: discord.Member):
    global team_size
    channel = await member.create_dm()
    invite_channel = ctx.message.author.voice.channel
    invite_link = await invite_channel.create_invite(max_uses=1, unique=True)
    content = ctx.message.author.name + " has invited you to a " + \
        str(team_size * 2) + ' man!\n\n' + str(invite_link)
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
    team1_embed = discord.Embed(
        title="TEAM 1", description=teamList1, color=discord.Color.blue())

    captain2 = m[1]
    teamList2 += str(captain2.display_name)
    team2ids.append(captain2.id)
    team2.append(captain2)
    team2_embed = discord.Embed(
        title="TEAM 2", description=teamList2, color=discord.Color.red())

    await ctx.send(embed=team1_embed)
    await ctx.send(embed=team2_embed)

    for player in channel.members:
        if (player.display_name != captain1.display_name and player.display_name != captain2.display_name):
            players.append(player.display_name)
            playersString += player.display_name + "\n"

    players_embed = discord.Embed(
        title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
    await ctx.send(embed=players_embed)
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

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)
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

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)

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

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)
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

            team1_embed = discord.Embed(
                title="TEAM 1", description=teamList1, color=discord.Color.blue())
            team2_embed = discord.Embed(
                title="TEAM 2", description=teamList2, color=discord.Color.red())

            await ctx.send(embed=team1_embed)
            await ctx.send(embed=team2_embed)

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

            players_embed = discord.Embed(
                title="PLAYERS", description=playersString, color=discord.Color.dark_purple())
            await ctx.send(embed=players_embed)

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
async def randomizeRoles(ctx):
    await randomRoleHelper(ctx)
    await printEmbed(ctx)


@client.command()
async def changePrefix(ctx, *, prefix):
    global commandPrefix
    temp = commandPrefix
    commandPrefix = prefix

    await ctx.send('Changed the prefix from ' + temp + ' to ' + prefix)


client.run(token)
