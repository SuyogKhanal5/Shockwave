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

# Every path in this file is built from BASE_DIR so it always points at this
# file's own folder, no matter what directory the process was started from.
# A relative path only works if you `cd` into the project folder first,
# which is easy to forget under a service manager (e.g. a systemd unit with
# no WorkingDirectory= set). helper.py's asset paths (TEAM_LOGO_DIR,
# FONTS_DIR, etc.) use the same os.path.dirname(__file__) trick.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Keeps shockwave.log at this many lines at most, instead of using the
# standard library's RotatingFileHandler, which would split it into
# shockwave.log, .log.1, .log.2, and so on once it got too big. This way
# there's always just one file, in time order, and only the oldest lines
# get dropped once it grows past the cap.
LOG_FILE_MAX_LINES = 10000


class MaxLinesFileHandler(logging.FileHandler):
    def __init__(self, filename, max_lines, encoding=None):
        self.max_lines = max_lines
        super().__init__(filename, mode="a", encoding=encoding)
        # Counted once at startup from whatever's already on disk, so this
        # run knows right away if a trim is already overdue, instead of
        # wrongly assuming the file starts empty.
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

    # Drops the oldest lines so the file holds exactly max_lines. Doesn't
    # need its own locking: this only ever runs from inside emit(), and
    # Handler.handle() already wraps every emit() call in self.acquire()/
    # release(), so a concurrent emit from another thread is already
    # blocked out.
    def _trim(self):
        self.stream.close()
        with open(self.baseFilename, encoding=self.encoding) as f:
            lines = f.readlines()
        kept = lines[-self.max_lines:]
        with open(self.baseFilename, "w", encoding=self.encoding) as f:
            f.writelines(kept)
        self._line_count = len(kept)
        self.stream = self._open()


# Logs to both the console and the line-capped file above. The file is what
# survives a restart, or a run with no attached terminal (e.g. as a
# background service). Stdout alone wouldn't survive that. This is set up
# on the root logger, so discord.py's own internal logging (gateway, HTTP)
# gets logged the same way, not just calls made from this file.
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

# encoding="utf-8" is set explicitly instead of left to the platform
# default, since that default differs by OS (Windows' ANSI codepage vs.
# Linux's near-universal UTF-8). A plain ASCII token would work fine
# either way, but there's no reason to leave it to chance.
with open(os.path.join(BASE_DIR, "token.txt"), encoding="utf-8") as f:
    token = f.readline().strip()

# Connect to Database
dataFolder = os.path.join(BASE_DIR, "data", "guildData", "serverInfo")
dbpath = os.path.join(dataFolder, "main.db")

# This check has to happen before sqlite3.connect() runs, because connect()
# creates the database file on disk as a side effect, even if it's empty.
# Check after connecting and the file would always already exist, so the
# CREATE TABLE below would never run, even on a brand new install. makedirs
# has to run first too: a fresh clone has no data/ folder at all (it's
# gitignored), and connect() only creates the file, not missing parent
# folders.
os.makedirs(dataFolder, exist_ok=True)
db_already_existed = path.isfile(dbpath)

mainDB = sqlite3.connect(dbpath, timeout=30)
cursor = mainDB.cursor()

# Every guild's writes go through this one connection/file, so under real
# concurrent load (several servers' games resolving around the same
# moment) sqlite's default rollback-journal mode would have a writer hold
# an exclusive lock that blocks every reader too, not just other writers.
# WAL mode lets readers keep going against the last-committed snapshot
# while a write is in flight, so a slow write on one guild's row doesn't
# stall an unrelated command reading a different guild's. synchronous=
# NORMAL is WAL's own recommended pairing: still fsyncs at WAL checkpoints
# (crash-safe), just not on every single commit the way the default FULL
# setting does. `timeout=30` above is the sqlite3 module's own "keep
# retrying a busy lock for this many seconds before raising
# OperationalError" setting (the 5-second default), given some headroom
# since a real command failing outright on lock contention is a worse
# outcome than it taking a little longer.
cursor.execute("PRAGMA journal_mode=WAL")
cursor.execute("PRAGMA synchronous=NORMAL")

# Caps how long a single logged line can get. A row update (a serialized
# team roster especially) can run to thousands of characters once its
# parameters are expanded inline, and a command's own logged params can
# include a full discord.py object repr. Without a cap, one oversized line
# could dominate the whole log file. Shared by _logDatabaseStatement below
# and LoggingCommandTree further down.
LOG_LINE_MAX_LENGTH = 500


def _truncateForLog(text):
    if len(text) > LOG_LINE_MAX_LENGTH:
        return text[:LOG_LINE_MAX_LENGTH] + "... (truncated)"
    return text


_MUTATING_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE")


# Logs every database write (INSERT/UPDATE/DELETE) to this file's log, so
# there's a real audit trail of every change made. Everything else that
# isn't a mutation (SELECTs, the trace callback's own "BEGIN " for an
# implicit transaction) is filtered out and not logged. sqlite3's trace
# callback hands us each statement with its bound parameters already
# filled in, not the raw `?` placeholders, so the log line reads like real
# SQL rather than something you'd have to decode. This covers database
# writes specifically. discord.py's own internal logging (View.on_error
# for a button callback, Loop._error for a background task) and
# on_app_command_error below already send unhandled exceptions to this
# same logger.
def _logDatabaseStatement(sql):
    statement = sql.strip()
    if not statement.upper().startswith(_MUTATING_SQL_PREFIXES):
        return
    logger.info("DB: %s", _truncateForLog(statement))


mainDB.set_trace_callback(_logDatabaseStatement)

# Where daily database snapshots land (see backupDatabaseTask below). Kept
# separate from serverInfo/, the live database's own folder, so a backup
# can never collide with, or get mistaken for, the real database file.
BACKUP_DIR = os.path.join(BASE_DIR, "data", "guildData", "backups")
BACKUP_RETENTION_DAYS = 7


def ensure_column(table, column, coltype="", default=None):
    # Adds a column to a table only if it isn't already there. This is the
    # migration mechanism for every table that existed before a given
    # feature: a fresh install gets the column from CREATE TABLE directly,
    # while an older database that predates the feature gets it added here
    # instead.
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
        "default_elo, betting_opened_at, disliked_role_user_ids, draft_pick_page, "
        "draft_players_message_id, draft_snake, betting_closed_message_id, make_teams_message_ids, "
        "matchup_message_id, roster_starting, roster_permissions_strict, max_wager, betting_enabled)"
    )
    mainDB.commit()
else:
    # result1/result2 are unused now (nothing reads or writes them). Kept
    # only so an older database's schema still matches.
    ensure_column("servers", "result1", "TEXT")
    ensure_column("servers", "result2", "TEXT")
    # captain1/captain2 are read and written by captainsHelper and the
    # draft-pick handlers (the /make-teams draft flow). They weren't part
    # of the original CREATE TABLE above, so a pre-existing database needs
    # them added here.
    ensure_column("servers", "captain1", "TEXT")
    ensure_column("servers", "captain2", "TEXT")
    ensure_column("servers", "betting_state", "TEXT", "'NONE'")
    ensure_column("servers", "betting_message_id", "INTEGER")
    ensure_column("servers", "betting_channel_id", "INTEGER")
    # When the current betting window was opened (unix seconds). Lets
    # reconcileStaleBettingWindows (called from on_ready) work out how much
    # time was actually left on the window if the bot restarts mid-window.
    ensure_column("servers", "betting_opened_at", "INTEGER")
    # Whether the current team1/team2 game was formed with ranked:true (via
    # /make-teams random or /make-teams draft). Gates whether recordResult
    # touches anyone's elo.
    ensure_column("servers", "is_ranked", "INTEGER", "0")
    # Set while a /tournament start sequential match is using team1/team2.
    # Tells recordResult to also advance the tournament bracket once the
    # normal betting/elo resolution for that game finishes.
    ensure_column("servers", "active_tournament_match_id", "INTEGER")
    # /set wager-channel: when set, every betting posting (open, closed,
    # winner report) goes here instead of wherever the game or tournament
    # match happened to start.
    ensure_column("servers", "wager_channel", "TEXT")
    # /set betting-timer: how long a betting window stays open. Replaces
    # what used to be a hardcoded value (BETTING_DURATION_SECONDS). For a
    # simultaneous-mode tournament round with several matches running at
    # once, this is the per-match base. The round's actual window is this
    # value times however many matches are open at once (see
    # _openConcurrentTournamentBetting).
    ensure_column("servers", "betting_timer_seconds", "INTEGER", str(helper.BETTING_DURATION_SECONDS))
    # Backs the live "reroll roles / start the game" buttons on a
    # just-posted, final roster (see _finalizeRoster/RosterActionView).
    # This replaced what used to be separate /randomize-roles and /start
    # commands. roster_team2_message_id is what a button click actually
    # gets checked against, and overwriting it on every new roster is what
    # makes an older roster's buttons stop working once a newer one posts.
    ensure_column("servers", "roster_team1_message_id", "INTEGER")
    ensure_column("servers", "roster_team2_message_id", "INTEGER")
    ensure_column("servers", "roster_channel_id", "INTEGER")
    ensure_column("servers", "roster_use_roles", "INTEGER", "0")
    # Flipped to true the instant a Start / Start (no move) click passes its
    # checks, so a second, near-simultaneous click on the same roster can't
    # also pass and start the game twice. This is the same double-click
    # guard that roster_team2_message_id used to serve as, before that
    # column needed to stay untouched so recordResult's own cleanup could
    # still find team2's roster message once the game ends. Reset back to 0
    # by _finalizeRoster whenever a fresh roster posts.
    ensure_column("servers", "roster_starting", "INTEGER", "0")
    # /set default-elo: what a brand new player's elo starts at in this
    # guild (see helpers._defaultEloForGuild). NULL until an admin sets it,
    # meaning "use the global helper.DEFAULT_ELO (1000)".
    ensure_column("servers", "default_elo", "INTEGER")
    # Comma-separated user ids of whoever the current team1/team2 roster
    # assigned a disliked role to (rankedTeamHelper, ranked:true
    # use_roles:true only). Read back by recordResult and
    # reportCorrectWinnerHelper so a win on a disliked role earns the
    # ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER bonus. This lives and
    # clears alongside team1/team2 (see clearTeamsHelper) rather than being
    # cleared per result, so a reused roster (/make-teams repeat) still
    # gets credit for the same assignments.
    ensure_column("servers", "disliked_role_user_ids", "TEXT")
    # Which page of the draft-pick button picker (CaptainsDraftPickView) is
    # currently shown. Only matters once the pool is too big to fit on one
    # page (see DRAFT_PICK_MAX_UNPAGINATED). Reset to 0 on every pick, since
    # the pool shrinking changes what each page even contains.
    ensure_column("servers", "draft_pick_page", "INTEGER", "0")
    # The posted "PLAYERS" pool embed's message id for a live captain draft
    # (see _updateDraftEmbeds). Edited in place on every pick, the same way
    # roster_team1_message_id/roster_team2_message_id already are, instead
    # of _applyDraftPick reposting all three embeds from scratch each time.
    ensure_column("servers", "draft_players_message_id", "INTEGER")
    # Whether the current captain draft (/make-teams draft snake:true)
    # reverses pick order every 2 picks instead of alternating every single
    # pick. See _nextDraftTurn. Lives and clears alongside team1/team2 (see
    # clearTeamsHelper), the same pattern disliked_role_user_ids uses.
    ensure_column("servers", "draft_snake", "INTEGER", "0")
    # The "Betting is now closed!" message that _closeBettingWindow posts
    # once the timer expires. Lets recordResult delete it, along with the
    # betting-open/winner-report message, once the game it was for is
    # actually scored. NULL whenever a winner got reported before the timer
    # ever fired (nothing to delete then).
    ensure_column("servers", "betting_closed_message_id", "INTEGER")
    # Comma-separated ids of every "team formation" text message for the
    # current roster: the "Teams created!" / "Captains selected!" replies,
    # plus a draft's own picker/pool messages. Not the roster embeds
    # themselves, which already have their own roster_team1_message_id/
    # roster_team2_message_id. All of these live in roster_channel_id and
    # get deleted by recordResult once the game is scored, the same cleanup
    # roster_team1_message_id/roster_team2_message_id already get.
    ensure_column("servers", "make_teams_message_ids", "TEXT")
    # The matchup graphic's own message id (see _sendMatchupImage), posted
    # right as the game starts. recordResult replies to it when posting the
    # result, so the two stay visually linked in the channel. NULL for a
    # tournament match (sequential mode never posts this graphic at all,
    # see _handleReadyClick) or before /start's own matchup image goes out.
    ensure_column("servers", "matchup_message_id", "INTEGER")
    # /set roster-permissions: whether Start/Start (no move)/Random Roles/
    # Balanced Roles are open to anyone who can see the roster message
    # (0, the default) or gated to a rostered player/Manage Server admin
    # like the winner-report buttons already are (1). See
    # _isAdminOrInCurrentGame.
    ensure_column("servers", "roster_permissions_strict", "INTEGER", "0")
    # /set max-wager: caps a single /wager team or /wager against bet.
    # NULL (the default) means no cap, same as today.
    ensure_column("servers", "max_wager", "INTEGER")
    # /set betting: whether /wager team and /wager against actually accept
    # bets at all. Games, elo, and the winner-report flow all work exactly
    # the same either way; this only gates the wagering layer on top of
    # them, for a server that doesn't want anything gambling-adjacent even
    # with fictional gold. Defaults to 1 (enabled), today's behavior.
    ensure_column("servers", "betting_enabled", "INTEGER", "1")

