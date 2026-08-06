# Import Statements
import random
import os.path as path
import sqlite3
import discord
from discord import app_commands
from TourneyClasses import Team, Tournament, Match, Player
import helper

# Get token from text file
token = ""

with open("token.txt") as f:
    token = f.readline()

# Connect to Database
dataFolder = "data/guildData/serverInfo/"
dbpath = dataFolder + "main.db"

mainDB = sqlite3.connect(dbpath)
cursor = mainDB.cursor()

helperObj = helper.helpers(cursor, mainDB)

# if database doesn't already exist, create it
if not path.isfile(dbpath):
    cursor.execute("CREATE TABLE servers(guildId, serverName, original_channel, team1, team2, players, channel1, channel2, mode, turn, team_size, tournament, elo)")

# Hash Map
roles = {
    0: "Top - ",
    1: "Jungle - ",
    2: "Mid - ",
    3: "Bottom - ",
    4: "Support - "
}

# create client object and slash commands
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    # TODO: remove id when deploying. current has banter server ID (also do for all commands)
    await tree.sync(guild=discord.Object(526081127643873280))
    print('Command: Shockwave')


@client.event
async def on_guild_join(ctx):
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)", (ctx.id, ctx.name))
    mainDB.commit()


@client.event
async def on_guild_remove(ctx):
    cursor.execute("""DELETE FROM servers WHERE guildId=?""", (ctx.id,))
    mainDB.commit()

# Commands
# TODO: change ids when putting into production


@tree.command(
    name="set-team-size",
    description="Set the size of the teams",
    guild=discord.Object(id=526081127643873280)
)
async def setTeamSize(ctx, *, sizechange: int):
    helperObj.update(ctx.guild.id, "team_size", sizechange)
    await ctx.response.send_message("Set team size!")


@tree.command(
    name="set-team-channels",
    description="Set the team channels",
    guild=discord.Object(id=526081127643873280)
)
async def setTeamChannels(ctx, *, team1: str, team2: str):
    await helperObj.setTeamHelper(ctx, team1, team2)


@tree.command(
    name="move",
    description="Move players to their respective channels",
    guild=discord.Object(id=526081127643873280)
)
async def move(ctx):
    await helperObj.movefunc(ctx)


# TODO: update website to current shockwave website
@tree.command(
    name="help",
    description="Get a list of commands",
    guild=discord.Object(id=526081127643873280)
)
async def help(ctx):
    await ctx.response.send_message("Visit WEBSITE NOT READY for a full list of commands")


# TODO: rename fullRandom to makeTeams
@tree.command(
    name="make-teams",
    description="Create teams",
    guild=discord.Object(id=526081127643873280)
)
async def fullRandom(ctx, roles: bool = False, movevar: bool = True):
    if roles == 'True':
        await helperObj.both(ctx)
    else:
        await helperObj.randomizeTeamHelper(ctx)

    if movevar:
        await helperObj.movefunc(ctx)

    team1 = helperObj.get(ctx.guild.id, "team1")
    team2 = helperObj.get(ctx.guild.id, "team2")

    team1Obj = Team()
    team1Obj.deserializeTeam(team1)
    team2Obj = Team()
    team2Obj.deserializeTeam(team2)

    await helperObj.printEmbed(ctx, team1Obj, team2Obj)


@tree.command(
    name="return",
    description="Return all members (including spectators) to the original channel",
    guild=discord.Object(id=526081127643873280)
)
async def returnAll(ctx):
    og = helperObj.get(ctx.guild.id, "original_channel")
    original_channel = discord.utils.get(ctx.guild.channels, name=og)
    chan1 = helperObj.get(ctx.guild.id, "channel1")
    chan2 = helperObj.get(ctx.guild.id, "channel2")
    original_channel = discord.utils.get(ctx.guild.channels, name=og)
    channel1 = discord.utils.get(ctx.guild.channels, name=chan1)
    channel2 = discord.utils.get(ctx.guild.channels, name=chan2)

    if original_channel == "":
        await ctx.response.send_message(
            'You have not been seperated into team voice channels! Use "/move" first.'
        )
    else:
        aggregate = channel1.members
        aggregate.extend(channel2.members)

        for i in aggregate:
            await i.move_to(original_channel)

    await ctx.response.send_message(
            'Moved!'
        )


