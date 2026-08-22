# Import Statements
import random
import itertools
import os
import os.path as path
import sqlite3
import logging
import time
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import tasks
from TourneyClasses import Team, Player
import helper

# Anchors every path below to this file's own directory rather than the
# process's current working directory: matches helper.py's own asset
# paths (see TEAM_LOGO_DIR/FONTS_DIR/etc., all built off
# os.path.dirname(__file__)). A relative path resolves against whatever
# directory the process happened to be launched from, which is easy to get
# right by always `cd`-ing into the project folder first on a dev machine,
# and easy to get wrong under a service manager (e.g. a systemd unit with
# no WorkingDirectory=) that doesn't set that up the same way.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Caps shockwave.log at a fixed number of lines rather than
# RotatingFileHandler's size-based, multi-file rotation: a single,
# chronologically-ordered file (oldest lines dropped once it grows past
# LOG_FILE_MAX_LINES) instead of scattered across shockwave.log/.1/.2/.3.
LOG_FILE_MAX_LINES = 10000


class MaxLinesFileHandler(logging.FileHandler):
    def __init__(self, filename, max_lines, encoding=None):
        self.max_lines = max_lines
        super().__init__(filename, mode="a", encoding=encoding)
        # Seeded once from whatever's already on disk (a previous run's
        # leftover log), so the very first emit this run already knows
        # whether a trim is overdue instead of assuming an empty file.
        self._line_count = self._countLines()

    def _countLines(self):
        try:
            with open(self.baseFilename, encoding=self.encoding) as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def emit(self, record):
        super().emit(record)
        self._line_count += 1
        if self._line_count > self.max_lines:
            self._trim()

    # Drops the oldest lines so the file holds exactly max_lines. No
    # locking of its own; Handler.handle() already wraps every emit()
    # call (and so this, called from inside one) in self.acquire()/
    # release(), so this is already safe against a concurrent emit from
    # another thread.
    def _trim(self):
        self.stream.close()
        with open(self.baseFilename, encoding=self.encoding) as f:
            lines = f.readlines()
        kept = lines[-self.max_lines:]
        with open(self.baseFilename, "w", encoding=self.encoding) as f:
            f.writelines(kept)
        self._line_count = len(kept)
        self.stream = self._open()


# Logs to both the console and the line-capped file above; the file is
# what survives a restart or a run with no attached terminal (e.g. as a
# background service), which stdout alone doesn't. Configured on the root
# logger so discord.py's own internal logging (gateway, HTTP) is captured
# the same way, not just this file's own logger calls.
LOG_FILE = os.path.join(BASE_DIR, "shockwave.log")
_log_formatter = logging.Formatter(
    "[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{"
)
_file_handler = MaxLinesFileHandler(LOG_FILE, LOG_FILE_MAX_LINES, encoding="utf-8")
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
logger = logging.getLogger("shockwave")

# Get token from text file
token = ""

# encoding="utf-8" pinned explicitly rather than left to the platform
# default text encoding, which differs (e.g. Windows' ANSI codepage vs.
# Linux's near-universal UTF-8); a plain ASCII token round-trips fine
# either way, but there's no reason to leave that to chance.
with open(os.path.join(BASE_DIR, "token.txt"), encoding="utf-8") as f:
    token = f.readline().strip()

# Connect to Database
dataFolder = os.path.join(BASE_DIR, "data", "guildData", "serverInfo")
dbpath = os.path.join(dataFolder, "main.db")

# sqlite3.connect() creates the database FILE on disk as a side effect of
# connecting, even if it's empty; this check has to run before connect()
# or the file will always already exist by the time it's checked, and
# CREATE TABLE below will never execute, even on a brand new install.
# makedirs is needed first too; a fresh clone has no data/ tree at all
# (it's gitignored), and sqlite3.connect() doesn't create missing parent
# directories, only the file itself.
os.makedirs(dataFolder, exist_ok=True)
db_already_existed = path.isfile(dbpath)

mainDB = sqlite3.connect(dbpath)
cursor = mainDB.cursor()

# A single row change (a long serialized team roster in particular) can run
# to thousands of characters once its bound parameters are expanded inline
# below, and a command's own params can include a full discord.py object
# repr, capped so one oversized line can't dominate the log file. Shared
# by _logDatabaseStatement below and LoggingCommandTree further down.
LOG_LINE_MAX_LENGTH = 500


def _truncateForLog(text):
    if len(text) > LOG_LINE_MAX_LENGTH:
        return text[:LOG_LINE_MAX_LENGTH] + "... (truncated)"
    return text


_MUTATING_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE")


# discord.py's own internal logging (View.on_error for a button callback,
# Loop._error for a background task) and on_app_command_error below already
# funnel unhandled exceptions into this same root-configured logger; this
# covers the other half: every database write. sqlite3's trace callback
# receives each executed statement with its bound parameters already
# expanded inline (not the raw `?` placeholders), so this reads as a real
# audit trail rather than opaque parameterized SQL. Everything that isn't
# an INSERT/UPDATE/DELETE (SELECTs, the trace callback's own "BEGIN " for
# an implicit transaction) is filtered out; only mutations are "important
# actions" worth a permanent record.
def _logDatabaseStatement(sql):
    statement = sql.strip()
    if not statement.upper().startswith(_MUTATING_SQL_PREFIXES):
        return
    logger.info("DB: %s", _truncateForLog(statement))


mainDB.set_trace_callback(_logDatabaseStatement)

# Where daily database snapshots land (see backupDatabaseTask below) -
# separate from serverInfo/ so a backup can never collide with or get
# mistaken for the live database file.
BACKUP_DIR = os.path.join(BASE_DIR, "data", "guildData", "backups")
BACKUP_RETENTION_DAYS = 7


def ensure_column(table, column, coltype="", default=None):
    # Adds a column to an existing table if it isn't already there, so
    # databases that predate the economy/betting feature don't need a one
    # only used for tables that existed before this feature.
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
        "roster_team1_message_id, roster_team2_message_id, roster_channel_id, roster_use_roles, "
        "default_elo, betting_opened_at, disliked_role_user_ids)"
    )
    mainDB.commit()
else:
    # result1/result2 are unused (nothing reads or writes them), kept only
    # for schema compatibility with databases that predate this migration.
    ensure_column("servers", "result1", "TEXT")
    ensure_column("servers", "result2", "TEXT")
    # captain1/captain2 are read and written by captainsHelper and
    # chooseFunc/chooseHelper (the /captains draft flow) but aren't part of
    # the original CREATE TABLE above, so a pre-existing database needs
    # them added via migration rather than already having them.
    ensure_column("servers", "captain1", "TEXT")
    ensure_column("servers", "captain2", "TEXT")
    ensure_column("servers", "betting_state", "TEXT", "'NONE'")
    ensure_column("servers", "betting_message_id", "INTEGER")
    ensure_column("servers", "betting_channel_id", "INTEGER")
    # When the current betting window was opened (unix seconds), lets
    # reconcileStaleBettingWindows (called from on_ready) work out how much
    # of the window was actually left if the bot restarts mid-window.
    ensure_column("servers", "betting_opened_at", "INTEGER")
    # Whether the current team1/team2 game was formed with ranked:true (on
    # /make-teams or /captains), gates whether recordResult touches anyone's elo.
    ensure_column("servers", "is_ranked", "INTEGER", "0")
    # Set while a /tournament-start sequential match is using team1/team2,
    # tells recordResult to also advance the tournament bracket once the
    # normal betting/elo resolution for that game finishes.
    ensure_column("servers", "active_tournament_match_id", "INTEGER")
    # /set's wager_channel param: when set, all betting postings (open/
    # closed/winner-report) go here instead of wherever a game (or a
    # tournament match) happened to start.
    ensure_column("servers", "wager_channel", "TEXT")
    # /set's betting_timer param: how long a betting window stays open (replaces
    # the previously-hardcoded BETTING_DURATION_SECONDS). For a
    # simultaneous-mode tournament round with several concurrent matches,
    # this is the PER-MATCH base; the round's actual window is this times
    # however many matches are open at once (see
    # _openConcurrentTournamentBetting).
    ensure_column("servers", "betting_timer_seconds", "INTEGER", str(helper.BETTING_DURATION_SECONDS))
    # The live "reroll roles / start the game" button controls on a just-
    # posted, actually-final roster, see _finalizeRoster/RosterActionView
    # (replaces the old standalone /randomize-roles and /start commands).
    # roster_team2_message_id is what a click is actually checked against;
    # overwriting it on every new roster is what makes an older roster's
    # buttons inert once a newer one has been posted.
    ensure_column("servers", "roster_team1_message_id", "INTEGER")
    ensure_column("servers", "roster_team2_message_id", "INTEGER")
    ensure_column("servers", "roster_channel_id", "INTEGER")
    ensure_column("servers", "roster_use_roles", "INTEGER", "0")
    # /set's default_elo param: what a brand new player's elo starts at in
    # this guild (see helpers._defaultEloForGuild), NULL until an admin
    # sets it, meaning "use the global helper.DEFAULT_ELO (1000)".
    ensure_column("servers", "default_elo", "INTEGER")
    # Comma-separated user ids of whoever the current team1/team2 roster
    # assigned a disliked role to (rankedTeamHelper, ranked:true
    # use_roles:true only), read back by recordResult/
    # reportCorrectWinnerHelper so a win on a disliked role earns the
    # ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER bonus. Lives and clears
    # alongside team1/team2 (see clearTeamsHelper), not per-result, so a
    # reused roster (/reuse) still gets credit for the same assignments.
    ensure_column("servers", "disliked_role_user_ids", "TEXT")

