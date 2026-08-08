# Shockwave:
A discord bot that allows users to easily create teams for 10 person League of Legends games. The teams can either be created randomly or through the use of team captains. The users will also be moved automatically to the appropriate voice channel once the teams are created. Utilizes the discord api to pull server/client data.

Full list of commands on shockwave.netlify.app

## Installing dependencies

Requires Python 3.10+. Install the dependencies with:

```
pip install -r requirements.txt
```

## How it works

This section is about *why* the code is shaped the way it is, not how to
run the commands — for that, see shockwave.netlify.app.

### Architecture

- **`bot.py`** — creates the `discord.Client`/`app_commands.CommandTree`,
  owns the sqlite connection and schema setup, and registers every slash
  command. Command bodies are thin: almost every command immediately hands
  off to a matching method on a single shared `helper.helpers` instance
  (`helperObj`).
- **`helper.py`** — the actual logic. One `helpers` class holds the
  `cursor`/`db` and every command's real implementation, plus the
  `on_raw_reaction_add` handlers that react to emoji clicks (reporting a
  winner, accepting a wager, paging a leaderboard).
- **`TourneyClasses.py`** — `Player` and `Team`, small data classes with
  hand-rolled `serialize`/`deserialize` methods so a team roster can be
  stored as a plain string in a sqlite `TEXT` column (see below) instead of
  needing a separate table.
- State lives in one sqlite database (`data/guildData/serverInfo/main.db`),
  one row per guild in `servers` for "current session" state (team
  rosters, channel names, betting status), plus `economy`, `wagers`,
  `duels`, `leaderboards`, and `last_result` for everything gold/elo
  related. New columns/tables are added with `ensure_column()` on startup
  rather than requiring a fresh database on every feature addition.

### Team formation

`/make-teams` and `/captains` both end up building two `Team` objects
seeded from whoever is in the caller's voice channel, then serializing
them into the `team1`/`team2` columns on `servers` — nothing is moved yet.
`/ranked` and `/ranked-captains` do the same thing but call
`formBalancedTeams` first: each player's elo gets a random ±100 nudge
(`ELO_BALANCE_JITTER`), the jittered list is sorted, and players are
handed out in a snake pattern (side A, B, B, A, B, B, A, …) so the two
sides land close in average elo without producing the exact same optimal
matchup every time. Forming a roster through any of these always runs
`clearTeamsHelper` first, which (among other things) resets `is_ranked` to
0 — only `/ranked`/`/ranked-captains` set it back to 1, which is what
later gates whether a reported result touches anyone's elo at all.

### Voice moves & the betting window

`/start` (`movefunc`) is the only command that actually moves players —
one `move_to()` Discord API call per member, which is slow enough to blow
the 3-second interaction window, so the command `defer()`s immediately and
confirms via a followup once every move finishes. Opening betting
(`startBettingHelper`) works the same way for a different reason: betting
has to stay open for 60 seconds while the bot keeps responding to *other*
commands, so the countdown runs as its own `asyncio.create_task`
(`_bettingTimer`), tracked per-guild in `self.bettingTasks` so a `/return`
or a fresh `/start` can cancel it instead of leaving it to fire later
against a game that no longer exists.

### Resolving a winner

Betting state for a guild is a small state machine stored in the
`betting_state` column: `NONE → OPEN → CLOSED → AWAITING_RESULT → NONE`.
Once betting closes, `_bettingTimer` posts a message asking who won and
reacts to it with 1️⃣/2️⃣ itself, then stores that message's id. Any
non-bot reaction anywhere goes through `on_raw_reaction_add` →
`handleWinnerReaction`, which checks the emoji, the stored message id, and
the state, then **flips the state to `NONE` synchronously before doing
anything `await`-based** — that ordering matters, since it's what stops
two near-simultaneous reactions (e.g. someone double-clicking, or two
different people reacting within milliseconds of each other) from both
passing the check and paying out twice. The same
flip-before-await-anything pattern shows up again in `_acceptDuel` and
`_resolveDuel` for `/wager-against`.

### The economy

