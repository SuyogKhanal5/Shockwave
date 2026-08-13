# Import Statements
import random
import itertools
import traceback
import os.path as path
import sqlite3
import discord
from discord import app_commands
from discord.ext import tasks
from TourneyClasses import Team, Player
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
        "betting_state, betting_message_id, betting_channel_id, is_ranked, "
        "active_tournament_match_id, wager_channel, betting_timer_seconds, "
        "roster_team1_message_id, roster_team2_message_id, roster_channel_id, roster_use_roles)"
    )
    # BUG FIX: the original CREATE TABLE call was never committed.
    mainDB.commit()
else:
    # LEGACY: the now-removed /randomize-roles command (randomRoleHelper)
    # used to write to "result1"/"result2", but these were never columns on
    # `servers` — on any pre-existing database, that command crashed with
    # "sqlite3.OperationalError: no such column: result1" the moment it ran.
    # Left in place (nothing reads/writes them anymore) rather than
    # attempting a column drop, matching this file's additive-only
    # migration approach elsewhere.
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
    # Whether the current team1/team2 game was formed with ranked:true (on
    # /make-teams or /captains) — gates whether recordResult touches anyone's elo.
    ensure_column("servers", "is_ranked", "INTEGER", "0")
    # Set while a /tournament-start sequential match is using team1/team2 —
    # tells recordResult to also advance the tournament bracket once the
    # normal betting/elo resolution for that game finishes.
    ensure_column("servers", "active_tournament_match_id", "INTEGER")
    # /wager-set-channel: when set, all betting postings (open/closed/
    # winner-report) go here instead of wherever a game (or a tournament
    # match) happened to start.
    ensure_column("servers", "wager_channel", "TEXT")
    # /set-betting-timer: how long a betting window stays open (replaces
    # the previously-hardcoded BETTING_DURATION_SECONDS). For a
    # simultaneous-mode tournament round with several concurrent matches,
    # this is the PER-MATCH base — the round's actual window is this times
    # however many matches are open at once (see
    # _openConcurrentTournamentBetting).
    ensure_column("servers", "betting_timer_seconds", "INTEGER", str(helper.BETTING_DURATION_SECONDS))
    # The live "reroll roles / start the game" reaction control on a just-
    # posted, actually-final roster — see _finalizeRoster/handleRosterReaction
    # (replaces the old standalone /randomize-roles and /start commands).
    # roster_team2_message_id is what a reaction is actually checked
    # against; overwriting it on every new roster is what makes an older
    # roster's reactions inert once a newer one has been posted.
    ensure_column("servers", "roster_team1_message_id", "INTEGER")
    ensure_column("servers", "roster_team2_message_id", "INTEGER")
    ensure_column("servers", "roster_channel_id", "INTEGER")
    ensure_column("servers", "roster_use_roles", "INTEGER", "0")

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
# The RANKED subset of game_wins/game_losses (a casual game bumps
# game_wins/game_losses but not these) — /stats and /leaderboard use them
# to break a player's record into casual vs ranked instead of just one
# combined total.
ensure_column("economy", "ranked_wins", "INTEGER", "0")
ensure_column("economy", "ranked_losses", "INTEGER", "0")
# Consecutive game wins right now — an achievement (see the "on_fire" key
# in helper.py's CARD_ACHIEVEMENT_TITLES), not a pure additive delta like
# every other economy column, so applyGameDeltas updates it with its own
# extra UPDATE (increment on a win, reset to 0 on a loss) rather than
# folding it into computeGameDeltas' own delta dict.
ensure_column("economy", "current_win_streak", "INTEGER", "0")
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
# One row per active /wager-against challenge — unlike the team-game
# `wagers` table above, several of these can be open at once per guild
# (different pairs of players), so each is tracked by its own row/message
# rather than a single column on `servers`.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS duels("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "challengerId, challengerName, targetId, targetName, amount, state)"
)
# One row per posted /leaderboard message, tracking which page it's
# currently showing so the paging reactions know what to re-render.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS leaderboards("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, filter, sort_order, page)"
)
# One row per posted /my-teams message — same paging idea as leaderboards
# above, but scoped to a single caller (userId) rather than the whole
# guild's stats, since each page here is one of THEIR teams, not a page of
# many players.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS my_team_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, userId, page)"
)
# One row per posted /stats message — recognizes that a reaction landed on
# a real /stats embed (see handleStatsReaction).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS stats_views(messageId INTEGER PRIMARY KEY, guildId)"
)
# BUG FIX: targetUserId and cardShown were added to stats_views after it
# already shipped — "CREATE TABLE IF NOT EXISTS" above is a no-op on a
# database that already has the table from before these columns existed
# (same story as economy/tournament_matches below), so a live server's
# stats_views was silently missing both until ensure_column started
# actually adding them. targetUserId is who to re-fetch the real avatar
# for when toggling back off the placeholder; cardShown flips to 1 once
# the trading-card reaction fires — the avatar toggle refuses to touch the
# message after that (see handleStatsReaction), since a trading card isn't
# shaped like a normal /stats embed anymore and toggling its thumbnail
# would just make a mess of it.
ensure_column("stats_views", "targetUserId")
ensure_column("stats_views", "cardShown", "INTEGER", "0")
# Which avatar the trading card is currently rendered with — 0 (default)
# for this server's own profile picture, 1 for the regular account-wide
# one. Only meaningful once cardShown=1; reset to 0 every time the card is
# (re-)entered so it always starts on the server avatar, matching the
# plain /stats embed's own default (see handleStatsReaction).
ensure_column("stats_views", "cardAvatarGlobal", "INTEGER", "0")
# A player's trading-card look (see /stats' \U0001f3b4 reaction and
# _renderTradingCardImage) — one row per (guild, player), created with
# Shockwave's own defaults the first time it's needed. Colors are stored as
# "#RRGGBB" hex, font_style is a named preset _cardFontPaths knows how to
# resolve (only "default" — Shockwave's own Chakra Petch/IBM Plex Sans
# pairing — exists today, but the column exists so more presets can be
# added later without a schema change). `customized` (see ensureCardSettings)
# tracks whether a row still just reflects Shockwave's own defaults (0) or
# was explicitly changed by something other than that self-healing insert
# (1) — there's no /card-customize command yet, so today every row is
# always 0, and /stats keeps it in sync with CARD_DEFAULT_* on every call
# rather than freezing at whatever they were the day the row was created.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS trading_cards("
    "guildId, userId, title, accent_color, background_color, text_color, font_style, "
    "PRIMARY KEY(guildId, userId))"
)
ensure_column("trading_cards", "customized", "INTEGER", "0")
# Which CARD_SHOP_COLOR_SCHEMES/tier-name a row's colors were last equipped
# from via /card-set, or NULL for a hand-edited custom hex
# value with nothing to track (see _resyncEquippedColorScheme). Lets an
# already-equipped scheme keep following that scheme's current colors
# instead of freezing at whatever they were the moment it was picked.
ensure_column("trading_cards", "color_scheme_name")
# Permanent record of which trading-card cosmetics (a title, a color
# scheme — see CARD_TIER_REWARD_TITLES) each player has unlocked in each
# guild, by reaching Diamond/Master/Grandmaster/Challenger at least once
# (see _checkTierRewardUnlocks). Nothing ever deletes a row here, so a
# reward stays unlocked even after the player deranks back below the tier
# that earned it — itemKey is a tier name ("Diamond", ...), itemType is
# "title" or "color_scheme" (both unlock together per tier, see
# _unlockCardReward), so the same key appears twice per reward.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS card_unlocks("
    "guildId, userId, itemType, itemKey, PRIMARY KEY(guildId, userId, itemType, itemKey))"
)
# One row per posted /team-stats message — recognizes that a reaction
# landed on a real /team-stats embed (see handleTeamStatsReaction), same
# idea as stats_views above but scoped to a team (teamId) rather than a
# player.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_stats_views("
    "messageId INTEGER PRIMARY KEY, guildId, teamId, cardShown INTEGER DEFAULT 0)"
)
# One row per posted /team-list message — same paging idea as leaderboards
# above, plus the filter/sort options it was posted with, so a page flip
# (handleTeamListReaction) re-applies the exact same view instead of
# resetting to the unfiltered/default-sorted list.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_list_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, search, recruitingOnly, sort, sort_order, page)"
)
# Every persistent team in a server. Distinct from the ephemeral team1/
# team2 columns on `servers` (which hold whatever roster the last /make-
# teams or /captains produced) — these are named teams a player can be
# registered on ahead of a tournament. A player can be listed on more than
# one row here; Tournament.register_team is what stops the same player
# from being entered on two teams in one tournament.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS teams("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, name, data)"
)
# One tournament per server — creating a new one while one already exists
# requires confirmation (see ConfirmTournamentOverwriteView) since it
# replaces this row outright. Columns mirror TourneyClasses.Tournament's
# attributes directly; `teams` and `bracket` are JSON since they're
# variable-length nested data.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournaments("
    "guildId PRIMARY KEY, name, team_size, num_teams, double_elimination, teams, bracket)"
)
# Losers bracket for a double-elimination tournament — JSON, same reason
# `bracket` above is: variable-length nested node-graph data. NULL for any
# tournament created before this existed, or one that isn't double
# elimination at all.
ensure_column("tournaments", "losers_bracket", "TEXT")
# One row per pending /team-invite — several can be open at once (different
# teams/invitees), so each is tracked by its own row/message like `duels`.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_invites("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "teamId, teamName, inviterId, targetId, targetName)"
)
# One row per tournament match ever played — /tournament-start creates a
# batch of these per round (sequential: one at a time; simultaneous: all
# at once), each keyed by its own id so /report-correct-winner can target
# a specific match. nodeIndex is the index into the tournament's bracket
# list of one of the two paired nodes for this match (the other is that
# node's .opponent) — that's how a resolved match knows which bracket
# node to advance the winner into.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_matches("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, roundIndex, nodeIndex, "
    "team1, team2, state, mode, messageId, channelId, winner)"
)
# 'winners', 'losers', or 'finals' — which bracket this match belongs to.
# Needed because roundIndex/nodeIndex are only unique WITHIN one bracket:
# a double-elimination tournament's winners round 0 and losers round 0 are
# two entirely different matches that happen to share the same numbers.
ensure_column("tournament_matches", "bracketType", "TEXT", "'winners'")
# Set once this match's own betting window (see
# _openConcurrentTournamentBetting) has closed — separate from `state`,
# since a match can still be unresolved (waiting on a reaction) after
# betting on it has already closed.
ensure_column("tournament_matches", "bettingClosed", "INTEGER", "0")
# JSON snapshot of exactly which wagers _settleMatchWagers paid out for this
# match (userId/username/team/amount) — tournament_wagers rows themselves
# get deleted once settled, so without this a later /report-correct-winner
# match_id correction would have no way to know who to reverse/repay.
# NULL for a match nobody bet on, or one settled before this existed.
ensure_column("tournament_matches", "settledWagers", "TEXT")
# Wagers on a SPECIFIC tournament match — unlike `wagers` above (one bet
# per user per guild, tied to whatever single casual/ranked game or
# sequential-mode tournament match is currently active), simultaneous-mode
# tournament rounds can have several matches open at once, so bets here are
# scoped per matchId instead: one bet per user per MATCH, not per guild.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_wagers("
    "matchId, guildId, userId, username, team, amount, "
    "PRIMARY KEY(matchId, userId))"
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

