# Shockwave

A Discord bot for organizing team-based voice games. It splits a voice channel
into teams (randomly, by live captain draft, or elo-balanced for ranked play)
and moves everyone into the right channel automatically.

It also comes with a full gold economy (pari-mutuel betting, daily gold,
heads-up wagers, a leaderboard) and a tournament system (persistent named teams,
a real single- or double-elimination bracket, sequential or simultaneous match
play).

It's built around League of Legends' 5v5 format, but nothing about team
formation, betting, or tournaments is League-specific. It uses the Discord API
to pull server/client data.

Full list of commands on addshockwave.com

## Installing dependencies

Requires Python 3.10+. Install the dependencies with:

```
pip install -r requirements.txt
```

## How it works

This section is about why the code is shaped the way it is, not how to run the
commands. For that, see addshockwave.com.

### Architecture

- **`bot.py`** creates the `discord.Client`/`app_commands.CommandTree`, owns
  the sqlite connection and schema setup, and registers every slash command.
  Command bodies are thin: almost every command immediately hands off to a
  matching method on a single shared `helper.helpers` instance (`helperObj`).
- **`helper.py`** holds the actual logic. One `helpers` class holds the
  `cursor`/`db` and every command's real implementation, plus every
  `discord.ui.View` button callback (reporting a winner, accepting a wager,
  paging a leaderboard).
- **`TourneyClasses.py`** defines `Player` and `Team`, small data classes with
  hand-rolled `serialize`/`deserialize` methods so a team roster can be stored
  as a plain string in a sqlite `TEXT` column instead of needing a separate
  table.
- State lives in one sqlite database (`data/guildData/serverInfo/main.db`).
  One row per guild in `servers` holds "current session" state: team rosters,
  channel names, betting status. `economy`, `wagers`, `duels`, `leaderboards`,
  and `last_result` hold everything gold/elo related. New columns and tables are
  added with `ensure_column()` on startup, so a fresh database isn't needed for
  every feature addition.
- Every `@tree.command` is registered with no `guild=` at all, making it a
  global command definition rather than one tied to a specific server.
  `syncCommandsToGuild` (`copy_global_to` plus a guild-scoped `sync`) is what
  actually publishes those definitions to a real server. It's called for
  every guild the bot is already in on `on_ready`, and again for whichever
  guild it just joined on `on_guild_join`. A single global `tree.sync()` with
  no guild argument would work too, but can take up to an hour to propagate.
  Syncing per-guild keeps registration effectively instant while still
  needing zero server-specific configuration.

### Logging and database backups

`mainDB` runs in WAL journal mode (`PRAGMA journal_mode=WAL`, set once right
after connecting) rather than sqlite's default rollback journal, since every
guild's data goes through this one connection/file. Under the default mode a
writer holds an exclusive lock that blocks every reader too, not just other
writers, so one guild's slow write (or the daily backup's own read pass, see
below) could stall an unrelated command reading a completely different
guild's row. WAL lets readers keep going against the last-committed snapshot
while a write is in flight instead. `synchronous=NORMAL` is WAL's own
recommended pairing (still fsyncs at checkpoints, just not on every single
commit), and the connection's own `timeout` is raised from sqlite3's 5-second
default to 30, so a command that does hit real lock contention waits it out
rather than surfacing `OperationalError: database is locked` outright.
`restore_backup.py`'s own safety copy (of the live database, taken right
before overwriting it with a chosen backup) also copies `main.db-wal`/
`main.db-shm` alongside `main.db` if either exists, since a clean shutdown
checkpoints and removes them automatically but an unclean one wouldn't, and a
raw file copy of `main.db` alone would then silently miss whatever commits
were still sitting in the WAL.

`BASE_DIR` (`os.path.dirname(os.path.abspath(__file__))`) anchors every path
`bot.py` touches (the log file, `main.db`, the backups folder, `token.txt`)
to this file's own directory, not the process's current working directory.
This matches how `helper.py`'s own asset paths already work (see
`TEAM_LOGO_DIR`/`FONTS_DIR`/etc.). A relative path would instead resolve
against whatever directory the process happened to launch from. That's easy
to get right on a dev machine, since you're always `cd`-ing into the project
folder first, but easy to get wrong under a service manager that doesn't set
`WorkingDirectory` the same way. `token.txt` is also opened with
`encoding="utf-8"` explicitly, rather than whatever the platform's default
text encoding happens to be (Windows' ANSI codepage vs. Linux's
near-universal UTF-8). That's harmless for a plain-ASCII token today, but
it's not left to chance either.

`bot.py` configures the root logger at import time: a custom
`MaxLinesFileHandler` (`shockwave.log`) plus a plain console handler, both
sharing one timestamped formatter. Configuring the root logger rather than
just a `shockwave`-named one means discord.py's own internal logging
(gateway, HTTP) lands in the same file, not just this project's own
`logger.info`/`logger.error` calls. `shockwave.log*` is gitignored, same
reasoning as `main.db` itself.

`MaxLinesFileHandler` caps the file at `LOG_FILE_MAX_LINES` (10,000) lines,
dropping the oldest ones once it grows past that. This keeps a single,
chronologically-ordered file, instead of
`logging.handlers.RotatingFileHandler`'s size-based rotation into separate
`shockwave.log`/`.1`/`.2`/`.3` files. At construction it seeds its own
in-memory line count from whatever's already on disk (a previous run's
leftover log) rather than assuming an empty file. That way, if a trim is
already overdue, it happens on the very first line this run emits, instead
of waiting until the file grows past the cap all over again. Trimming itself
(`_trim`) closes the handler's own stream, rewrites the file with just the
last `max_lines` lines kept, and reopens it. It needs no locking of its own,
since `logging.Handler.handle()` already wraps every `emit()` call, and so
this call too, reached from inside one, in `self.acquire()`/`release()`.

`backupDatabaseTask` is a `discord.ext.tasks.loop(hours=24)`, started from
`on_ready` the same way `rotateStatus` is, and guarded against
double-starting on a reconnect the same way. It snapshots `main.db` into
`data/guildData/backups/` once a day, then deletes any backup older than
`BACKUP_RETENTION_DAYS` (7). It uses `mainDB.backup(backup_conn)`, sqlite3's
own point-in-time backup API, rather than a plain file copy. That's because
`main.db` is a live connection other code can be reading or writing between
event loop ticks, so copying the raw file risks capturing it mid-write. It
runs directly on the event loop rather than via `asyncio.to_thread`, for two
reasons: `mainDB` was opened with the default `check_same_thread=True`, so
handing it to a different thread would raise outright, and backing up a
database this size finishes well within the time a trading-card render
already blocks the loop for anyway. `data/guildData/backups/` is gitignored,
for the same reason as `main.db` itself.

`restore_backup.py`, run standalone (`python restore_backup.py`) with the bot
stopped, is how a host reverts to one of those snapshots. There's no
in-Discord command for it, since `main.db` is one shared database across
every guild the bot serves: a live restore triggered by one server's admin
would silently roll back every other server's data too. It lists backups
newest-first with their timestamp and size, and restores whichever one is
picked, by number or filename. Before overwriting anything, it copies the
current live `main.db` into `data/guildData/backups/` as
`main-before-restore-<timestamp>.db`, so restoring the wrong backup, or
restoring at all, is itself undoable the same way. The step-by-step version
of this process, aimed at whoever's actually hosting a copy of the bot
rather than reading this file, lives on addshockwave.com's self-hosting
guide.

`LOG_LINE_MAX_LENGTH` (500) caps any single log line. A serialized team
roster or a command's own object-repr params can run long, and one oversized
line shouldn't be able to dominate the file. `_truncateForLog` is the shared
helper that enforces it, used by the database, command, and completion
logging below.

Every database mutation is logged too, without needing to instrument each of
the file's many individual `cursor.execute()` calls. `_logDatabaseStatement`
is registered via `mainDB.set_trace_callback`, sqlite3's own hook that
receives the text of every statement actually executed on the connection,
with bound parameters already expanded inline rather than left as raw `?`
placeholders. It filters to just `INSERT`/`UPDATE`/`DELETE`: a `SELECT`, or
the trace callback's own `"BEGIN "` for an implicit transaction, isn't a
mutation worth a permanent record. It logs unconditionally, with no special
case for tests. `tests.py`'s own `BotModuleTestCase`-based tests exercise
this same trace callback against their own in-memory databases thousands of
times over a full run, but `_import_bot_module()` (see below) reaches it
through a root logger whose file handler was itself constructed with
`open()` mocked out, so none of that ever reaches a real file.