# Per-member currency: gold balance plus win/loss and wagering stats, one
# row per (guild, user).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS economy("
    "guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, last_daily, "
    "PRIMARY KEY(guildId, userId))"
)
# BUG-PRONE PATTERN AVOIDED: "CREATE TABLE IF NOT EXISTS" above is a no-op
# on a database that already has an `economy` table from before these
# columns existed; ensure_column() is what actually adds them on those.
ensure_column("economy", "gold_lost", "INTEGER", "0")
ensure_column("economy", "game_wins", "INTEGER", "0")
ensure_column("economy", "game_losses", "INTEGER", "0")
ensure_column("economy", "elo", "INTEGER", str(helper.DEFAULT_ELO))
# The RANKED subset of game_wins/game_losses (a casual game bumps
# game_wins/game_losses but not these); /stats and /leaderboard use them
# to break a player's record into casual vs ranked instead of just one
# combined total.
ensure_column("economy", "ranked_wins", "INTEGER", "0")
ensure_column("economy", "ranked_losses", "INTEGER", "0")
# Consecutive game wins right now, an achievement (see the "on_fire" key
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
# and the exact deltas applied), lets /report-correct-winner undo a
# misreported result precisely instead of guessing at what to reverse.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS last_result(guildId PRIMARY KEY, data)"
)
# One row per active /wager-against challenge; unlike the team-game
# `wagers` table above, several of these can be open at once per guild
# (different pairs of players), so each is tracked by its own row/message
# rather than a single column on `servers`.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS duels("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "challengerId, challengerName, targetId, targetName, amount, state)"
)
# One row per posted /leaderboard message, tracking which page it's
# currently showing so the paging buttons know what to re-render. cards/
# cardShown are /team-list's own cards:true toggle carried over here,
# see the identically-named columns on team_list_views for what each one
# means; LeaderboardPagingView reads both back the same way
# TeamListPagingView does.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS leaderboards("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, filter, sort_order, page, "
    "cards INTEGER DEFAULT 0, cardShown INTEGER DEFAULT 0)"
)
ensure_column("leaderboards", "cards", "INTEGER", "0")
ensure_column("leaderboards", "cardShown", "INTEGER", "0")
# One row per posted /my-teams message, same paging idea as leaderboards
# above, but scoped to a single caller (userId) rather than the whole
# guild's stats, since each page here is one of THEIR teams, not a page of
# many players.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS my_team_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, userId, page)"
)
# One row per posted /stats message, recognizes that a click landed on
# a real /stats embed (see StatsView).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS stats_views(messageId INTEGER PRIMARY KEY, guildId)"
)
# "CREATE TABLE IF NOT EXISTS" above is a no-op on a database that already
# has the table, so targetUserId/cardShown (added after stats_views
# shipped, same story as economy/tournament_matches below) need
# ensure_column to actually reach an existing server's table. targetUserId
# is who to re-fetch the real avatar for when toggling back off the
# placeholder; cardShown flips to 1 once the Card button is pressed; the
# avatar toggle refuses to touch the message after that (see StatsView),
# since a trading card isn't shaped like a normal /stats embed anymore and
# toggling its thumbnail would just make a mess of it.
ensure_column("stats_views", "targetUserId")
ensure_column("stats_views", "cardShown", "INTEGER", "0")
# Which avatar the trading card is currently rendered with: 0 (default)
# for this server's own profile picture, 1 for the regular account-wide
# one. Only meaningful once cardShown=1; reset to 0 every time the card is
# (re-)entered so it always starts on the server avatar, matching the
# plain /stats embed's own default (see StatsView).
ensure_column("stats_views", "cardAvatarGlobal", "INTEGER", "0")
# A player's trading-card look (see /stats' Card button and
# _renderTradingCardImage), one row per (guild, player), created with
# Shockwave's own defaults the first time it's needed. Colors are stored as
# "#RRGGBB" hex, font_style is a named preset _cardFontPaths knows how to
# resolve: "Default" (Shockwave's own Chakra Petch/IBM Plex Sans pairing,
# always available) or any of CARD_SHOP_FONT_STYLES' unlockable ones.
# `customized` (see ensureCardSettings)
# tracks whether a row still just reflects Shockwave's own defaults (0) or
# was explicitly changed by something other than that self-healing insert
# (1); there's no /card-customize command yet, so today every row is
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
# scheme, see CARD_TIER_REWARD_TITLES) each player has unlocked in each
# guild, by reaching Diamond/Master/Grandmaster/Challenger at least once
# (see _checkTierRewardUnlocks). Nothing ever deletes a row here, so a
# reward stays unlocked even after the player deranks back below the tier
# that earned it; itemKey is a tier name ("Diamond", ...), itemType is
# "title" or "color_scheme" (both unlock together per tier, see
# _unlockCardReward), so the same key appears twice per reward.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS card_unlocks("
    "guildId, userId, itemType, itemKey, PRIMARY KEY(guildId, userId, itemType, itemKey))"
)
# One row per posted /team-stats message, recognizes that a click
# landed on a real /team-stats embed (see TeamStatsView), same idea as
# stats_views above but scoped to a team (teamId) rather than a player.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_stats_views("
    "messageId INTEGER PRIMARY KEY, guildId, teamId, cardShown INTEGER DEFAULT 0)"
)
# One row per posted /team-list message, same paging idea as leaderboards
# above, plus the filter/sort options it was posted with, so a page flip
# (_handleTeamListPageClick) re-applies the exact same view instead of
# resetting to the unfiltered/default-sorted list.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_list_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, search, recruitingOnly, sort, sort_order, page, "
    "cards INTEGER DEFAULT 0, cardShown INTEGER DEFAULT 0, memberIds, memberNames)"
)
# 1 when a posted /team-list message is in "cards" mode (one team's full
# stats card per page, same shape as /my-teams, sourced from the same
# filtered/sorted team list a plain /team-list would show) rather than the
# default summary-list mode; _handleTeamListPageClick reads this back to
# know which of the two ways to re-render on a page flip. cardShown further
# narrows cards mode: 0 for that team's plain stats card, 1 for its actual
# trading card (see TeamListPagingView's own Card/Back toggle), carried
# across a page flip so paging while looking at trading cards keeps
# showing trading cards, not stats. memberIds (comma-separated user ids,
# same "" for none / CSV otherwise shape servers.disliked_role_user_ids
# uses) narrows the list to teams rostering every one of them;
# memberNames is the same set's display names, captured once at post time
# purely for the footer text so a page flip never needs to re-resolve
# Discord members from bare ids. All reached by an existing database whose
# team_list_views predates these columns; the CREATE TABLE above already
# includes them for a fresh one.
ensure_column("team_list_views", "cards", "INTEGER", "0")
ensure_column("team_list_views", "cardShown", "INTEGER", "0")
ensure_column("team_list_views", "memberIds", "TEXT")
ensure_column("team_list_views", "memberNames", "TEXT")
# Every persistent team in a server. Distinct from the ephemeral team1/
# team2 columns on `servers` (which hold whatever roster the last /make-
# teams or /captains produced); these are named teams a player can be
# registered on ahead of a tournament. A player can be listed on more than
# one row here; Tournament.register_team is what stops the same player
# from being entered on two teams in one tournament.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS teams("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, name, data)"
)
# One tournament per server; creating a new one while one already exists
# requires confirmation (see ConfirmTournamentOverwriteView) since it
# replaces this row outright. Columns mirror TourneyClasses.Tournament's
# attributes directly; `teams` and `bracket` are JSON since they're
# variable-length nested data.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournaments("
    "guildId PRIMARY KEY, name, team_size, num_teams, double_elimination, teams, bracket)"
)
# Losers bracket for a double-elimination tournament, JSON, same reason
# `bracket` above is: variable-length nested node-graph data. NULL for any
# tournament created before this existed, or one that isn't double
# elimination at all.
ensure_column("tournaments", "losers_bracket", "TEXT")
# One row per pending /team-invite; several can be open at once (different
# teams/invitees), so each is tracked by its own row/message like `duels`.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_invites("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "teamId, teamName, inviterId, targetId, targetName)"
)
# One row per tournament match ever played; /tournament-start creates a
# batch of these per round (sequential: one at a time; simultaneous: all
# at once), each keyed by its own id so /report-correct-winner can target
# a specific match. nodeIndex is the index into the tournament's bracket
# list of one of the two paired nodes for this match (the other is that
# node's .opponent); that's how a resolved match knows which bracket
# node to advance the winner into.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_matches("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, roundIndex, nodeIndex, "
    "team1, team2, state, mode, messageId, channelId, winner)"
)
# 'winners', 'losers', or 'finals': which bracket this match belongs to.
# Needed because roundIndex/nodeIndex are only unique WITHIN one bracket:
# a double-elimination tournament's winners round 0 and losers round 0 are
# two entirely different matches that happen to share the same numbers.
ensure_column("tournament_matches", "bracketType", "TEXT", "'winners'")
# Set once this match's own betting window (see
# _openConcurrentTournamentBetting) has closed, separate from `state`,
# since a match can still be unresolved (waiting on a report) after
# betting on it has already closed.
ensure_column("tournament_matches", "bettingClosed", "INTEGER", "0")
# JSON snapshot of exactly which wagers _settleMatchWagers paid out for this
# match (userId/username/team/amount); tournament_wagers rows themselves
# get deleted once settled, so without this a later /report-correct-winner
# match_id correction would have no way to know who to reverse/repay.
# NULL for a match nobody bet on, or one settled before this existed.
ensure_column("tournament_matches", "settledWagers", "TEXT")
# Wagers on a SPECIFIC tournament match; unlike `wagers` above (one bet
# per user per guild, tied to whatever single casual/ranked game or
# sequential-mode tournament match is currently active), simultaneous-mode
# tournament rounds can have several matches open at once, so bets here are
# scoped per matchId instead: one bet per user per MATCH, not per guild.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_wagers("
    "matchId, guildId, userId, username, team, amount, "
    "PRIMARY KEY(matchId, userId))"
)
# /setup: each role a player has explicitly said they like or dislike
# playing; `role` is one of SETUP_ROLE_NAMES, `preference` is 'like' or
# 'dislike'. The PRIMARY KEY includes `role` (not `preference`), so a role
# can only ever have ONE stored preference per player at a time. A role
# picked in both the liked and disliked steps of the same /setup run never
# reaches this table at all (see helper.py's _confirmSetupRoleStep; it's
# left out of both sides instead, i.e. neutral), so nothing here has to
# resolve that contradiction itself. Meant to feed a future role-aware elo
# balance; for now it's read only to gate /make-teams' use_roles on
# everyone in the voice channel having run /setup at least once.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS player_role_preferences("
    "guildId, userId, role, preference, PRIMARY KEY(guildId, userId, role))"
)
# /setup's own in-progress role-picker: one row per posted message while
# the caller is still toggling/confirming, deleted once they finish (or
# the view times out). `step` is 'liked' or 'disliked' (which round is
# currently live); `selectedRoles` is a comma-separated snapshot of
# whichever roles are CURRENTLY toggled on for that round, kept in sync by
# _handleSetupRoleToggleClick as the caller presses each role's button;
# `likedRoles` only gets filled in once the liked round is confirmed,
# carrying that finished set forward so the disliked round's own confirm
# can check both against each other.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS setup_role_sessions("
    "messageId INTEGER PRIMARY KEY, guildId, userId, step, selectedRoles, likedRoles)"
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