# Pure personalization — the bot's Discord status cycles through these
# instead of sitting on one fixed line. Recalled from memory rather than
# pulled from a script, so treat the exact wording as close-but-not-
# necessarily-verbatim if that ever matters.
ORIANNA_QUOTES = [
    "Winding...",
    "Precision is everything.",
    "Tick, tock, tick, tock...",
    "Command: Shockwave.",
]
_orianna_quote_cycle = itertools.cycle(ORIANNA_QUOTES)


# Runs immediately on .start() and then every 30 minutes after — a fixed
# status never changes, and cycling it is purely cosmetic, so there's no
# reason to update any faster than that. The try/except is deliberate: a
# presence update can fail if the client's connection state isn't fully
# settled yet (e.g. right after a reconnect, or — in tests — a Client that
# was never actually connected at all), and this is purely cosmetic, so
# there's nothing worth doing beyond letting the next scheduled tick retry.
@tasks.loop(minutes=30)
async def rotateStatus():
    try:
        await client.change_presence(activity=discord.Game(name=next(_orianna_quote_cycle)))
    except Exception:
        pass


# Commands are registered on `tree` with no guild= at all (see every
# @tree.command below), which makes them "global" command *definitions* —
# copy_global_to() + a guild-scoped sync() is what actually publishes them
# to a specific server. Doing it per-guild rather than a single
# tree.sync() (truly global commands) is what keeps registration instant:
# a real global sync can take up to an hour to show up for users.
async def syncCommandsToGuild(guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


# BUG FIX: the four roster_* columns (added later via ensure_column, see
# above) meant the plain positional INSERT below started supplying fewer
# values than the table actually has — sqlite3.OperationalError on every
# single on_guild_join, silently swallowed by discord.py's own event-error
# logging, so the guild's servers row was simply never created. Every
# command that reads a column via helperObj.get() (a bare
# cursor.fetchone()[0]) then crashed with "'NoneType' object is not
# subscriptable" the moment anyone tried to use the bot in that guild.
# ensure_guild_row is now the one place that inserts a row — check first,
# insert only if missing, so it's safe to call from on_ready too (self-
# healing any guild whose row never got created, or was lost to a wiped/
# restored database) without ever creating a duplicate row for a guild
# that already has one (servers.guildId has no UNIQUE constraint to lean
# on INSERT OR IGNORE for).
def ensure_guild_row(guild_id, guild_name):
    cursor.execute("SELECT 1 FROM servers WHERE guildId=?", (guild_id,))
    if cursor.fetchone() is not None:
        return
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0, NULL, NULL, ?, "
        "NULL, NULL, NULL, 0)",
        (guild_id, guild_name, helper.BETTING_DURATION_SECONDS)
    )
    mainDB.commit()