`LoggingCommandTree` (`bot.py`'s `tree`, subclassing `app_commands.CommandTree`)
overrides `interaction_check`, a single global hook that discord.py's own
`CommandTree._call` runs before dispatching *any* application command in the
tree. This logs every real command invocation (name, params, calling user,
guild) in one place, instead of instrumenting each of the ~40
`@tree.command` functions individually. `interaction.command`/`.namespace`
are independently-resolved cached properties on `Interaction`, so both are
already available at this point, even though the tree hasn't actually
invoked the command yet. `interaction_check` also fires for autocomplete
interactions, i.e. typing into a field with `@app_commands.autocomplete`,
which would otherwise turn every keystroke into a logged "command called".
That's filtered out by checking
`interaction.type is discord.InteractionType.application_command`. The
override always returns `True`, matching the default implementation's
behavior, so it never blocks anything. Per-command checks like `/clear`'s
`has_permissions` still run separately afterward, unaffected.

The whole body is wrapped in its own `try`/`except Exception`, logging and
swallowing rather than letting anything through. `interaction.command`/
`.namespace` run discord.py's real option-resolution machinery, which is
nothing a `FakeInteraction`-based test can faithfully exercise.
`CommandTree._from_interaction`'s own wrapper only catches `AppCommandError`
around the whole dispatch, so anything else raised here would otherwise
escape uncaught: the interaction dies silently, Discord shows "This
interaction failed", and neither `on_app_command_error` nor this file's own
log sees it. A logging-only hook has no business reason to ever fail, so it
needs to be structurally unable to take down the feature it's just supposed
to be watching.

A successful completion is logged separately, from
`on_app_command_completion`. This is an event discord.py dispatches itself
only once a command has actually run to completion without raising (see
`CommandTree._call`), so it only ever fires for a genuine success. It never
fires for a command that errored, since that path goes through
`on_app_command_error` instead, or for one that `interaction_check` rejected
before it ran.

`_runStartupSelfTests` runs the full `tests.py` suite before
`client.run(token)` in the `if __name__ == "__main__":` guard. This way a
broken deploy shows up in the log immediately, rather than only being
noticed once something breaks in production. It shells out to
`pytest -n auto tests.py` in its own subprocess
(`subprocess.run([sys.executable, "-m", "pytest", ...], cwd=BASE_DIR, ...)`).
pytest-xdist splits the suite's ~900 tests across every CPU core instead of
running them one at a time. A genuinely separate process also means
`tests.py` is *that* process's own real entry point, so its first nested
`_import_bot_module()` call gets the same inert, `open()`-mocked root log
handler an ordinary `pytest tests.py` run from a terminal always has.
(`_import_bot_module()` patches `builtins.open` for the duration of `import
bot`, so `MaxLinesFileHandler`'s very first `_open()` call, the one
`logging.basicConfig` latches onto for the rest of that process's life,
returns a harmless mock stream, not a real file handle.) Because of that,
none of the thousands of test-fixture DB-statement/asyncio-debug log lines a
full run produces can leak into this file's own real log: there's no shared
process for them to leak into in the first place.

Results come back via `--junitxml=<path>`, pytest-xdist's own
already-stitched-across-workers summary: one
`<testsuite tests=... failures=... errors=... time=...>` with a `<testcase>`
per test, and `<failure>`/`<error>` children on whichever ones didn't pass.
This is parsed with `xml.etree.ElementTree` rather than scraping
worker-interleaved terminal output. Every run logs one info-level line off
that summary (`967/967 passed in 24.1s.`), pass or fail. A failing suite
additionally logs a warning naming exactly which tests failed
(`classname.name`, read straight off the XML), plus the full subprocess
output at debug level, rather than aborting startup. The reasoning: a real
deploy should still come up and serve players even if, say, a test itself is
stale, rather than a self-test regression taking the whole bot down. The
same reasoning covers pytest itself failing to launch, or running but never
producing a report at all (e.g. a stale install missing `pytest-xdist`).
Both cases are caught and logged as a warning rather than raised.

### Team formation

`/make-teams random` and `/make-teams draft` both build two `Team` objects
seeded from whoever is in the caller's voice channel, then serialize them into
the `team1`/`team2` columns on `servers`. Nothing is moved yet.

Both commands check `ctx.user.voice`/`.channel` up front and reply with a plain
explanation if the caller isn't in a voice channel. `/make-teams draft`
additionally needs at least two people in that channel.

Each `Team`'s `.name` comes from `_rosterTeamNames(guild_id)`: an admin's
configured `channel1`/`channel2` names (`/set channels`' `team1`/`team2` params) if
there are any, otherwise the generic "Team 1"/"Team 2" fallback. That name is
what `printEmbed` titles the roster with, and what `_renderMatchupImage`
labels the matchup graphic with. It's also threaded through
`computeGameDeltas`/`recordResult`/`saveLastResult` all the way to
`formatResultMessage` and `reportCorrectWinnerHelper`, so the win
announcement, the elo-change line, and a later correction all use it too. A
server that's named its channels "Red"/"Blue" sees "Red"/"Blue" everywhere a
game touches, not a mix of that and "Team 1"/"Team 2".

`getRosterName(guild_id, column, fallback)` is the read-side counterpart.
`recordResult` and `/wager`'s confirmation both use it to recover the currently
loaded roster's name, while `saveLastResult` snapshots the names actually shown
at the time so a correction later still says the right thing even if a newer
roster (with different names) has since been formed.

`ranked:true` on either command does the same thing but calls
`formBalancedTeams` first (for `/make-teams`, routing straight to
`rankedTeamHelper` instead of the random/roles flow). Each player's elo gets
a random ±100 nudge (`ELO_BALANCE_JITTER`), the jittered list is sorted, and
players are handed out in a snake pattern (side A, B, B, A, B, B, A, ...).
That way the two sides land close in average elo without producing the
exact same optimal matchup every time.

`/make-teams ranked:true use_roles:true` additionally assigns
Top/Jungle/Mid/Bottom/Support, but only when the caller's voice channel holds
exactly 10 people. (Otherwise `rankedTeamHelper` falls back to the plain
`formBalancedTeams` split, with a note explaining why roles weren't applied.)
`formRoleBalancedTeams` drives the role assignment in two passes.

The first pass, `_assignRolesForBalance`, walks the five roles jungle-first
(`ROLE_BALANCE_FILL_ORDER`, since junglers are usually the scarcest genuine
takers). For each role it fills both slots from whoever likes that role
first (per `getRolePreferences`, from `/setup`), then reaches for someone
neutral on it, and only forces in someone who marked it disliked if nobody
else is left. Playing a role you didn't list as liked knocks 100 elo off
your *effective* elo for balancing purposes only
(`ROLE_BALANCE_OFF_ROLE_PENALTY`); a role you explicitly disliked knocks off
200 (`ROLE_BALANCE_DISLIKED_ROLE_PENALTY`). Neither penalty touches your
real elo.

The second pass, `_refineRoleBalance`, hill-climbs on top of the first. It
tries pairwise role swaps between players and keeps any swap that lets
`_splitRoleBalancedTeams`, a brute force over which of each role's two
players lands on which side (32 combinations), find a tighter effective-elo
split than before. It stops once a full pass turns up no further
improvement.

Unlike those two penalties, `ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER`
(1.5) *does* touch real elo. `rankedTeamHelper` records every user_id who
ended up on a disliked role in that particular split, in
`servers.disliked_role_user_ids`, a plain comma-separated list set alongside
`team1`/`team2` (so it lives and clears with them the same way, including
surviving a `/make-teams repeat`). `computeGameDeltas` then multiplies up a
winning player's own elo delta if their id is in that set for the game just
resolved. Only a win earns it: a loss on a disliked role gets no such
break, just the plain team-average delta every teammate gets. It's a reward
for actually pulling off a win on a less-wanted assignment, on top of
whatever the normal team-average swing already gave that player.
`formatResultMessage` lists who earned it and how much as its own
"Disliked-role win bonus" line whenever
`summary["disliked_role_bonus_players"]` isn't empty. The bonus is
snapshotted into `last_result` too (see `saveLastResult`/`getLastResult`),
so `/set correct-winner` recomputes it correctly for whoever the real
winner turns out to be, no matter how much later the correction happens or
what `team1`/`team2` have moved on to since.

Forming a roster through any of these always runs `clearTeamsHelper` first,
which resets `is_ranked` to 0 among other things. Only `ranked:true` sets it
back to 1, which is what later gates whether a reported result touches anyone's
elo at all.

`clearTeamsHelper` also checks `betting_state` before wiping anything. If a game
built from the current `team1`/`team2` is still actively being bet on or played
out, it calls `cancelGameHelper` first (the same refund, move-back, and "Game
cancelled" notice `WinnerReportView`'s own Cancel Game button triggers), so an
in-flight game is always cleanly resolved rather than silently destroyed by the
next team-formation command.

`/make-teams repeat` (`reuseTeamsHelper`) re-posts whichever two rosters
`/make-teams random`, `/make-teams draft`, or `/make-teams saved` most recently produced,
without drawing a fresh random split, elo-balanced split, or captains draft.
Nothing clears `team1`/`team2` just because a game resolved. Only the next
team-forming command's `clearTeamsHelper` call does that, so they already
hold exactly the last game's roster right up until something overwrites them.

`/make-teams repeat` reads them back, along with
`mode`/`is_ranked`/`roster_use_roles`, and never writes any of the three. A
reused ranked game stays ranked, a casual one stays casual, and a
role-eligible roster keeps showing role labels, matching whatever the
original game actually was.

If a game built from those same teams is still being bet on or played, it's
cancelled first (`cancelGameHelper`: refund plus move back), the same safety net
`clearTeamsHelper` uses, just without the `team1`/`team2` wipe that comes with
it, since reusing them is the entire point.

### Voice moves and the betting window

There's no standalone `/start` command. Moving players and opening betting both
live behind a `RosterActionView` (`Start`/`Start (no move)`/`Random Roles`/
`Balanced Roles` buttons) that `_finalizeRoster` attaches to the second of the
two team embeds every roster-forming path posts (`/make-teams random`, ranked
or not; `/make-teams draft` once the draft finishes; `/make-teams saved`).

`RosterActionView` is persistent: a fixed `custom_id` on every button,
`timeout=None`, registered once via `client.add_view` in `bot.py`'s
`on_ready`, so an un-started roster keeps accepting clicks even across a bot
restart. A persistent view is one shared instance covering every roster in
every guild at once, so its callbacks re-derive everything (which roster,
whether it's still live) from the interaction itself rather than from
anything stored on the view.

`printEmbed` returns both posted messages so its callers can hand them to
`_finalizeRoster`, which records
`roster_team1_message_id`/`roster_team2_message_id`/`roster_channel_id` on the
guild's `servers` row, the same "remember which message is still live" shape
`betting_message_id` uses for the winner-report message. That's what lets
`_handleRosterStartClick`/`_handleRosterRerollClick`/
`_handleRosterBalanceRolesClick` tell a stale roster apart from the current
one, since forming a new roster just overwrites those columns.

Start (no move) is the same button, `move=False`, for a group that's already
elsewhere (a stage channel, another platform, in person) and doesn't want
Shockwave touching anyone's voice state at all. `_handleRosterStartClick`
handles both, with the whole channel-move block skipped when `move=False`.

For Start, the "channel to send everyone back to later" is found by scanning
the roster's own players for whichever one is currently sitting in a voice
channel (`_findRosterVoiceChannel`), rather than assuming the clicker
themselves is in voice. Anyone can click it, not just someone at the table.

`channel1`/`channel2` (set by `/set channels`' `team1`/`team2` params, admin-only) are
looked up next. If either is missing, `_ensureDefaultTeamChannels` self-heals
onto `DEFAULT_TEAM_CHANNEL_NAMES` (`"Team-1"`/`"Team-2"`), creating whichever
one doesn't already exist and writing them back to `channel1`/`channel2` so this
only happens once per guild.

Start (no move) skips all of that, since nobody has to be in a voice channel
at all to click it. It also explicitly clears `original_channel` back to
`""`, so `moveMembersToOriginalChannel` no-ops once the game ends: nothing
moved at the start, nothing to move back either. That clear matters even
though Start (no move) itself never sets `original_channel`.
`captainsHelper` captures the drafting caller's voice channel the moment a
`/make-teams draft` draft starts, so a stale value left over from an
earlier Start game is possible too.

A dedicated `roster_starting` column on `servers` is flipped to `1`
synchronously, before any `await`, the moment the checks above it pass.
This is the same "flip before doing anything async" shape
`_handleWinnerReportPick`'s own `betting_message_id` clear uses.
`_handleRosterStartClick`'s own guard checks both the stored
`roster_team2_message_id` and `roster_starting` together, and that
combination is what stops two near-simultaneous Start/Start (no move)
clicks from both passing the guard and starting the game twice.
`roster_team2_message_id` itself is left intact rather than cleared,
because `recordResult`'s own end-of-game cleanup
(`_deleteMakeTeamsMessages`) still needs it to find team2's roster message
once the game actually resolves. Clearing it here would just make that
later cleanup unable to find the message to delete. `_finalizeRoster`
resets `roster_starting` back to `0` the next time it posts a fresh roster,
so a new roster is clickable again from scratch.

By default all four roster buttons (Start, Start (no move), Random Roles,
Balanced Roles) are clickable by anyone who can see the message. `/set
roster-permissions strict:true` narrows that to a rostered player or a
Manage Server admin, the same `_isAdminOrInCurrentGame` gate the
winner-report buttons already enforce. `_checkRosterPermission` is the
shared check behind `strict`, called first thing inside
`_handleRosterStartClick`/`_handleRosterRerollClick`/
`_handleRosterBalanceRolesClick`; `strict:false` (the default) skips it
entirely.

Once the moves are done (or skipped), it posts the same matchup graphic a
tournament match gets: `_sendMatchupImage` calls the tournament path's own
`_renderMatchupImage`, just with no match id or tournament name in the
subtitle. When the roster was formed with roles (`use_roles`),
`_drawMatchupColumn` also draws each player's role icon just to the left of
their name. That switches that side's whole roster from `makeEmbedString`'s
usual centered layout to a left-justified one, so every row's icon lines up
at the same x position regardless of name length. `_roleIconImage` sources
these icons from `assets/role-icons/`, one file per `SETUP_ROLE_NAMES`
entry, trying a short list of filename variants per role
(`ROLE_ICON_FILENAME_CANDIDATES`, e.g. `top.png` or `Top_icon.png`) so an
icon set doesn't need renaming to match exactly. A missing icon file just
degrades to no icon and no extra row width, the same "off until the assets
exist" shape `TEAM_LOGO_DIR`/`ELO_BADGE_DIR` already use elsewhere.

It then calls `_openBetting`. Betting stays open for a configurable window
while the bot keeps responding to other commands, so the countdown runs as
its own `asyncio.create_task` (`_bettingTimer`), tracked per-guild in
`self.bettingTasks`. That way Cancel Game or a fresh Start/Start (no move)
click can cancel it, instead of leaving it to fire later against a game
that no longer exists. `_openBetting` also stamps `betting_opened_at` (unix
seconds) on the `servers` row. `self.bettingTasks` is only ever in-memory:
it's lost on a genuine process restart, though not on a mere gateway
reconnect, where it's untouched. Without that timestamp, a guild that had
`betting_state=OPEN` at the moment of a restart would stay `OPEN` forever,
since nobody would ever flip it to `CLOSED` again. `reconcileStaleBettingWindows`,
called once from `on_ready`, resumes any such window with whatever time it
actually had left, or closes it outright if that time had already passed.
It skips any guild that already has a live task tracked (the reconnect
case), so it can't stomp a window that was never actually interrupted.

The headline text comes from whichever `mode` string
(`"Normal"`/`"Ranked"`/`"Captains"`/`"Ranked Captains"`) the most recent
team-forming command left in `servers` (`_matchupLabelForMode`), so it reads
correctly no matter how the two teams got there.

Random Roles and Balanced Roles sit alongside Start/Start (no move) on that
same message, but only when the roster actually qualifies: both teams
landed at exactly 5, regardless of `use_roles` or whether the game is
ranked at all. That means roles can be bolted onto any 5v5 roster after the
fact, including a plain `/make-teams random` split. Since `RosterActionView`
is one shared persistent instance, it can't conditionally omit a button per
message the way adding a reaction conditionally once could. Instead,
`_finalizeRoster` builds the view with `include_role_buttons=size_eligible`,
which genuinely removes both items from that specific message. (A
persistent view's registration only governs custom_id routing, not which
buttons any one message actually shows.) `roster_use_roles` itself still
only starts `True` when the roster was posted with `use_roles` (i.e. ranked
with roles). It only turns on for everyone else once one of these two
buttons is actually clicked.

Clicking Random Roles (`_rerollRoster`) shuffles both teams' player order,
persists the shuffle back to `team1`/`team2`, and edits both posted
messages in place with freshly role-labeled embeds. This is also how a
roster that never had `use_roles` set gets roles turned on in the first
place, not just a re-shuffle of an already-labeled one. Since a pure
shuffle makes no preference claim about who lands where, it also clears
`disliked_role_user_ids` (see above), so no stale bonus flag survives a
reshuffle.

Clicking Balanced Roles (`_handleRosterBalanceRolesClick` →
`_applyBalancedRolesToRoster`) runs the same elo-plus-preference logic
`/make-teams ranked use_roles:true` uses, just adapted so team membership
never moves. `_assignRolesForFixedTeams` runs `_assignRolesForBalance`
independently for each side: the same preference-first fill
`formRoleBalancedTeams` uses, just with `per_role=1` and scoped to one
team's own five players instead of two per role across ten.
`_refineFixedTeamRoleBalance` then hill-climbs pairwise role swaps *within*
each team, never between them since these two rosters are already fixed, to
shrink the gap between the two teams' total effective elo. This is the same
way `_refineRoleBalance` hill-climbs for ranked's own from-scratch split.
The result is persisted back to `team1`/`team2` (same players, new role
order), `disliked_role_user_ids` is recomputed from whichever assignment
actually landed, and both posted embeds are edited in place with role
labels showing.

### Resolving a winner, or cancelling the game

Betting state for a guild is a finite state machine stored in the
`betting_state` column: `NONE → OPEN → CLOSED → NONE`. The winner-report
message doesn't wait for betting to close. `_openBetting` posts it
immediately, in the same message as "betting is open," with a
`WinnerReportView` (Team 1/Team 2/Cancel Game buttons) attached right from
the start, and stores that message's id before returning. Team 1/Team 2
aren't fixed labels: `_openBetting` reads the roster's own name back with
`getRosterName(..., escape=False)` and builds each button's label from it
(`_teamButtonLabel`, truncated to Discord's 80-character cap), so the
buttons read the same team names the roster embed and matchup graphic
already show. The `escape=False` matters because a button label renders as
plain text; without it, the markdown-escaping the same call does for
message text would show up as a literal backslash.

`WinnerReportView` is persistent (fixed `custom_id`s, `timeout=None`,
registered once via `client.add_view`), the same reasoning as
`RosterActionView`: an open betting window can outlive a bot restart, so its
buttons need to keep working across one.

Reporting (or cancelling) is valid the whole time `betting_state` is `OPEN` or
`CLOSED`. `_bettingTimer` firing after the configured duration only flips `OPEN
→ CLOSED` and posts a short "betting is now closed" notice. It doesn't touch the
report message or its buttons at all.

A Team 1/Team 2 click calls `_handleWinnerReportPick`, which checks the stored
message id and that `betting_state` is `OPEN`/`CLOSED`. It then clears
`betting_message_id` synchronously before doing anything `await`-based. That
ordering is what stops two near-simultaneous clicks (someone double-clicking,
or two different people clicking within milliseconds of each other) from both
passing the check and double-processing the same game.

It's the stored message id that gets cleared rather than `betting_state` itself,
since the cancel path still needs to read the real, un-flipped `betting_state`
afterward to know whether there's anything to refund. `_cancelBettingTimerTask`
stops the running timer either way, since a winner can be reported (or the game
cancelled) while it's still counting down.

Neither a Team 1/Team 2 pick nor Cancel Game acts right away. Both post their
own confirmation view instead of the original message continuing to look
live: `_handleWinnerReportPick` posts `ConfirmWinnerReportView`
(Confirm/Cancel), `_handleWinnerReportCancelClick` posts
`ConfirmCancelGameView` (Confirm/Cancel). Neither a real elo/payout/
game-record change nor a refund-and-move-everyone-back should hinge on one
accidental click. Only Start/Start (no move)/Reroll stay single-click,
since a fresh roster or `/clear` cleanly undoes those, while a recorded
result only has the heavier `/set correct-winner` as its way back, and a
cancelled game (refunded bets, everyone moved) has no undo at all.

Confirm on the winner-report side calls `recordResult` with the
channel/guild from the button-click interaction itself, then deletes both
the original report message and the confirmation prompt itself outright,
rather than just stripping their buttons (`ConfirmWinnerReportView.confirm`
calls `_deleteMessageSafely` on each). `formatResultMessage`'s own result
message is what the channel keeps instead, so there's no real reason for
either the "betting is open"/report message or a "reported as the winner,
confirm to finalize" prompt that's already been acted on to keep cluttering
the channel.

That result message replies to whichever matchup graphic the game/match it's
for actually has, so it stays visually anchored to it instead of just
landing further down the channel (handy specifically once `/set
matchup-channel` can put the graphic somewhere other than a plain scroll
away). `_matchupMessageLocation(guild_id, match_id=None)` is the shared
lookup: a casual/ranked game's own via `matchup_message_id`
(`_sendMatchupImage`), or, given an explicit tournament `match_id` (or
implicitly via `active_tournament_match_id` when one isn't given), that
match's own `tournament_matches.messageId`/`channelId` - whichever of
`_postReadyCheck`'s ready-check message or `_postMatchReport`'s report
message posted it. `_fetchMatchupMessage` wraps that with the actual
`fetch_message` call, best-effort (`None` on anything unresolvable or
already deleted); `_matchupGraphicLink` wraps it into a jump-to-message URL
instead, for `_openBetting`'s "betting is open" text (see below).
`_postReadyCheck`/`_postMatchReport` write `channelId` on every post (not
just `messageId`), so this stays accurate even once `/set matchup-channel`
moves the graphic somewhere other than wherever the round itself started.
Every tournament match-resolution path's own result line
(`_resolveTournamentMatch`, `_resolveLosersMatch`, `_resolveFinalsMatch`)
replies the same way, passing its own `match_id` explicitly since none of
those run through `recordResult`/`active_tournament_match_id` at all for
simultaneous mode.

Confirm on the cancel side calls `_finishGameCancel`, which is just
`cancelGameHelper`: the refund via `cancelBettingHelper`, also clearing
`active_tournament_match_id` so an abandoned tournament match's
bracket-advance hook can't fire against whatever unrelated game starts
next, plus the move back to the original channel. That's followed by
deleting the original report message outright (`_deleteMessageSafely`),
the same "nothing left to say" treatment the winner-report side gives it;
`ConfirmCancelGameView.confirm` itself then deletes the confirmation
prompt too, mirroring `ConfirmWinnerReportView.confirm` exactly.
`cancelGameHelper`'s own "Game cancelled." message is what the channel
keeps instead.

Cancel and a timeout on either confirmation view instead call
`_restoreWinnerReportMessage`, which puts the original report message's id
back into `betting_message_id` so its buttons work again. It only does this
if nothing else has resolved the game a different way in the meantime
(`betting_state` still `OPEN`/`CLOSED`, and `betting_message_id` still
unset), so a stale restore can't resurrect a game that the other path
already cleaned up. Every one of these buttons is open to anyone to click,
since there's no single "invoker" to restrict them to the way a
slash-command's own confirm view has `ctx.user`.

The report click also closes betting immediately, flipping `betting_state`
from `OPEN` to `CLOSED` synchronously alongside the `betting_message_id`
clear, before the confirmation ever posts. Without that, `/wager` would
stay open for the whole confirmation window, since nothing else touches
`betting_state` until `recordResult` eventually clears every wager. That
would let someone place a brand new bet on whichever side just got reported
before it's even confirmed as the real winner. It stays `CLOSED` even if
the report is later cancelled: the same "once reported, betting's done"
choice either way.

`cancelBettingHelper` is also what `_openBetting` calls first, to silently clear
out a stale unresolved round before a fresh one opens. That path never moves
anyone, since clearing a stale round isn't the player-facing "the game was
cancelled" event `cancelGameHelper` handles.

Message cleanup happens at two other points in the same cycle, both scoped
to a casual/ranked game formed through `/make-teams`. (`recordResult`'s own
`is_tournament_match` check skips all of it for a tournament match, which
never posts a matchup graphic or a "Teams created!" intro in the first
place, sequential mode included.) Once a roster's Start button is clicked
and the matchup graphic actually posts, `_handleRosterStartClick` calls
`_deleteMakeTeamsIntroMessages` right after `_sendMatchupImage`. This
deletes the original "Teams created!"-style team-formation announcement,
and for a draft, its picker/pool messages too, since the graphic already
shows both full rosters and that earlier text reply has nothing left to
say.

Once the game's result is actually recorded, `recordResult` deletes the
"Betting is now closed!" notice if `_bettingTimer` posted one
(`betting_closed_message_id`), and the roster embeds themselves
(`_deleteMakeTeamsMessages`). It then replies to the matchup graphic
message itself (`matchup_message_id`) when it sends `formatResultMessage`'s
result text, so the result stays visually anchored to the graphic instead
of just landing further down the channel. This is a best-effort reply,
falling back to a plain send if the graphic message was itself somehow
already deleted.

The same flip-before-await-anything pattern shows up again in `_acceptDuel` and
`_resolveDuel` for `/wager against`.

### The economy

Payouts are pari-mutuel: everyone who bet on the winning team splits the losing
team's pool, proportional to their own wager, on top of getting their own wager
back (`payout = amount + (amount / winningPool) * rakedLosingPool`). A bet on
the side fewer people backed still pays out more than the same-sized bet on the
favorite.

`computeGameDeltas` is a pure function. Given the wagers and rosters, it returns
a plain dict of `{user_id: {balance, wins, losses, ...}}` deltas without
touching the database at all. `recordResult` is what actually calls
`applyGameDeltas` to write them. Keeping the math and the writing separate is
what makes `/set correct-winner` possible.

`rakedLosingPool` isn't just `losingPool`. `_imbalanceRakeFraction(winning_pool,
losing_pool)` takes a cut that scales with how lopsided the pool was: 0% at an
even 50/50 split (a genuine coin-flip still pays full odds), up to
`MAX_IMBALANCE_RAKE` (50%) at a maximally one-sided pool. The tax lands
specifically on "safe" betting, never on real risk-taking. A pool where the
eventual winners were actually the minority (a real upset) isn't raked at all.

The raked share isn't paid to anyone. It was already deducted from losers'
balances the moment they placed those bets, so simply not crediting it to the
winners removes it from the economy outright, which also helps offset the
inflation `GAME_WIN_GOLD`/`GAME_LOSS_GOLD` introduce on their own. The same
helper backs both `computeGameDeltas` (casual/ranked/sequential-tournament
games) and `_matchWagerDeltas` (simultaneous-tournament match wagers).

Separately from wagering, every rostered player gets gold just for
finishing the game: `GAME_WIN_GOLD` (300) for the winning side,
`GAME_LOSS_GOLD` (150) for the losing side. This happens the moment a game
resolves, ranked or casual, whether they bet on it or not. It's folded into
the same `balance` delta `computeGameDeltas` already produces per player,
right alongside `game_wins`/`game_losses`/`elo` in the roster loop, so it
rides along for free with `applyGameDeltas`/`reportCorrectWinnerHelper`'s
existing apply/reverse/reapply cycle. Correcting a misreported winner
correctly flips which rostered players get the win amount versus the loss
amount, along with everything else a correction re-derives.
`gold_wagered`/`gold_won`/`gold_lost` stay wager-only and never see it.

### Elo and ranked play

Elo only moves for games formed with `ranked:true` (`is_ranked` on the guild
row). A casual `/make-teams` game updates the Game Record but never touches elo.

When it does apply, it's a standard Elo update: `expected = 1 / (1 + 10 **
((their_avg - your_avg) / 400))`, `delta = round(32 * (actual_result -
expected))`, computed once per team using each side's average rating.

`/stats` and `/leaderboard` translate the raw number into a League-style tier
via `eloRankLabel`: nine tiers spaced 250 elo apart (1000 default elo lands new
players in Platinum), with Iron through Diamond further split into four
divisions each. Master and above show no division, matching League's switch to
raw LP at that point.

That 1000 is only the global fallback. An admin can move where new players start
with `/set default-elo`'s `elo` param (`_defaultEloForGuild`), per guild. It only
affects brand-new players (`ensureGameStatsRow`) and `/clear elo`'s reset,
never anyone's already-tracked rating. Unlike elo itself, `default_elo` isn't
split per game - every game a server plays starts new players at the same
configured value.

### Multiple games (`/set game`)

Elo and game-record stats (`game_wins`/`game_losses`/`ranked_wins`/
`ranked_losses`/`current_win_streak`) are tracked per game, not just per
player: a `game_stats` table, one row per `(guildId, userId, game)`,
separate from `economy`, which stays the single shared gold/bet ledger a
player carries across every game a server plays (balance, bet wins/losses,
gold wagered/won/lost, the daily-claim cooldown). An elo rating or win
streak from one game would mean nothing mixed into another's; gold, on
the other hand, reads fine as one pool regardless of which game it was
earned in.

`servers.current_game` is the admin-configured "what's next" setting,
defaulting to `"League"` (the only game this bot tracked before this
existed, and the one every pre-existing server's history was migrated
into - see bot.py's one-time `game_stats` backfill, keyed off whether that
table already existed at startup). `/set game` (`setGameHelper`) updates
it and registers the name into `guild_games` (one row per `(guildId,
game)`, seeded with `"League"` for every guild) so it shows up in future
autocomplete suggestions - typing something not already in that list is
still accepted outright, `gameAutocomplete` is a convenience, not a
restriction.

Switching `current_game` only affects the *next* roster formed, not
whatever's already in progress. Every team-forming path
(`randomizeTeamHelper`, `rankedTeamHelper`, `captainsHelper`,
`useTeamsHelper`, `_handleReadyClick` for a sequential tournament match)
stamps a separate `servers.game` column from `current_game` at the moment
it forms, the same "capture once, read later" shape `mode`/`is_ranked`
already use. `_activeGame(guild_id)` (`servers.game`, falling back to
`current_game` if unset) is what `recordResult` and friends read at
resolution time, so an admin running `/set game` mid-draft can't
retroactively change which game's ladder the in-progress game affects.
`clearTeamsHelper` resets `game` back to `NULL` alongside `team1`/`team2`,
so a stale value can't leak into whatever forms next.

`computeGameDeltas`'s delta-dict shape didn't need to change at all -
`applyGameDeltas(guild_id, deltas, game=None, sign=1)` (defaulting to
`_currentGame` when not given) is what actually splits a delta at apply
time: `balance`/`wins`/`losses`/`gold_wagered`/`gold_won`/`gold_lost` go to
`economy` as before, `game_wins`/`game_losses`/`ranked_wins`/
`ranked_losses`/`elo`/`current_win_streak` go to the resolved game's
`game_stats` row instead (skipped entirely for a pure bettor with no
roster delta, so betting on a game never seeds a stats row for it).
`_checkTierRewardUnlocks`/`_checkAchievements` read from that same
resolved row, so a tier reward or the veteran/on-fire ladders reflect
whichever game the result was actually for.

`saveLastResult`'s snapshot carries a `game` key (defaulting to
`_currentGame` if not given; an older snapshot from before this existed
reads back as `"League"`, via `getLastResult`'s own `setdefault`), so
`/set correct-winner`'s reverse-and-reapply cycle
(`reportCorrectWinnerHelper`/`_invalidateLastResult`) always resolves
against the SAME game a result was originally for, never whatever
`current_game` happens to be by the time someone corrects it.
`/set elo`/`/clear elo` (`adminSetHelper`/`resetEloHelper`) both operate
on the current game only, not every game a player's ever touched;
`/clear economy` still wipes every game's `game_stats` rows outright
(`resetEconomyHelper`), matching its own "wipe everything" scope.

`getLeaderboardEntries`/`_buildStatsEmbed` both scope to `_currentGame`,
LEFT JOINing `game_stats` onto `economy` (so a player who's only ever bet,
or only played a different game, still shows up with 0s/defaults rather
than being excluded outright) rather than requiring a `game_stats` row to
already exist. `/stats`' embed title and `/leaderboard`'s own title both
say which game they're for.

Role-based team balancing (Random Roles/Balanced Roles, role icons on the
matchup graphic, `use_roles`) is League-only - simpler to link it to the
active game directly (`_gameSupportsRoles(game): return game == "League"`)
than maintain a separate per-game flag. Team-forming code checks this
against `_currentGame` (the roster being formed hasn't stamped its own
`game` yet); anything about an already-formed roster (the buttons,
`_handleRosterStartClick`'s own matchup image) checks it against
`_activeGame` instead. `_finalizeRoster` folds this into `roles_eligible`
(`size_eligible and _gameSupportsRoles(...)`), which gates both whether
`use_roles` is honored at all and whether the Random Roles/Balanced Roles
buttons even show up on the message - not just whether they're clickable.
The button handlers (`_handleRosterRerollClick`/
`_handleRosterBalanceRolesClick`) still re-check it themselves too, the
same defense-in-depth the 5v5 size check already had, for a stale message
whose buttons were already posted before the game changed.

The matchup graphic's headline includes the game name:
`_matchupLabelForMode(mode, game)` prepends it for a casual/ranked game
(`"League Ranked Match"`), and `_postReadyCheck`/`_postMatchReport` do the
same for a tournament match's own round label.

### Correcting a misreported winner

`/set correct-winner` can't just recompute the game from scratch, because by
the time someone notices a misreport, elo ratings have already moved.
Recomputing against current (already-wrong) ratings would give the wrong
correction.

Instead, `recordResult` calls `saveLastResult` to snapshot exactly what was
applied (the wagers, both rosters, the computed deltas, and whether the game was
ranked) into the `last_result` table. Correcting a result means applying the
saved deltas with `sign=-1` to undo them exactly, recomputing fresh deltas
against the now-restored elo values for the correct winner, applying those, and
saving a new snapshot. That way a second correction is possible from the new
baseline too.

`team` and `invalidate` are mutually exclusive. `reportCorrectWinnerHelper`
rejects giving both, or neither. `invalidate` stops after the undo step:
`_invalidateLastResult` reverses `last["deltas"]` the exact same way (bet
payouts, records, elo, `GAME_WIN_GOLD`/`GAME_LOSS_GOLD`), but never recomputes
or reapplies anything for either team, and then deletes the `last_result` row
outright rather than saving a new snapshot. There's no "corrected winner" for a
further correction to work from once a game's been invalidated.

Reversing the deltas alone isn't a refund for a bettor, though. A winner's
stored delta credited their whole payout (stake plus winnings), so undoing it
removes the payout entirely and leaves them down by exactly their stake, the
same state as if they'd lost. `_invalidateLastResult` adds each wager's original
`amount` back afterward specifically to fix that, landing every bettor (winner
or loser) back at their exact pre-bet balance. That's the same "add the stake
back" refund `cancelBettingHelper` does for a bet round that never resolved at
all.

Invalidating isn't supported yet for a `match_id`-scoped tournament match.
Invalidating one would also mean un-advancing whatever it fed into the bracket,
a bigger change than reversing a guild-wide economy snapshot.

### Heads-up wagers (`/wager against`)

A 1-on-1 side bet between two specific players, kept independent of the
team-game betting above: own table, own buttons, no active game required.
Unlike the single-active-game betting state stored directly on the `servers`
row, several duels can be open at once between different pairs of players in
the same guild, so each one gets its own row in `duels` keyed by the current
message id.

Nothing is escrowed at challenge time, only a balance sanity-check, so a
challenge that's never accepted doesn't leave anyone's gold stuck. Both
players' gold is only locked once the target presses Accept
(`DuelAcceptView`), which also strips that Accept button via
`_clearMessageButtons` right after `_acceptDuel` runs. At that point a
second message goes out with a `DuelResultView` (Challenger Won/Target Won
buttons). Picking a result posts a `ConfirmDuelResultView` rather than
paying out immediately, the same two-step confirm shape the team-game
winner report uses and for the same reason: a real gold transfer shouldn't
hinge on one accidental click. Confirming there also strips the result
message's own buttons, matching `ConfirmWinnerReportView`. Both
`DuelAcceptView` and `DuelResultView` are persistent, for the same
reasoning as `WinnerReportView`. `ConfirmDuelResultView` is a short-lived
confirm view, like `ConfirmWinnerReportView`.

### Leaderboard paging

`/leaderboard` builds the full sorted/filtered player list once, up front,
then only ever sends one message. `getLeaderboardEntries` returns every
player with an economy row. `_filterLeaderboardEntries` then drops anyone
with a 0W-0L record in whichever category the selected stat is about.
(`LEADERBOARD_RECORD_KEYS` maps each stat to its relevant wins/losses pair,
e.g. bet wins/losses for a bet stat, or the combined game record for the
elo-sorted overview.) Stats with no wins/losses concept, like balance, net
gold, or gold wagered, still show everyone.

`LeaderboardPagingView`'s First/Prev/Next/Last buttons don't post anything
new. `_handleLeaderboardPageClick` looks up the stored filter/order/page
for that message id in the `leaderboards` table, recomputes the requested
page, and edits the original message via
`interaction.response.edit_message()`. `/team lookup` and `/team list` page
the exact same way, through their own
`MyTeamsPagingView`/`TeamListPagingView` and
`my_team_views`/`team_list_views` tables. All three share a single
`_computeNewPage(direction, page, total_pages)` helper for the
First/Prev/Next/Last arithmetic itself, so that part can't drift out of
sync between them. All three paging views are persistent too, for the same
"shouldn't die across a restart" reasoning as everything else long-lived in
this file.

Missing stats (a win rate with zero games played, for example) sort to the
bottom regardless of ascending/descending order, rather than a `None`/0 value
looking like the best or worst score on the board.

### Redirecting where things get posted (`/set wager-channel`, `/set matchup-channel`)

By default every posting (the matchup graphic, the "betting is open" text,
the winner-report message with its buttons, the closed notice, a reported
result or a cancellation) goes to wherever a game, or a tournament match,
happened to start. Two independent settings redirect different slices of
that: `/set wager-channel` for the wagering side, `/set matchup-channel`
for the graphic/report side, and they can point at two different channels.

`_openBetting` (the shared core both `RosterActionView`'s Start button and
a sequential tournament match call) resolves both `servers.wager_channel`
and `servers.matchup_channel` by name right before anything else, via the
shared `_resolveConfiguredChannel` helper, each falling back to the
channel it was handed if unset or no longer resolvable. It then posts two
separate messages instead of one: the "betting is open" text (only when
`/set betting` is on) goes to the wager channel, and the winner-report
message (Team 1/Team 2/Cancel Game buttons, always posted regardless of
whether betting's on) goes to the matchup channel. The open text names
both teams and, when the matchup graphic is resolvable, ends with a plain
jump-to-message link to it (`_matchupGraphicLink`, see "Resolving a
winner, or cancelling the game" above for the shared
`_matchupMessageLocation` lookup it and `recordResult`'s own
reply-to-graphic both build on) - handy specifically because the two can
now be sitting in different channels.
`betting_channel_id` tracks only the wager side, read back by
`_bettingTimer`'s closed notice
and `reconcileStaleBettingWindows`. The report message needs no such
tracking: every later step that touches it (`_handleWinnerReportPick`,
`recordResult`) already works off `interaction.channel`, which is
whichever channel the click actually happened in.

`/set matchup-channel` also redirects `_sendMatchupImage` (a casual/ranked
game's own graphic) and, for tournament matches, `_postReadyCheck`
(sequential mode's ready-check-plus-graphic message) and `_postMatchReport`
(simultaneous mode's per-match report-plus-graphic message). Since a
tournament match's whole later chain (`_resolveTournamentMatch` and
friends: the match result line, the bracket update, the next match or
round) is threaded through wherever that match's own ready/report
interaction came from, redirecting just those two posting points is
enough to carry the whole match through to the matchup channel too.
Simultaneous mode's round-wide betting notices
(`_openConcurrentTournamentBetting`/`_concurrentBettingTimer`) resolve
`wager_channel` independently, the tournament equivalent of `_openBetting`'s
own split. `recordResult`'s betting-closed-notice cleanup and
`_deleteRoundBettingMessages` both resolve the wager channel explicitly
too, rather than trusting whatever channel they were handed, since that's
now the matchup channel more often than not.

### Capping and disabling wagers (`/set max-wager`, `/set betting`)

`/set max-wager` caps how much gold a single `/wager team`, `/wager
against`, or duel challenge can be (`servers.max_wager`, `NULL` by default,
meaning no cap). `wagerHelper` and `challengeDuelHelper` both check it
right after their existing amount-must-be-positive check, before doing
anything else about the bet itself.

`/set betting` (`servers.betting_enabled`, on by default) turns
`/wager team`/`/wager against` off outright, without touching games, elo,
or winner-reporting. `_openBetting` always posts the winner-report message
either way; with betting disabled it just skips the separate "betting is
open" message and the countdown timer, never creating a `_bettingTimer`
task. `reconcileStaleBettingWindows` skips a betting-disabled guild the
same way, since there's never a timer to resume for one.

### Admin resets and permissions

`/clear` is a subcommand group, one independent command per reset
(`clearGroup`, seven `@clearGroup.command`s: `teams`, `channels`, `tournament`,
`elo`, `economy`, `achievements`, `card-unlocks`). Discord enforces each
subcommand's own parameters, so there's no way to submit half of an intended
combination and only find out it was invalid after the fact. The tradeoff
is that clearing several unrelated things at once takes several separate
calls instead of one combined response. Every subcommand requires the
Manage Server permission outright (`app_commands.checks.has_permissions`,
same as `/set correct-winner`), and every one of them requires confirmation
before anything actually happens.

`/clear teams`, `/clear channels`, and `/clear tournament` share
`confirmClearActionHelper`, which posts a plain warning with "Confirm"/"Cancel"
buttons (`ConfirmClearActionView`). `/clear teams` just clears the current
teams/draft (cancelling, with a refund, any in-progress game built from them
first, the same safety net `clearTeamsHelper` always applies). `/clear
channels` does the same, plus forgets the saved `channel1`/`channel2` names.
`/clear tournament` deletes the server's tournament outright (bracket,
registrations, match history, via `deleteTournamentHelper`) alongside the
same teams/draft clear. Registered, persistent teams in the `teams` table
aren't touched. All three only actually clear anything from inside the view's
`confirm` button callback, which always calls `clearTeamsHelper` regardless of
which of the three it's backing.

`/clear elo`, `/clear economy`, `/clear achievements`, and
`/clear card-unlocks` instead clear the current teams/draft immediately and
synchronously, the moment the command runs (`await
helperObj.clearTeamsHelper(ctx)`, in `bot.py` itself, before the
confirmation is ever shown). They only gate the actual player-data reset
behind confirmation, since that part touches every player in the server (or
one specific player, see below) rather than just the guild's own
in-progress session. `confirmDestructiveClearHelper` posts that warning
with "Confirm reset"/"Cancel" buttons (`ConfirmResetView`). `/clear elo`
resets every player's elo back to this server's default (`/set
default-elo`, 1000 otherwise). `/clear economy` wipes balance, elo, game
record, betting record, and gold wagered/won/lost for every player,
superseding the narrower elo reset since the whole-row wipe already resets
elo too.

`interaction_check` on both view classes rejects anyone who isn't the member
who ran the command, and both time out after `CLEAR_CONFIRM_TIMEOUT_SECONDS`
(30 seconds) with nothing changed if ignored, editing the message to
"Confirmation expired. Run /clear again if you still want to do this."

`resetAchievementsHelper` (backing `/clear achievements`) only deletes
`card_unlocks` rows whose `itemKey` is a `CARD_ACHIEVEMENT_TITLES` key. Every
other unlock (tier rewards, special grants, shop purchases) and the underlying
`economy` stats those achievements were computed from (`game_wins`,
`current_win_streak`, ...) are untouched, so a player who still qualifies
simply earns them back the next time something self-heals. This is a "clear
the trophies off the shelf" reset, not a "make everyone start over" one.

`resetCardUnlocksHelper` (backing `/clear card-unlocks`) goes further: it
deletes every `card_unlocks` row regardless of `itemType` (tier rewards,
special grants, shop purchases, and achievement titles alike) and resets the
equipped `trading_cards` row back to Shockwave's own defaults, since leaving it
pointed at a title/scheme/font that no longer resolves to anything would
surface as a broken card the next time it renders.

`/clear achievements` and `/clear card-unlocks` each also take an optional
`user` param, narrowing either from "every player in the server" down to just
that one member, still gated behind the exact same confirm/cancel view.
`/clear elo`/`/clear economy` take no such param and always stay whole-server.
`resetAchievementsHelper(guild_id,
user_id=None)`/`resetCardUnlocksHelper(guild_id, user_id=None)` carry that split
down to the SQL: `user_id=None` deletes every row for the guild, a real one
narrows the `DELETE` with an extra `AND userId=?`.

`/tournament create` follows a narrower version of the same "confirm before
overwriting" idea: creating a server's first tournament needs no permission at
all, but overwriting an existing one checks
`ctx.user.guild_permissions.manage_guild` before it will even show the
confirmation view.

### Persistent teams

Separate from the ephemeral `team1`/`team2` a `/make-teams random` or
`/make-teams draft` game produces, `/team create` writes a row to a dedicated `teams` table: one per
named team, keyed by its own autoincrement id, with a serialized `Team`
(captain, roster, target size, voice channel) as its payload. A player can sit
on more than one team's roster in this table.

`/team create` normally makes the caller the captain, but an optional `captain`
member argument lets someone stand a team up on another player's behalf. When
given, that member becomes the sole initial roster entry and captain instead of
`ctx.user`.

`/team save` builds one the same way, just pre-filled instead of empty. It
copies whichever side (`team1`/`team2`, picked by its `team` choice param)
of the guild's last completed game, the same ephemeral roster
`/make-teams repeat` re-posts, into a brand new persistent team, with the
caller as captain. `saveTeamHelper` only ever reads `team1`/`team2`, never
writes them, so the source roster is untouched and still reusable or
reportable afterward. A caller who wasn't actually rostered on the side
they asked to save gets rejected outright, so a spectator can't claim a
team they never played on. The new team's `team_size` is stamped to its
current roster size, so it's saved already full rather than recruiting.

Team names are unique per guild case-insensitively. `getTeamRow` looks a team up
with `name = ? COLLATE NOCASE`, so "red" finds "Red", and `/team create`'s (and
`/team save`'s and `/team rename`'s) own uniqueness check rejects "red" as
taken if "Red" already exists. The one exception is renaming a team to a pure
capitalization change of its own current name ("Red" → "RED"), which
`teamRenameHelper` special-cases by comparing `.lower()` first rather than
letting the collision check find the team colliding with itself. `/make-teams
saved` compares its two team-name params the same case-insensitive way before
ever calling `getTeamRow`, so picking "Red" and "red" is still caught as "the
same team twice."

`/team invite` uses the same press-to-accept pattern as everything else
that needs a specific person's consent. A single `TeamInviteAcceptView`
(one shared Accept button, persistent like `WinnerReportView`) is posted
once even when several people are invited in the same call, backed by its
own `team_invites` table keyed by message id with one row per invitee. Each
invitee's own click only ever resolves their own row, since
`_handleTeamInviteAcceptClick` scopes its lookup to
`targetId=interaction.user.id`. That means several different people can
accept off the same message independently. Its `force` param (Manage
Server only, checked separately from and on top of the ordinary
captain-or-admin gate every `/team invite` call still has to pass first)
skips the whole press-to-accept dance: every valid member goes straight
onto the roster via the same `add_player`/`updateTeamData` pair
`_handleTeamInviteAcceptClick` itself commits once a real invite is
accepted, just run immediately instead of waiting on a click. With
`force`, there's no posted invite, no Accept button, and no `team_invites`
row for anyone to accept later.

`/team leave` is the self-service opposite of `/team invite`. Removing yourself
needs nobody else's permission, so it's the one team command with no
captain/admin gate at all. The team's own captain is the one exception:
`teamLeaveHelper` refuses outright, pointing them at `/team transfer` to hand
off the captaincy first or `/team delete` if they want the team gone
entirely, rather than ever leaving a team with no one `isTeamCaptain`
recognizes.

`/team transfer` is that hand-off command: the team's captain, or anyone
with Manage Server, points it at another player already on the roster, and
`team.set_captain(...)` swaps who holds it. `Team.set_captain` itself
enforces "captain must be a roster player," so the new captain has to
already be rostered. Inviting them first with `/team invite` is on the
caller, not something this does automatically.

`/make-teams saved` is the shortcut: it loads two persistent teams straight
into `team1`/`team2` so a casual or ranked game can start immediately, without
cloning any state back into the `teams` table. The in-memory copy gets
`set_id(1)`/`set_id(2)`
purely for `RosterActionView`'s own start handler.

Every persistent team name gets run through `discord.utils.escape_markdown`
right before it's dropped into message text, since a team name is free text and
Discord parses markdown emphasis markers across an entire message, not per line.
`getRosterName` (the one place a roster's stored name is read back out for
display) and `/make-teams saved`'s own messages are where this actually
matters, since every other display path reads a name back through one of
those two.

`/team rename`, `/team set`, `/team invite`, `/team delete`, `/team transfer`,
and `/tournament register` are all captain-gated the same way
(`isTeamCaptain`), but every one of them also lets any member with the Manage
Server permission through. That's `not isTeamCaptain(...) and not
ctx.user.guild_permissions.manage_guild`, the same check repeated at each
command, so a team whose captain has gone inactive, left the server, or just
isn't around isn't stuck. An admin can rename it, change its voice channel or logo,
invite players, register it for a tournament, transfer its captaincy, or
delete it without being on the roster first. `/team save` has no such gate
at all, since it never touches an existing team's roster or captaincy. Its
only real check is that the caller was actually playing on the game roster
it's copying.

`myCaptainedTeamAutocomplete` (the suggestion list backing all six of those
commands' `team` param) checks the same permission and switches from
`getTeamsCaptainedBy` to `getTeamsForGuild` for an admin, so they can actually
find a team they don't captain to type in. `myTeamAutocomplete` (backing
`/team stats` and `/make-teams saved`'s `team1`/`team2` params, none of which
require captaincy/rostering at all) gets the same admin carve-out, swapping
`getTeamsForPlayer` for `getTeamsForGuild`.

Discord's autocomplete is only a suggestion list, not a hard restriction. Typing
a name that isn't offered still submits fine, so this doesn't (and shouldn't)
replace the backing helpers' own captain/existence checks. It just means someone
usually doesn't have to remember exact spelling for their own teams.

Renaming updates the `teams` row's own `name` column and the `name` embedded in
its serialized `data` together (`_renameTeam`), since `getTeamRow` looks a team
up by the column. Letting the two drift apart would make the renamed team
invisible under its new name while a stale row still answered to the old one.

Deleting is destructive and irreversible, so it goes through the same
confirm/cancel button pattern `/clear` and `/tournament create`'s overwrite path
use (`ConfirmTeamDeleteView`) rather than running immediately. On confirm, it
also deletes any pending `/team invite` rows for that team (`_deleteTeam`), so
nobody can later "accept" an invite into a team that's already gone.

A tournament this team is already registered in is untouched. `register_team`
snapshots a copy of the `Team` at registration time, not a live reference back
into the `teams` table, so the bracket entry plays out exactly as registered
either way.

### Setup and role preferences

`/setup` is a one-stop first command for a new player: a short explanation of
what Shockwave does (pointing at `/help` for the rest), a personal "solo team"
(a persistent, team-size-1 team with just them on it), and their liked/disliked
roles for future role-aware matchmaking, picked by pressing role buttons on a
posted message rather than typing role names.

`solo_team_name` is only required the very first time. The solo team is looked
up by captaincy plus size rather than remembered in a separate column:
`setupHelper` scans `getTeamsCaptainedBy` for a team with `get_team_size() ==
1`. If one already exists, the name can be omitted (the team's left alone) or
given again to rename it, through the same case-insensitive collision check and
pure-capitalization carve-out `/team rename` uses.

Picking roles is a two-step flow, both steps sharing one message and one
`SetupRoleSelectionView` shape: five `SetupRoleToggleButton`s (one per
`SETUP_ROLE_NAMES` entry: Top, Jungle, Mid, Bottom, Support) plus a Confirm
button. `setupHelper` posts the message with a fresh view (nothing selected
yet) and inserts a `setup_role_sessions` row keyed by that message's id.

Each `SetupRoleToggleButton` shows `primary` (highlighted) when its role is
currently selected, `secondary` otherwise, so the live selection is visible
at a glance. There's no separate "un-click" the way a reaction's remove
event was: pressing an already-selected role's button toggles it off the
exact same way pressing an unselected one toggles it on.
`_handleSetupRoleToggleClick` flips that one role in the session's
`selectedRoles` column (a plain `symmetric_difference_update`) and rebuilds
a fresh `SetupRoleSelectionView` reflecting the new selection, since a
button's style is derived fresh on every render rather than mutated in
place. It ignores anyone other than whoever ran `/setup`, enforced by the
view's own `interaction_check` (the same single-invoker guard every other
confirm-style view in this file uses), and no-ops with a plain ephemeral
note on a stale or expired session.

Pressing Confirm reads the session's current step and acts accordingly.
During the liked-roles step, it snapshots `selectedRoles` as the confirmed
liked set, flips the session to the disliked step, and edits the message in
with a brand new `SetupRoleSelectionView` (`selected_roles=()`) for the
second round. This is different from `ConfirmResetView`/`ConfirmTeamDeleteView`,
which reuse `self` in their own button callbacks, because the disliked
round has to start every button back at unselected, and a fresh instance
gives that for free. Which step is live is still read fresh from
`setup_role_sessions` on every click either way, not tracked on the view
itself.

During the disliked-roles step, Confirm finalizes things. Any role toggled
in both steps is a contradiction, so it's left out of both the final liked
and disliked sets entirely rather than being written either way, and the
summary message names it and tells the caller to run `/setup` again if
they'd like to fix it. `_applySetupRolePreferences` then replaces
`player_role_preferences` for that player outright, a full `DELETE` then
re-`INSERT` rather than a per-role merge, with whatever survived, since the
flow always walks both steps in full on every run. Running `/setup` for the
first time unlocks the Onboarded achievement at this point too.
`hasCompletedSetup` reuses that same `card_unlocks` row as its signal,
rather than a separate completion table.

The view times out (`SETUP_ROLE_TIMEOUT_SECONDS`) if nobody presses Confirm,
deleting the session row and editing the message to say so, the same
`on_timeout` shape `ConfirmResetView`/`ConfirmTeamDeleteView` already use.

`/make-teams random`'s `use_roles` param reads `hasCompletedSetup`: before forming
role-based teams (casual or, combined with `ranked:true`, elo-balanced ones
too, see Team formation), `bot.py` checks every non-bot member currently in
the caller's voice channel, and if anyone hasn't run `/setup` to completion
yet, it stops and mentions who's still missing instead of forming plain
(role-less) teams or failing partway through.

### Tournaments and the bracket

`Tournament` (in `TourneyClasses.py`) holds a name, team size, bracket size,
elimination type, its registered teams, and its bracket. One `tournaments` row
per guild, `INSERT OR REPLACE`d as a whole each time
(`saveTournament`/`getTournament`), since a server only ever has one.

`register_team` is the one piece of business logic that lives on the class
itself rather than in `helper.py`. It rejects a team if any of its players are
already on a team registered for that same tournament, while leaving the shared
`teams` table alone. The same player can freely be on other teams elsewhere.

The bracket is a real linked structure, not just a list of pairings.
`BracketNode` has three pointers: `opponent` (its paired node this round),
`next` (the node its winner advances into, `None` only for the finals slot), and
`previous` (one of the two nodes that feed into it; the other is reachable via
`previous.opponent`, so one pointer is enough to reconstruct the full pairing).

`buildBracket` shuffles the registered teams, rounds the count up to the next
power of two, and wires every round's nodes to the next in one pass. Slots
beyond the real team count are byes (`team=None`), auto-advanced with no match
created for them.

Since a graph of objects can't go through `json.dumps` directly,
`serialize_bracket`/`deserialize_bracket` convert to and from a flat list of
`{team, opponent, next, previous}` dicts referencing each other by index into
that same list, reconstructed into real object pointers on load.

`/tournament print-bracket` renders the bracket as an actual image
(`renderBracketImages`, via Pillow), walking `previous`/`previous.opponent` all
the way down to the leaves. Discord has a hard 2000-character limit on a
message's text, which a bracket past a handful of teams blows through fast (a
64-team double-elimination bracket is something like 25,000 characters of ASCII
art). An image sidesteps that entirely and renders fully inline at any size.

`_assignBracketPositions` computes every node's pixel position in one pass. A
leaf's position comes from a shared counter so leaves stack top to bottom in
seed order; anything else is the midpoint of its two children. The canvas is
sized purely from actual content bounds, so it's exactly as big as it needs to
be. `_drawBracketNode` draws real lines (`ImageDraw.line`) rather than
box-drawing text characters, so nothing depends on whatever font happens to be
available having those glyphs. `renderBracketText` still returns a short
plain-text status line (which team's currently the champion, `TBD` until
decided) that goes out alongside the image as the message's regular content.

Colors come straight from `shockwave-site/assets/styles.css`'s `:root` palette
(dark ink background, gold titles/champion, light body text, muted connector
lines), so the bracket image reads as part of the same brand rather than a plain
black-on-white chart. Fonts do too: `assets/fonts/` bundles the same two
families the site's CSS uses (`--font-display`/`--font-body`: Chakra Petch for
anything headline-ish, IBM Plex Sans for body text), loaded via `_loadFont`
instead of Pillow's built-in default font. Both are Google/SIL-OFL-licensed and
bundled rather than linked, so rendering doesn't depend on network access.

Every image is drawn `BRACKET_SUPERSAMPLE` (2) times bigger than it's meant to
end up, then downscaled with `Image.LANCZOS` resampling in `_imageToFile`, the
one place that scale gets undone. Pillow's `ImageDraw` has no antialiasing of
its own, so rendering bigger and shrinking down is the standard way around a
jagged line or glyph edge. Every pixel-valued layout constant (font sizes,
margins, line widths, radii, ...) is already expressed at the supersampled
scale, so the drawing code itself never has to think about the scale factor.

Every top-level render call (`renderBracketImages`, `_buildGrandFinalsImage`,
`_renderMatchupImage`, `_renderTeamCardImage`, `_renderTradingCardImage`,
`_renderPreviewImages`) is invoked via `asyncio.to_thread` from its own
async caller, not called directly. Pillow's actual drawing work, many
draw/text/paste calls per image, would otherwise block the event loop, and
so every other guild's commands too, for the whole time one image takes to
render. `_imageToFile`'s own downscale is left on the main thread, a
comparatively small cost next to the drawing itself.

`_renderGrandFinalsImage` is the one render function that also reads from
the database, to check which stage of Grand Finals has actually been
played, which makes it genuinely unsafe to run from a different thread
since `self.cursor` was opened with sqlite3's default
`check_same_thread=True`. It's split into `_grandFinalsRenderInputs` (the
DB read, run on the calling thread) and `_buildGrandFinalsImage` (the pure
drawing, the part that's actually offloaded) for exactly this reason.
`_renderGrandFinalsImage` itself is a synchronous convenience wrapper
around both, kept fully synchronous for callers (and tests) that don't
need the split.

A bracket 16+ teams deep (`BRACKET_TWO_SIDED_MIN_ROUNDS`) renders as two
mirrored halves converging toward a champion in the center, the same layout a
printed tournament bracket poster uses, keeping the image roughly square instead
of very tall. `_drawBracketNode` takes a `mirror` flag for this: it flips the
text anchor (`"rm"` instead of `"lm"`) and every connector offset, so the right
half is a genuine mirror-image layout rather than a raster flip.

The winners bracket splits at the champion's own two children, which are always
exactly even halves since `buildBracket` produces a perfectly balanced tree. The
losers bracket can't use that same split point: its last round is always a
lopsided drop-in (a deep surviving lineage vs. a single bare leaf), so splitting
there would put an entire tree on one side and one name on the other.

Every round after round 1 keeps a match's winners-bracket-left and
winners-bracket-right losers strictly separate, right up until the
second-to-last round, which is always exactly one node: the first and only point
where the two sides genuinely merge. `_renderLosersTwoSidedTreeImage` splits
there instead, then extends one more ordinary, single-sided hop past that merge
point to reach the true champion.

### Playing a tournament out (`/tournament start`)

Each pairing that's ready to play becomes its own row in `tournament_matches`,
holding the two teams, which round/bracket-node it belongs to, and its own
state, independent of any single guild-wide "current game." More than one of
these can exist across a tournament's lifetime, and in simultaneous mode, within
the same round.

Every match, either mode, gets a matchup graphic posted alongside its text
announcement (`_renderMatchupImage`, called from
`_postReadyCheck`/`_postMatchReport`): both teams' logos and rosters facing off,
captain starred and floated to the top of the list (`_orderedRoster`), and which
round of the tournament it is (`_matchRoundLabel`). It reuses the bracket
image's own canvas/header drawing code (`_createBracketCanvas`,
`_drawBracketHeader`) so it reads as the same product.

Sequential mode reuses the ordinary game cycle rather than reimplementing it.
Pressing a match's ready-check button (`TournamentReadyView`, either captain)
sets `servers.team1`/`team2` to that match's two teams, strips the ready-check
message's own Ready button via `_clearMessageButtons`, and calls
`_openBetting`, the exact function `RosterActionView`'s own Start handler
calls, so betting, the winner report, and payouts all work unmodified. The
only addition is `active_tournament_match_id`, a column on `servers` that's
`None` for every ordinary game and only gets set while a tournament match is
borrowing the cycle. `recordResult` checks it once its normal work is done
and, if set, hands off to `_resolveTournamentMatch` to advance the bracket.
`TournamentReadyView`, like every other view backing a flow that can sit open
indefinitely, is persistent.

Simultaneous mode can't reuse that cycle, since `team1`/`team2` and
`betting_state` are guild-wide singletons and simultaneous mode needs several
matches live at once. It skips movement entirely and posts every match's own
`TournamentMatchReportView` at once, scoped by each match's own row instead of
guild state. Its two buttons are built per-message from the match's actual
team names (`_teamButtonLabel`, the same helper/pattern `WinnerReportView`
uses), not a fixed "Team 1"/"Team 2" label. Betting still happens, just
through a second, match-scoped path (`_openConcurrentTournamentBetting`/
`tournament_wagers`) instead of the singleton `wagers` table a normal game
uses.

A team-button click on a simultaneous match doesn't resolve it right away
either, the same reasoning `ConfirmWinnerReportView` has for a normal game.
It posts a `ConfirmTournamentMatchReportView` instead
(`_handleTournamentMatchReportClick`), first flipping the match's `state`
from `AWAITING_RESULT` to a new `CONFIRMING` value and setting
`bettingClosed=1` on it, both synchronously before anything `await`-based.
`CONFIRMING` guards the same double-processing risk `AWAITING_RESULT`
already needed a guard for: a second near-simultaneous click on the same
match can't also pass the `state='AWAITING_RESULT'` check and post a
second confirmation. `bettingClosed=1` closes `/wager match_id:` on that
match immediately, since otherwise it would stay open for the whole
confirmation window and let someone bet on whichever side just got
reported before it's even confirmed.

Confirm calls `_resolveTournamentMatch` directly, then strips the original
match message's own buttons via `_clearMessageButtons`, matching
`ConfirmWinnerReportView`. Cancel or a timeout calls
`_restoreTournamentMatchAwaitingResult`, a conditional
`UPDATE ... WHERE id=? AND state='CONFIRMING'` that puts the match back to
`AWAITING_RESULT` so its buttons work again, but only if nothing else has
resolved it a different way in the meantime. `bettingClosed` is never
un-set on cancel: the same "once reported, betting's done regardless of
outcome" choice the normal game's own `betting_state` close makes.

Either way, actually resolving a match funnels through the same
`_resolveTournamentMatch`, only ever reached now once a report's been
confirmed. It flips the match to `RESOLVED` before doing anything `await`-based,
propagates the winner into the shared bracket node, and prints the updated
bracket. Once every match in a round has resolved, not just the two teams' own
next match being ready to go, it posts a "Round N has ended!" transition
message with a fresh bracket and starts the next round, or announces the
champion if there isn't one.

None of this involves a sleep or a blocking wait anywhere. It's one button
click posting the next button, so other commands (including `/wager` on an
unrelated game) keep working the entire time a tournament round is in
progress.

### Concurrent tournament betting

The singleton `wagers` table (`PRIMARY KEY(guildId, userId)`) can only ever
represent one active bet per player per guild. That's fine for an ordinary game
and sequential-mode tournament matches, where there's only ever one game live at
a time, but it can't let one player bet on several matches at once, which
simultaneous mode routinely has.

Simultaneous-mode betting gets its own, genuinely separate mechanism instead: a
`tournament_wagers` table keyed by `(matchId, userId)` instead of just
`(guildId, userId)`, so the same player can hold one bet per match across
however many matches are open in the round simultaneously.

`_openConcurrentTournamentBetting` opens one combined window covering every
match `_startRound`/`_startLosersRound`/`_startGrandFinals` just queued for the
round: the guild's configured per-match base (`_getBettingTimerSeconds`, backing
`/set betting-timer`'s `seconds` param) times how many matches are in the round, capped
by `MAX_CONCURRENT_BETTING_SECONDS`.

`/wager team` takes an optional `match_id` to say which concurrently-open match
a bet is for. Omitted, it falls back to the singleton behavior for a
casual/ranked game or a sequential-mode match. Each match settles its own bets
independently
(`_settleMatchWagers`, same pari-mutuel formula `computeGameDeltas` uses) the
instant it resolves, rather than waiting on the rest of the round.

### Team logos

`Team.logo_path` is a local file path, not image data, resolved against
`assets/clash-logos/` (Riot Games' official Clash-mode faction/region logos;
`/team set`'s `logo` autocomplete lists every file there by name) via
`_resolveLogoPath`.

A team with no logo set gets one assigned randomly the moment it's next loaded.
`_ensureLogo` is called from every read path (`getTeamRow`, `getTeamById`,
`getTeamsForGuild`) as well as `_saveNewTeam`, so a team just self-heals the
first time it's touched rather than needing a one-off migration.

`/team stats` and `/team lookup` attach a logo as an embed thumbnail via
Discord's `attachment://<filename>` scheme. The matchup graphic pastes it
directly into the rendered image instead.

`_ensureLogo` only ever runs for persistent teams, since it needs a
`team_id` row to write the pick back to. The ad-hoc `Team` objects
`/make-teams random`, `/make-teams draft`, and ranked team formation build
on the fly for a casual game never go through it, so `team.get_logo_path()`
is still `None` for them by the time its matchup graphic renders.
`_drawMatchupColumn` picks a random built-in logo right at render time for
those instead, rather than drawing a bare accent-colored ring. It isn't
persisted anywhere, so a re-render can land on a different one, which is
fine for a team with no identity to keep consistent in the first place. It
falls back to the ring only if the built-in set itself is unavailable.

### Trading cards

`/stats` posts a `StatsView` alongside the embed: Avatar, Card, and (once the
card is up) Back buttons. `StatsView` is persistent, same reasoning as
`WinnerReportView`: nothing ever expires a `/stats` view on its own. Avatar
toggles the thumbnail between this server's own profile picture for that
player and their regular, account-wide one. `_resolveMemberAvatarUrl` handles
the server half (`member.display_avatar`, which already resolves a per-server
override if one's set), and `_resolveGlobalAvatarUrl` handles the regular half
by fetching the plain `discord.User` behind the member, bypassing any guild
avatar override.

Card throws the whole embed away and replaces it with a rendered trading card
(`_renderTradingCardImage`): Shockwave's logo and the server's name across the
top (the same `_drawBracketHeader` every other rendered image uses), the
player's actual Discord username small in the header's top-right, their live
avatar as a circular centerpiece, a customizable title underneath it, then
elo/ranked record/ranked win rate as three stacked lines, and finally, if
they're rostered on any, their persistent teams with each one's logo pasted
alongside its name.

The elo line's tier "emoji" is a real, saved image of that tier's actual emoji
(`assets/elo-badges/<Tier>.png`, one PNG per `ELO_TIERS` entry, pasted by
`_drawEloBadge`/`_eloBadgeImage`), since PIL's bundled TTF fonts can't render
color emoji glyphs directly. The assets themselves were generated once, offline,
by rendering each tier's real emoji character through a color emoji font and
auto-cropping to its glyph bounding box, so there's no color-emoji-font
dependency in production. `_eloBadgeImage` loads, resizes, and caches each
tier's PNG the first time it's needed.

Card doesn't apply anymore once the card is up, so `_handleStatsShowCardClick`
re-renders with a fresh `StatsView(card_shown=True)`, which swaps Card out
for Back. (A persistent view's registered template covers every button
shape it might need; which ones a given message actually shows is decided
per-render, the same way `RosterActionView`'s own Reroll button is included
or omitted.) Pressing Back rebuilds the plain `/stats` embed
(`_buildStatsEmbed`) via `_swapTradingCardForStats`, sets
`stats_views.cardShown` back to 0, and swaps Back back out for Card: a real
back-and-forth toggle.

Avatar isn't touched by either swap, since it applies on both sides of the
embed/card divide. `_handleStatsAvatarToggleClick` branches on `cardShown`,
and once a card is up the toggle re-renders the whole card image in place
instead of swapping an embed thumbnail URL, since the avatar is baked into the
PNG. `_resolveCardAvatarImage` picks between `member`'s per-server avatar and a
plain `discord.User`'s account-wide one, and `stats_views.cardAvatarGlobal`
tracks which one is currently showing, reset to 0 every time the card is
(re-)entered so it always starts on the server avatar.

A card's look lives in `trading_cards` (one row per guild/player pair, same
self-healing "insert defaults on first read" shape `ensureEconomyRow` uses for
the economy table): `title`, `accent_color`/`background_color`/`text_color` as
`"#RRGGBB"` hex, and `font_style` (a named preset `_cardFontPaths` resolves to
actual bundled font files). Defaults are a saturated purple background, "Rookie"
as a placeholder title, and Chakra Petch/IBM Plex Sans as the font pairing.
`_renderTradingCardImage` always reads through `getCardSettings` rather than
hardcoding anything, so a changed row shows up on the next card rendered with no
code changes needed.

`/set correct-winner` fixes a specific tournament match via its optional
`match_id`, a narrower, separate path from the economy correction above. It
flips the match's recorded winner and re-propagates the bracket node, but
refuses if the next round has already started. It only supports winners-bracket
matches for now; correcting a losers-bracket or Grand Finals match is refused
with an explanatory message.

### Trading-card cosmetic unlocks

`card_unlocks` (one row per unlocked item, `(guildId, userId, itemType,
itemKey)`) permanently records which trading-card cosmetics a player has earned,
separately from `trading_cards`' own currently-equipped settings. Unlocking
something and actually wearing it are different concerns.

`CARD_TIER_REWARD_TITLES` is the tier-reward catalog: reaching Diamond, Master,
Grandmaster, or Challenger for the first time unlocks that tier's own title
(Diamond gives "Diamond Mind") and a matching color scheme whose accent is that
tier's own `ELO_TIER_BADGE_COLORS` entry, looked up from `ELO_TIERS` itself
rather than duplicated.

`_checkTierRewardUnlocks` checks every reward tier `elo` currently qualifies
for, not just whichever one it's presently sitting in, so a big enough single
swing that jumps straight from Platinum to Grandmaster still credits Diamond and
Master along the way. `_unlockCardReward`'s `INSERT OR IGNORE` makes checking
idempotent, and since nothing deletes from `card_unlocks`, a reward earned once
stays unlocked even after the player deranks back below the tier that earned it.

The unlock check runs from two places: `applyGameDeltas`, right after a ranked
result actually changes someone's elo (guarded to `sign > 0` only, since a
tournament correction's reversal is "undo," not "grant a reward on the way
down"), and lazily from `_buildStatsEmbed` every time `/stats` runs for someone.
`getUnlockedCardTitles`/`getUnlockedCardColorSchemes` read back what's unlocked
in customization-ready form: display strings for titles, `{name, accent_color,
background_color}` hex dicts for schemes.

`/card-set` is the command that consumes `getUnlockedCardTitles`,
`getUnlockedCardColorSchemes`, and `getUnlockedCardFontStyles` together.
`title`, `color_scheme`, and `font_style` are all optional params on the same
command, each with its own autocomplete offering the default plus whatever the
caller's personally unlocked, and any combination of the three can be set in one
call. `cardSetHelper` re-validates every provided field against its own catalog
before writing anything, so a bad value in one field can't leave another,
genuinely valid field half-applied.
`setCardTitle`/`setCardColorScheme`/`setCardFontStyle` are the trusting internal
setters `cardSetHelper` calls for whichever fields were actually given, and each
also flips `trading_cards.customized` to 1, the flag `ensureCardSettings`'s own
resync-to-defaults check respects.

The color-scheme half of `cardSetHelper` (`getUnlockedCardColorSchemes`) runs
each scheme's `ELO_TIER_BADGE_COLORS` accent through `_ensureReadableAccent`
(`CARD_MIN_ACCENT_CONTRAST`) before ever offering it, since a badge color was
picked for how it reads as a small circle standing in for an emoji, not for
driving a whole card's header/label text against its own darkened background.
Only the accent gets boosted. The background stays the badge color's raw
darkened shade either way, so a scheme's overall mood still authentically
reflects the tier that earned it.

`grantSpecialCardTitle` is the escape hatch for a title with no elo tier behind
it at all, a per-guild/player `card_unlocks` row with the same shape as a tier
reward's own title unlock. Shockwave's own developer gets "Developer" a
different way: `SHOCKWAVE_DEVELOPER_ID` is a hardcoded Discord user id
`getUnlockedCardTitles` checks directly, so it's available in every guild the
bot is in, including ones with no `card_unlocks` row for them at all.

### Shop

`/shop browse`/`/shop buy` add a third, gold-priced path into `card_unlocks` alongside
reaching an elo tier and a special grant. `CARD_SHOP_TITLES`,
`CARD_SHOP_COLOR_SCHEMES`, and `CARD_SHOP_FONT_STYLES` are the three catalogs
(name to price, color schemes also carrying their own hex pair), kept
name-distinct from each other and from every `ELO_TIERS`/`CARD_SPECIAL_TITLES`
name so `shopBuyHelper`'s single `item` parameter can resolve a purchase to its
category without needing to know it ahead of time.

`CARD_SHOP_COLOR_SCHEMES` is the biggest of the three: a handful of standalone
themes (Crimson, Emerald, Azure, Sunset, Fire) plus one per Runeterra region
(Demacia, Noxus, Freljord, Ionia, Piltover, Zaun, Shurima, Shadow Isles,
Bilgewater, Bandle City, Targon), the same region set `assets/clash-logos/`
covers for `/team set`'s `logo` option.

A purchase is `economy.balance -= price` plus the same `INSERT OR IGNORE INTO
card_unlocks` a tier reward or a special grant writes. There's no separate "did
I buy this" bookkeeping anywhere, so a purchased item shows up through
`getUnlockedCardTitles`/`getUnlockedCardColorSchemes` automatically. Font styles
are shop-only, since there's no elo-tier path to one at all, so
`getUnlockedCardFontStyles` is a plain lookup against `CARD_SHOP_FONT_STYLES`.

`shopHelper` lists every item grouped by category with its price and the
caller's current balance. An owned item still shows its price, with a ✅
appended, so comparing costs across a whole category never loses an
already-bought item's price from view. `shopBuyHelper` refuses an unknown
item, one already owned, or one the caller can't afford, and on success
tells them it's ready to equip with `/card-set`.

`ShopSortView` posts alongside the listing itself: "Sort: Price"/"Sort:
Owned" plus "Ascending"/"Descending" buttons that re-render the same embed
through `_buildShopEmbed` (the one place both the initial `/shop` call and
every later button click build the listing from, so they can never drift
apart) instead of needing the command re-run. Sorting only ever reorders the
items inside each category's own field; Titles, Color Schemes, and Fonts
never mix together. `interaction_check` restricts clicks to whoever ran
`/shop`, the same "only the original caller" guard every other confirmation
view in this file uses, and the view just freezes in place
(`SHOP_SORT_TIMEOUT_SECONDS`) once it times out rather than needing anything
restored, since re-sorting never touches gold or ownership.

The font half of `cardSetHelper`/`getAvailableCardFontStyles` is otherwise the
same shape as titles/color schemes for `trading_cards.font_style`.
`_cardFontPaths` resolves a `font_style` key to a dict:
`name_font`/`name_variation` and `title_font`/`title_variation` for the card's
two biggest typographic elements, `body_font` plus a
`label_weight`/`value_weight`/`team_weight` for everything smaller.

`CARD_SHOP_FONT_STYLES`' nine styles each back `name_font`/`title_font` with a
genuinely different bundled typeface: `"Bold"` is `RUSSO_ONE`, `"Elegant"` is
`CINZEL`, `"Handwritten"` is `PERMANENT_MARKER`, `"Cyber"` is `ORBITRON`,
`"Retro"` is `PRESS_START_2P`, `"Villain"` is `CREEPSTER`, `"Military"` is
`BLACK_OPS_ONE`, `"Neon"` is `BUNGEE`, `"Western"` is `RYE`, all pulled from
Google Fonts (SIL Open Font License) into `assets/fonts/`. `body_font` stays
`IBM_PLEX_SANS` for every style, so a style's effect on the smaller text is a
different named `_loadFont` weight of that same file. Cinzel and Orbitron are
variable fonts with a `name_variation`; every other style is a single static
weight, so its `_variation` fields are `None`.

Price tracks how loud a style reads, not when it was added. `"Bold"`,
`"Elegant"`, and `"Handwritten"` are the three "quiet" faces (a plain display
face, a plain serif, a plain handwritten marker) and sit at the cheaper of two
flat prices. Every other style commits hard to one specific loud aesthetic
(futuristic, pixel-arcade, horror, military stencil, neon sign, wanted-poster
western) and sits at the pricier of the two.

`PRESS_START_2P`'s near-monospace, unusually-wide-per-glyph metrics mean a long
real Discord username (up to 32 characters) rendered in it at the standard
`CARD_NAME_FONT_SIZE` could measure at or past `CARD_WIDTH` itself, with nowhere
left to draw the card's own border. Every other bundled font stays comfortably
clear of the edge at the same size. `_fitNameFont` handles this generally rather
than special-casing the one font: it shrinks whichever font/variation
`_renderTradingCardImage` picked, in `_loadFont`-sized steps, down toward
`CARD_NAME_MIN_FONT_SIZE`, until the name's measured width clears `CARD_WIDTH -
BRACKET_MARGIN * 2`. It's a no-op for the other five styles, since none of them
come close to the limit. Only the drawn font size changes; `name_y`/`title_y`
and the rest of the card's layout stay anchored to the fixed
`CARD_NAME_FONT_SIZE` slot regardless, so a shrunk name just leaves a little
extra breathing room under it.

`/card-set` shows the card, not just confirms the change in text.
`_renderMemberTradingCardFile` (the caller's own current
title/scheme/font/stats/teams/avatar, rendered once) and the thin
`_cardPreviewEmbedAndFile` wrapper around it render it, with the confirmation
string passed as `content=` alongside the `embed=`/`file=` pair.

`/shop preview type:<Logos|Card Titles|Color Schemes|Fonts>` (`previewHelper`)
is the "what are my options" counterpart to `/card-set`/`/team set`: a single
gallery image showing every option for one type, not just what a given player
has personally unlocked.

Logos and Color Schemes are real `PREVIEW_COLUMNS`-wide grids
(`_renderPreviewGridPage`, shared by both). They only differ in what `draw_cell`
puts inside a cell: a pasted-in logo image for Logos, a
background-fill-plus-accent-circle swatch of the scheme's own two colors for
Color Schemes. Each item's exact name/key sits underneath, so it doubles as a
lookup table for what to actually type.

Fonts and Card Titles skip the grid entirely. A font style has a real typeface
to show (`_cardFontPaths`) but nothing to lay out in columns, and a title has no
visual difference at all beyond the text, so both are simple one-column lists
instead. `_paginateGridItems` splits a grid onto more than one image if its
height would clear `PREVIEW_MAX_PAGE_HEIGHT`, though none of the four types are
anywhere close to that today.

Nothing here reads a specific player's unlocks. Logos comes from
`listAvailableLogos()`, Color Schemes from `CARD_DEFAULT_SCHEME_NAME` plus every
`CARD_SHOP_COLOR_SCHEMES` entry, Fonts from `CARD_DEFAULT_FONT_STYLE` plus every
`CARD_SHOP_FONT_STYLES` key, Card Titles from `CARD_DEFAULT_TITLE` plus every
`CARD_TITLE_CATALOG` value (achievement titles included). The gallery always
shows the complete catalog, purchasable-but-not-yet-owned items included.

Each type is rendered once and cached to `PREVIEW_DIR` (`assets/previews/`) as
`<stem>-1.png`, `<stem>-2.png`, and so on. `_cachedPreviewFiles` probes
sequentially until the next page is missing, so `/shop preview` never re-runs
Pillow on a later call unless the cached file(s) are deleted by hand. None of these
four catalogs change without a code change, so deleting the relevant file(s) is
how a developer forces a regenerate after adding a new logo/scheme/font/title.

### Achievements

`/achievements` and `CARD_ACHIEVEMENT_TITLES` add a fourth path into
`card_unlocks` (title only), alongside a tier reward, a special grant, and a
shop purchase. Same `INSERT OR IGNORE`-shaped row (`_unlockAchievement` uses
`rowcount` to tell a genuine first unlock from a no-op repeat), so an
achievement title shows up through `getUnlockedCardTitles`/`/card-set`
automatically like any other.

What's different is why one unlocks: conditions tied to actual gameplay rather
than rank or gold spent. First win (First Blood). A single tournament win
(Tournament Champion). Being rostered on `CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS`
(3)+ persistent teams at once (Team Player) or actually captaining one of them
(The Captain). Owning `CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS` (3)+ items bought
from `/shop` (Big Spender). A single-match elo swing of
`CARD_ACHIEVEMENT_UNDERDOG_ELO_GAIN` (20)+ (Giant Slayer). Winning a single
`CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD` (5000)+ gold bet (High Roller) or one paying
out `CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER` (3)x+ the wager (Jackpot).
Placing `CARD_ACHIEVEMENT_GAMBLER_BETS` (25)+ total bets (Frequent Bettor).
Racking up `CARD_ACHIEVEMENT_IRON_WILL_LOSSES` (20)+ game losses without giving
up (Iron Will). Running `/setup` for the first time (Onboarded).

Veteran and On Fire are each a ladder rather than a single condition: the same
`game_wins`/`current_win_streak` column crossing further thresholds for further,
genuinely distinct titles, so a card's epithet keeps meaning something as the
number climbs. Veteran (`CARD_ACHIEVEMENT_VETERAN_WINS`, 10) → Elite (50) →
Battle-Hardened (150) → Immortal (500) career wins. On Fire
(`CARD_ACHIEVEMENT_ON_FIRE_STREAK`, 5) → Unstoppable (10) → Untouchable (20) win
streak. Each ladder is walked as a `(threshold, achievement_key)` list, so
crossing a big enough number in one jump unlocks every rung up to it in the same
pass.

Gold-based achievements are keyed off a single transaction, never a balance
milestone, since `/daily` hands out free gold every day regardless of size. High
Roller, Jackpot, and Giant Slayer are checked inline inside `applyGameDeltas`'s
per-user loop, since each needs that specific event's own context. Every other
achievement lives in `_checkAchievements`, a single read against the caller's
current `economy` row, run from `applyGameDeltas` itself and lazily from
`_buildStatsEmbed`. `current_win_streak` is maintained right alongside those
checks: incremented on a win, reset to 0 on a loss, skipped entirely on
`sign=-1`.

Tournament Champion has no natural home in `applyGameDeltas`, since a tournament
win isn't a per-game delta at all, so `_grantTournamentChampionAchievement` is a
separate hook called from both places a tournament can end: single elimination's
`_startRound` and double elimination's `_resolveFinalsMatch`. It unlocks the
achievement for every rostered player on the winning team in one pass.

Earning an achievement also posts a notification, unlike the other unlock paths.
`applyGameDeltas` returns the list of newly-unlocked `(user_id,
achievement_key)` pairs for its callers to hand to `_announceAchievements`.
`reportCorrectWinnerHelper`/`_correctTournamentMatchHelper`'s own reapply half
does too, so a correction can still retroactively earn an achievement it should
have.

`/achievements` itself (`achievementsHelper`/`getAchievementCatalog`, no
permission gate) lists every achievement with its description and a ✅/🔒 marker
for the caller, self-healing via a `_checkAchievements` call first. The embed
groups fields the same way `/shop browse` groups by item type. Veteran and On Fire each
get their own field, tiers listed lowest-to-highest, and everything else lands
in a shared `__Other__` field.

### Team cards

`/team stats` gets the same card treatment as `/stats`, via its own
`TeamStatsView` (Card/Back buttons, same persistent shape as `StatsView` minus
the avatar toggle, a team card has no per-player avatar to flip): Card throws
the embed away for a portrait card (`_renderTeamCardImage`) built around the
team's own logo as the focal point.

Unlike the player trading card, there's no `trading_cards`-style settings row
backing this. "Color scheme matches the logo" means sampling a color straight
off the logo file itself on every render (`_dominantLogoColor`). That function
buckets a downscaled copy of the logo into 16-value RGB groups and returns the
most common one, skipping near-transparent pixels (the background) and
near-white/near-black ones (padding/outlines), so a logo's actual identifying
color wins out.

The sampled color becomes the card's accent (header title, frame, rule, stat
labels). The background is that same color darkened to 28% and lightened back up
30% for the vignette center, the same relationship the player card's
customizable `background_color` has to its own center, just derived instead of
stored.

A logo's dominant color has no readability guarantee, so `_ensureReadableAccent`
lightens the sampled color toward white just enough to clear
`TEAM_CARD_MIN_ACCENT_CONTRAST` above the vignette center, leaving an
already-readable color untouched. Only the background derivation uses the true,
unboosted sample. Every drawn accent element uses the boosted version.

Below the logo: the team's name, three stat rows (captain, record, win rate),
then its full roster (`_orderedRoster`, captain floated to the front) with the
captain marked by the same drawn star `_drawMatchupColumn` uses, capped at
`TEAM_CARD_MAX_ROSTER_ROWS` with a "+N more players" overflow line past that. A
team with no logo file at all falls back to `TEAM_CARD_FALLBACK_ACCENT_COLOR`
rather than a bare frame.

`TEAM_CARD_RETURN_EMOJI` (↩️) swaps back to the plain embed
(`_renderTeamStatsEmbed`) via `_swapTeamCardForStats` and restores 🛡️. Tracked
in its own `team_stats_views` table (`teamId` instead of a player's
`targetUserId`).

### Double elimination: losers bracket and Grand Finals

`/tournament create-bracket`'s double-elimination option builds a real losers
bracket. `buildLosersBracket` builds a second tree wired to the winners
bracket's own, and `/tournament start` plays winners bracket, then losers
bracket, then Grand Finals (with a bracket reset if needed) as one continuous
sequence, entirely on its own once started.

The losers bracket reuses `BracketNode` (two extra fields: `loser`, the team
that lost a winners-bracket match, and `drop_to`, the losers-bracket leaf that
loser feeds into) rather than a second node type.

Losers-bracket rounds alternate a fixed pattern for a k-round winners bracket.
Round 1 pairs up winners-round-1's losers against each other. Odd rounds after
that pair up last round's survivors against each other. Even rounds pair each
survivor against a fresh loser dropping in from winners round `r // 2 + 1`. A
winners-bracket bye produces no loser to drop down, so it's treated as a
losers-bracket bye too: auto-advance, no match created.

Because losers-bracket round sizes don't follow the winners bracket's simple
halving pattern, `Tournament` stores its rounds explicitly
(`losers_bracket_rounds`) rather than re-deriving them from the graph. Since the
losers bracket is rendered as an image too, a "fresh drop-in" leaf just gets
positioned at its actual round's x coordinate like any other node.
`/tournament print-bracket` posts the winners bracket and (for double
elimination) the losers bracket as two separate image attachments on the same
message.

Grand Finals (winners-bracket champion vs. losers-bracket champion) isn't part
of either bracket's own node graph. It's just another two rows in
`tournament_matches` (`bracketType='finals'`, `roundIndex` 0 for game one and 1
for the reset), created once both brackets have produced a champion. If the
winners-bracket champion wins game one, the tournament's over outright. If the
losers-bracket champion wins instead, both sides now have exactly one loss, so
`_resolveFinalsMatch` posts a second, decider match. `_tournamentChampionName`
reads that state back out, since there's no single "is this tournament over"
flag to check.

A 2-team double-elimination bracket is a genuine degenerate case: with only one
winners-bracket match total, its loser has nobody left to play, so they become
the "losers-bracket champion" with zero losers-bracket matches.
`buildLosersBracket` special-cases this rather than forcing the general
round-alternation pattern through it.

#### Interleaved losers bracket scheduling

By default (`losers_bracket_timing="after_winners"`), the losers bracket doesn't
start until the winners bracket has crowned its champion. The two brackets never
overlap in time.

Passing `losers_bracket_timing="interleaved"` instead makes the winners bracket
pause after each of its own rounds and let any losers-bracket round that round
just unlocked play out first, before continuing to the next winners round.

Which losers round depends on which winners round isn't 1-to-1, so
`buildLosersBracket` returns a third value, `wb_dependency`, alongside
`all_nodes`/`rounds`. `wb_dependency[i]` is the winners-bracket `round_index`
that losers round `i` needs fully resolved before it can start (or `None` if it
only depends on the previous losers round finishing). `Tournament` stores this
list and the timing string alongside the losers bracket itself.

The scheduling decision lives entirely in `_startRound` and
`_startLosersRound`'s own entry checks, not in the match-resolution tails that
call them:

- `_startRound(round_index)` first checks (via
  `_readyUnstartedLosersRoundIndex`) whether a losers round is unstarted and
  unlocked. If so it starts that instead of `round_index`, leaving the winners
  bracket paused there.
- `_startLosersRound(round_index)` checks whether its own round is actually
  unlocked yet. If not, it defers to `_advanceInterleavedTournament`.
- `_advanceInterleavedTournament` is the shared "what plays next" decision
  both of the above fall back to: start the next ready losers round if there is
  one, else resume the winners bracket if it still has a round to play, else
  attempt Grand Finals (safe to call unconditionally, since it no-ops until both
  brackets have a champion).

`_nextUnstartedWinnersRoundIndex`/`_nextUnstartedLosersRoundIndex` (and their
`*RoundFullyResolved` counterparts) answer "how far has this bracket actually
gotten" purely from `tournament_matches` rows, correctly treating an all-bye
round that never got a row as resolved once play has moved past it.

### Data model

| Table | Scope | Holds |
|---|---|---|
| `servers` | one row per guild | current team rosters, channel names, betting state, `is_ranked`, `wager_channel`, `active_tournament_match_id`, `betting_timer_seconds`, `current_game`/`game` (all admin-configurable via `/set`) |
| `economy` | one row per (guild, player) | balance, bet win/loss counts, gold wagered/won/lost - shared across every game a server plays (see `/set game`) |
| `game_stats` | one row per (guild, player, game) | elo, game win/loss counts, ranked win/loss counts, current win streak - split from `economy` since these mean nothing mixed across different games |
| `guild_games` | one row per (guild, game) | every game name a server has ever run `/set game` to, for its autocomplete suggestions; always seeded with `"League"` |
| `wagers` | active team-game bets (singleton, one per guild/player) | cleared out (paid or refunded) once the game resolves |
| `tournament_wagers` | active simultaneous-tournament-match bets (one per match/player) | cleared out once that specific match resolves |
| `duels` | active `/wager against` challenges | one row per challenge, several can be open at once |
| `leaderboards` | posted `/leaderboard` messages | which filter/order/page each message is currently showing, plus `cards`/`cardShown` for the Cards-button view |
| `my_team_views` | posted `/team lookup` messages | which page (and whose team list) each message is currently showing |
| `team_list_views` | posted `/team list` messages | which filter/sort/page each message is currently showing (`memberIds`/`memberNames` for the member filter), plus `cards`/`cardShown` for cards:true mode |
| `last_result` | one row per guild | a snapshot of the most recently resolved game, for `/set correct-winner` |
| `teams` | persistent named teams | one row per team: captain, roster, target size, voice channel, `logo_path` |
| `tournaments` | one row per guild | name, team/bracket size, elimination type, registered teams, the winners bracket, and (double elimination only) the losers bracket |
| `team_invites` | pending `/team invite`s | one row per invitee per invite. Several invitees from one `/team invite` call share a `messageId`, each accepting independently |
| `tournament_matches` | every tournament match ever played | which bracket (`bracketType`: winners/losers/finals) and round/bracket-node it's for, its two teams, state, (once decided) its winner, and `bettingClosed` |
| `player_role_preferences` | each player's liked/disliked roles from `/setup` | one row per (guild, player, role), `preference` is `like` or `dislike` |

