# Shockwave:
A Discord bot for organizing team-based voice games. Split a voice channel into teams — randomly, by live captain draft, or elo-balanced for ranked play — and move everyone into the right channel automatically. Comes with a full gold economy (pari-mutuel betting, daily gold, heads-up wagers, a leaderboard) and a tournament system (persistent named teams, a real single-elimination bracket, and sequential or simultaneous match play). Built around League of Legends' 5v5 format, but nothing about team formation, betting, or tournaments is League-specific. Utilizes the Discord API to pull server/client data.

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

### Redirecting where bets get posted (`/wager-set-channel`)

By default every betting message (open/closed/winner-report) goes to
wherever `/start` — or a tournament match — happened to run. Setting a
wager channel changes that: `_openBetting` (the shared core both `/start`
and a sequential tournament match call) resolves `servers.wager_channel`
by name right before anything else, and swaps it in for the channel it
was handed. Since every later step in the cycle (`_bettingTimer`, the
winner report, `recordResult`) just keeps using whatever channel it was
given, redirecting at that one entry point is enough to redirect the
whole thing.

### Admin resets and permissions

`/clear` requires the **Manage Server** permission outright
(`app_commands.checks.has_permissions`, same as `/report-correct-winner`).
Within it, `clear_elo` and `clear_economy` additionally act on *every
player* in the server, so neither runs the moment the command is invoked
— `/clear` posts a `discord.ui.View` with "Confirm reset"/"Cancel" buttons
(`ConfirmResetView`), and the reset only happens from inside that view's
button callback. `interaction_check` on the view rejects anyone who isn't
the member who ran `/clear`, and the view times out after 30 seconds with
nothing changed if it's ignored. `/tournament-create` follows a narrower
version of the same idea: creating a server's *first* tournament needs no
permission at all, but overwriting an existing one checks
`ctx.user.guild_permissions.manage_guild` before it will even show the
confirmation view.

### Persistent teams

Separate from the ephemeral `team1`/`team2` a `/make-teams` or `/captains`
game produces, `/team-create` writes a row to a dedicated `teams` table —
one per named team, keyed by its own autoincrement id, with a serialized
`Team` (captain, roster, target size, voice channel) as its payload. A
player can sit on more than one team's roster in this table; nothing
about team membership itself is exclusive. `/team-invite` uses the same
react-to-accept pattern as everything else that needs a specific person's
consent (`TEAM_INVITE_ACCEPT_EMOJI`, its own `team_invites` table keyed by
message id) — the captain check on both invites and voice-channel changes
relies on `Team.get_captain()` actually returning a real `Player` object,
which turned out to need its own fix (see below). `/team-use` is the
shortcut: it loads two persistent teams straight into `team1`/`team2` so
a casual or ranked game can start immediately, without cloning any state
back into the `teams` table — the in-memory copy gets `set_id(1)`/`set_id(2)`
purely for `movefunc`'s sake.

**Bugs fixed to make this possible:** `Team.deserializeTeam` never parsed
its `id` or `captain` fields back to real types after a database
round-trip — `id` stayed a string, and `captain` stayed the raw
`"(id,name)"` text instead of a `Player`. Neither had ever been read
anywhere before persistent teams existed, so the bugs were silent; they'd
have broken every captain-permission check the moment they went live.

### Tournaments and the bracket

`Tournament` (in `TourneyClasses.py`) holds a name, team size, bracket
size, elimination type, its registered teams, and its bracket — one
`tournaments` row per guild, `INSERT OR REPLACE`d as a whole each time
(`saveTournament`/`getTournament`), since a server only ever has one.
`register_team` is the one piece of business logic that lives on the
class itself rather than in `helper.py`: it rejects a team if any of its
players are already on a team registered for that *same* tournament,
while leaving the shared `teams` table alone — the same player can freely
be on other teams elsewhere.