@client.event
async def on_ready():
    for guild in client.guilds:
        await syncCommandsToGuild(guild)
        ensure_guild_row(guild.id, guild.name)
    if not rotateStatus.is_running():
        rotateStatus.start()
    print('Command: Shockwave')


@client.event
async def on_guild_join(ctx):
    await syncCommandsToGuild(ctx)
    ensure_guild_row(ctx.id, ctx.name)


@client.event
async def on_guild_remove(ctx):
    cursor.execute("""DELETE FROM servers WHERE guildId=?""", (ctx.id,))
    mainDB.commit()


# Every reaction-driven feature's handler — each one looks at `payload` and
# no-ops immediately if it's not for one of ITS OWN messages (a leaderboard
# page, a tournament ready-check, ...), so running all of them per reaction
# is cheap. They're called individually rather than in a loop over a plain
# list of callables so each one's name still shows up in a traceback.
REACTION_HANDLERS = (
    "handleGameReportReaction", "handleDuelReaction", "handleLeaderboardReaction",
    "handleMyTeamsReaction", "handleTeamListReaction", "handleTeamInviteReaction",
    "handleTournamentReaction", "handleStatsReaction", "handleTeamStatsReaction",
    "handleRosterReaction",
)


@client.event
async def on_raw_reaction_add(payload):
    # Ignore the bot's own TEAM_EMOJIS/CANCEL_GAME_EMOJI reactions on the
    # winner-report message, and DM reactions (no guild).
    if payload.member is None or payload.member.bot or payload.guild_id is None:
        return

    # BUG FIX: these used to run as one unguarded sequence of awaits — an
    # exception raised by any one of them (say, handleGameReportReaction on a
    # malformed payload) skipped every handler after it for that same
    # reaction, with nothing telling the user their click didn't do
    # anything. Each now gets its own try/except so a bug in one handler
    # can't silently swallow the rest.
    for handler_name in REACTION_HANDLERS:
        try:
            await getattr(helperObj, handler_name)(payload)
        except Exception:
            traceback.print_exc()


# Catch-all for every slash command's errors. discord.py calls this after
# ANY command's own local .error handler runs too (CommandTree._dispatch_
# error always calls both, not one or the other — see setBettingTimer_error/
# reportCorrectWinner_error/clearAll_error below), so this only needs to
# cover what those don't: everything without a local handler at all (most
# commands), and re-raised errors from the ones that do. Without this, an
# unhandled exception anywhere just leaves the user staring at "The
# application did not respond" while the actual traceback only ever reaches
# the console.
@tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You don't have permission to use this command."
    else:
        message = "Something went wrong running that command. Try again, and let an admin know if it keeps happening."
        traceback.print_exception(type(error), error, error.__traceback__)

    # A local .error handler that already responded to this interaction
    # (e.g. the MissingPermissions branch above, handled first by
    # clearAll_error/etc.) means posting a second, generic message here
    # would just stack on top of the specific one they already got.
    if interaction.response.is_done():
        return

    try:
        await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# Commands


