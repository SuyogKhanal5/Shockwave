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


def ensure_column(table, column, coltype="", default=None):
    # Adds a column to an existing table if it isn't already there, so
    # installs that predate the economy/betting feature don't need a fresh
    # database — only used for tables that existed before this feature.
    cursor.execute(f"PRAGMA table_info({table})")
    existing_cols = [row[1] for row in cursor.fetchall()]
    if column not in existing_cols:
        ddl = f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
        if default is not None:
            ddl += f" DEFAULT {default}"
        cursor.execute(ddl)
        mainDB.commit()


if not db_already_existed:
    cursor.execute(
        "CREATE TABLE servers(guildId, serverName, original_channel, team1, team2, "
        "players, channel1, channel2, mode, turn, team_size, tournament, elo, "
        "result1, result2, captain1, captain2, "
        "betting_state, betting_message_id, betting_channel_id, is_ranked)"
    )
    # BUG FIX: the original CREATE TABLE call was never committed.
    mainDB.commit()
else:
    # BUG FIX: /randomize-roles (randomRoleHelper) writes to "result1" and
    # "result2", but these were never columns on `servers` — on any
    # pre-existing database, that command has always crashed with
    # "sqlite3.OperationalError: no such column: result1" the moment it ran.
    ensure_column("servers", "result1", "TEXT")
    ensure_column("servers", "result2", "TEXT")
    # BUG FIX: same story for "captain1"/"captain2" — captainsHelper and
    # chooseFunc/chooseHelper read and write these, but they were never
    # columns either, so the entire /captains draft flow has always
    # crashed with "no such column: captain1" on the very first call.
    ensure_column("servers", "captain1", "TEXT")
    ensure_column("servers", "captain2", "TEXT")
    ensure_column("servers", "betting_state", "TEXT", "'NONE'")
    ensure_column("servers", "betting_message_id", "INTEGER")
    ensure_column("servers", "betting_channel_id", "INTEGER")
    # Whether the current team1/team2 game was formed via /ranked or
    # /ranked-captains — gates whether recordResult touches anyone's elo.
    ensure_column("servers", "is_ranked", "INTEGER", "0")

# Per-member currency: gold balance plus win/loss and wagering stats, one
# row per (guild, user).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS economy("
    "guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, last_daily, "
    "PRIMARY KEY(guildId, userId))"
)
# BUG-PRONE PATTERN AVOIDED: "CREATE TABLE IF NOT EXISTS" above is a no-op
# on a database that already has an `economy` table from before these
# columns existed — ensure_column() is what actually adds them on those.
ensure_column("economy", "gold_lost", "INTEGER", "0")
ensure_column("economy", "game_wins", "INTEGER", "0")
ensure_column("economy", "game_losses", "INTEGER", "0")
ensure_column("economy", "elo", "INTEGER", str(helper.DEFAULT_ELO))
# Active bets for the game currently in progress in a guild. Cleared out
# (paid out or refunded) by the time the game resolves.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS wagers("
    "guildId, userId, username, team, amount, "
    "PRIMARY KEY(guildId, userId))"
)
# Snapshot of the most recently resolved game per guild (wagers, rosters,
# and the exact deltas applied) — lets /report-correct-winner undo a
# misreported result precisely instead of guessing at what to reverse.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS last_result(guildId PRIMARY KEY, data)"
)
mainDB.commit()

helperObj = helper.helpers(cursor, mainDB)

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
helperObj.client = client


@client.event
async def on_ready():
    # TODO: remove id when deploying. current has banter server ID (also do for all commands)
    await tree.sync(guild=discord.Object(526081127643873280))
    print('Command: Shockwave')


@client.event
async def on_guild_join(ctx):
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0)",
        (ctx.id, ctx.name)
    )
    mainDB.commit()


@client.event
async def on_guild_remove(ctx):
    cursor.execute("""DELETE FROM servers WHERE guildId=?""", (ctx.id,))
    mainDB.commit()


