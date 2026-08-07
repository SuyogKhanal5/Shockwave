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
    token = f.readline().strip()

# Connect to Database
dataFolder = "data/guildData/serverInfo/"
dbpath = dataFolder + "main.db"

# BUG FIX: sqlite3.connect() creates the database FILE on disk as a side
# effect of connecting, even if it's empty. The original code checked
# `path.isfile(dbpath)` *after* calling connect(), so the file always
# already existed by the time the check ran — meaning CREATE TABLE never
# executed, even on a brand new install. Check existence first.
db_already_existed = path.isfile(dbpath)

mainDB = sqlite3.connect(dbpath)
cursor = mainDB.cursor()

helperObj = helper.helpers(cursor, mainDB)

if not db_already_existed:
    cursor.execute(
        "CREATE TABLE servers(guildId, serverName, original_channel, team1, team2, "
        "players, channel1, channel2, mode, turn, team_size, tournament, elo)"
    )
    # BUG FIX: the original CREATE TABLE call was never committed.
    mainDB.commit()

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
intents.voice_states = True
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
    # BUG FIX: movefunc() does one move_to() API call per member and used
    # to never respond to the interaction at all, which Discord shows to
    # the user as "The application did not respond" once it also risked
    # the same 3-second timeout as /return. Defer immediately, then
    # confirm via followup once the moves are done.
    await ctx.response.defer()
    await helperObj.movefunc(ctx)
    await ctx.followup.send("Moved!")


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
async def fullRandom(ctx, use_roles: bool = False, movevar: bool = True):
    # BUG FIX: `use_roles` (renamed from `roles`, which was shadowing the
    # module-level `roles` dict above) is already a bool from the slash
    # command's type annotation. The original code compared it to the
    # *string* 'True' (`if roles == 'True':`), which is always False no
    # matter what the user picks, so the roles branch was unreachable.
    #
    # BUG FIX: this command can end up calling movefunc(), which loops
    # move_to() calls and can outrun the 3-second interaction response
    # window (same root cause as /return and /move above). Defer up front
    # so it's safe regardless of movevar.
    await ctx.response.defer()

    if use_roles:
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

    await ctx.followup.send("Teams created!")
    await helperObj.printEmbed(ctx, team1Obj, team2Obj)


@tree.command(
    name="return",
    description="Return all members (including spectators) to the original channel",
    guild=discord.Object(id=526081127643873280)
)
async def returnAll(ctx):
    og = helperObj.get(ctx.guild.id, "original_channel")
    chan1 = helperObj.get(ctx.guild.id, "channel1")
    chan2 = helperObj.get(ctx.guild.id, "channel2")

    original_channel = discord.utils.get(ctx.guild.channels, name=og)
    channel1 = discord.utils.get(ctx.guild.channels, name=chan1)
    channel2 = discord.utils.get(ctx.guild.channels, name=chan2)

    # BUG FIX: `original_channel == ""` never catches the "not set" case,
    # since discord.utils.get() returns None (not "") when nothing matches.
    if original_channel is None:
        await ctx.response.send_message(
            'You have not been seperated into team voice channels! Use "/move" first.'
        )
        return

    aggregate = []
    if channel1 is not None:
        aggregate.extend(channel1.members)
    if channel2 is not None:
        aggregate.extend(channel2.members)

    # BUG FIX: Discord requires the initial interaction response within 3
    # seconds. This loop makes one API call per member to move them, and
    # with enough people (or normal API latency) that can blow past 3
    # seconds before send_message() ever runs — the interaction token
    # expires and send_message() 404s with "Unknown interaction", even
    # though every move_to() already succeeded. Deferring immediately
    # acknowledges the interaction right away and extends the window to
    # ~15 minutes, then we report the result via followup instead of
    # response.
    await ctx.response.defer()

    for i in aggregate:
        await i.move_to(original_channel)

    await ctx.followup.send('Moved!')


@tree.command(
    name="captains",
    description="Start captain draft",
    guild=discord.Object(id=526081127643873280)
)
async def captains(ctx, captain_1: discord.Member = None, captain_2: discord.Member = None, use_random: bool = False):
    # BUG FIX: renamed `random` param to `use_random` — it was shadowing the
    # `random` module imported at the top of this file (harmless here since
    # the module isn't used inside this function, but a landmine for future
    # edits).
    if ctx.user.voice is None or len(ctx.user.voice.channel.members) < 2:
        await ctx.response.send_message("Not enough players in the voice channel!")
        return

    if use_random:
        players = [player.name for player in ctx.user.voice.channel.members]
        helperObj.update(ctx.guild.id, "players", players)

        # BUG FIX: `while captain1 is None:` on its own is fine, but the
        # captain2 loop `while captain2 is None and captain2 == captain1:`
        # can never be True (a value can't be both None and equal to a
        # non-None captain1), so it never actually re-rolled on a
        # collision. Loop on "still None" OR "same as captain1" instead.
        captain1 = await helperObj.getRandomMember(ctx)
        while captain1 is None:
            captain1 = await helperObj.getRandomMember(ctx)

        captain2 = await helperObj.getRandomMember(ctx)
        while captain2 is None or captain2 == captain1:
            captain2 = await helperObj.getRandomMember(ctx)
    else:
        captain1 = captain_1
        captain2 = captain_2

    if captain1 is None or captain2 is None:
        await ctx.response.send_message("Mention two team captains!")
        return

    await helperObj.captainsHelper(ctx, captain1, captain2)


@tree.command(
    name="choose",
    description="Choose a player for your team (captains only)",
    guild=discord.Object(id=526081127643873280)
)
async def choose(ctx, member: discord.Member = None, use_random: bool = False):
    if use_random:
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


# BUG FIX: this was previously also named `notify`, silently reusing the
# same Python name as the command above. It didn't break registration
# (the decorator runs at definition time either way) but it's a landmine —
# renamed for clarity.
@tree.command(
    name="notify-role",
    description="Send a role an invite to the channel",
    guild=discord.Object(id=526081127643873280)
)
async def notifyRole(ctx, role: discord.Role):
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
    if num > 1:
        rand = random.randint(1, num)
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
    result1 = helperObj.get(ctx.guild.id, "result1")
    result2 = helperObj.get(ctx.guild.id, "result2")
    await ctx.response.send_message(f"**Team 1**\n{result1}\n**Team 2**\n{result2}")

# TODO: move this somewhere else??
# or move all the setup code at the start to a main function here
client.run(token)