# A common "who/where" suffix for both the call and completion log lines
# below; DM interactions have no guild, so that's spelled out rather than
# crashing on interaction.guild.name.
def _interactionLogContext(interaction):
    guild = interaction.guild
    guild_desc = f"{guild.name} ({guild.id})" if guild is not None else "DM"
    return f"user={interaction.user} ({interaction.user.id}) guild={guild_desc}"


# Best-effort snapshot of the interaction an error was raised from, appended
# to on_app_command_error's log line so a failure is diagnosable from the
# log alone (which command, with what parameters, for whom) instead of
# needing a live repro. interaction.command/.namespace run the same real
# discord.py option-resolution machinery LoggingCommandTree.interaction_check
# above has to guard against, wrapped the same way here so a dump failure
# never swallows the actual error it was trying to add context to.
def _errorVariableDump(interaction):
    try:
        command = interaction.command
        name = command.qualified_name if command is not None else interaction.data.get("name", "?")
        params = dict(interaction.namespace) if command is not None else {}
    except Exception:
        name, params = "?", "<unresolvable>"
    return f"command=/{name} params={params} {_interactionLogContext(interaction)}"


# Logs every real command invocation (name, params, who, where) in one
# place rather than instrumenting each of the ~40 @tree.command functions
# individually; interaction_check is a global hook discord.py's own
# CommandTree._call runs before dispatching ANY application command in the
# tree. interaction.command/.namespace are independently-resolved cached
# properties (see discord.py's Interaction class), so both are already
# available here even though the tree hasn't actually invoked the command
# yet. Only actual command invocations are logged, not every keystroke
# into an autocomplete field; interaction_check also fires for those
# (same InteractionType family), but InteractionType.application_command
# excludes them. The default implementation this overrides just returns
# True unconditionally, so returning True here (never blocking anything)
# preserves that; per-command checks (e.g. /clear's has_permissions) still
# run separately afterward and are unaffected by this.
#
# BUG FOUND IN PRODUCTION: interaction.command/.namespace run discord.py's
# own real option-resolution machinery (Namespace.__init__ in particular
# indexes each option's fields directly, not via .get()), which nothing in
# tests.py can faithfully exercise; every test here goes through a plain
# FakeInteraction with no real payload to resolve. Worse, CommandTree.
# _from_interaction's own wrapper only catches AppCommandError around the
# whole dispatch, so any OTHER exception raised in here (a logging-only
# path with no business reason to ever fail) escaped uncaught, silently
# killing the interaction before the command it was meant to observe ever
# ran; Discord shows "This interaction failed" and nothing reaches
# on_app_command_error or this file's own log at all. A try/except around
# the entire body, unconditionally returning True either way, is what
# makes this hook genuinely unable to take down the feature it's just
# supposed to be watching.
class LoggingCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        try:
            if interaction.type is discord.InteractionType.application_command:
                command = interaction.command
                name = command.qualified_name if command is not None else interaction.data.get("name", "?")
                params = dict(interaction.namespace) if command is not None else {}
                logger.info(
                    "Command called: /%s %s | %s",
                    name, _truncateForLog(str(params)), _interactionLogContext(interaction)
                )
        except Exception:
            logger.exception("LoggingCommandTree.interaction_check failed, continuing without logging this call")
        return True