@client.event
async def on_raw_reaction_add(payload):
    # Ignore the bot's own 1️⃣/2️⃣ reactions on the winner-report message,
    # and DM reactions (no guild).
    if payload.member is None or payload.member.bot or payload.guild_id is None:
        return

    await helperObj.handleWinnerReaction(payload)

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
    name="start",
    description="Move players to their respective channels and open betting on the game",
    guild=discord.Object(id=526081127643873280)
)
async def start(ctx):
    # BUG FIX: movefunc() does one move_to() API call per member and used
    # to never respond to the interaction at all, which Discord shows to
    # the user as "The application did not respond" once it also risked
    # the same 3-second timeout as /return. Defer immediately, then
    # confirm via followup once the moves are done.
    await ctx.response.defer()
    await helperObj.movefunc(ctx)
    await ctx.followup.send("Moved!")
    await helperObj.startBettingHelper(ctx)


@tree.command(
    name="wager",
    description="Wager gold on the current game",
    guild=discord.Object(id=526081127643873280)
)
@app_commands.describe(amount="Amount of gold to wager", team="Which team you think will win")
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
async def wager(ctx, amount: int, team: app_commands.Choice[int]):
    await helperObj.wagerHelper(ctx, amount, team.value)


@tree.command(
    name="daily",
    description="Claim your daily 1000 gold",
    guild=discord.Object(id=526081127643873280)
)
async def daily(ctx):
    await helperObj.dailyHelper(ctx)


@tree.command(
    name="stats",
    description="View your (or another player's) game record, elo, and economy stats",
    guild=discord.Object(id=526081127643873280)
)
@app_commands.describe(member="Whose stats to look up — defaults to you")
async def stats(ctx, member: discord.Member = None):
    await helperObj.statsHelper(ctx, member)


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
async def fullRandom(ctx, use_roles: bool = False):
    # BUG FIX: `use_roles` (renamed from `roles`, which was shadowing the
    # module-level `roles` dict above) is already a bool from the slash
    # command's type annotation. The original code compared it to the
    # *string* 'True' (`if roles == 'True':`), which is always False no
    # matter what the user picks, so the roles branch was unreachable.
    #
    # This command only announces the teams — it used to optionally move
    # everyone immediately (a `movevar` flag), but moving players and
    # opening betting is now exclusively /start's job, so a roster can be
    # announced and reviewed before anyone actually gets pulled into a
    # voice channel.
    if use_roles:
        await helperObj.both(ctx)
    else:
        await helperObj.randomizeTeamHelper(ctx)

    team1 = helperObj.get(ctx.guild.id, "team1")
    team2 = helperObj.get(ctx.guild.id, "team2")

    team1Obj = Team()
    team1Obj.deserializeTeam(team1)
    team2Obj = Team()
    team2Obj.deserializeTeam(team2)

    await ctx.response.send_message("Teams created!")

    # Roles (Top/Jungle/Mid/Bottom/Support) only make sense for a 5-player
    # team — makeEmbedString() silently falls back to a plain roster for
    # any other size. Explain that instead of leaving people wondering
    # where the roles went.
    if use_roles:
        unroled = [
            f"{team.get_name()} ({len(team.get_players())} players)"
            for team in (team1Obj, team2Obj)
            if len(team.get_players()) != 5
        ]
        if unroled:
            await ctx.channel.send(
                "Roles need exactly 5 players on a team to assign, so no roles were "
                f"assigned for: {', '.join(unroled)}. Showing the roster as normal instead."
            )

    await helperObj.printEmbed(ctx, team1Obj, team2Obj, useRoles=use_roles)

    # BUG FIX: this used to be folded into the very first response message,
    # which gets posted *before* the team embeds and is easy to scroll past
    # once the (visually much bigger) rosters land right after it. Posting
    # it last — after the rosters, bolded — puts it where people are
    # actually looking once they're done reading the teams.
    await ctx.channel.send(
        '📣 **Ready?** Use "/start" to move everyone into their channels and open betting.'
    )


@tree.command(
    name="ranked",
    description="Form roughly elo-balanced teams from your voice channel for a ranked game",
    guild=discord.Object(id=526081127643873280)
)
async def ranked(ctx):
    # rankedTeamHelper handles its own response + team embeds (elo averages
    # need per-player lookups it already has to do anyway), unlike
    # /make-teams where bot.py builds the response itself.
    await helperObj.rankedTeamHelper(ctx)