# Per-member currency: gold balance plus win/loss and wagering stats, one
# row per (guild, user).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS economy("
    "guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, last_daily, "
    "PRIMARY KEY(guildId, userId))"
)
# "CREATE TABLE IF NOT EXISTS" above does nothing on a database that
# already has an `economy` table from before these columns existed.
# ensure_column() is what actually adds them there.
ensure_column("economy", "gold_lost", "INTEGER", "0")
ensure_column("economy", "game_wins", "INTEGER", "0")
ensure_column("economy", "game_losses", "INTEGER", "0")
ensure_column("economy", "elo", "INTEGER", str(helper.DEFAULT_ELO))
# The ranked-only subset of game_wins/game_losses (a casual game bumps
# game_wins/game_losses but not these). /stats and /leaderboard use them to
# break a player's record into casual vs. ranked instead of one combined
# total.
ensure_column("economy", "ranked_wins", "INTEGER", "0")
ensure_column("economy", "ranked_losses", "INTEGER", "0")
# Consecutive game wins right now, backing the "on_fire" achievement (see
# CARD_ACHIEVEMENT_TITLES in helper.py). Unlike every other economy column,
# this isn't a simple additive delta, so applyGameDeltas updates it with
# its own separate UPDATE (increment on a win, reset to 0 on a loss)
# instead of folding it into computeGameDeltas' delta dict.
ensure_column("economy", "current_win_streak", "INTEGER", "0")
# Active bets for the game currently in progress in a guild. Cleared out
# (paid out or refunded) by the time the game resolves.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS wagers("
    "guildId, userId, username, team, amount, "
    "PRIMARY KEY(guildId, userId))"
)
# A snapshot of the most recently resolved game per guild: wagers, rosters,
# and the exact deltas applied. Lets /set correct-winner undo a
# misreported result precisely instead of guessing at what to reverse.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS last_result(guildId PRIMARY KEY, data)"
)
# One row per active /wager against challenge. Unlike the team-game
# `wagers` table above, several of these can be open at once per guild
# (different pairs of players), so each is tracked by its own row and
# message instead of a single column on `servers`.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS duels("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "challengerId, challengerName, targetId, targetName, amount, state)"
)
# One row per posted /leaderboard message, tracking which page it's
# currently showing so the paging buttons know what to re-render. cards
# and cardShown carry over /team list's own cards:true toggle. See the
# identically-named columns on team_list_views for what each one means.
# LeaderboardPagingView reads both back the same way TeamListPagingView
# does.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS leaderboards("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, filter, sort_order, page, "
    "cards INTEGER DEFAULT 0, cardShown INTEGER DEFAULT 0)"
)
ensure_column("leaderboards", "cards", "INTEGER", "0")
ensure_column("leaderboards", "cardShown", "INTEGER", "0")
# One row per posted /team lookup message, same paging idea as
# leaderboards above, but scoped to a single caller (userId) instead of the
# whole guild's stats, since each page here is one of that player's own
# teams, not a page of many players.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS my_team_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, userId, page)"
)
# One row per posted /stats message, so we can recognize that a click
# landed on a real /stats embed (see StatsView).
cursor.execute(
    "CREATE TABLE IF NOT EXISTS stats_views(messageId INTEGER PRIMARY KEY, guildId)"
)
# "CREATE TABLE IF NOT EXISTS" above does nothing on a database that
# already has the table, so targetUserId and cardShown, added after
# stats_views first shipped, need ensure_column to actually reach an
# existing server's table. targetUserId is who to re-fetch the real avatar
# for when toggling back off the placeholder. cardShown flips to 1 once
# the Card button is pressed. After that, the avatar toggle refuses to
# touch the message (see StatsView), since a trading card isn't shaped
# like a normal /stats embed and toggling its thumbnail would just make a
# mess of it.
ensure_column("stats_views", "targetUserId")
ensure_column("stats_views", "cardShown", "INTEGER", "0")
# Which avatar the trading card is currently rendered with: 0 (default)
# for this server's own profile picture, 1 for the regular account-wide
# one. Only matters once cardShown=1. Reset to 0 every time the card is
# (re-)entered, so it always starts on the server avatar, matching the
# plain /stats embed's own default (see StatsView).
ensure_column("stats_views", "cardAvatarGlobal", "INTEGER", "0")
# A player's trading-card look (see /stats' Card button and
# _renderTradingCardImage). One row per (guild, player), created with
# Shockwave's own defaults the first time it's needed. Colors are stored
# as "#RRGGBB" hex. font_style is a named preset that _cardFontPaths
# resolves: either "Default" (Shockwave's own Chakra Petch/IBM Plex Sans
# pairing, always available) or one of CARD_SHOP_FONT_STYLES' unlockable
# fonts. `customized` (see ensureCardSettings) tracks whether a row still
# just reflects Shockwave's defaults (0) or was explicitly changed by
# something other than that self-healing insert (1). There's no
# /card-customize command yet, so every row is currently always 0, and
# /stats keeps it in sync with CARD_DEFAULT_* on every call instead of
# freezing at whatever the values were the day the row was created.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS trading_cards("
    "guildId, userId, title, accent_color, background_color, text_color, font_style, "
    "PRIMARY KEY(guildId, userId))"
)
ensure_column("trading_cards", "customized", "INTEGER", "0")
# Which CARD_SHOP_COLOR_SCHEMES tier name a row's colors were last equipped
# from via /card-set, or NULL for a hand-edited custom hex value with
# nothing to track (see _resyncEquippedColorScheme). Lets an already-
# equipped scheme keep following that scheme's current colors instead of
# freezing at whatever they were the moment it was picked.
ensure_column("trading_cards", "color_scheme_name")
# A permanent record of which trading-card cosmetics (a title, a color
# scheme; see CARD_TIER_REWARD_TITLES) each player has unlocked in each
# guild, by reaching Diamond, Master, Grandmaster, or Challenger at least
# once (see _checkTierRewardUnlocks). Nothing ever deletes a row here, so
# a reward stays unlocked even after the player deranks back below the
# tier that earned it. itemKey is a tier name ("Diamond", etc.), itemType
# is "title" or "color_scheme" (both unlock together per tier, see
# _unlockCardReward), so the same key appears twice per reward.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS card_unlocks("
    "guildId, userId, itemType, itemKey, PRIMARY KEY(guildId, userId, itemType, itemKey))"
)
# One row per posted /team stats message, so we can recognize that a click
# landed on a real /team stats embed (see TeamStatsView). Same idea as
# stats_views above, but scoped to a team (teamId) instead of a player.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_stats_views("
    "messageId INTEGER PRIMARY KEY, guildId, teamId, cardShown INTEGER DEFAULT 0)"
)
# One row per posted /team list message, same paging idea as leaderboards
# above, plus the filter and sort options it was posted with. That way a
# page flip (_handleTeamListPageClick) re-applies the exact same view
# instead of resetting to the unfiltered, default-sorted list.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_list_views("
    "messageId INTEGER PRIMARY KEY, guildId, channelId, search, recruitingOnly, sort, sort_order, page, "
    "cards INTEGER DEFAULT 0, cardShown INTEGER DEFAULT 0, memberIds, memberNames)"
)
# cards is 1 when a posted /team list message is in "cards" mode: one
# team's full stats card per page (same shape as /team lookup), sourced
# from the same filtered/sorted team list a plain /team list would show,
# instead of the default summary-list mode. _handleTeamListPageClick reads
# this back to know which of the two ways to re-render on a page flip.
# cardShown further narrows cards mode: 0 for that team's plain stats
# card, 1 for its actual trading card (see TeamListPagingView's own
# Card/Back toggle). It's carried across a page flip so paging while
# looking at trading cards keeps showing trading cards, not stats.
# memberIds is a comma-separated list of user ids (empty string for none,
# the same shape servers.disliked_role_user_ids uses), narrowing the list
# to teams that have every one of those members on the roster. memberNames
# is the same set's display names, captured once at post time purely for
# the footer text, so a page flip never needs to re-resolve Discord
# members from bare ids. All four of these reach an existing database
# whose team_list_views table predates them. The CREATE TABLE above
# already includes them for a fresh install.
ensure_column("team_list_views", "cards", "INTEGER", "0")
ensure_column("team_list_views", "cardShown", "INTEGER", "0")
ensure_column("team_list_views", "memberIds", "TEXT")
ensure_column("team_list_views", "memberNames", "TEXT")
# Every persistent team in a server. Distinct from the ephemeral team1/
# team2 columns on `servers`, which hold whatever roster the last
# /make-teams random or /make-teams draft produced. These are named teams
# a player can be registered on ahead of a tournament. A player can be
# listed on more than one row here. Tournament.register_team is what stops
# the same player from being entered on two teams in one tournament.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS teams("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, name, data)"
)
# One tournament per server. Creating a new one while one already exists
# requires confirmation (see ConfirmTournamentOverwriteView), since it
# replaces this row outright. Columns mirror TourneyClasses.Tournament's
# attributes directly. `teams` and `bracket` are stored as JSON, since
# they're variable-length nested data.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournaments("
    "guildId PRIMARY KEY, name, team_size, num_teams, double_elimination, teams, bracket)"
)
# Losers bracket for a double-elimination tournament, stored as JSON for
# the same reason `bracket` above is: it's variable-length, nested
# node-graph data. NULL for any tournament created before this existed, or
# one that isn't double elimination at all.
ensure_column("tournaments", "losers_bracket", "TEXT")
# One row per pending /team invite. Several can be open at once (different
# teams, different invitees), so each is tracked by its own row and
# message, like `duels` above.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS team_invites("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "teamId, teamName, inviterId, targetId, targetName)"
)
# One row per tournament match ever played. /tournament start creates a
# batch of these per round (sequential mode: one at a time; simultaneous
# mode: all at once), each keyed by its own id so /set correct-winner can
# target a specific match. nodeIndex is the index into the tournament's
# bracket list for one of the two paired nodes in this match (the other is
# that node's .opponent). That's how a resolved match knows which bracket
# node to advance the winner into.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_matches("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, roundIndex, nodeIndex, "
    "team1, team2, state, mode, messageId, channelId, winner)"
)
# 'winners', 'losers', or 'finals': which bracket this match belongs to.
# Needed because roundIndex/nodeIndex are only unique within one bracket.
# A double-elimination tournament's winners round 0 and losers round 0 are
# two entirely different matches that happen to share the same numbers.
ensure_column("tournament_matches", "bracketType", "TEXT", "'winners'")
# Set once this match's own betting window (see
# _openConcurrentTournamentBetting) has closed. Kept separate from
# `state`, since a match can still be unresolved (waiting on a report)
# after betting on it has already closed.
ensure_column("tournament_matches", "bettingClosed", "INTEGER", "0")
# A JSON snapshot of exactly which wagers _settleMatchWagers paid out for
# this match (userId, username, team, amount). tournament_wagers rows
# themselves get deleted once settled, so without this snapshot, a later
# /set correct-winner match_id correction would have no way to know who to
# reverse or repay. NULL for a match nobody bet on, or one settled before
# this existed.
ensure_column("tournament_matches", "settledWagers", "TEXT")
# Every match in a simultaneous-mode round shares one "Betting is open on
# N matches" / "Betting is now closed" message pair (see
# _openConcurrentTournamentBetting/_concurrentBettingTimer), so every row
# for that round gets the SAME value here instead of each match tracking
# its own. Read back from whichever row, and deleted once the round's last
# match resolves. See _resolveTournamentMatch/_resolveLosersMatch/
# _resolveFinalsMatch. NULL for sequential mode, which has no round-wide
# betting message at all.
ensure_column("tournament_matches", "roundBettingMessageId", "INTEGER")
ensure_column("tournament_matches", "roundBettingClosedMessageId", "INTEGER")
# Wagers on one specific tournament match. Unlike `wagers` above (one bet
# per user per guild, tied to whichever single casual/ranked game or
# sequential-mode tournament match is currently active), simultaneous-mode
# tournament rounds can have several matches open at once. So bets here
# are scoped per matchId instead: one bet per user per match, not per
# guild.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS tournament_wagers("
    "matchId, guildId, userId, username, team, amount, "
    "PRIMARY KEY(matchId, userId))"
)
# /setup: each role a player has explicitly said they like or dislike
# playing. `role` is one of SETUP_ROLE_NAMES, `preference` is 'like' or
# 'dislike'. The PRIMARY KEY includes `role` (not `preference`), so a role
# can only ever have one stored preference per player at a time. A role
# picked in both the liked and disliked steps of the same /setup run never
# reaches this table at all. It's left out of both sides instead, treated
# as neutral (see helper.py's _confirmSetupRoleStep). So nothing here has
# to resolve that contradiction. This is meant to feed a future role-aware
# elo balance. For now it's only read to check that everyone in the voice
# channel has run /setup at least once, gating /make-teams' use_roles.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS player_role_preferences("
    "guildId, userId, role, preference, PRIMARY KEY(guildId, userId, role))"
)
# /setup's own in-progress role picker: one row per posted message while
# the caller is still toggling and confirming choices, deleted once they
# finish (or the view times out). `step` is 'liked' or 'disliked',
# tracking which round is currently live. `selectedRoles` is a
# comma-separated snapshot of whichever roles are currently toggled on for
# that round, kept in sync by _handleSetupRoleToggleClick as the caller
# presses each role's button. `likedRoles` only gets filled in once the
# liked round is confirmed, carrying that finished set forward so the
# disliked round's own confirm step can check both sets against each
# other.
cursor.execute(
    "CREATE TABLE IF NOT EXISTS setup_role_sessions("
    "messageId INTEGER PRIMARY KEY, guildId, userId, step, selectedRoles, likedRoles)"
)
mainDB.commit()