# create client object and slash commands
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
client = discord.Client(intents=intents)
tree = LoggingCommandTree(client)
helperObj.client = client

# Pure personalization; the bot's Discord status cycles through these
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


# Runs immediately on .start() and then every 30 minutes after; a fixed
# status never changes, and cycling it is purely cosmetic, so there's no
# reason to update any faster than that. The try/except is deliberate: a
# presence update can fail if the client's connection state isn't fully
# settled yet (e.g. right after a reconnect, or, in tests, a Client that
# was never actually connected at all), and this is purely cosmetic, so
# there's nothing worth doing beyond letting the next scheduled tick retry.
@tasks.loop(minutes=30)
async def rotateStatus():
    try:
        await client.change_presence(activity=discord.Game(name=next(_orianna_quote_cycle)))
    except Exception:
        logger.debug("Presence update skipped, connection not settled yet.", exc_info=True)


# Snapshots main.db into BACKUP_DIR and prunes anything older than
# BACKUP_RETENTION_DAYS. Uses sqlite3's own backup() API rather than a
# plain file copy; main.db is a live connection other code can be
# reading/writing between event loop ticks, and copying the raw file
# risks capturing it mid-write; backup() takes a proper point-in-time
# snapshot instead. Runs on the event loop rather than a thread: mainDB
# was opened with the default check_same_thread=True, so handing it to a
# different thread (e.g. via asyncio.to_thread) would raise outright, and
# a 100KB-scale database backs up in well under the time a trading-card
# render already blocks the loop for.
def _backupDatabase():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"main-{timestamp}.db")
    backup_conn = sqlite3.connect(backup_path)
    try:
        mainDB.backup(backup_conn)
    finally:
        backup_conn.close()

    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for name in os.listdir(BACKUP_DIR):
        entry_path = os.path.join(BACKUP_DIR, name)
        if os.path.isfile(entry_path) and os.path.getmtime(entry_path) < cutoff:
            os.remove(entry_path)


# Runs immediately on .start() (an on_ready right after boot always gets a
# fresh snapshot) and then every 24 hours after.
@tasks.loop(hours=24)
async def backupDatabaseTask():
    try:
        _backupDatabase()
        logger.info("Database backup completed.")
    except Exception:
        logger.exception("Database backup failed.")


# Commands are registered on `tree` with no guild= at all (see every
# @tree.command below), which makes them "global" command *definitions*;
# copy_global_to() + a guild-scoped sync() is what actually publishes them
# to a specific server. Doing it per-guild rather than a single
# tree.sync() (truly global commands) is what keeps registration instant:
# a real global sync can take up to an hour to show up for users.
async def syncCommandsToGuild(guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


# The one place that inserts a `servers` row, check first, insert only if
# missing, so it's safe to call from on_ready too (self-healing any guild
# whose row never got created, or was lost to a wiped/restored database)
# without ever creating a duplicate row for a guild that already has one
# (servers.guildId has no UNIQUE constraint to lean on INSERT OR IGNORE
# for). The positional INSERT below has to supply a value for every column
# on `servers`, including the roster_* ones added later via ensure_column
# above; falling out of sync with the table's actual column count throws
# sqlite3.OperationalError.
def ensure_guild_row(guild_id, guild_name):
    cursor.execute("SELECT 1 FROM servers WHERE guildId=?", (guild_id,))
    if cursor.fetchone() is not None:
        return
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0, NULL, NULL, ?, "
        "NULL, NULL, NULL, 0, NULL, NULL, NULL)",
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
    if not backupDatabaseTask.is_running():
        backupDatabaseTask.start()
    registerPersistentViews()
    await helperObj.reconcileStaleBettingWindows(client)
    logger.info("Shockwave is ready, logged in as %s.", client.user)


# Persistent views (every button has a fixed custom_id, timeout=None) need
# registering exactly once per process so Discord keeps routing their
# clicks to this bot even across a restart/redeploy; on_ready can fire
# more than once (e.g. on reconnect), so this is guarded the same way
# rotateStatus.is_running() above guards the status-rotation task from
# being started twice.
_persistent_views_registered = False


def registerPersistentViews():
    global _persistent_views_registered
    if _persistent_views_registered:
        return
    client.add_view(helper.WinnerReportView(helperObj))
    client.add_view(helper.DuelAcceptView(helperObj))
    client.add_view(helper.DuelResultView(helperObj))
    client.add_view(helper.TournamentReadyView(helperObj))
    client.add_view(helper.TournamentMatchReportView(helperObj))
    client.add_view(helper.RosterActionView(helperObj))
    client.add_view(helper.TeamInviteAcceptView(helperObj))
    client.add_view(helper.StatsView(helperObj))
    client.add_view(helper.TeamStatsView(helperObj))
    client.add_view(helper.LeaderboardPagingView(helperObj))
    client.add_view(helper.MyTeamsPagingView(helperObj))
    client.add_view(helper.TeamListPagingView(helperObj))
    _persistent_views_registered = True


@client.event
async def on_guild_join(ctx):
    await syncCommandsToGuild(ctx)
    ensure_guild_row(ctx.id, ctx.name)


@client.event
async def on_guild_remove(ctx):
    cursor.execute("""DELETE FROM servers WHERE guildId=?""", (ctx.id,))
    mainDB.commit()



# Companion to LoggingCommandTree.interaction_check's "Command called" line;
# discord.py dispatches this event itself (see CommandTree._call) only
# once a command has actually run to completion without raising, so this
# only ever logs a genuine success, never a command that errored out (that
# path logs separately via on_app_command_error below) or one interaction_
# check rejected before it ran at all.
@client.event
async def on_app_command_completion(interaction, command):
    # discord.py's own Client._run_event already keeps an exception here
    # from propagating anywhere harmful (it's routed to on_error instead,
    # and this fires only after the command it's about already fully
    # succeeded and responded), caught explicitly anyway so a bug in this
    # logging-only path still reaches this file's own log instead of only
    # discord.py's default stderr-only on_error, matching interaction_check's
    # own reasoning above.
    try:
        logger.info("Command completed: /%s | %s", command.qualified_name, _interactionLogContext(interaction))
    except Exception:
        logger.exception("on_app_command_completion logging failed")


# Catch-all for every slash command's errors. discord.py calls this after
# ANY command's own local .error handler runs too (CommandTree._dispatch_
# error always calls both, not one or the other, see setBettingTimer_error/
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
        logger.error(
            "Unhandled application command error | %s",
            _truncateForLog(_errorVariableDump(interaction)),
            exc_info=(type(error), error, error.__traceback__),
        )

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
    name="set",
    description="Admin: change server settings - channels, betting timer, wager channel, elo, default elo"
)
@app_commands.describe(
    team1="Name for the first team's voice channel - give together with team2",
    team2="Name for the second team's voice channel - give together with team1",
    size="Number of players per team",
    betting_timer="Seconds a betting window stays open (1-600) - multiplied per-match for a "
                  "concurrent tournament round",
    wager_channel="Name of the text channel to direct all wager/betting postings to - created if it doesn't exist",
    member="Whose elo to set - give together with elo",
    elo="The exact elo value to set member to - give together with member",
    default_elo="Elo a brand new player starts at here (default 1000) - doesn't change existing players",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setAdmin(
    ctx, team1: str = None, team2: str = None, size: int = None, betting_timer: int = None,
    wager_channel: str = None, member: discord.Member = None, elo: int = None,
    default_elo: int = None,
):
    await helperObj.adminSetHelper(
        ctx, team1, team2, size, betting_timer, wager_channel, member, elo, default_elo
    )


@setAdmin.error
async def setAdmin_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to change server settings."
        )
    else:
        raise error