@tree.command(
    name="set-channels",
    description="Admin: set the team channels and, optionally, the team size"
)
@app_commands.describe(
    team1="Name for the first team's voice channel",
    team2="Name for the second team's voice channel",
    size="Number of players per team (optional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setTeamChannels(ctx, *, team1: str, team2: str, size: int = None):
    await helperObj.setTeamHelper(ctx, team1, team2, size)


@setTeamChannels.error
async def setTeamChannels_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to set the team channels."
        )
    else:
        raise error


@tree.command(
    name="wager-set-channel",
    description="Direct all wager/betting postings to a specific text channel"
)
@app_commands.describe(channel_name="Name of the text channel to use — created if it doesn't exist")
async def setWagerChannel(ctx, channel_name: str):
    await helperObj.setWagerChannelHelper(ctx, channel_name)


@tree.command(
    name="set-betting-timer",
    description="Admin: set how long a betting window stays open (1-600 seconds)"
)
@app_commands.describe(
    seconds="Seconds a betting window stays open — for a tournament round with several concurrent "
            "matches, this is multiplied by the number of matches"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setBettingTimer(ctx, seconds: int):
    await helperObj.setBettingTimerHelper(ctx, seconds)


@setBettingTimer.error
async def setBettingTimer_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to change the betting timer."
        )
    else:
        raise error


@tree.command(
    name="set-elo",
    description="Admin: set a player's elo directly, to an exact value"
)
@app_commands.describe(member="Whose elo to set", elo="The exact elo value to set it to")
@app_commands.checks.has_permissions(manage_guild=True)
async def setElo(ctx, member: discord.Member, elo: int):
    await helperObj.setEloHelper(ctx, member, elo)


@setElo.error
async def setElo_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to set a player's elo."
        )
    else:
        raise error


@tree.command(
    name="wager",
    description="Wager gold on the current game — or, with a match id, on one tournament match"
)
@app_commands.describe(
    amount="Amount of gold to wager", team="Which team you think will win",
    match_id="A specific tournament match's id — omit to bet on the current casual/ranked game instead"
)
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
async def wager(ctx, amount: int, team: app_commands.Choice[int], match_id: int = None):
    await helperObj.wagerHelper(ctx, amount, team.value, match_id)


@tree.command(
    name="wager-against",
    description="Challenge another player to a heads-up gold wager"
)
@app_commands.describe(member="Who to challenge", amount="How much gold is on the line")
async def wagerAgainst(ctx, member: discord.Member, amount: int):
    await helperObj.challengeDuelHelper(ctx, member, amount)


@tree.command(
    name="daily",
    description="Claim your daily 1000 gold"
)
async def daily(ctx):
    await helperObj.dailyHelper(ctx)


@tree.command(
    name="stats",
    description="View your (or another player's) game record, elo, and economy stats"
)
@app_commands.describe(member="Whose stats to look up — defaults to you")
async def stats(ctx, member: discord.Member = None):
    await helperObj.statsHelper(ctx, member)