The bracket is a real linked structure, not just a list of pairings.
`BracketNode` has three pointers: `opponent` (its paired node this round),
`next` (the node its winner advances into — `None` only for the finals
slot), and `previous` (one of the two nodes that feed into it — the other
is reachable via `previous.opponent`, so one pointer is enough to
reconstruct the full pairing). `buildBracket` shuffles the registered
teams, rounds the count up to the next power of two, and wires every
round's nodes to the next in one pass; slots beyond the real team count
are byes (`team=None`), auto-advanced with no match created for them.
Since a graph of objects can't go through `json.dumps` directly,
`serialize_bracket`/`deserialize_bracket` convert to/from a flat list of
`{team, opponent, next, previous}` dicts referencing each other by index
into that same list — reconstructed into real object pointers on load.

`/tournament-print-bracket` renders it as text: real team names for round
1, and one level of `"Winner of (X vs Y)"` resolution for the round right
after that (via `_nodeLabel`, walking `previous`/`previous.opponent`) —
deliberately capped at one level so the text doesn't grow exponentially
for a large bracket. Rounds further out just show `TBD`.

### Playing a tournament out (`/tournament-start`)

Each pairing that's ready to play becomes its own row in
`tournament_matches`, holding the two teams, which round/bracket-node it
belongs to, and its own state — independent of any single guild-wide
"current game," since more than one of these can exist across a
tournament's lifetime (and, in simultaneous mode, within the same round).

**Sequential mode** genuinely reuses the ordinary game cycle rather than
reimplementing it: accepting a match's ready-check (✅, either captain)
sets `servers.team1`/`team2` to that match's two teams and calls
`_openBetting` — the exact function `/start` calls — so betting, the
1️⃣/2️⃣ winner report, and payouts all work unmodified. The only addition
is `active_tournament_match_id`, a column on `servers` that's `None` for
every ordinary game and only gets set while a tournament match is
borrowing the cycle; `recordResult` checks it once its normal work is
done and, if set, hands off to `_resolveTournamentMatch` to advance the
bracket. That's a small additive hook on otherwise heavily-tested shared
code, not a fork of it — zero behavior change for any non-tournament game.

**Simultaneous mode** can't reuse that cycle, since `team1`/`team2` and
`betting_state` are guild-wide singletons and simultaneous mode needs
several matches live at once — so it skips movement and betting entirely
and posts every match's 1️⃣/2️⃣ report at once through its own lightweight
reaction path, scoped by each match's own row instead of guild state.

Either way, resolving a match funnels through the same
`_resolveTournamentMatch`: it flips the match to `RESOLVED` before doing
anything `await`-based (the same double-processing guard used everywhere
else reactions resolve something), propagates the winner into the shared
bracket node, and prints the updated bracket. Once every match in a round
has resolved — not just the two teams' own next match being ready to go —
it posts a "Round N has ended!" transition message with a fresh bracket
and starts the next round, or announces the champion if there isn't one.
None of this involves a sleep or a blocking wait anywhere: it's reactions
calling reactions, so other commands (including `/wager` on an unrelated
game) keep working the entire time a tournament round is in progress.

`/report-correct-winner` fixes a specific tournament match via its
optional `match_id` — a narrower, separate path from the economy
correction described above. It flips the match's recorded winner and
re-propagates the bracket node, but refuses if the next round has already
started, rather than risk quietly corrupting a bracket that's moved on.

### Data model

| Table | Scope | Holds |
|---|---|---|
| `servers` | one row per guild | current team rosters, channel names, betting state, `is_ranked`, `wager_channel`, `active_tournament_match_id` |
| `economy` | one row per (guild, player) | balance, elo, bet & game win/loss counts, gold wagered/won/lost |
| `wagers` | active team-game bets | cleared out (paid or refunded) once the game resolves |
| `duels` | active `/wager-against` challenges | one row per challenge, several can be open at once |
| `leaderboards` | posted `/leaderboard` messages | which filter/order/page each message is currently showing |
| `last_result` | one row per guild | a snapshot of the most recently resolved game, for `/report-correct-winner` |
| `teams` | persistent named teams | one row per team — captain, roster, target size, voice channel |
| `tournaments` | one row per guild | name, team/bracket size, elimination type, registered teams, the bracket itself |
| `team_invites` | pending `/team-invite`s | one row per invite, several can be open at once |
| `tournament_matches` | every tournament match ever played | which round/bracket-node it's for, its two teams, state, and (once decided) its winner |