@tree.command(
    name="wager",
    description="Wager gold on the current game - or, with a match id, on one tournament match"
)
@app_commands.describe(
    amount="Amount of gold to wager", team="Which team you think will win",
    match_id="A specific tournament match's id - omit to bet on the current casual/ranked game instead"
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
@app_commands.describe(member="Whose stats to look up - defaults to you")
async def stats(ctx, member: discord.Member = None):
    await helperObj.statsHelper(ctx, member)


# The caller's own available titles only (CARD_DEFAULT_TITLE plus whatever
# they've unlocked, see getAvailableCardTitles); unlike logoAutocomplete's
# static list, this one depends on who's typing.
async def cardTitleAutocomplete(ctx, current: str):
    current = current.lower()
    titles = helperObj.getAvailableCardTitles(ctx.guild.id, ctx.user.id)
    matches = [t for t in titles if current in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


# Same shape as cardTitleAutocomplete above, the caller's own available
# schemes only (CARD_DEFAULT_SCHEME_NAME plus whatever they've unlocked).
async def cardColorSchemeAutocomplete(ctx, current: str):
    current = current.lower()
    schemes = helperObj.getAvailableCardColorSchemes(ctx.guild.id, ctx.user.id)
    matches = [s["name"] for s in schemes if current in s["name"].lower()]
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


# Same shape as cardTitleAutocomplete/cardColorSchemeAutocomplete above -
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
    title="Which title to equip - pick from your unlocked ones",
    color_scheme="Which color scheme to equip - pick from your unlocked ones",
    font_style="Which font to equip - pick from your unlocked ones",
)
@app_commands.autocomplete(
    title=cardTitleAutocomplete, color_scheme=cardColorSchemeAutocomplete, font_style=cardFontAutocomplete
)
async def cardSet(ctx, title: str = None, color_scheme: str = None, font_style: str = None):
    await helperObj.cardSetHelper(ctx, title, color_scheme, font_style)


@tree.command(
    name="preview",
    description="See every option for a customization type at once, in one image"
)
@app_commands.describe(type="What to preview")
@app_commands.choices(type=[
    app_commands.Choice(name="Logos", value="Logos"),
    app_commands.Choice(name="Card Titles", value="Card Titles"),
    app_commands.Choice(name="Color Schemes", value="Color Schemes"),
    app_commands.Choice(name="Fonts", value="Fonts"),
])
async def preview(ctx, type: app_commands.Choice[str]):
    await helperObj.previewHelper(ctx, type.value)


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
# offer what's already unlocked), this one offers what's still buyable -
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
@app_commands.describe(item="Which item to purchase - pick from what you don't already own")
@app_commands.autocomplete(item=shopBuyAutocomplete)
async def shopBuy(ctx, item: str):
    await helperObj.shopBuyHelper(ctx, item)


@tree.command(
    name="leaderboard",
    description="Rank the server by a stat - buttons to page through it"
)
@app_commands.describe(
    filter="Which stat to rank by - omit for an overview of elo, balance, and record",
    order="Highest-first or lowest-first - defaults to highest-first",
    cards="Flip through each player's full stats card one at a time, like /my-teams, instead of a ranked list",
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
    ctx, filter: app_commands.Choice[str] = None, order: app_commands.Choice[str] = None, cards: bool = False
):
    stat = filter.value if filter is not None else None
    sort_order = order.value if order is not None else "desc"
    await helperObj.leaderboardHelper(ctx, stat, sort_order, cards)


SITE_COMMANDS_URL = "https://addshockwave.com/commands.html"

# Short descriptions for /help <command>. Kept separate from each command's
# `description=` (which is what Discord's own command picker shows) since
# that has to stay short enough to fit there; this can afford a real
# sentence or two, closer to what commands.html says.
COMMAND_HELP = {
    "set": "Admin one-stop for server settings: team1+team2 names the two voice channels teams get moved into (creates them if missing), size sets how many players make up one side, betting_timer sets how long a betting window stays open (1-600 seconds, multiplied by the number of matches for a concurrent tournament round), wager_channel redirects every betting posting to one specific text channel, member+elo sets a player's elo directly to an exact value (still credits any Diamond+ tier reward the new elo qualifies for), and default_elo sets what a brand new player in this server starts at (1000 by default; doesn't touch anyone's existing elo - use /clear's clear_elo to reset current players to it). Give any combination in one call - team1/team2 and member/elo must each be given as a pair. Requires the Manage Server permission.",
    "clear": "Wipes the current teams/draft so you can start a fresh session. clear_tournament deletes this server's tournament entirely. clear_elo and clear_economy reset data for every player; clear_achievements and clear_card_unlocks do too unless a user is given, which narrows either to just them. Requires the Manage Server permission.",
    "make-teams": "Randomly splits everyone in your voice channel into two even teams and posts the roster, with a Start button on it to move everyone and open betting when you're ready (Start (no move) to open betting without moving anyone; Reroll to reroll roles too, if use_roles was set). ranked:true forms roughly elo-balanced teams instead, and tracks elo once a winner is reported. Combine ranked:true with use_roles:true (10 players only) to also assign Top/Jungle/Mid/Bottom/Support, preferring each player's liked roles from /setup and nudging the split toward whichever side is more balanced once roles are considered.",
    "captains": "Starts a live captain draft. Name two captains, or use_random to pick two automatically; everyone else lands in a pool picked from with /choose. Once both teams are set, press Start on the roster to move everyone and open betting, or Start (no move) to open betting without moving anyone. ranked:true tracks elo for the resulting game.",
    "choose": "Captains only. Picks one player from the draft pool onto your team, then passes the turn to the other captain.",
    "notify": "DMs a one-time invite link to your voice channel - to one member, or to everyone holding a given role. message optionally replaces the default invite text; either way it's signed \"Sent by\" you. You must be sitting in a voice channel yourself to run this.",
    "wager": "Bets gold on one team winning the current game - or, with a match id, on a specific tournament match. Only while betting is open, one bet per player per game/match.",
    "wager-against": "Challenges another player to a heads-up gold wager - separate from team-game betting, no active game required.",
    "daily": "Claims 1000 free gold. Once per calendar day, per player.",
    "stats": "Shows a player's elo, ranked/casual/game record, betting record, balance, and net gold - defaults to you. Press Avatar to toggle the shown avatar between this server's own profile picture and their regular account-wide one, or Card to replace the whole embed with a customizable trading card; Back swaps back.",
    "card-set": "Equips your unlocked trading-card title, color scheme, and/or font in one go (see /stats' Card button) - set any combination of the three at once. Reaching Diamond, Master, Grandmaster, or Challenger permanently unlocks that tier's own title and scheme, even if you derank afterward; \"Default\" is always available for both. Fonts are purchased from /shop.",
    "preview": "Shows every option for one customization type - Logos, Card Titles, Color Schemes, or Fonts - in a single gallery image (a few images only if there are too many to fit), regardless of what you've personally unlocked yet.",
    "shop": "Browse every trading-card title, color scheme, and font purchasable with gold, with a ✅ next to anything you already own. Sort: Price / Sort: Owned buttons under the listing re-sort each category (Ascending/Descending toggle which way) without needing to re-run the command.",
    "achievements": "Browse every gameplay achievement, what it takes to earn it, and whether you already have. Earning one unlocks its title for /card-set and posts a one-time announcement in the channel.",
    "shop-buy": "Purchases a trading-card cosmetic with gold, permanently unlocking it for /card-set. Refuses if you already own it or can't afford it.",
    "leaderboard": "Ranks the server by a stat, including ranked-only and casual-only wins/losses/win rate. Omit filter for an elo-sorted overview. Players with a 0W-0L record in the selected stat's category (or who've never played a game at all, for the overview and elo views) are left off, so a currency-based stat like balance still shows everyone. Buttons page through the results, and Ascending/Descending buttons flip the sort direction without re-running the command. cards:true flips through each player's full stats card one at a time instead, same as /my-teams but ranked - a Card button on that view swaps the current player's stats card for their actual trading card, and stays selected as you keep paging.",
    "report-correct-winner": "Fixes a misreported winner - undoes and reapplies the payouts, records, and elo. invalidate undoes the last game entirely instead (bets refunded, nothing reapplied), as if it never happened. Requires Manage Server.",
    "team-create": "Creates a persistent team with you as its captain, or captain as its captain if given.",
    "team-set": "Sets a persistent team's voice channel and/or logo, any combination in one call. new_voice_channel creates a fresh one named after the team. The team's captain, or anyone with Manage Server, can do this.",
    "team-rename": "Renames a persistent team. The new name can't already belong to another team in this server. The team's captain, or anyone with Manage Server, can do this.",
    "team-delete": "Deletes a persistent team - its roster, record, and any pending invites go with it. The team's captain, or anyone with Manage Server, can do this; confirmation required. Doesn't affect a tournament it's already registered in.",
    "team-transfer": "Hands off a persistent team's captaincy to another player already on its roster. The team's captain, or anyone with Manage Server, can do this.",
    "team-invite": "Invites one or more members (up to 5 per call) to a team. Each invitee must accept before joining. The team's captain, or anyone with Manage Server, can do this. force (Manage Server only) skips the invitee's confirmation and adds them straight to the roster.",
    "team-leave": "Removes you from a persistent team's roster. Anyone rostered can do this to themselves, no permission needed - except the team's captain, who has to use /team-delete instead since there's no one to hand the captaincy to.",
    "my-teams": "Lists the teams you're a rostered player on in this server, with paging to flip through each one's full stats card.",
    "team-stats": "Shows a persistent team's captain, roster, voice channel, and win/loss record. Press Card to swap it for a team card - its logo as the focal point, colors sampled from that logo, captain/roster/record/win rate. Back swaps back.",
    "team-list": "Browse every team in the server with filtering (name search, recruiting-only, up to 5 members who all have to be on the roster) and sorting (name, wins, losses, win rate, roster size - sort:\"Win Rate\" order:\"Descending\" to rank teams by win rate). Buttons page through it. cards:true flips through each team's full stats card one at a time instead, same as /my-teams but for every team in the server - a Card button on that view swaps the current team's stats card for its actual trading card, and stays selected as you keep paging.",
    "team-use": "Loads two persistent teams straight into a casual or ranked game, skipping the random-split-or-draft step. Posts a roster with the same Start/Start (no move) buttons as /make-teams to start it.",
    "reuse": "Re-posts the exact same two teams from whichever of /make-teams, /captains, or /team-use ran last, instead of drawing a fresh random split or captains draft. Stays ranked if the last game was ranked, casual if it was casual. Cancels an actively in-progress game from those same teams first (refund + move back) if there is one.",
    "tournament-create": "Creates an empty tournament shell for this server - name, team size, and bracket size. One tournament per server.",
    "tournament-register": "Registers one of your teams for the server's tournament. The team's captain, or anyone with Manage Server, can do this.",
    "tournament-create-bracket": "Builds the tournament bracket from whichever teams are currently registered, seeded randomly. Rerunning it rerolls the bracket. For double elimination, losers_bracket_timing picks whether the losers bracket waits for the whole winners bracket to finish, or interleaves as each round unlocks.",
    "tournament-print-bracket": "Prints the current bracket.",
    "tournament-start": "Starts playing the current round of the bracket. mode is Sequential (one match at a time) or Simultaneous (all at once, no betting).",
    "roll": "Rolls a random number between 1 and num.",
    "setup": "Introduces Shockwave, creates your personal solo team, and walks you through picking which roles you like/dislike playing (press a role to toggle it, then press Confirm) for future role-aware team balancing. solo_team_name is always optional - omit it the first time and your solo team is named after your current server display name. Run it any time afterward to update either. Unlocks the Onboarded achievement the first time.",
    "help": "Shows this message, or details on one command.",
}


@tree.command(
    name="setup",
    description="Get started with Shockwave: your solo team and role preferences"
)
@app_commands.describe(
    solo_team_name="Name for your personal solo team - always optional, defaults to your display name",
)
async def setup(ctx, solo_team_name: str = None):
    await helperObj.setupHelper(ctx, solo_team_name)


async def helpCommandAutocomplete(ctx, current: str):
    current = current.lower()
    names = [n for n in COMMAND_HELP if current in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@tree.command(
    name="help",
    description="Get a list of commands, or details on a specific one"
)
@app_commands.describe(command="Command name to look up - omit for the full list on the site")
@app_commands.autocomplete(command=helpCommandAutocomplete)
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
    description="Create teams - randomly, or roughly elo-balanced for a ranked game"
)
@app_commands.describe(
    use_roles="Assign Top/Jungle/Mid/Bottom/Support roles (5-player teams only) - also works with ranked",
    ranked="Form roughly elo-balanced teams from your voice channel and track elo for this game",
)
async def makeTeams(ctx, use_roles: bool = False, ranked: bool = False):
    if ctx.user.voice is None or ctx.user.voice.channel is None:
        await ctx.response.send_message(
            "You need to be in a voice channel to form teams from it - join one and try again."
        )
        return

    # Role-aware team formation needs every rostered player to have run
    # /setup at least once (it's what /setup's liked/disliked roles feed
    # into, both here and for rankedTeamHelper's own role balancing below),
    # checked here, before anything about the current roster is touched,
    # so an incomplete voice channel gets a clear "who's missing" message
    # instead of forming plain (role-less) teams anyway or failing partway
    # through. Bots can't run /setup, so they're excluded rather than
    # permanently blocking the check.
    if use_roles:
        not_setup = [
            member for member in ctx.user.voice.channel.members
            if not member.bot and not helperObj.hasCompletedSetup(ctx.guild.id, member.id)
        ]
        if not_setup:
            mentions = ", ".join(member.mention for member in not_setup)
            await ctx.response.send_message(
                f"Everyone needs to run /setup before role-based teams can be formed. Still missing: {mentions}."
            )
            return

    if ranked:
        # rankedTeamHelper handles its own response + team embeds (elo
        # averages need per-player lookups it already has to do anyway),
        # a completely separate flow from the random split below, which
        # bot.py builds the response for itself.
        await helperObj.rankedTeamHelper(ctx, use_roles)
        return

    # `use_roles` (not `roles`, which would shadow the module-level `roles`
    # dict above) is already a bool from the slash command's type
    # annotation; comparing it to the string 'True' would always be False.
    #
    # This command only announces the teams; it used to optionally move
    # everyone immediately (a `movevar` flag), but moving players and
    # opening betting only happens once the posted roster's own Start
    # button is clicked (see _finalizeRoster), so a roster can be
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
    # team; makeEmbedString() silently falls back to a plain roster for
    # any other size, and _finalizeRoster silently skips the Reroll
    # button for the same reason. Explain that instead of leaving people
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

    # Posted last (after the rosters, bolded) rather than folded into the
    # very first response message, which is easy to scroll past once the
    # (visually much bigger) rosters land right after it; this puts it
    # where people are actually looking once they're done reading the teams.
    await ctx.channel.send(
        "📣 **Ready?** Press Start on the roster above to move everyone into their channels and open "
        "betting, or Start (no move) to open betting without moving anyone."
    )


@tree.command(
    name="report-correct-winner",
    description="Admin: fix a misreported winner, or invalidate the last game entirely"
)
@app_commands.describe(
    team="The team that actually won - omit if invalidating instead",
    match_id="Optional: correct a specific tournament match instead of the last game",
    invalidate="Undo the last game entirely instead of picking a winner - refunds bets, undoes elo/records/gold",
)
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def reportCorrectWinner(
    ctx, team: app_commands.Choice[int] = None, match_id: int = None, invalidate: bool = False
):
    team_value = team.value if team is not None else None
    await helperObj.reportCorrectWinnerHelper(ctx, team_value, match_id, invalidate)


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
    captain_1="First captain - required unless use_random is set",
    captain_2="Second captain - required unless use_random is set",
    use_random="Pick two captains at random from the voice channel instead",
    ranked="Track elo for this game - defaults to casual",
)
async def captains(
    ctx, captain_1: discord.Member = None, captain_2: discord.Member = None,
    use_random: bool = False, ranked: bool = False,
):
    await startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked=ranked)