@tree.command(
    name="captains",
    description="Start captain draft",
    guild=discord.Object(id=526081127643873280)
)
async def captains(ctx, captain_1: discord.Member = None, captain_2: discord.Member = None, random: bool = False):
    if len(ctx.user.voice.channel.members) < 2:
        ctx.response.send_message("Not enough players in the voice channel!")
    else:
        if random:
            players = []
            for player in ctx.message.author.voice.channel.members:
                players.append(player.name)

            helperObj.update(ctx.guild.id, "players", players)

            # randomly choose captains
            captain1 = await helperObj.getRandomMember(ctx)
            while captain1 is None:
                captain1 = await helperObj.getRandomMember(ctx)

            # make sure captain1 and captain2 are different
            captain2 = await helperObj.getRandomMember(ctx)
            while captain2 is None and captain2 == captain1:
                captain2 = await helperObj.getRandomMember(ctx)
        else:
            captain1 = captain_1
            captain2 = captain_2

        # TODO: given our current code, is this check needed? yes since they can pass in no captains and random is false with more than 2 in vc
        if captain_1 is None or captain_2 is None:
            ctx.response.send_message("Mention two team captains!")

        await helperObj.captainsHelper(ctx, captain_1, captain_2)


@tree.command(
    name="choose",
    description="Choose a player for your team (captains only)",
    guild=discord.Object(id=526081127643873280)
)
async def choose(ctx, member: discord.Member = None, random: bool = False):
    if random:
        await helperObj.chooseRandomMember(ctx)
    else:
        await helperObj.chooseFunc(ctx, member)


@tree.command(
    name="clear",
    description="Clear data",
    guild=discord.Object(id=526081127643873280)
)
async def clearAll(ctx, clear_channels: bool = False, clear_tournament: bool = False, clear_elo: bool = False):
    await helperObj.clearTeamsHelper(ctx)

    if clear_channels:
        helperObj.update(ctx.guild.id, "channel1", "")
        helperObj.update(ctx.guild.id, "channel2", "")

    if clear_tournament:
        helperObj.update(ctx.guild.id, "tournament", "")
    
    if clear_elo:
        helperObj.update(ctx.guild.id, "elo", "")

    await ctx.response.send_message("Cleared!")


@tree.command(
    name="notify",
    description="Send a server member an invite to the channel",
    guild=discord.Object(id=526081127643873280)
)
async def notify(ctx, member: discord.Member):
    await helperObj.notifyHelper(ctx, member)
    team_size = helperObj.get(ctx.guild.id, "team_size")
    await ctx.response.send_message("Sent an invite for the " + str(team_size * 2) + " man!")

@tree.command(
    name="notify-role",
    description="Send a role an invite to the channel",
    guild=discord.Object(id=526081127643873280)
)
async def notify(ctx, role: discord.Role):
    members = role.members
    for member in members:
        await helperObj.notifyHelper(ctx, member)

    team_size = helperObj.get(ctx.guild.id, "team_size")
    await ctx.response.send_message("Sent an invite for the " + str(team_size * 2) + " man!")
    
@tree.command(
    name="roll",
    description="Roll a number between 1 and the number you provide",
    guild=discord.Object(id=526081127643873280)
)
async def roll(ctx, *, num: int):
    if int(num) > 1:
        rand = random.randint(1, int(num))
        await ctx.response.send_message("You rolled " + str(rand))
    else:
        await ctx.response.send_message("Please use a number greater than 1.")


@tree.command(
    name="randomize-roles",
    description="Randomize roles",
    guild=discord.Object(id=526081127643873280)
)
async def randomizeRoles(ctx):
    await helperObj.randomRoleHelper(ctx)
    await helperObj.printEmbed(ctx)

# TODO: move this somewhere else??
# or move all the setup code at the start to a main function here
client.run(token)