# The caller's own available titles only (CARD_DEFAULT_TITLE plus whatever
# they've unlocked, see getAvailableCardTitles) — unlike logoAutocomplete's
# static list, this one depends on who's typing.
async def cardTitleAutocomplete(ctx, current: str):
    current = current.lower()
    titles = helperObj.getAvailableCardTitles(ctx.guild.id, ctx.user.id)
    matches = [t for t in titles if current in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


# Same shape as cardTitleAutocomplete above — the caller's own available
# schemes only (CARD_DEFAULT_SCHEME_NAME plus whatever they've unlocked).
async def cardColorSchemeAutocomplete(ctx, current: str):
    current = current.lower()
    schemes = helperObj.getAvailableCardColorSchemes(ctx.guild.id, ctx.user.id)
    matches = [s["name"] for s in schemes if current in s["name"].lower()]
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


# Same shape as cardTitleAutocomplete/cardColorSchemeAutocomplete above —
# the caller's own available font styles only.
async def cardFontAutocomplete(ctx, current: str):
    current = current.lower()
    styles = helperObj.getAvailableCardFontStyles(ctx.guild.id, ctx.user.id)
    matches = [s for s in styles if current in s.lower()]
    return [app_commands.Choice(name=s, value=s) for s in matches[:25]]


@tree.command(
    name="card-set",
    description="Equip an unlocked trading-card title, color scheme, and/or font"
)
@app_commands.describe(
    title="Which title to equip — pick from your unlocked ones",
    color_scheme="Which color scheme to equip — pick from your unlocked ones",
    font_style="Which font to equip — pick from your unlocked ones",
)
@app_commands.autocomplete(
    title=cardTitleAutocomplete, color_scheme=cardColorSchemeAutocomplete, font_style=cardFontAutocomplete
)
async def cardSet(ctx, title: str = None, color_scheme: str = None, font_style: str = None):
    await helperObj.cardSetHelper(ctx, title, color_scheme, font_style)


@tree.command(
    name="shop",
    description="Browse trading-card cosmetics purchasable with gold"
)
async def shop(ctx):
    await helperObj.shopHelper(ctx)


@tree.command(
    name="achievements",
    description="Browse gameplay achievements and their trading-card title rewards"
)
async def achievements(ctx):
    await helperObj.achievementsHelper(ctx)


# Unlike the three cardXAutocomplete functions above (which only ever
# offer what's already unlocked), this one offers what's still buyable —
# getShopCatalog's own "owned" flag is what filters an already-purchased
# item out, so /shop-buy never suggests something there's nothing left to
# do with.
async def shopBuyAutocomplete(ctx, current: str):
    current = current.lower()
    catalog = helperObj.getShopCatalog(ctx.guild.id, ctx.user.id)
    matches = [i for i in catalog if not i["owned"] and current in i["name"].lower()]
    return [app_commands.Choice(name=f"{i['name']} ({i['price']} gold)", value=i["name"]) for i in matches[:25]]


@tree.command(
    name="shop-buy",
    description="Purchase a trading-card cosmetic with gold"
)
@app_commands.describe(item="Which item to purchase — pick from what you don't already own")
@app_commands.autocomplete(item=shopBuyAutocomplete)
async def shopBuy(ctx, item: str):
    await helperObj.shopBuyHelper(ctx, item)


@tree.command(
    name="leaderboard",
    description="Rank the server by a stat — react to page through it"
)
@app_commands.describe(
    filter="Which stat to rank by — omit for an overview of elo, balance, and record",
    order="Highest-first or lowest-first — defaults to highest-first"
)
@app_commands.choices(filter=[
    app_commands.Choice(name="Elo", value="elo"),
    app_commands.Choice(name="Balance", value="balance"),
    app_commands.Choice(name="Game Wins", value="game_wins"),
    app_commands.Choice(name="Game Losses", value="game_losses"),
    app_commands.Choice(name="Game Win Rate", value="game_win_rate"),
    app_commands.Choice(name="Ranked Wins", value="ranked_wins"),
    app_commands.Choice(name="Ranked Losses", value="ranked_losses"),
    app_commands.Choice(name="Ranked Win Rate", value="ranked_win_rate"),
    app_commands.Choice(name="Casual Wins", value="casual_wins"),
    app_commands.Choice(name="Casual Losses", value="casual_losses"),
    app_commands.Choice(name="Casual Win Rate", value="casual_win_rate"),
    app_commands.Choice(name="Bet Wins", value="bet_wins"),
    app_commands.Choice(name="Bet Losses", value="bet_losses"),
    app_commands.Choice(name="Bet Win Rate", value="bet_win_rate"),
    app_commands.Choice(name="Net Gold", value="net_gold"),
    app_commands.Choice(name="Gold Wagered", value="gold_wagered"),
])
@app_commands.choices(order=[
    app_commands.Choice(name="Descending (highest first)", value="desc"),
    app_commands.Choice(name="Ascending (lowest first)", value="asc"),
])
async def leaderboard(
    ctx, filter: app_commands.Choice[str] = None, order: app_commands.Choice[str] = None
):
    stat = filter.value if filter is not None else None
    sort_order = order.value if order is not None else "desc"
    await helperObj.leaderboardHelper(ctx, stat, sort_order)


SITE_COMMANDS_URL = "https://shockwave.netlify.app/commands.html"

# Short descriptions for /help <command>. Kept separate from each command's
# `description=` (which is what Discord's own command picker shows) since
# that has to stay short enough to fit there — this can afford a real
# sentence or two, closer to what commands.html says.
COMMAND_HELP = {
    "set-channels": "Names the two voice channels teams get moved into. Creates them if they don't already exist. size optionally sets how many players make up one side. Requires the Manage Server permission.",
    "clear": "Wipes the current teams/draft so you can start a fresh session. clear_tournament deletes this server's tournament entirely. clear_elo and clear_economy reset data for every player; clear_achievements and clear_card_unlocks do too unless a user is given, which narrows either to just them. Requires the Manage Server permission.",
    "make-teams": "Randomly splits everyone in your voice channel into two even teams and posts the roster, with a ▶️ reaction on it to move everyone and open betting when you're ready (🔄 to reroll roles too, if use_roles was set). ranked:true forms roughly elo-balanced teams instead, and tracks elo once a winner is reported.",
    "captains": "Starts a live captain draft. Name two captains, or use_random to pick two automatically; everyone else lands in a pool picked from with /choose. Once both teams are set, react ▶️ on the roster to move everyone and open betting. ranked:true tracks elo for the resulting game.",
    "choose": "Captains only. Picks one player from the draft pool onto your team, then passes the turn to the other captain.",
    "notify": "DMs a one-time invite link to your voice channel — to one member, or to everyone holding a given role.",
    "wager-set-channel": "Redirects every betting posting to one specific text channel, instead of wherever the game was started from.",
    "set-betting-timer": "Sets how long a betting window stays open (1-600 seconds). Multiplied by the number of matches for a concurrent tournament round. Requires the Manage Server permission.",
    "set-elo": "Sets a player's elo directly to an exact value, correcting a broken rating without fighting the match-result math to get there. Still credits any Diamond+ tier reward the new elo qualifies for. Requires the Manage Server permission.",
    "wager": "Bets gold on one team winning the current game — or, with a match id, on a specific tournament match. Only while betting is open, one bet per player per game/match.",
    "wager-against": "Challenges another player to a heads-up gold wager — separate from team-game betting, no active game required.",
    "daily": "Claims 1000 free gold. Once per calendar day, per player.",
    "stats": "Shows a player's elo, ranked/casual/game record, betting record, balance, and net gold — defaults to you. React with \U0001f5bc️ to toggle the avatar between this server's own profile picture and their regular account-wide one, or \U0001f3b4 to replace the whole embed with a customizable trading card; \U0001faaa swaps back.",
    "card-set": "Equips your unlocked trading-card title, color scheme, and/or font in one go (see /stats' \U0001f3b4 reaction) — set any combination of the three at once. Reaching Diamond, Master, Grandmaster, or Challenger permanently unlocks that tier's own title and scheme, even if you derank afterward; \"Default\" is always available for both. Fonts are purchased from /shop.",
    "shop": "Browse every trading-card title, color scheme, and font purchasable with gold, and what you already own.",
    "achievements": "Browse every gameplay achievement, what it takes to earn it, and whether you already have. Earning one unlocks its title for /card-set and posts a one-time announcement in the channel.",
    "shop-buy": "Purchases a trading-card cosmetic with gold, permanently unlocking it for /card-set. Refuses if you already own it or can't afford it.",
    "leaderboard": "Ranks the server by a stat, including ranked-only and casual-only wins/losses/win rate. Omit filter for an elo-sorted overview. Reactions page through the results.",
    "report-correct-winner": "Fixes a misreported winner — undoes and reapplies the payouts, records, and elo. Requires Manage Server.",
    "team-create": "Creates a persistent team with you as its captain.",
    "team-set": "Sets a persistent team's voice channel and/or logo, any combination in one call. new_voice_channel creates a fresh one named after the team. Captain-only.",
    "team-invite": "Invites one or more members (up to 5 per call) to a team you captain. Captain-only — each invitee must accept before joining.",
    "my-teams": "Lists the teams you're a rostered player on in this server, with paging to flip through each one's full stats card.",
    "team-stats": "Shows a persistent team's captain, roster, voice channel, and win/loss record. React with \U0001f6e1️ to swap it for a team card — its logo as the focal point, colors sampled from that logo, captain/roster/record/win rate. ↩️ swaps back.",
    "team-list": "Browse every team in the server with filtering (name search, recruiting-only) and sorting (name, wins, losses, win rate, roster size — sort:\"Win Rate\" order:\"Descending\" for the old /team-leaderboard ranking). React to page through it.",
    "team-use": "Loads two persistent teams straight into a casual or ranked game, skipping the random-split-or-draft step.",
    "tournament-create": "Creates an empty tournament shell for this server — name, team size, and bracket size. One tournament per server.",
    "tournament-register": "Registers one of your teams for the server's tournament. Captain-only.",
    "tournament-create-bracket": "Builds the tournament bracket from whichever teams are currently registered, seeded randomly. Rerunning it rerolls the bracket. For double elimination, losers_bracket_timing picks whether the losers bracket waits for the whole winners bracket to finish, or interleaves as each round unlocks.",
    "tournament-print-bracket": "Prints the current bracket.",
    "tournament-start": "Starts playing the current round of the bracket. mode is Sequential (one match at a time) or Simultaneous (all at once, no betting).",
    "roll": "Rolls a random number between 1 and num.",
    "help": "Shows this message, or details on one command.",
}


@tree.command(
    name="help",
    description="Get a list of commands, or details on a specific one"
)
@app_commands.describe(command="Command name to look up — omit for the full list on the site")
async def help(ctx, command: str = None):
    if command is None:
        await ctx.response.send_message(f"Full command list: {SITE_COMMANDS_URL}")
        return

    name = command.strip().lstrip("/").lower()
    description = COMMAND_HELP.get(name)
    if description is None:
        await ctx.response.send_message(
            f"Don't recognize `/{name}`. Full command list: {SITE_COMMANDS_URL}"
        )
        return

    registered = discord.utils.get(tree.get_commands(), name=name)
    usage = f"/{name}"
    if registered is not None:
        for param in registered.parameters:
            token = param.display_name if param.required else f"{param.display_name}?"
            usage += f" <{token}>"

    await ctx.response.send_message(f"**{usage}**\n{description}")


@tree.command(
    name="make-teams",
    description="Create teams — randomly, or roughly elo-balanced for a ranked game"
)
@app_commands.describe(
    use_roles="Assign Top/Jungle/Mid/Bottom/Support roles (5-player teams only) — ignored if ranked",
    ranked="Form roughly elo-balanced teams from your voice channel and track elo for this game",
)
async def makeTeams(ctx, use_roles: bool = False, ranked: bool = False):
    if ranked:
        # rankedTeamHelper handles its own response + team embeds (elo
        # averages need per-player lookups it already has to do anyway) —
        # a completely separate flow from the random split below, which
        # bot.py builds the response for itself. use_roles doesn't apply
        # here (elo-balanced teams get their own embed format).
        await helperObj.rankedTeamHelper(ctx)
        return

    # BUG FIX: `use_roles` (renamed from `roles`, which was shadowing the
    # module-level `roles` dict above) is already a bool from the slash
    # command's type annotation. The original code compared it to the
    # *string* 'True' (`if roles == 'True':`), which is always False no
    # matter what the user picks, so the roles branch was unreachable.
    #
    # This command only announces the teams — it used to optionally move
    # everyone immediately (a `movevar` flag), but moving players and
    # opening betting only happens once the posted roster's own ▶️
    # reaction is clicked (see _finalizeRoster), so a roster can be
    # announced and reviewed before anyone actually gets pulled into a
    # voice channel.
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
    # any other size, and _finalizeRoster silently skips the 🔄 reroll
    # reaction for the same reason. Explain that instead of leaving people
    # wondering where the roles went.
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

    team1_message, team2_message = await helperObj.printEmbed(ctx, team1Obj, team2Obj, useRoles=use_roles)
    await helperObj._finalizeRoster(ctx.guild.id, team1_message, team2_message, team1Obj, team2Obj, use_roles)

    # BUG FIX: this used to be folded into the very first response message,
    # which gets posted *before* the team embeds and is easy to scroll past
    # once the (visually much bigger) rosters land right after it. Posting
    # it last — after the rosters, bolded — puts it where people are
    # actually looking once they're done reading the teams.
    await ctx.channel.send(
        f"📣 **Ready?** React {helper.TEAM_START_EMOJI} on the roster above to move everyone into their "
        "channels and open betting."
    )


@tree.command(
    name="report-correct-winner",
    description="Admin: fix a misreported winner for the last game and adjust stats/payouts"
)
@app_commands.describe(
    team="The team that actually won",
    match_id="Optional: correct a specific tournament match instead of the last game"
)
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def reportCorrectWinner(ctx, team: app_commands.Choice[int], match_id: int = None):
    await helperObj.reportCorrectWinnerHelper(ctx, team.value, match_id)


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
    description="Start captain draft"
)
@app_commands.describe(
    captain_1="First captain — required unless use_random is set",
    captain_2="Second captain — required unless use_random is set",
    use_random="Pick two captains at random from the voice channel instead",
    ranked="Track elo for this game — defaults to casual",
)
async def captains(
    ctx, captain_1: discord.Member = None, captain_2: discord.Member = None,
    use_random: bool = False, ranked: bool = False,
):
    await startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked=ranked)