helperObj = helper.helpers(cursor, mainDB)

# Role index -> display label, used wherever a roster needs to show which
# of the 5 roles a player was assigned.
roles = {
    0: "Top - ",
    1: "Jungle - ",
    2: "Mid - ",
    3: "Bottom - ",
    4: "Support - "
}

# A shared "who, where" suffix for both the call and completion log lines
# below. DM interactions have no guild, so that case is spelled out
# explicitly instead of crashing on interaction.guild.name.
def _interactionLogContext(interaction):
    guild = interaction.guild
    guild_desc = f"{guild.name} ({guild.id})" if guild is not None else "DM"
    return f"user={interaction.user} ({interaction.user.id}) guild={guild_desc}"


# A best-effort snapshot of the interaction an error was raised from,
# appended to on_app_command_error's log line. This makes a failure
# diagnosable from the log alone (which command, with what parameters, for
# whom), without needing to reproduce it live. interaction.command and
# .namespace run the same real discord.py option-resolution machinery that
# LoggingCommandTree.interaction_check below has to guard against, so this
# is wrapped in a try/except the same way, to make sure a failure while
# building this dump never swallows the real error it was meant to add
# context to.
def _errorVariableDump(interaction):
    try:
        command = interaction.command
        name = command.qualified_name if command is not None else interaction.data.get("name", "?")
        params = dict(interaction.namespace) if command is not None else {}
    except Exception:
        name, params = "?", "<unresolvable>"
    return f"command=/{name} params={params} {_interactionLogContext(interaction)}"


# Logs every real command invocation (name, params, who, where) in one
# place, instead of adding logging to each of the roughly 40 @tree.command
# functions individually. interaction_check is a global hook that
# discord.py's own CommandTree._call runs before dispatching any
# application command in the tree. interaction.command and .namespace are
# already resolved and cached by this point (see discord.py's Interaction
# class), so both are available here even though the tree hasn't actually
# invoked the command yet. Only real command invocations get logged, not
# every keystroke into an autocomplete field: interaction_check also fires
# for those (they're the same InteractionType family), but
# InteractionType.application_command excludes them. The default
# implementation this overrides just returns True unconditionally, so
# always returning True here preserves that behavior and never blocks
# anything. Per-command checks (like /clear elo's has_permissions) still
# run separately afterward, unaffected by this.
#
# A past production bug lives here as a cautionary note: interaction.
# command and .namespace run discord.py's own real option-resolution code
# (Namespace.__init__ in particular reads each option's fields directly,
# not through .get()), and nothing in tests.py can faithfully exercise
# that. Every test here goes through a plain FakeInteraction with no real
# payload to resolve. Worse, CommandTree._from_interaction's own wrapper
# only catches AppCommandError around the whole dispatch, so any other
# exception raised in here (a logging-only path that has no real reason to
# ever fail) went uncaught and silently killed the interaction before the
# command it was meant to observe ever ran. Discord showed "This
# interaction failed" and nothing reached on_app_command_error or this
# file's own log at all. The try/except around the whole body below,
# always returning True either way, is what makes this hook unable to
# take down the very feature it's supposed to be watching.
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
# _DraftPickSlotButton's callback (a DynamicItem, not a plain view button)
# can't receive helperObj through a constructor the way every other view
# does, because discord.py reconstructs it straight from a matched
# custom_id after a restart. It reaches back through
# interaction.client.helperObj instead.
client.helperObj = helperObj

# Pure personalization: the bot's Discord status cycles through these
# instead of sitting on one fixed line. Recalled from memory rather than
# pulled from a script, so treat the exact wording as close, not
# necessarily verbatim, if that ever matters.
ORIANNA_QUOTES = [
    "Winding...",
    "Precision is everything.",
    "Tick, tock, tick, tock...",
    "Command: Shockwave.",
]
_orianna_quote_cycle = itertools.cycle(ORIANNA_QUOTES)


# Runs immediately on .start(), then every 30 minutes after. This is
# purely cosmetic, so there's no reason to update any faster than that.
# The try/except is deliberate: a presence update can fail if the
# client's connection isn't fully settled yet (e.g. right after a
# reconnect, or in tests, where the Client was never actually connected at
# all). Since this is only cosmetic, there's nothing worth doing beyond
# letting the next scheduled tick retry.
@tasks.loop(minutes=30)
async def rotateStatus():
    try:
        await client.change_presence(activity=discord.Game(name=next(_orianna_quote_cycle)))
    except Exception:
        logger.debug("Presence update skipped, connection not settled yet.", exc_info=True)