# Shared by /captains regardless of its ranked flag, identical draft flow,
# the only difference is whether the resulting game is marked ranked
# (captainsHelper sets is_ranked accordingly, which gates whether recordResult later
# touches anyone's elo).
async def startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked):
    # Named `use_random`, not `random`; that would shadow the `random`
    # module imported at the top of this file.
    if ctx.user.voice is None or ctx.user.voice.channel is None:
        await ctx.response.send_message(
            "You need to be in a voice channel to start a captains draft - join one and try again."
        )
        return

    if len(ctx.user.voice.channel.members) < 2:
        await ctx.response.send_message("Not enough players in the voice channel!")
        return

    if use_random:
        # sqlite3 can't bind a plain Python list as a query parameter, and
        # getRandomMember() needs each player's id (to look the Member back
        # up), not just their name; serialize into a Team, the same
        # convention every other "players" column write in this file uses.
        players = Team()
        for player in ctx.user.voice.channel.members:
            players.add_player(Player(player.id, player.name))
        helperObj.update(ctx.guild.id, "players", players.serializeTeam())

        # Loop on "still None OR same as captain1"; "AND" can never be
        # True (a value can't be both None and equal to a non-None
        # captain1), so it would never actually re-roll on a collision.
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
    member="The player to pick - required unless use_random is set",
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
    clear_tournament="Delete this server's tournament entirely - bracket, registrations, match history. Can't be undone",
    clear_elo="Reset every player's elo back to this server's default elo (confirmation required)",
    clear_economy="Wipe every player's balance/elo/record/gold entirely for this server (confirmation required)",
    clear_achievements="Reset earned achievements, or just one player if `user` is set (confirmation required)",
    clear_card_unlocks="Wipe trading-card unlocks, or just one player if `user` is set (confirmation required)",
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
            "`user` only applies to `clear_achievements`/`clear_card_unlocks` - set one of those too."
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
    # them run immediately; a confirm/cancel view goes out as a followup
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