@tree.command(
    name="return",
    description="Return all members (including spectators) to the original channel",
    guild=discord.Object(id=526081127643873280)
)
async def returnAll(ctx):
    # BUG FIX: `original_channel == ""` never caught the "not set" case,
    # since discord.utils.get() returns None (not "") when nothing matches.
    # BUG FIX: Discord requires the initial interaction response within 3
    # seconds, and this moves one member per API call — with enough people
    # that can blow past 3 seconds before a response is sent, expiring the
    # interaction token. Both fixes (and the refund-active-bets behavior)
    # now live in helperObj.returnHelper, which /return and the automatic
    # refund-before-payout path share.
    await helperObj.returnHelper(ctx)


@tree.command(
    name="report-correct-winner",
    description="Admin: fix a misreported winner for the last game and adjust stats/payouts",
    guild=discord.Object(id=526081127643873280)
)
@app_commands.describe(team="The team that actually won")
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def reportCorrectWinner(ctx, team: app_commands.Choice[int]):
    await helperObj.reportCorrectWinnerHelper(ctx, team.value)


@reportCorrectWinner.error
async def reportCorrectWinner_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to correct a game result."
        )
    else:
        raise error


@tree.command(
    name="captains",
    description="Start captain draft",
    guild=discord.Object(id=526081127643873280)
)
async def captains(ctx, captain_1: discord.Member = None, captain_2: discord.Member = None, use_random: bool = False):
    await startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked=False)


@tree.command(
    name="ranked-captains",
    description="Draft your own ranked teams — same as /captains, but elo is tracked",
    guild=discord.Object(id=526081127643873280)
)
async def rankedCaptains(ctx, captain_1: discord.Member = None, captain_2: discord.Member = None, use_random: bool = False):
    await startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked=True)


# Shared by /captains and /ranked-captains — identical draft flow, the only
# difference is whether the resulting game is marked ranked (captainsHelper
# sets is_ranked accordingly, which gates whether recordResult later
# touches anyone's elo).
async def startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked):
    # BUG FIX: renamed `random` param to `use_random` — it was shadowing the
    # `random` module imported at the top of this file (harmless here since
    # the module isn't used inside this function, but a landmine for future
    # edits).
    if ctx.user.voice is None or len(ctx.user.voice.channel.members) < 2:
        await ctx.response.send_message("Not enough players in the voice channel!")
        return

    if use_random:
        # BUG FIX: this used to build a plain Python list of names and pass
        # it straight to helperObj.update(), which binds it as a sqlite3
        # query parameter — sqlite3 can't bind a list at all (raises
        # InterfaceError), so this path crashed on every call before ever
        # reaching getRandomMember(). getRandomMember() also needs each
        # player's id (to look the Member back up), not just their name.
        # Serialize into a Team, the same convention every other "players"
        # column write in this file uses.
        players = Team()
        for player in ctx.user.voice.channel.members:
            players.add_player(Player(player.id, player.name))
        helperObj.update(ctx.guild.id, "players", players.serializeTeam())

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

    await helperObj.captainsHelper(ctx, captain1, captain2, ranked=ranked)


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
async def clearAll(
    ctx,
    clear_channels: bool = False,
    clear_tournament: bool = False,
    clear_elo: bool = False,
    clear_economy: bool = False,
):
    await helperObj.clearTeamsHelper(ctx)

    if clear_channels:
        helperObj.update(ctx.guild.id, "channel1", "")
        helperObj.update(ctx.guild.id, "channel2", "")

    if clear_tournament:
        helperObj.update(ctx.guild.id, "tournament", "")

    if clear_elo:
        helperObj.update(ctx.guild.id, "elo", "")

    await ctx.response.send_message("Cleared!")

    # clear_elo (reset every player's elo) and clear_economy (wipe every
    # player's whole economy row) both act on every player in the server,
    # so neither runs immediately — a confirm/cancel view goes out as a
    # followup and the actual reset waits for that click.
    if clear_economy or clear_elo:
        await helperObj.confirmDestructiveClearHelper(ctx, clear_economy)


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
#
# Guarded so tests.py can import this module (to exercise command callbacks
# and event handlers directly) without connecting to Discord as a side
# effect of the import.
if __name__ == "__main__":
    client.run(token)