# Shared by /captains regardless of its ranked flag — identical draft flow,
# the only difference is whether the resulting game is marked ranked
# (captainsHelper sets is_ranked accordingly, which gates whether recordResult later
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
    description="Choose a player for your team (captains only)"
)
@app_commands.describe(
    member="The player to pick — required unless use_random is set",
    use_random="Pick a random remaining player instead of naming one",
)
async def choose(ctx, member: discord.Member = None, use_random: bool = False):
    if use_random:
        await helperObj.chooseRandomMember(ctx)
    else:
        await helperObj.chooseFunc(ctx, member)


@tree.command(
    name="clear",
    description="Admin: clear data"
)
@app_commands.describe(
    clear_channels="Also forget the saved team channel names",
    clear_tournament="Delete this server's tournament entirely — bracket, registrations, match history. Can't be undone",
    clear_elo="Reset every player's elo back to 1000 for this server (confirmation required)",
    clear_economy="Wipe every player's balance/elo/record/gold entirely for this server (confirmation required)",
    clear_achievements="Reset earned achievements for this server, or just one player if `user` is set (confirmation required)",
    clear_card_unlocks="Wipe trading-card unlocks (titles/schemes/fonts) for this server, or just one player if `user` is set (confirmation required)",
    user="With clear_achievements/clear_card_unlocks: only reset this player instead of everyone",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearAll(
    ctx,
    clear_channels: bool = False,
    clear_tournament: bool = False,
    clear_elo: bool = False,
    clear_economy: bool = False,
    clear_achievements: bool = False,
    clear_card_unlocks: bool = False,
    user: discord.Member = None,
):
    if user is not None and not (clear_achievements or clear_card_unlocks):
        await ctx.response.send_message(
            "`user` only applies to `clear_achievements`/`clear_card_unlocks` — set one of those too."
        )
        return

    await helperObj.clearTeamsHelper(ctx)

    if clear_channels:
        helperObj.update(ctx.guild.id, "channel1", "")
        helperObj.update(ctx.guild.id, "channel2", "")

    if clear_tournament:
        helperObj.deleteTournamentHelper(ctx.guild.id)

    await ctx.response.send_message("Cleared!")

    # clear_elo, clear_economy, clear_achievements, and clear_card_unlocks
    # all act on every player in the server (clear_achievements/
    # clear_card_unlocks only, if narrowed to a single `user`), so none of
    # them run immediately — a confirm/cancel view goes out as a followup
    # and the actual reset waits for that click.
    if clear_economy or clear_elo or clear_achievements or clear_card_unlocks:
        await helperObj.confirmDestructiveClearHelper(
            ctx, clear_economy, clear_elo, clear_achievements, clear_card_unlocks, user
        )


@clearAll.error
async def clearAll_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to use /clear."
        )
    else:
        raise error