# Snapshots main.db into BACKUP_DIR and prunes anything older than
# BACKUP_RETENTION_DAYS. Uses sqlite3's own backup() API instead of a
# plain file copy, since main.db is a live connection that other code can
# be reading and writing between event loop ticks. Copying the raw file
# risks capturing it mid-write. backup() takes a proper point-in-time
# snapshot instead. This runs directly on the event loop rather than a
# separate thread: mainDB was opened with the default
# check_same_thread=True, so handing it to a different thread (e.g. via
# asyncio.to_thread) would raise outright. A database at this scale
# (around 100KB) backs up in well under the time a trading-card render
# already blocks the loop for.
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


# Runs immediately on .start(), so an on_ready right after boot always
# gets a fresh snapshot, then every 24 hours after.
@tasks.loop(hours=24)
async def backupDatabaseTask():
    try:
        _backupDatabase()
        logger.info("Database backup completed.")
    except Exception:
        logger.exception("Database backup failed.")


# Every @tree.command below is registered with no guild= at all, which
# makes them "global" command *definitions*. copy_global_to() plus a
# guild-scoped sync() is what actually publishes them to a specific
# server. Doing it per-guild instead of one global tree.sync() is what
# keeps registration instant: a real global sync can take up to an hour to
# show up for users.
async def syncCommandsToGuild(guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


# The only place that inserts a `servers` row. It checks first and only
# inserts if the row is missing, so it's safe to call from on_ready too,
# self-healing any guild whose row never got created, or was lost to a
# wiped or restored database, without ever creating a duplicate for a
# guild that already has one (servers.guildId has no UNIQUE constraint to
# lean on INSERT OR IGNORE for). The positional INSERT below has to supply
# a value for every column on `servers`, including the roster_* ones added
# later via ensure_column above. If that count ever falls out of sync with
# the table's actual column count, this throws sqlite3.OperationalError.
def ensure_guild_row(guild_id, guild_name):
    cursor.execute("SELECT 1 FROM servers WHERE guildId=?", (guild_id,))
    if cursor.fetchone() is not None:
        return
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0, NULL, NULL, ?, "
        "NULL, NULL, NULL, 0, NULL, NULL, NULL, 0, NULL, 0, NULL, NULL, NULL, 0, 0, NULL, 1)",
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


# Persistent views (every button has a fixed custom_id and timeout=None)
# need registering exactly once per process, so Discord keeps routing
# their clicks to this bot even across a restart or redeploy. on_ready can
# fire more than once (e.g. on reconnect), so this is guarded the same way
# rotateStatus.is_running() above guards the status-rotation task from
# starting twice.
_persistent_views_registered = False


def registerPersistentViews():
    global _persistent_views_registered
    if _persistent_views_registered:
        return
    client.add_dynamic_items(helper._DraftPickSlotButton)
    client.add_view(helper.CaptainsDraftPickView(helperObj))
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



# Companion to LoggingCommandTree.interaction_check's "Command called"
# line. discord.py dispatches this event itself (see CommandTree._call)
# only once a command has actually run to completion without raising, so
# this only ever logs a genuine success. It never logs a command that
# errored out (that's on_app_command_error below) or one that
# interaction_check rejected before it even ran.
@client.event
async def on_app_command_completion(interaction, command):
    # discord.py's own Client._run_event already keeps an exception here
    # from causing real harm (it gets routed to on_error instead, and this
    # only fires after the command it's about has already fully succeeded
    # and responded). Caught explicitly anyway, so a bug in this
    # logging-only path still reaches this file's own log instead of only
    # discord.py's default stderr-only on_error. Same reasoning as
    # interaction_check above.
    try:
        logger.info("Command completed: /%s | %s", command.qualified_name, _interactionLogContext(interaction))
    except Exception:
        logger.exception("on_app_command_completion logging failed")


# Catch-all for every slash command's errors. discord.py calls this after
# any command's own local .error handler runs too. CommandTree.
# _dispatch_error always calls both, not one or the other. See
# setBettingTimer_error/reportCorrectWinner_error/clearAll_error below.
# So this only needs to cover what those don't: commands with no local
# handler at all (most of them), and re-raised errors from the ones that
# do have one. Without this, an unhandled exception anywhere would just
# leave the user staring at "The application did not respond," while the
# real traceback only ever reached the console.
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

    # If a local .error handler already responded to this interaction
    # (e.g. the MissingPermissions branch above, handled first by
    # clearAll_error and similar), posting a second, generic message here
    # would just stack on top of the specific one they already got.
    if interaction.response.is_done():
        return

    try:
        await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# Commands


# One subcommand per setting, instead of a single command with 8 optional,
# manually-paired parameters (team1/team2, member/elo). Discord enforces a
# subcommand's own required parameters for you, so there's no way to
# submit half of a pair and only find out it's invalid after the fact. The
# tradeoff is that changing several settings at once now takes several
# calls instead of one combined "Updated X, Y, and Z." response.
# adminSetHelper still accepts every field. Each subcommand below just
# fills in its own.
setGroup = app_commands.Group(
    name="set",
    description="Admin: change server settings"
)


async def _setAdminPermissionError(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to change server settings."
        )
    else:
        raise error


@setGroup.command(
    name="channels",
    description="Admin: set the voice channel names teams get moved into"
)
@app_commands.describe(
    team1="Name for the first team's voice channel; created if it doesn't exist",
    team2="Name for the second team's voice channel; created if it doesn't exist",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setChannels(ctx, team1: str, team2: str):
    await helperObj.adminSetHelper(ctx, team1, team2, None, None, None, None, None, None)

setChannels.error(_setAdminPermissionError)


@setGroup.command(
    name="team-size",
    description="Admin: set how many players make up one side"
)
@app_commands.describe(size="Number of players per team")
@app_commands.checks.has_permissions(manage_guild=True)
async def setTeamSize(ctx, size: int):
    await helperObj.adminSetHelper(ctx, None, None, size, None, None, None, None, None)

setTeamSize.error(_setAdminPermissionError)


@setGroup.command(
    name="betting-timer",
    description="Admin: set how long a betting window stays open"
)
@app_commands.describe(
    seconds="Seconds a betting window stays open (1-600); multiplied per match for a concurrent tournament round"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setBettingTimer(ctx, seconds: int):
    await helperObj.adminSetHelper(ctx, None, None, None, seconds, None, None, None, None)

setBettingTimer.error(_setAdminPermissionError)


@setGroup.command(
    name="wager-channel",
    description="Admin: redirect every betting posting to one specific text channel"
)
@app_commands.describe(channel="Name of the text channel; created if it doesn't exist")
@app_commands.checks.has_permissions(manage_guild=True)
async def setWagerChannel(ctx, channel: str):
    await helperObj.adminSetHelper(ctx, None, None, None, None, channel, None, None, None)

setWagerChannel.error(_setAdminPermissionError)


@setGroup.command(
    name="elo",
    description="Admin: set a specific player's elo directly to an exact value"
)
@app_commands.describe(member="Whose elo to set", elo="The exact elo value to set them to")
@app_commands.checks.has_permissions(manage_guild=True)
async def setElo(ctx, member: discord.Member, elo: int):
    await helperObj.adminSetHelper(ctx, None, None, None, None, None, member, elo, None)

setElo.error(_setAdminPermissionError)


@setGroup.command(
    name="default-elo",
    description="Admin: set what a brand new player in this server starts at"
)
@app_commands.describe(elo="Elo a brand new player starts at (default 1000); doesn't change existing players")
@app_commands.checks.has_permissions(manage_guild=True)
async def setDefaultElo(ctx, elo: int):
    await helperObj.adminSetHelper(ctx, None, None, None, None, None, None, None, elo)

setDefaultElo.error(_setAdminPermissionError)


@setGroup.command(
    name="correct-winner",
    description="Admin: fix a misreported winner, or invalidate the last game entirely"
)
@app_commands.describe(
    team="The team that actually won; omit if invalidating instead",
    match_id="Optional: correct a specific tournament match instead of the last game",
    invalidate="Undo the last game entirely instead of picking a winner: refunds bets, undoes elo/records/gold",
)
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def setCorrectWinner(
    ctx, team: app_commands.Choice[int] = None, match_id: int = None, invalidate: bool = False
):
    team_value = team.value if team is not None else None
    await helperObj.reportCorrectWinnerHelper(ctx, team_value, match_id, invalidate)

setCorrectWinner.error(_setAdminPermissionError)


@setGroup.command(
    name="roster-permissions",
    description="Admin: restrict Start/Random Roles/Balanced Roles to rostered players and admins"
)
@app_commands.describe(
    strict="True: only a rostered player or Manage Server admin can use the roster buttons. "
           "False: anyone who can see the message can (the default)."
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setRosterPermissions(ctx, strict: bool):
    await helperObj.setRosterPermissionsHelper(ctx, strict)

setRosterPermissions.error(_setAdminPermissionError)


@setGroup.command(
    name="max-wager",
    description="Admin: cap how much gold a single /wager can be"
)
@app_commands.describe(amount="Max gold for a single wager; omit to remove the cap")
@app_commands.checks.has_permissions(manage_guild=True)
async def setMaxWager(ctx, amount: int = None):
    await helperObj.setMaxWagerHelper(ctx, amount)

setMaxWager.error(_setAdminPermissionError)


@setGroup.command(
    name="betting",
    description="Admin: turn /wager team and /wager against on or off for this server"
)
@app_commands.describe(enabled="True: bets are accepted (the default). False: /wager rejects outright.")
@app_commands.checks.has_permissions(manage_guild=True)
async def setBetting(ctx, enabled: bool):
    await helperObj.setBettingHelper(ctx, enabled)

setBetting.error(_setAdminPermissionError)


tree.add_command(setGroup)


# /wager and /wager-against are grouped under one /wager command instead
# of two separate top-level ones, so typing /wager surfaces both the
# team-game bet and the 1-on-1 challenge together.
wagerGroup = app_commands.Group(
    name="wager",
    description="Wager gold on a team game, or challenge another player 1-on-1"
)


# team is free text with autocomplete, not a static Choice, so the picker
# can show the roster's own real team names instead of a generic "Team 1"
# / "Team 2" disconnected from what's actually on screen. This always
# suggests the current game's own names, not whichever match_id the
# caller might also be filling in on the same command: discord.py's real
# option-resolution for interaction.namespace can't be faithfully
# exercised by this file's own FakeInteraction-based tests (see the
# production-bug note on LoggingCommandTree above), and match_id comes
# after team in the command's own parameter order anyway, so it's rarely
# even set yet at this point. wagerHelper's own server-side resolution
# still matches against the real match_id's team names when one is given.
# This autocomplete only builds the suggestion list.
async def wagerTeamAutocomplete(ctx, current: str):
    name1, name2 = helperObj.getWagerTeamNames(ctx.guild.id)
    current = current.lower()
    choices = [
        app_commands.Choice(name=name1, value="1"),
        app_commands.Choice(name=name2, value="2"),
    ]
    return [c for c in choices if current in c.name.lower()]


@wagerGroup.command(
    name="team",
    description="Wager gold on the current game, or on one tournament match if you give a match id"
)
@app_commands.describe(
    amount="Amount of gold to wager", team="Which team you think will win",
    match_id="A match's id, shown as \"Match #N\" in the bracket; omit to bet on the current game instead"
)
@app_commands.autocomplete(team=wagerTeamAutocomplete)
async def wagerTeam(ctx, amount: int, team: str, match_id: int = None):
    name1, name2 = helperObj.getWagerTeamNames(ctx.guild.id, match_id)
    team_value = helperObj.resolveWagerTeamValue(team, name1, name2)
    if team_value is None:
        await ctx.response.send_message(
            f"Couldn't tell which team **{team}** is. Pick **{name1}** or **{name2}**.", ephemeral=True
        )
        return
    await helperObj.wagerHelper(ctx, amount, team_value, match_id)


@wagerGroup.command(
    name="against",
    description="Challenge another player to a heads-up gold wager"
)
@app_commands.describe(member="Who to challenge", amount="How much gold is on the line")
async def wagerAgainst(ctx, member: discord.Member, amount: int):
    await helperObj.challengeDuelHelper(ctx, member, amount)


tree.add_command(wagerGroup)


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
@app_commands.describe(member="Whose stats to look up; defaults to you")
async def stats(ctx, member: discord.Member = None):
    await helperObj.statsHelper(ctx, member)


# Only the caller's own available titles (CARD_DEFAULT_TITLE plus
# whatever they've unlocked; see getAvailableCardTitles). Unlike
# logoAutocomplete's static list below, this one depends on who's typing.
async def cardTitleAutocomplete(ctx, current: str):
    current = current.lower()
    titles = helperObj.getAvailableCardTitles(ctx.guild.id, ctx.user.id)
    matches = [t for t in titles if current in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


# Same shape as cardTitleAutocomplete above: only the caller's own
# available schemes (CARD_DEFAULT_SCHEME_NAME plus whatever they've
# unlocked).
async def cardColorSchemeAutocomplete(ctx, current: str):
    current = current.lower()
    schemes = helperObj.getAvailableCardColorSchemes(ctx.guild.id, ctx.user.id)
    matches = [s["name"] for s in schemes if current in s["name"].lower()]
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


# Same shape as cardTitleAutocomplete/cardColorSchemeAutocomplete above:
# only the caller's own available font styles.
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
    title="Which title to equip, from your unlocked ones",
    color_scheme="Which color scheme to equip, from your unlocked ones",
    font_style="Which font to equip, from your unlocked ones",
)
@app_commands.autocomplete(
    title=cardTitleAutocomplete, color_scheme=cardColorSchemeAutocomplete, font_style=cardFontAutocomplete
)
async def cardSet(ctx, title: str = None, color_scheme: str = None, font_style: str = None):
    await helperObj.cardSetHelper(ctx, title, color_scheme, font_style)


# /preview, /shop, and /shop-buy are grouped under one /shop command
# instead of three separate top-level ones, so typing /shop surfaces
# browsing, previewing, and buying together.
shopGroup = app_commands.Group(
    name="shop",
    description="Browse, preview, or buy trading-card cosmetics with gold"
)


@shopGroup.command(
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
async def shopPreview(ctx, type: app_commands.Choice[str]):
    await helperObj.previewHelper(ctx, type.value)


@shopGroup.command(
    name="browse",
    description="Browse trading-card cosmetics purchasable with gold"
)
async def shopBrowse(ctx):
    await helperObj.shopHelper(ctx)


@tree.command(
    name="achievements",
    description="Browse gameplay achievements and their trading-card title rewards"
)
async def achievements(ctx):
    await helperObj.achievementsHelper(ctx)


# Unlike the three cardXAutocomplete functions above, which only ever
# offer what's already unlocked, this one offers what's still buyable.
# getShopCatalog's own "owned" flag filters out anything already
# purchased, so /shop buy never suggests something there's nothing left to
# do with.
async def shopBuyAutocomplete(ctx, current: str):
    current = current.lower()
    catalog = helperObj.getShopCatalog(ctx.guild.id, ctx.user.id)
    matches = [i for i in catalog if not i["owned"] and current in i["name"].lower()]
    return [app_commands.Choice(name=f"{i['name']} ({i['price']} gold)", value=i["name"]) for i in matches[:25]]


@shopGroup.command(
    name="buy",
    description="Purchase a trading-card cosmetic with gold"
)
@app_commands.describe(item="Which item to purchase, from what you don't already own")
@app_commands.autocomplete(item=shopBuyAutocomplete)
async def shopBuy(ctx, item: str):
    await helperObj.shopBuyHelper(ctx, item)


tree.add_command(shopGroup)


@tree.command(
    name="leaderboard",
    description="Rank the server by a stat; buttons to page through it"
)
@app_commands.describe(
    filter="Which stat to rank by; omit for an overview of elo, balance, and record",
    order="Highest-first or lowest-first; defaults to highest-first",
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
async def leaderboard(ctx, filter: app_commands.Choice[str] = None, order: app_commands.Choice[str] = None):
    stat = filter.value if filter is not None else None
    sort_order = order.value if order is not None else "desc"
    await helperObj.leaderboardHelper(ctx, stat, sort_order)


SITE_COMMANDS_URL = "https://addshockwave.com/commands.html"

# Short descriptions for /help <command>. Kept separate from each
# command's `description=` (what Discord's own command picker shows),
# since that has to stay short enough to fit there. This can afford a real
# sentence or two, closer to what commands.html says.
COMMAND_HELP = {
    "set channels": "Sets the voice channel names teams get moved into (creates them if missing). Requires the Manage Server permission.",
    "set team-size": "Sets how many players make up one side. Requires the Manage Server permission.",
    "set betting-timer": "Sets how long a betting window stays open (1-600 seconds, multiplied by the number of matches for a concurrent tournament round). Requires the Manage Server permission.",
    "set wager-channel": "Redirects every betting posting to one specific text channel (created if it doesn't exist). Requires the Manage Server permission.",
    "set elo": "Sets a player's elo directly to an exact value (still credits any Diamond+ tier reward the new elo qualifies for). Requires the Manage Server permission.",
    "set default-elo": "Sets what a brand new player in this server starts at (1000 by default); doesn't touch anyone's existing elo, use /clear elo to reset current players to it. Requires the Manage Server permission.",
    "set correct-winner": "Fixes a misreported winner: undoes and reapplies the payouts, records, and elo. invalidate undoes the last game entirely instead (bets refunded, nothing reapplied), as if it never happened. Requires Manage Server.",
    "set roster-permissions": "Controls who can use the Start/Start (no move)/Random Roles/Balanced Roles buttons on a posted roster. strict:true restricts them to a rostered player or a Manage Server admin, matching how the winner-report buttons already work; strict:false (the default) leaves them open to anyone who can see the message. Requires the Manage Server permission.",
    "set max-wager": "Caps how much gold a single /wager team or /wager against bet can be. Omit amount to remove the cap. Requires the Manage Server permission.",
    "set betting": "Turns /wager team and /wager against on or off for this server. Games, elo, and reporting a winner all still work the same either way; this only gates the wagering layer on top of them. Requires the Manage Server permission.",
    "clear teams": "Wipes the current teams/draft so you can start a fresh session. Requires the Manage Server permission.",
    "clear channels": "Wipes the current teams/draft, and also forgets the saved team channel names. Requires the Manage Server permission.",
    "clear tournament": "Wipes the current teams/draft, and deletes this server's tournament entirely: bracket, registrations, match history. Can't be undone. Requires the Manage Server permission.",
    "clear elo": "Wipes the current teams/draft, and resets every player's elo back to this server's default elo. Confirmation required. Requires the Manage Server permission.",
    "clear economy": "Wipes the current teams/draft, and resets every player's balance/elo/record/gold entirely for this server. Confirmation required. Requires the Manage Server permission.",
    "clear achievements": "Wipes the current teams/draft, and resets earned achievements for every player, or just one player if user is set. Confirmation required. Requires the Manage Server permission.",
    "clear card-unlocks": "Wipes the current teams/draft, and resets trading-card unlocks for every player, or just one player if user is set. Confirmation required. Requires the Manage Server permission.",
    "make-teams random": "Randomly splits everyone in your voice channel into two even teams and posts the roster, with a Start button on it to move everyone and open betting when you're ready (Start (no move) to open betting without moving anyone). If both teams land at exactly 5 players, Random Roles and Balanced Roles buttons also appear: Random Roles shuffles who's shown in which of Top/Jungle/Mid/Bottom/Support, while Balanced Roles assigns them by elo + each player's liked roles from /setup, the same logic ranked roles uses, without moving anyone between teams. ranked:true forms roughly elo-balanced teams instead, and tracks elo once a winner is reported. Combine ranked:true with use_roles:true (10 players only) to have the roster already show Top/Jungle/Mid/Bottom/Support the moment it posts, nudging the split toward whichever side is more balanced once roles are considered.",
    "make-teams draft": "Starts a live captain draft. Name two captains, or use_random to pick two automatically; everyone else lands in a pool the captains draft from using the buttons on the posted picker (blue for Team 1's turn, red for Team 2's, plus a Random pick and paging once the pool is too big for one page). Once both teams are set, press Start on the roster to move everyone and open betting, or Start (no move) to open betting without moving anyone. ranked:true tracks elo for the resulting game. snake:true reverses pick order every 2 picks (1,2,2,1,1,2,...) instead of alternating every single pick, so neither captain always drafts right after seeing the other's pick.",
    "make-teams saved": "Loads two persistent teams straight into a casual or ranked game, skipping the random-split-or-draft step. Posts a roster with the same Start/Start (no move) buttons as /make-teams random to start it.",
    "make-teams repeat": "Re-posts the exact same two teams from whichever of /make-teams random, /make-teams draft, or /make-teams saved ran last, instead of drawing a fresh random split or captains draft. Stays ranked if the last game was ranked, casual if it was casual. Cancels an actively in-progress game from those same teams first (refund + move back) if there is one.",
    "test-image": "Admin/dev tool: renders the matchup graphic (with role icons) against two dummy 5-player teams and posts it, so you can preview image changes without needing a real 10-player roster. Requires the Manage Server permission.",
    "notify": "DMs a one-time invite link to your voice channel, to one member, or to everyone holding a given role. message optionally replaces the default invite text; either way it's signed \"Sent by\" you. You must be sitting in a voice channel yourself to run this.",
    "wager team": "Bets gold on one team winning the current game, or on a specific tournament match if you give a match id. Only while betting is open, one bet per player per game/match.",
    "wager against": "Challenges another player to a heads-up gold wager, separate from team-game betting, no active game required.",
    "daily": "Claims 1000 free gold. Once per calendar day, per player.",
    "stats": "Shows a player's elo, ranked/casual/game record, betting record, balance, and net gold; defaults to you. Press Avatar to toggle the shown avatar between this server's own profile picture and their regular account-wide one, or Card to replace the whole embed with a customizable trading card; Back swaps back.",
    "card-set": "Equips your unlocked trading-card title, color scheme, and/or font in one go (see /stats' Card button); set any combination of the three at once. Reaching Diamond, Master, Grandmaster, or Challenger permanently unlocks that tier's own title and scheme, even if you derank afterward; \"Default\" is always available for both. Fonts are purchased from /shop buy.",
    "shop preview": "Shows every option for one customization type (Logos, Card Titles, Color Schemes, or Fonts) in a single gallery image (a few images only if there are too many to fit), regardless of what you've personally unlocked yet.",
    "shop browse": "Browse every trading-card title, color scheme, and font purchasable with gold, with a ✅ next to anything you already own. Sort: Price / Sort: Owned buttons under the listing re-sort each category (Ascending/Descending toggle which way) without needing to re-run the command.",
    "achievements": "Browse every gameplay achievement, what it takes to earn it, and whether you already have. Earning one unlocks its title for /card-set and posts a one-time announcement in the channel.",
    "shop buy": "Purchases a trading-card cosmetic with gold, permanently unlocking it for /card-set. Refuses if you already own it or can't afford it.",
    "leaderboard": "Ranks the server by a stat, including ranked-only and casual-only wins/losses/win rate. Omit filter for an elo-sorted overview. Players with a 0W-0L record in the selected stat's category (or who've never played a game at all, for the overview and elo views) are left off, so a currency-based stat like balance still shows everyone. Buttons page through the results, and Ascending/Descending buttons flip the sort direction without re-running the command. Press Cards to flip through each player's full stats card one at a time instead, same as /team lookup but ranked; a Card button on that view swaps the current player's stats card for their actual trading card, and List brings you back to the ranked list.",
    "team create": "Creates a persistent team with you as its captain, or captain as its captain if given.",
    "team save": "Saves Team 1 or Team 2 from the last game in this server as a new persistent team, with you as its captain. You must have actually been rostered on that side to save it, and the new name can't already belong to another team here.",
    "team set": "Sets a persistent team's voice channel and/or logo, any combination in one call. new_voice_channel creates a fresh one named after the team. The team's captain, or anyone with Manage Server, can do this.",
    "team rename": "Renames a persistent team. The new name can't already belong to another team in this server. The team's captain, or anyone with Manage Server, can do this.",
    "team delete": "Deletes a persistent team: its roster, record, and any pending invites go with it. The team's captain, or anyone with Manage Server, can do this; confirmation required. Doesn't affect a tournament it's already registered in.",
    "team transfer": "Hands off a persistent team's captaincy to another player already on its roster. The team's captain, or anyone with Manage Server, can do this.",
    "team invite": "Invites one or more members (up to 5 per call) to a team. Each invitee must accept before joining. The team's captain, or anyone with Manage Server, can do this. force (Manage Server only) skips the invitee's confirmation and adds them straight to the roster.",
    "team leave": "Removes you from a persistent team's roster. Anyone rostered can do this to themselves, no permission needed, except the team's captain, who has to use /team delete instead since there's no one to hand the captaincy to.",
    "team lookup": "Lists the teams you're a rostered player on in this server, with paging to flip through each one's full stats card.",
    "team stats": "Shows a persistent team's captain, roster, voice channel, and win/loss record. Press Card to swap it for a team card: its logo as the focal point, colors sampled from that logo, captain/roster/record/win rate. Back swaps back.",
    "team list": "Browse every team in the server with filtering (name search, recruiting-only, up to 5 members who all have to be on the roster) and sorting (name, wins, losses, win rate, roster size; sort:\"Win Rate\" order:\"Descending\" to rank teams by win rate). Buttons page through it. cards:true flips through each team's full stats card one at a time instead, same as /team lookup but for every team in the server; a Card button on that view swaps the current team's stats card for its actual trading card, and stays selected as you keep paging.",
    "tournament create": "Creates an empty tournament shell for this server: name, team size, and bracket size. One tournament per server.",
    "tournament register": "Registers one of your teams for the server's tournament. The team's captain, or anyone with Manage Server, can do this.",
    "tournament create-bracket": "Builds the tournament bracket from whichever teams are currently registered, seeded randomly. Rerunning it rerolls the bracket. For double elimination, losers_bracket_timing picks whether the losers bracket waits for the whole winners bracket to finish, or interleaves as each round unlocks.",
    "tournament print-bracket": "Prints the current bracket, with each match's id for use with /wager team and /set correct-winner.",
    "tournament start": "Starts playing the current round of the bracket. mode is Sequential (one match at a time) or Simultaneous (all at once, no betting).",
    "roll": "Rolls a random number between 1 and num.",
    "setup": "Introduces Shockwave, creates your personal solo team, and walks you through picking which roles you like/dislike playing (press a role to toggle it, then press Confirm) for future role-aware team balancing. solo_team_name is always optional; omit it the first time and your solo team is named after your current server display name. Run it any time afterward to update either. Unlocks the Onboarded achievement the first time.",
    "help": "Shows this message, or details on one command.",
}


@tree.command(
    name="setup",
    description="Get started with Shockwave: your solo team and role preferences"
)
@app_commands.describe(
    solo_team_name="Name for your personal solo team; always optional, defaults to your display name",
)
async def setup(ctx, solo_team_name: str = None):
    await helperObj.setupHelper(ctx, solo_team_name)


async def helpCommandAutocomplete(ctx, current: str):
    current = current.lower()
    names = [n for n in COMMAND_HELP if current in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# COMMAND_HELP is keyed by qualified name: "make-teams random" for a
# group's subcommand (the same shape app_commands.Command.qualified_name
# already produces), or plain "wager" for a command with no group. This
# walks the tree the same way to resolve a name back to its live Command
# object, so the usage line below can pull in the real parameter names and
# whether each one is required.
def _resolveCommand(name):
    parts = name.split(" ")
    command = discord.utils.get(tree.get_commands(), name=parts[0])
    for part in parts[1:]:
        if command is None or not hasattr(command, "commands"):
            return None
        command = discord.utils.get(command.commands, name=part)
    return command


@tree.command(
    name="help",
    description="Get a list of commands, or details on a specific one"
)
@app_commands.describe(command="Command name to look up; omit for the full list on the site")
@app_commands.autocomplete(command=helpCommandAutocomplete)
async def help(ctx, command: str = None):
    if command is None:
        await ctx.response.send_message(
            f"Full command list: {SITE_COMMANDS_URL}\nNew here? Run /setup first."
        )
        return

    name = command.strip().lstrip("/").lower()
    description = COMMAND_HELP.get(name)
    if description is None:
        await ctx.response.send_message(
            f"Don't recognize `/{name}`. Full command list: {SITE_COMMANDS_URL}"
        )
        return

    registered = _resolveCommand(name)
    usage = f"/{name}"
    if registered is not None:
        for param in registered.parameters:
            token = param.display_name if param.required else f"{param.display_name}?"
            usage += f" <{token}>"

    await ctx.response.send_message(f"**{usage}**\n{description}")


# All four ways to form a game's two teams (random split, captain draft,
# loading two saved persistent teams, replaying the last game's teams) are
# grouped under one /make-teams command instead of four separate top-level
# ones, so typing /make-teams surfaces all of them together. This
# replaced what used to be separate /captains, /team-use, and /reuse
# commands, which a user had to discover existed on their own.
makeTeamsGroup = app_commands.Group(
    name="make-teams",
    description="Form a game's two teams: random split, captain draft, saved teams, or a repeat"
)


@makeTeamsGroup.command(
    name="random",
    description="Create teams randomly, or roughly elo-balanced for a ranked game"
)
@app_commands.describe(
    use_roles="Assign Top/Jungle/Mid/Bottom/Support roles (5-player teams only); also works with ranked",
    ranked="Form roughly elo-balanced teams from your voice channel and track elo for this game",
)
async def makeTeamsRandom(ctx, use_roles: bool = False, ranked: bool = False):
    if ctx.user.voice is None or ctx.user.voice.channel is None:
        await ctx.response.send_message(
            "You need to be in a voice channel to form teams from it. Join one and try again."
        )
        return

    # getRolePreferences already treats a player with no submitted rows as
    # neutral on every role (see _roleTier), the same tier as someone who
    # ran /setup and genuinely marked every role neutral, so role-aware
    # team formation doesn't actually need everyone to have run /setup
    # first - it just balances a little worse for whoever hasn't, the same
    # as it would for anyone else sitting neutral on everything. Rather
    # than blocking the whole command over it (locking role-based
    # matchmaking behind every single voice-channel member's own
    # /setup), whoever's missing it is just named in the response so it's
    # clear why they weren't weighted toward a liked lane. Bots can't run
    # /setup, so they're excluded from the check entirely.
    not_setup_note = None
    if use_roles:
        not_setup = [
            member for member in ctx.user.voice.channel.members
            if not member.bot and not helperObj.hasCompletedSetup(ctx.guild.id, member.id)
        ]
        if not_setup:
            mentions = ", ".join(member.mention for member in not_setup)
            verb = "hasn't" if len(not_setup) == 1 else "haven't"
            not_setup_note = f"{mentions} {verb} run /setup, so they'll be treated as having no role preference."

    if ranked:
        # rankedTeamHelper handles its own response and team embeds, since
        # computing elo averages needs per-player lookups it already has
        # to do anyway. That makes this a completely separate flow from
        # the random split below, where bot.py builds the response itself.
        await helperObj.rankedTeamHelper(ctx, use_roles, not_setup_note=not_setup_note)
        return

    # `use_roles` is named that way, not `roles`, to avoid shadowing the
    # module-level `roles` dict above. It's already a bool from the slash
    # command's type annotation, so comparing it to the string 'True' would
    # always be False.
    #
    # This command only announces the teams. It used to optionally move
    # everyone immediately, via a `movevar` flag, but moving players and
    # opening betting now only happens once the posted roster's own Start
    # button is clicked (see _finalizeRoster). That way a roster can be
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
    intro_messages = [await ctx.original_response()]

    # Roles (Top/Jungle/Mid/Bottom/Support) only make sense for a 5-player
    # team. makeEmbedString() silently falls back to a plain roster for any
    # other size, and _finalizeRoster silently skips the Random
    # Roles/Balanced Roles buttons for the same reason. This message
    # explains that instead of leaving people wondering where the roles
    # went.
    if use_roles:
        unroled = [
            f"{team.get_name()} ({len(team.get_players())} players)"
            for team in (team1Obj, team2Obj)
            if len(team.get_players()) != 5
        ]
        if unroled:
            intro_messages.append(await ctx.channel.send(
                "Roles need exactly 5 players on a team to assign, so no roles were "
                f"assigned for: {', '.join(unroled)}. Showing the roster as normal instead."
            ))
        elif not_setup_note:
            # Only relevant once roles actually got assigned (both teams
            # landed at 5); the unroled case above already explains why
            # nobody's preferences mattered this time.
            intro_messages.append(await ctx.channel.send(not_setup_note))

    team1_message, team2_message = await helperObj.printEmbed(ctx, team1Obj, team2Obj, useRoles=use_roles)

    # Posted last, after the rosters, in bold, instead of folded into the
    # very first response message. That first message is easy to scroll
    # past once the much bigger roster embeds land right after it, so this
    # puts the call to action where people are actually looking once
    # they're done reading the teams.
    intro_messages.append(await ctx.channel.send(
        "📣 **Ready?** Press Start on the roster above to move everyone into their channels and open "
        "betting, or Start (no move) to open betting without moving anyone."
    ))
    await helperObj._finalizeRoster(
        ctx.guild.id, team1_message, team2_message, team1Obj, team2Obj, use_roles,
        intro_messages=intro_messages,
    )


@makeTeamsGroup.command(
    name="draft",
    description="Start a live captain draft"
)
@app_commands.describe(
    captain_1="First captain; required unless use_random is set",
    captain_2="Second captain; required unless use_random is set",
    use_random="Pick two captains at random from the voice channel instead",
    ranked="Track elo for this game; defaults to casual",
    snake="Snake draft: reverse pick order every 2 picks (1,2,2,1,1,2,...) instead of alternating every pick",
)
async def makeTeamsDraft(
    ctx, captain_1: discord.Member = None, captain_2: discord.Member = None,
    use_random: bool = False, ranked: bool = False, snake: bool = False,
):
    await startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked=ranked, snake=snake)


# Shared by /make-teams draft regardless of its ranked flag. The draft
# flow itself is identical either way. The only difference is whether the
# resulting game is marked ranked, which captainsHelper handles by setting
# is_ranked accordingly, gating whether recordResult later touches
# anyone's elo.
async def startCaptainsDraft(ctx, captain_1, captain_2, use_random, ranked, snake=False):
    # Named `use_random`, not `random`, since `random` would shadow the
    # `random` module imported at the top of this file.
    if ctx.user.voice is None or ctx.user.voice.channel is None:
        await ctx.response.send_message(
            "You need to be in a voice channel to start a captains draft. Join one and try again."
        )
        return

    if len(ctx.user.voice.channel.members) < 2:
        await ctx.response.send_message("Not enough players in the voice channel!")
        return

    if use_random:
        # sqlite3 can't bind a plain Python list as a query parameter, and
        # getRandomMember() needs each player's id, to look the Member back
        # up, not just their name. So this serializes into a Team, the
        # same convention every other "players" column write in this file
        # uses.
        players = Team()
        for player in ctx.user.voice.channel.members:
            players.add_player(Player(player.id, player.name))
        helperObj.update(ctx.guild.id, "players", players.serializeTeam())

        # This loops on "still None OR same as captain1", not "AND": a
        # value can never be both None and equal to a non-None captain1 at
        # the same time, so "AND" would never actually re-roll when the
        # two picks collide.
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

    await helperObj.captainsHelper(ctx, captain1, captain2, ranked=ranked, snake=snake)


# One subcommand per independent reset, instead of a single command with 6
# unrelated boolean flags where some execute instantly and others require
# a confirm click. That way a single /clear call can't end up doing part
# of its job immediately while leaving the rest pending on a button. Every
# subcommand still clears the current in-progress teams/draft first, the
# same "start a fresh session" base behavior every reset needs.
# /clear teams is that base behavior on its own, with nothing else
# attached.
clearGroup = app_commands.Group(
    name="clear",
    description="Admin: clear teams, tournament, or economy data"
)


async def _clearPermissionError(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to use /clear."
        )
    else:
        raise error


@clearGroup.command(
    name="teams",
    description="Admin: wipe the current teams/draft so you can start a fresh session (confirmation required)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearTeams(ctx):
    await helperObj.confirmClearActionHelper(
        ctx, "teams",
        "Clear the current teams/draft? Any in-progress game will be cancelled (refunded) first. "
        "This can't be undone.",
    )

clearTeams.error(_clearPermissionError)


@clearGroup.command(
    name="channels",
    description="Admin: wipe the current teams/draft, and forget the saved channel names too "
                 "(confirmation required)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearChannels(ctx):
    await helperObj.confirmClearActionHelper(
        ctx, "channels",
        "Clear the current teams/draft and forget the saved team channel names? This can't be undone.",
    )

clearChannels.error(_clearPermissionError)


@clearGroup.command(
    name="tournament",
    description="Admin: delete this server's tournament (bracket, registrations, history) "
                 "(confirmation required)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearTournament(ctx):
    await helperObj.confirmClearActionHelper(
        ctx, "tournament",
        "Delete this server's tournament entirely (bracket, registrations, match history)? This also "
        "clears the current teams/draft. This can't be undone.",
    )

clearTournament.error(_clearPermissionError)


@clearGroup.command(
    name="elo",
    description="Admin: reset every player's elo back to this server's default elo (confirmation required)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearElo(ctx):
    await helperObj.clearTeamsHelper(ctx)
    await helperObj.confirmDestructiveClearHelper(ctx, False, True, False, False, None)

clearElo.error(_clearPermissionError)


@clearGroup.command(
    name="economy",
    description="Admin: wipe every player's balance/elo/record/gold for this server (confirmation required)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def clearEconomy(ctx):
    await helperObj.clearTeamsHelper(ctx)
    await helperObj.confirmDestructiveClearHelper(ctx, True, False, False, False, None)

clearEconomy.error(_clearPermissionError)


@clearGroup.command(
    name="achievements",
    description="Admin: reset earned achievements, or just one player if user is set (confirmation required)"
)
@app_commands.describe(user="Only reset this player instead of everyone")
@app_commands.checks.has_permissions(manage_guild=True)
async def clearAchievements(ctx, user: discord.Member = None):
    await helperObj.clearTeamsHelper(ctx)
    await helperObj.confirmDestructiveClearHelper(ctx, False, False, True, False, user)

clearAchievements.error(_clearPermissionError)


@clearGroup.command(
    name="card-unlocks",
    description="Admin: wipe trading-card unlocks, or just one player if user is set (confirmation required)"
)
@app_commands.describe(user="Only reset this player instead of everyone")
@app_commands.checks.has_permissions(manage_guild=True)
async def clearCardUnlocks(ctx, user: discord.Member = None):
    await helperObj.clearTeamsHelper(ctx)
    await helperObj.confirmDestructiveClearHelper(ctx, False, False, False, True, user)

clearCardUnlocks.error(_clearPermissionError)


tree.add_command(clearGroup)


# Only the caller's own captained teams, for team-name params on commands
# that require being that team's captain. Same "only suggest what's
# actually usable" idea cardTitleAutocomplete uses for card unlocks, just
# scoped to captaincy (getTeamsCaptainedBy) instead. This doesn't stop
# someone from typing a different name by hand: Discord's autocomplete is
# a suggestion list, not a restriction, and the backing helpers still do
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
# only read a team instead of requiring captaincy of it (/team stats,
# /make-teams saved). Suggests every team the caller is rostered on at all
# (getTeamsForPlayer), captain or not. Same Manage Server carve-out as
# myCaptainedTeamAutocomplete: an admin sees every team in the guild here
# too, not just ones they're on.
async def myTeamAutocomplete(ctx, current: str):
    current = current.lower()
    if ctx.user.guild_permissions.manage_guild:
        teams = helperObj.getTeamsForGuild(ctx.guild.id)
    else:
        teams = helperObj.getTeamsForPlayer(ctx.guild.id, ctx.user.id)
    names = [team.get_name() for _team_id, team in teams if current in team.get_name().lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# /team stats works on any team in the guild by exact name. It's a lookup,
# not an action you need to be rostered for. But suggesting literally
# every team in a large server the instant the param is focused would bury
# the ones you actually belong to under everyone else's. So an empty box
# still suggests just your own teams (myTeamAutocomplete's own behavior,
# admin carve-out included). The moment you actually type something,
# that's a deliberate search, so this widens to every team in the guild
# that matches it, the same as /team list's own search would.
async def teamStatsAutocomplete(ctx, current: str):
    if not current:
        return await myTeamAutocomplete(ctx, current)
    current = current.lower()
    teams = helperObj.getTeamsForGuild(ctx.guild.id)
    names = [team.get_name() for _team_id, team in teams if current in team.get_name().lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# Every tournament command is grouped under one /tournament command
# instead of five separate top-level ones, so the full create, register,
# build bracket, start sequence shows up together the moment a user types
# /tournament, in roughly the order they'd actually run it.
tournamentGroup = app_commands.Group(
    name="tournament",
    description="Run this server's tournament: create, register teams, build the bracket, start"
)


@tournamentGroup.command(
    name="create",
    description="Create a tournament for this server"
)
@app_commands.describe(
    name="Tournament name",
    teamsize="Number of players per team",
    numteams="Number of teams the bracket holds",
    double_elim="Double elimination instead of single; defaults to single"
)
async def tournamentCreate(ctx, name: str, teamsize: int, numteams: int, double_elim: bool = False):
    await helperObj.createTournamentHelper(ctx, name, teamsize, numteams, double_elim)


@tournamentGroup.command(
    name="register",
    description="Register a team for this server's tournament"
)
@app_commands.describe(team="Name of the team to register")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def tournamentRegister(ctx, team: str):
    await helperObj.registerTeamHelper(ctx, team)


@tournamentGroup.command(
    name="create-bracket",
    description="Create (or reroll) the tournament bracket from registered teams"
)
@app_commands.describe(
    elimination_type="Single or double elimination for this tournament",
    losers_bracket_timing="Double elimination only: when the losers bracket plays; defaults to "
                           "after winners finishes"
)
@app_commands.choices(elimination_type=[
    app_commands.Choice(name="Single elimination", value="single"),
    app_commands.Choice(name="Double elimination", value="double"),
])
@app_commands.choices(losers_bracket_timing=[
    app_commands.Choice(name="After the winners bracket finishes entirely", value="after_winners"),
    app_commands.Choice(name="Interleaved, as soon as each round unlocks it", value="interleaved"),
])
async def tournamentCreateBracket(
    ctx, elimination_type: app_commands.Choice[str], losers_bracket_timing: app_commands.Choice[str] = None
):
    timing_value = losers_bracket_timing.value if losers_bracket_timing is not None else "after_winners"
    await helperObj.createBracketHelper(ctx, elimination_type.value == "double", timing_value)


@tournamentGroup.command(
    name="print-bracket",
    description="Print the bracket, with each match's id for use with /wager team and /set correct-winner"
)
async def tournamentPrintBracket(ctx):
    await helperObj.printBracketHelper(ctx)


@tournamentGroup.command(
    name="start",
    description="Start playing the tournament, one round at a time"
)
@app_commands.describe(
    mode="Sequential: one match at a time, ready-checked. Simultaneous: every match in the round at once."
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Sequential", value="sequential"),
    app_commands.Choice(name="Simultaneous", value="simultaneous"),
])
async def tournamentStart(ctx, mode: app_commands.Choice[str]):
    await helperObj.startTournamentHelper(ctx, mode.value)


tree.add_command(tournamentGroup)


# Every persistent-team management and browsing command is grouped under
# one /team command instead of ten separate top-level ones: team-create,
# team-invite, team-leave, team-set, team-rename, team-delete,
# team-transfer, team-stats, team-list, and my-teams (renamed here to
# `mine`). They already shared a "team-" prefix, but were otherwise
# scattered across the top-level command list with no indication they
# were related.
teamGroup = app_commands.Group(
    name="team",
    description="Manage your persistent teams: create, invite, leave, and more"
)


@teamGroup.command(
    name="create",
    description="Create a persistent team you're the captain of"
)
@app_commands.describe(
    name="Team name", team_size="How many players the team is looking for",
    captain="Optional: make this member the captain instead of you",
)
async def teamCreate(ctx, name: str, team_size: int, captain: discord.Member = None):
    await helperObj.createTeamHelper(ctx, name, team_size, captain)


@teamGroup.command(
    name="save",
    description="Save Team 1 or Team 2 from the last game as a new persistent team, with you as captain"
)
@app_commands.describe(
    team="Which side from the last game to save", name="Name for the new team",
)
@app_commands.choices(team=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
async def teamSave(ctx, team: app_commands.Choice[int], name: str):
    await helperObj.saveTeamHelper(ctx, team.value, name)


@teamGroup.command(
    name="invite",
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


@teamGroup.command(
    name="leave",
    description="Leave a persistent team you're rostered on"
)
@app_commands.describe(team="Name of the team to leave")
@app_commands.autocomplete(team=myTeamAutocomplete)
async def teamLeave(ctx, team: str):
    await helperObj.teamLeaveHelper(ctx, team)


# Discord caps a slash command option at 25 static choices, and the
# built-in logo set is bigger than that (see assets/clash-logos).
# Autocomplete is the only way to offer the full list, filtered live as
# the user types.
async def logoAutocomplete(ctx, current: str):
    current = current.lower()
    names = [n for n in helperObj.listAvailableLogos() if current in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@teamGroup.command(
    name="set",
    description="Set a team's voice channel and/or logo (captain only)"
)
@app_commands.describe(
    team="Name of the team",
    voice_channel="Existing voice channel to use for the team",
    new_voice_channel="Create a brand new voice channel named after the team",
    logo="Which built-in logo to use",
)
@app_commands.autocomplete(logo=logoAutocomplete, team=myCaptainedTeamAutocomplete)
async def teamSet(
    ctx, team: str, voice_channel: discord.VoiceChannel = None,
    new_voice_channel: bool = False, logo: str = None,
):
    await helperObj.teamSetHelper(ctx, team, voice_channel, new_voice_channel, logo)


@teamGroup.command(
    name="rename",
    description="Rename a persistent team you captain"
)
@app_commands.describe(team="Current name of the team", new_name="New name for the team")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def teamRename(ctx, team: str, new_name: str):
    await helperObj.teamRenameHelper(ctx, team, new_name)


@teamGroup.command(
    name="delete",
    description="Delete a persistent team you captain (confirmation required)"
)
@app_commands.describe(team="Name of the team to delete")
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def teamDelete(ctx, team: str):
    await helperObj.teamDeleteHelper(ctx, team)


@teamGroup.command(
    name="transfer",
    description="Hand off captaincy of a persistent team you captain to another player on its roster"
)
@app_commands.describe(
    team="Name of the team to transfer",
    member="Who to make the new captain; must already be on the team's roster",
)
@app_commands.autocomplete(team=myCaptainedTeamAutocomplete)
async def teamTransfer(ctx, team: str, member: discord.Member):
    await helperObj.teamTransferHelper(ctx, team, member)


@teamGroup.command(
    name="stats",
    description="View a team's roster and record"
)
@app_commands.describe(team="Name of the team")
@app_commands.autocomplete(team=teamStatsAutocomplete)
async def teamStats(ctx, team: str):
    await helperObj.teamStatsHelper(ctx, team)


@teamGroup.command(
    name="lookup",
    description="List the teams you (or another player) belong to and flip through their stats"
)
@app_commands.describe(member="Whose teams to look up; defaults to you")
async def teamLookup(ctx, member: discord.Member = None):
    await helperObj.myTeamsHelper(ctx, member)


@teamGroup.command(
    name="list",
    description="Browse every team in this server, with filtering and sorting; buttons to page through it"
)
@app_commands.describe(
    search="Only show teams whose name contains this",
    recruiting_only="Only show teams still short of their target roster size",
    sort="What to sort by; defaults to name",
    order="Ascending or descending; defaults to ascending",
    cards="Flip through each team's full stats card one at a time, like /team lookup, instead of a summary list",
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


tree.add_command(teamGroup)


@makeTeamsGroup.command(
    name="saved",
    description="Load two persistent teams into a casual or ranked game"
)
@app_commands.describe(
    team1="Name of the first persistent team",
    team2="Name of the second persistent team",
    ranked="Track elo for this game; defaults to casual"
)
@app_commands.autocomplete(team1=myTeamAutocomplete, team2=myTeamAutocomplete)
async def makeTeamsSaved(ctx, team1: str, team2: str, ranked: bool = False):
    await helperObj.useTeamsHelper(ctx, team1, team2, ranked)


@makeTeamsGroup.command(
    name="repeat",
    description="Re-post the last game's two teams instead of making a fresh split/draft"
)
async def makeTeamsRepeat(ctx):
    await helperObj.reuseTeamsHelper(ctx)


tree.add_command(makeTeamsGroup)


@tree.command(
    name="test-image",
    description="Admin/dev: render the matchup graphic (with roles) against dummy teams to preview image changes"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def testImage(ctx):
    await helperObj.testImageHelper(ctx)


@testImage.error
async def testImage_error(ctx, error):
    if isinstance(error, app_commands.MissingPermissions):
        await ctx.response.send_message(
            "You need the Manage Server permission to use /test-image."
        )
    else:
        raise error


@tree.command(
    name="notify",
    description="Send a member (or everyone in a role) an invite to the channel"
)
@app_commands.describe(
    member="A specific member to invite",
    role="Invite every member of this role instead; give one or the other, not both",
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
            "You need to be in a voice channel to invite someone to it. Join one and try again."
        )
        return

    # notifyHelper DMs the target directly instead of responding to the
    # interaction, so calling it once per role member in a loop is safe.
    # ctx.response.send_message below still only ever fires once either
    # way. A member with closed DMs (notifyHelper returns False) doesn't
    # stop the rest of the batch from being invited.
    targets = role.members if role is not None else [member]
    failures = 0
    for target in targets:
        if not await helperObj.notifyHelper(ctx, target, message):
            failures += 1

    if member is not None:
        if failures:
            await ctx.response.send_message(
                f"Couldn't DM {member.name} — they may have DMs disabled for this server.",
                ephemeral=True,
            )
        else:
            await ctx.response.send_message(f"Sent an invite to {member.name}!")
    else:
        count = len(targets)
        sent = count - failures
        if failures:
            await ctx.response.send_message(
                f"Sent an invite to {sent}/{count} member{'s' if count != 1 else ''} in {role.name}; "
                f"{failures} couldn't be DMed."
            )
        else:
            await ctx.response.send_message(
                f"Sent an invite to {count} member{'s' if count != 1 else ''} in {role.name}!"
            )


@tree.command(
    name="roll",
    description="Roll a number between 1 and the number you provide"
)
@app_commands.describe(num="Top of the range; must be greater than 1")
async def roll(ctx, *, num: int):
    if num > 1:
        rand = random.randint(1, num)
        await ctx.response.send_message("You rolled " + str(rand))
    else:
        await ctx.response.send_message(
            f"{num} isn't greater than 1, so there's nothing to roll between 1 and it. "
            "Try a number greater than 1.",
            ephemeral=True,
        )


# Runs the full test suite before connecting to Discord, so a broken
# deploy shows up in the log immediately instead of only being noticed
# once something breaks in production.
#
# This shells out to `pytest -n auto` in its own subprocess, rather than
# running the tests in-process with stdlib unittest the way this used to
# work, for two reasons. First, pytest-xdist splits the roughly 900 tests
# across every CPU core instead of running them one at a time. Second, a
# genuinely separate process means tests.py becomes that process's own
# real entry point: its first nested `_import_bot_module()` call gets the
# same inert, open()-mocked root log handler that an ordinary
# `pytest tests.py` run from a terminal always has (see readme.md). That
# keeps the thousands of test-fixture DB and asyncio-debug log lines a
# full run produces out of this file's own real log, the way they used to
# leak in when the suite ran in-process here directly. Running it
# in-process used to need logger._suppress_db_logging plus temporarily
# raising the "asyncio"/"discord" logger levels around the run. Both are
# gone now, since there's nothing left in this process for them to
# protect against.
#
# `--junitxml` gives back pytest-xdist's own already-stitched-together
# summary (total/failed counts plus one <testcase> per test) as structured
# XML, instead of having to scrape worker-interleaved terminal output.
# `cwd=BASE_DIR` and `sys.executable` keep this correct no matter what the
# process's own working directory is, or which Python environment is
# actually running the bot.
#
# Every run logs one info-level summary line (how many passed out of how
# many, and how long the whole subprocess took), regardless of outcome. A
# failing suite, or pytest itself failing to launch or produce a report at
# all (e.g. a stale install missing pytest-xdist), additionally logs a
# warning rather than aborting startup: a real deploy should still come up
# and serve players even if, say, a test itself is stale, rather than a
# self-test regression taking the whole bot down.
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


# Guarded so tests.py can import this module, to exercise command
# callbacks and event handlers directly, without connecting to Discord as
# a side effect of the import.
if __name__ == "__main__":
    _runStartupSelfTests()
    client.run(token)