Payouts are pari-mutuel: everyone who bet on the winning team splits the
losing team's entire pool, proportional to their own wager, on top of
getting their own wager back
(`payout = amount + (amount / winningPool) * losingPool`) — so a bet on
the side fewer people backed pays out more than the same-sized bet on the
favorite. `computeGameDeltas` is a **pure function**: given the wagers and
rosters, it returns a plain dict of `{user_id: {balance, wins, losses, …}}`
deltas without touching the database at all. `recordResult` is what
actually calls `applyGameDeltas` to write them. Keeping the math and the
writing separate is what makes `/report-correct-winner` possible (below).

### Elo & ranked play

Elo only moves for games formed with `/ranked`/`/ranked-captains`
(`is_ranked` on the guild row) — a casual `/make-teams` game updates the
Game Record but never touches elo. When it does apply, it's a standard
Elo update: `expected = 1 / (1 + 10 ** ((their_avg - your_avg) / 400))`,
`delta = round(32 * (actual_result - expected))`, computed once per team
using each side's *average* rating. `/stats` and `/leaderboard` translate
the raw number into a League-style tier via `eloRankLabel` — nine tiers
spaced 250 elo apart (1000 default elo lands new players in Platinum),
with Iron through Diamond further split into four divisions each; Master
and above show no division, matching League's switch to raw LP at that
point.

### Correcting a misreported winner

`/report-correct-winner` can't just recompute the game from scratch,
because by the time someone notices a misreport, elo ratings have already
moved — recomputing against *current* (already-wrong) ratings would give
the wrong correction. Instead, `recordResult` calls `saveLastResult` to
snapshot exactly what was applied (the wagers, both rosters, the computed
deltas, and whether the game was ranked) into the `last_result` table.
Correcting a result means: apply the saved deltas with `sign=-1` to undo
them exactly, recompute fresh deltas against the now-restored elo values
for the *correct* winner, apply those, and save a new snapshot — so a
second correction is possible from the new baseline too.

### Heads-up wagers (`/wager-against`)

A 1-on-1 side bet between two specific players, deliberately kept
independent of the team-game betting above (own table, own emoji, no
`/start` required). Unlike the single-active-game betting state stored
directly on the `servers` row, several duels can be open at once between
different pairs of players in the same guild, so each one gets its own row
in `duels` keyed by the current message id. Nothing is escrowed at
challenge time — only a balance sanity-check — so a challenge that's never
accepted doesn't leave anyone's gold stuck. Both players' gold is only
locked once the *target* reacts ✅ to accept, at which point a second
message goes out for anyone to react 🔵/🔴 on, resolving the same way a
team game's winner does.

### Leaderboard paging

`/leaderboard` builds the full sorted/filtered player list once, up
front, then only ever sends *one* message. Reacting with ⏮️ ◀️ ▶️ ⏭️
doesn't post anything new — `handleLeaderboardReaction` looks up the
stored filter/order/page for that message id in the `leaderboards` table,
recomputes the requested page, and calls `message.edit()` on the original
message. Missing stats (e.g. a win rate with zero games played) sort to
the bottom regardless of ascending/descending order, rather than a
`None`/0 value looking like the best or worst score on the board.

### Admin resets (`/clear`)

`clear_elo` and `clear_economy` both act on *every player* in the server,
so neither runs the moment the command is invoked — `/clear` posts a
`discord.ui.View` with "Confirm reset"/"Cancel" buttons
(`ConfirmResetView`), and the reset only happens from inside that view's
button callback. `interaction_check` on the view rejects anyone who isn't
the member who ran `/clear`, and the view times out after 30 seconds with
nothing changed if it's ignored.

### Data model

| Table | Scope | Holds |
|---|---|---|
| `servers` | one row per guild | current team rosters, channel names, betting state, `is_ranked` |
| `economy` | one row per (guild, player) | balance, elo, bet & game win/loss counts, gold wagered/won/lost |
| `wagers` | active team-game bets | cleared out (paid or refunded) once the game resolves |
| `duels` | active `/wager-against` challenges | one row per challenge, several can be open at once |
| `leaderboards` | posted `/leaderboard` messages | which filter/order/page each message is currently showing |
| `last_result` | one row per guild | a snapshot of the most recently resolved game, for `/report-correct-winner` |