@tree.command(
    name="tournament-create",
    description="Create a tournament for this server"
)
@app_commands.describe(
    name="Tournament name",
    teamsize="Number of players per team",
    numteams="Number of teams the bracket holds",
    double_elim="Double elimination instead of single — defaults to single"
)
async def createTournament(ctx, name: str, teamsize: int, numteams: int, double_elim: bool = False):
    await helperObj.createTournamentHelper(ctx, name, teamsize, numteams, double_elim)


@tree.command(
    name="tournament-register",
    description="Register a team for this server's tournament"
)
@app_commands.describe(team="Name of the team to register")
async def registerTeam(ctx, team: str):
    await helperObj.registerTeamHelper(ctx, team)


@tree.command(
    name="tournament-create-bracket",
    description="Create (or reroll) the tournament bracket from registered teams"
)
@app_commands.describe(
    elimination_type="Single or double elimination for this tournament",
    losers_bracket_timing="Double elimination only: when the losers bracket plays — defaults to "
                           "after the winners bracket finishes"
)
@app_commands.choices(elimination_type=[
    app_commands.Choice(name="Single elimination", value="single"),
    app_commands.Choice(name="Double elimination", value="double"),
])
@app_commands.choices(losers_bracket_timing=[
    app_commands.Choice(name="After the winners bracket finishes entirely", value="after_winners"),
    app_commands.Choice(name="Interleaved — as soon as each round unlocks it", value="interleaved"),
])
async def createBracket(
    ctx, elimination_type: app_commands.Choice[str], losers_bracket_timing: app_commands.Choice[str] = None
):
    timing_value = losers_bracket_timing.value if losers_bracket_timing is not None else "after_winners"
    await helperObj.createBracketHelper(ctx, elimination_type.value == "double", timing_value)


@tree.command(
    name="tournament-print-bracket",
    description="Print the tournament bracket"
)
async def printBracket(ctx):
    await helperObj.printBracketHelper(ctx)


@tree.command(
    name="tournament-start",
    description="Start playing the tournament, one round at a time"
)
@app_commands.describe(
    mode="Sequential: one match at a time, ready-checked. Simultaneous: every match in the round at once."
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Sequential", value="sequential"),
    app_commands.Choice(name="Simultaneous", value="simultaneous"),
])
async def startTournament(ctx, mode: app_commands.Choice[str]):
    await helperObj.startTournamentHelper(ctx, mode.value)