# The caller's own captained teams only, for team-name params on commands
# that require being that team's captain. Same "only suggest what's
# actually usable" idea cardTitleAutocomplete uses for card unlocks, just
# scoped to captaincy (getTeamsCaptainedBy) instead. Doesn't stop someone
# from typing a different name by hand (Discord's autocomplete is a
# suggestion list, not a restriction to it); the backing helpers still do
# their own captain check either way. A member with Manage Server can act
# on any team (see the helpers' own "or manage_guild" override), so they
# get every team suggested here too, not just ones they happen to captain.
async def myCaptainedTeamAutocomplete(ctx, current: str):
    current = current.lower()
    if ctx.user.guild_permissions.manage_guild:
        teams = helperObj.getTeamsForGuild(ctx.guild.id)
    else:
        teams = helperObj.getTeamsCaptainedBy(ctx.guild.id, ctx.user.id)
    names = [team.get_name() for _team_id, team in teams if current in team.get_name().lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# Same shape as myCaptainedTeamAutocomplete, but for team-name params that
# only read a team rather than requiring captaincy of it (/team-stats,
# /team-use), every team the caller is rostered on at all (getTeamsForPlayer),
# captain or not. Same Manage Server carve-out as myCaptainedTeamAutocomplete:
# an admin sees every team in the guild here too, not just ones they're on.
async def myTeamAutocomplete(ctx, current: str):
    current = current.lower()
    if ctx.user.guild_permissions.manage_guild:
        teams = helperObj.getTeamsForGuild(ctx.guild.id)
    else:
        teams = helperObj.getTeamsForPlayer(ctx.guild.id, ctx.user.id)
    names = [team.get_name() for _team_id, team in teams if current in team.get_name().lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@tree.command(
    name="tournament-create",
    description="Create a tournament for this server"
)
@app_commands.describe(
    name="Tournament name",
    teamsize="Number of players per team",
    numteams="Number of teams the bracket holds",
    double_elim="Double elimination instead of single - defaults to single"
)
async def createTournament(ctx, name: str, teamsize: int, numteams: int, double_elim: bool = False):
    await helperObj.createTournamentHelper(ctx, name, teamsize, numteams, double_elim)


@tree.command(
    name="tournament-register",
    description="Register a team for this server's tournament"
)
@app_commands.describe(team="Name of the team to register")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def registerTeam(ctx, team: str):
    await helperObj.registerTeamHelper(ctx, team)


@tree.command(
    name="tournament-create-bracket",
    description="Create (or reroll) the tournament bracket from registered teams"
)
@app_commands.describe(
    elimination_type="Single or double elimination for this tournament",
    losers_bracket_timing="Double elimination only: when the losers bracket plays - defaults to "
                           "after winners finishes"
)
@app_commands.choices(elimination_type=[
    app_commands.Choice(name="Single elimination", value="single"),
    app_commands.Choice(name="Double elimination", value="double"),
])
@app_commands.choices(losers_bracket_timing=[
    app_commands.Choice(name="After the winners bracket finishes entirely", value="after_winners"),
    app_commands.Choice(name="Interleaved - as soon as each round unlocks it", value="interleaved"),
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
@app_commands.describe(
    name="Team name", team_size="How many players the team is looking for",
    captain="Optional: make this member the captain instead of you",
)
async def createTeam(ctx, name: str, team_size: int, captain: discord.Member = None):
    await helperObj.createTeamHelper(ctx, name, team_size, captain)


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
    force="Manage Server only: add them straight to the roster, skipping their own confirmation",
)
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def teamInvite(
    ctx, team: str, member_1: discord.Member,
    member_2: discord.Member = None, member_3: discord.Member = None,
    member_4: discord.Member = None, member_5: discord.Member = None,
    force: bool = False,
):
    members = [m for m in (member_1, member_2, member_3, member_4, member_5) if m is not None]
    await helperObj.teamInviteHelper(ctx, team, members, force)


@tree.command(
    name="team-leave",
    description="Leave a persistent team you're rostered on"
)
@app_commands.describe(team="Name of the team to leave")
@app_commands.autocomplete(team=myTeamAutocomplete)
async def teamLeave(ctx, team: str):
    await helperObj.teamLeaveHelper(ctx, team)


# Discord caps a slash command option at 25 static choices, and the built-in
# logo set is bigger than that (see assets/clash-logos); autocomplete is
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
@app_commands.autocomplete(logo=logoAutocomplete, team=myCaptainedTeamAutocomplete)
async def setTeam(
    ctx, team: str, voice_channel: discord.VoiceChannel = None,
    new_voice_channel: bool = False, logo: str = None,
):
    await helperObj.teamSetHelper(ctx, team, voice_channel, new_voice_channel, logo)


@tree.command(
    name="team-rename",
    description="Rename a persistent team you captain"
)
@app_commands.describe(team="Current name of the team", new_name="New name for the team")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def renameTeam(ctx, team: str, new_name: str):
    await helperObj.teamRenameHelper(ctx, team, new_name)


@tree.command(
    name="team-delete",
    description="Delete a persistent team you captain (confirmation required)"
)
@app_commands.describe(team="Name of the team to delete")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def deleteTeam(ctx, team: str):
    await helperObj.teamDeleteHelper(ctx, team)


@tree.command(
    name="team-transfer",
    description="Hand off captaincy of a persistent team you captain to another player on its roster"
)
@app_commands.describe(
    team="Name of the team to transfer",
    member="Who to make the new captain - must already be on the team's roster",
)
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def transferTeam(ctx, team: str, member: discord.Member):
    await helperObj.teamTransferHelper(ctx, team, member)


@tree.command(
    name="team-stats",
    description="View a team's roster and record"
)
@app_commands.describe(team="Name of the team")
@app_commands.autocomplete(team=myTeamAutocomplete)
async def teamStats(ctx, team: str):
    await helperObj.teamStatsHelper(ctx, team)


@tree.command(
    name="my-teams",
    description="List the teams you (or another player) belong to and flip through their stats"
)
@app_commands.describe(member="Whose teams to look up - defaults to you")
async def myTeams(ctx, member: discord.Member = None):
    await helperObj.myTeamsHelper(ctx, member)


@tree.command(
    name="team-list",
    description="Browse every team in this server, with filtering and sorting - buttons to page through it"
)
@app_commands.describe(
    search="Only show teams whose name contains this",
    recruiting_only="Only show teams still short of their target roster size",
    sort="What to sort by - defaults to name",
    order="Ascending or descending - defaults to ascending",
    cards="Flip through each team's full stats card one at a time, like /my-teams, instead of a summary list",
    member_1="Only show teams that have this member on their roster",
    member_2="Also required on the roster (optional)",
    member_3="Also required on the roster (optional)",
    member_4="Also required on the roster (optional)",
    member_5="Also required on the roster (optional)",
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
    sort: app_commands.Choice[str] = None, order: app_commands.Choice[str] = None, cards: bool = False,
    member_1: discord.Member = None, member_2: discord.Member = None, member_3: discord.Member = None,
    member_4: discord.Member = None, member_5: discord.Member = None,
):
    sort_value = sort.value if sort is not None else "name"
    order_value = order.value if order is not None else "asc"
    members = [m for m in (member_1, member_2, member_3, member_4, member_5) if m is not None]
    await helperObj.teamListHelper(ctx, search, recruiting_only, sort_value, order_value, cards, members)


@tree.command(
    name="team-use",
    description="Load two persistent teams into a casual or ranked game"
)
@app_commands.describe(
    team1="Name of the first persistent team",
    team2="Name of the second persistent team",
    ranked="Track elo for this game - defaults to casual"
)
@app_commands.autocomplete(team1=myTeamAutocomplete, team2=myTeamAutocomplete)
async def useTeams(ctx, team1: str, team2: str, ranked: bool = False):
    await helperObj.useTeamsHelper(ctx, team1, team2, ranked)


@tree.command(
    name="reuse",
    description="Re-post the last game's two teams instead of making a fresh split/draft"
)
async def reuseTeams(ctx):
    await helperObj.reuseTeamsHelper(ctx)


@tree.command(
    name="notify",
    description="Send a member - or everyone in a role - an invite to the channel"
)
@app_commands.describe(
    member="A specific member to invite",
    role="Invite every member of this role instead - give one or the other, not both",
    message="Custom text to send instead of the default invite message",
)
async def notify(ctx, member: discord.Member = None, role: discord.Role = None, message: str = None):
    if member is None and role is None:
        await ctx.response.send_message("Mention a member or a role to invite.")
        return
    if member is not None and role is not None:
        await ctx.response.send_message("Give a member or a role, not both.")
        return
    if ctx.user.voice is None or ctx.user.voice.channel is None:
        await ctx.response.send_message(
            "You need to be in a voice channel to invite someone to it - join one and try again."
        )
        return

    # notifyHelper DMs the target directly rather than responding to the
    # interaction, so calling it once per role member in a loop is safe -
    # ctx.response.send_message below still only ever fires once either way.
    targets = role.members if role is not None else [member]
    for target in targets:
        await helperObj.notifyHelper(ctx, target, message)

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
@app_commands.describe(num="Top of the range - must be greater than 1")
async def roll(ctx, *, num: int):
    if num > 1:
        rand = random.randint(1, num)
        await ctx.response.send_message("You rolled " + str(rand))
    else:
        await ctx.response.send_message("Please use a number greater than 1.")


# Runs the full test suite before connecting to Discord, so a broken
# deploy shows up in the log immediately instead of only being noticed
# once something breaks in production. Shelled out to `pytest -n auto` in
# its own subprocess rather than run in-process with stdlib unittest the
# way this used to work, for two reasons: pytest-xdist splits the ~900
# tests across every CPU core instead of running them one at a time, and
# a genuinely separate process means tests.py is that process's own real
# entry point; its first nested `_import_bot_module()` call gets the
# same inert, open()-mocked root log handler an ordinary `pytest tests.py`
# run from a terminal always has (see readme.md), so none of the
# thousands of test-fixture DB/asyncio-debug log lines a full run
# produces can leak into this file's own real log the way they could
# when the suite ran in-process here directly. That used to need
# logger._suppress_db_logging plus temporarily raising the "asyncio"/
# "discord" logger levels around the run, both gone now, since there's
# nothing left in this process for them to protect against.
# `--junitxml` gives back pytest-xdist's own already-stitched-across-
# workers summary (total/failed counts plus one <testcase> per test) as
# structured XML, rather than scraping worker-interleaved terminal
# output. `cwd=BASE_DIR` and `sys.executable` keep this correct
# regardless of the process's own working directory or which Python
# environment is actually running the bot. Every run logs one info-level
# summary line (how many passed out of how many, and how long the whole
# subprocess took) regardless of outcome. A failing suite (or pytest
# itself failing to launch or produce a report at all, e.g. a stale
# install missing pytest-xdist) additionally logs a warning rather than
# aborting startup: a real deploy should still come up and serve players
# even if, say, a test itself is stale, rather than a self-test
# regression taking the whole bot down.
def _runStartupSelfTests():
    import subprocess
    import sys
    import tempfile
    import xml.etree.ElementTree as ET

    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "results.xml")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests.py", "-n", "auto", "-q", f"--junitxml={report_path}"],
                cwd=BASE_DIR, capture_output=True, text=True,
            )
        except Exception:
            logger.warning("Startup self-test: failed to launch pytest, starting the bot anyway.", exc_info=True)
            return

        try:
            suite = ET.parse(report_path).getroot()
        except (FileNotFoundError, ET.ParseError):
            logger.warning(
                "Startup self-test: pytest produced no readable report (exit code %d), starting the bot "
                "anyway. Output:\n%s",
                proc.returncode, _truncateForLog(proc.stdout + proc.stderr),
            )
            return

    if suite.tag != "testsuite":
        suite = suite.find("testsuite")

    total = int(suite.get("tests", 0))
    failed_count = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    duration_seconds = float(suite.get("time", 0))
    logger.info("Startup self-test: %d/%d passed in %.1fs.", total - failed_count, total, duration_seconds)

    if failed_count == 0:
        return

    logger.debug("Startup self-test output:\n%s", proc.stdout + proc.stderr)
    failed = [
        f"{testcase.get('classname')}.{testcase.get('name')}" for testcase in suite.iter("testcase")
        if testcase.find("failure") is not None or testcase.find("error") is not None
    ]
    logger.warning(
        "Startup self-test: %d/%d tests failed, starting the bot anyway. Failed: %s",
        failed_count, total, _truncateForLog("; ".join(failed)),
    )


# Guarded so tests.py can import this module (to exercise command callbacks
# and event handlers directly) without connecting to Discord as a side
# effect of the import.
if __name__ == "__main__":
    _runStartupSelfTests()
    client.run(token)