@tree.command(
    name="team-create",
    description="Create a persistent team you're the captain of"
)
@app_commands.describe(name="Team name", team_size="How many players the team is looking for")
async def createTeam(ctx, name: str, team_size: int):
    await helperObj.createTeamHelper(ctx, name, team_size)


@tree.command(
    name="team-invite",
    description="Invite one or more members to a team you captain"
)
@app_commands.describe(
    team="Name of the team",
    member_1="Who to invite",
    member_2="Another member to invite (optional)",
    member_3="Another member to invite (optional)",
    member_4="Another member to invite (optional)",
    member_5="Another member to invite (optional)",
)
async def teamInvite(
    ctx, team: str, member_1: discord.Member,
    member_2: discord.Member = None, member_3: discord.Member = None,
    member_4: discord.Member = None, member_5: discord.Member = None,
):
    members = [m for m in (member_1, member_2, member_3, member_4, member_5) if m is not None]
    await helperObj.teamInviteHelper(ctx, team, members)


# Discord caps a slash command option at 25 static choices, and the built-in
# logo set is bigger than that (see assets/clash-logos) — autocomplete is
# the only way to offer the full list, filtered live as the user types.
async def logoAutocomplete(ctx, current: str):
    current = current.lower()
    names = [n for n in helperObj.listAvailableLogos() if current in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@tree.command(
    name="team-set",
    description="Set a team's voice channel and/or logo (captain only)"
)
@app_commands.describe(
    team="Name of the team",
    voice_channel="Existing voice channel to use for the team",
    new_voice_channel="Create a brand new voice channel named after the team",
    logo="Which built-in logo to use",
)
@app_commands.autocomplete(logo=logoAutocomplete)
async def setTeam(
    ctx, team: str, voice_channel: discord.VoiceChannel = None,
    new_voice_channel: bool = False, logo: str = None,
):
    await helperObj.teamSetHelper(ctx, team, voice_channel, new_voice_channel, logo)


@tree.command(
    name="team-stats",
    description="View a team's roster and record"
)
@app_commands.describe(team="Name of the team")
async def teamStats(ctx, team: str):
    await helperObj.teamStatsHelper(ctx, team)


@tree.command(
    name="my-teams",
    description="List the teams you're on and flip through their stats"
)
async def myTeams(ctx):
    await helperObj.myTeamsHelper(ctx)


@tree.command(
    name="team-list",
    description="Browse every team in this server, with filtering and sorting — react to page through it"
)
@app_commands.describe(
    search="Only show teams whose name contains this",
    recruiting_only="Only show teams still short of their target roster size",
    sort="What to sort by — defaults to name",
    order="Ascending or descending — defaults to ascending",
)
@app_commands.choices(sort=[
    app_commands.Choice(name="Name", value="name"),
    app_commands.Choice(name="Wins", value="wins"),
    app_commands.Choice(name="Losses", value="losses"),
    app_commands.Choice(name="Win Rate", value="win_rate"),
    app_commands.Choice(name="Roster Size", value="roster_size"),
])
@app_commands.choices(order=[
    app_commands.Choice(name="Ascending", value="asc"),
    app_commands.Choice(name="Descending", value="desc"),
])
async def teamList(
    ctx, search: str = None, recruiting_only: bool = False,
    sort: app_commands.Choice[str] = None, order: app_commands.Choice[str] = None,
):
    sort_value = sort.value if sort is not None else "name"
    order_value = order.value if order is not None else "asc"
    await helperObj.teamListHelper(ctx, search, recruiting_only, sort_value, order_value)


@tree.command(
    name="team-use",
    description="Load two persistent teams into a casual or ranked game"
)
@app_commands.describe(
    team1="Name of the first persistent team",
    team2="Name of the second persistent team",
    ranked="Track elo for this game — defaults to casual"
)
async def useTeams(ctx, team1: str, team2: str, ranked: bool = False):
    await helperObj.useTeamsHelper(ctx, team1, team2, ranked)


@tree.command(
    name="notify",
    description="Send a member — or everyone in a role — an invite to the channel"
)
@app_commands.describe(
    member="A specific member to invite",
    role="Invite every member of this role instead — give one or the other, not both",
)
async def notify(ctx, member: discord.Member = None, role: discord.Role = None):
    if member is None and role is None:
        await ctx.response.send_message("Mention a member or a role to invite.")
        return
    if member is not None and role is not None:
        await ctx.response.send_message("Give a member or a role, not both.")
        return

    # notifyHelper DMs the target directly rather than responding to the
    # interaction, so calling it once per role member in a loop is safe —
    # ctx.response.send_message below still only ever fires once either way.
    targets = role.members if role is not None else [member]
    for target in targets:
        await helperObj.notifyHelper(ctx, target)

    # BUG FIX: this used to always reference `member.name`, which crashed
    # with AttributeError whenever /notify was called with `role` instead
    # (member is None in that case).
    if member is not None:
        summary = member.name
    else:
        count = len(targets)
        summary = f"{count} member{'s' if count != 1 else ''} in {role.name}"
    await ctx.response.send_message(f"Sent an invite to {summary}!")


@tree.command(
    name="roll",
    description="Roll a number between 1 and the number you provide"
)
@app_commands.describe(num="Top of the range — must be greater than 1")
async def roll(ctx, *, num: int):
    if num > 1:
        rand = random.randint(1, num)
        await ctx.response.send_message("You rolled " + str(rand))
    else:
        await ctx.response.send_message("Please use a number greater than 1.")


# Guarded so tests.py can import this module (to exercise command callbacks
# and event handlers directly) without connecting to Discord as a side
# effect of the import.
if __name__ == "__main__":
    client.run(token)