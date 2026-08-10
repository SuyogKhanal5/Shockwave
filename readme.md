# Shockwave:
A Discord bot for organizing team-based voice games. Split a voice channel into teams — randomly, by live captain draft, or elo-balanced for ranked play — and move everyone into the right channel automatically. Comes with a full gold economy (pari-mutuel betting, daily gold, heads-up wagers, a leaderboard) and a tournament system (persistent named teams, a real single- or double-elimination bracket, and sequential or simultaneous match play). Built around League of Legends' 5v5 format, but nothing about team formation, betting, or tournaments is League-specific. Utilizes the Discord API to pull server/client data.

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
- Every `@tree.command` is registered with no `guild=` at all, which makes
  it a *global* command definition rather than one tied to a specific
  server. `syncCommandsToGuild` (`copy_global_to` + a guild-scoped `sync`)
  is what actually publishes those definitions to a real server — called
  for every guild the bot is already in on `on_ready`, and again for
  whichever one it just joined on `on_guild_join`. This is a deliberate
  middle ground: a single global `tree.sync()` with no guild argument
  would work too, but can take up to an hour to propagate; syncing
  per-guild instead keeps registration effectively instant while still
  needing zero server-specific configuration.

### Team formation

`/make-teams` and `/captains` both end up building two `Team` objects
seeded from whoever is in the caller's voice channel, then serializing
them into the `team1`/`team2` columns on `servers` — nothing is moved yet.
`ranked:true` on either command does the same thing but calls
`formBalancedTeams` first (for `/make-teams`, routing straight to
`rankedTeamHelper` instead of the random/roles flow — see `fullRandom` in
`bot.py`): each player's elo gets a random ±100 nudge
(`ELO_BALANCE_JITTER`), the jittered list is sorted, and players are
handed out in a snake pattern (side A, B, B, A, B, B, A, …) so the two
sides land close in average elo without producing the exact same optimal
matchup every time. Forming a roster through any of these always runs
`clearTeamsHelper` first, which (among other things) resets `is_ranked` to
0 — only `ranked:true` sets it back to 1, which is what
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

Betting state for a guild is a finite state machine stored in the
`betting_state` column: `NONE → OPEN → CLOSED → AWAITING_RESULT → NONE`.
Once betting closes, `_bettingTimer` posts a message asking who won and
reacts to it with 🔵/🔴 (`TEAM_EMOJIS`) itself, then stores that message's id. Any
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

Elo only moves for games formed with `ranked:true`
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

`/tournament-print-bracket` renders it as an actual image (`renderBracketImages`,
via Pillow), not text — a real bracket tree with connecting lines and team
names, walking `previous`/`previous.opponent` all the way down to the
leaves the same way the old text renderer did. Discord has a hard 2000-
character limit on a message's text, which a bracket past a handful of
teams blows through fast (a 64-team double-elimination bracket is
something like 25,000 characters of ASCII art) — an image sidesteps that
entirely and renders fully inline at any size, rather than getting split
across a dozen-plus messages or truncated in a file-attachment preview.
`_assignBracketPositions` computes every node's pixel position in one pass
(a leaf's position comes from a shared counter so leaves stack top to
bottom in seed order; anything else is the midpoint of its two children),
sized purely from actual content bounds, so the canvas is exactly as big
as it needs to be — no ASCII-art-style column-width bookkeeping needed,
since pixel space doesn't have to stay tightly packed the way monospace
text did. `_drawBracketNode` then draws real lines (`ImageDraw.line`)
rather than box-drawing text characters, deliberately — that way nothing
depends on whatever font happens to be available having those glyphs.
`renderBracketText` still returns a short plain-text status line (which
team's currently the champion, `TBD` until decided) that goes out
alongside the image as the message's regular content.

Colors come straight from `shockwave-site/assets/styles.css`'s `:root`
palette (dark ink background, gold titles/champion, light body text, muted
connector lines) — the bracket image is meant to look like it belongs to
the same brand, not a plain black-on-white chart Pillow happened to
produce. Fonts do too: `assets/fonts/` bundles the same two families the
site's CSS uses (`--font-display`/`--font-body` — Chakra Petch for
anything headline-ish, IBM Plex Sans for body text), loaded via
`_loadFont` instead of Pillow's built-in default font, which is a small
bitmap face that looks rough once scaled up to heading sizes. Both are
Google/SIL-OFL-licensed and bundled rather than linked, so rendering
doesn't depend on network access or the host machine happening to have
them installed.

Every image is actually drawn `BRACKET_SUPERSAMPLE` (2) times bigger than
it's meant to end up, then downscaled with `Image.LANCZOS` resampling in
`_imageToFile` — the one place that scale gets undone, since every
renderer's output passes through there on its way to becoming a
`discord.File`. Pillow's `ImageDraw` has no antialiasing of its own, so a
line or a glyph edge drawn directly at 1x always comes out visibly jagged;
rendering bigger and shrinking down is the standard way around that.
Every pixel-valued layout constant (font sizes, margins, line widths,
radii, …) is already expressed at the supersampled scale, so the drawing
code itself never has to think about the scale factor — only
`_imageToFile` does.

A bracket 16+ teams deep (`BRACKET_TWO_SIDED_MIN_ROUNDS`) renders as two
mirrored halves converging toward a champion in the center — the same
layout a printed tournament bracket poster uses — instead of one long
strip, which keeps the image roughly square instead of very tall (a
64-team bracket goes from about 871×1714px one-sided to roughly square
two-sided). `_drawBracketNode` takes a `mirror` flag for this: it flips
the text anchor (`"rm"` instead of `"lm"`) and every connector offset, so
the right half is a genuine mirror-image layout rather than a raster flip
(which would've mirrored the letters themselves, not just the position).

The winners bracket splits at the champion's own two children, which are
always exactly even halves — `buildBracket` produces a perfectly balanced
tree, so this is a clean, symmetric split. The losers bracket can't use
that same split point: its last round is always a lopsided drop-in (a
deep surviving lineage vs a single bare leaf — the team that lost the
winners-bracket final outright), so splitting there would put an entire
tree on one side and one name on the other. Working out where the losers
bracket's two winners-bracket-side lineages actually *do* meet took a bit
of tracing through `buildLosersBracket`'s round-alternation pattern: every
round after round 1 keeps a match's winners-bracket-left and winners-
bracket-right losers strictly separate (a drop-in always pairs a survivor
against a fresh loser from the *same* side), right up until the second-to-
last round, which is always exactly one node — the first and only point
where the two sides genuinely merge. `_renderLosersTwoSidedTreeImage`
splits there instead, then extends one more ordinary, single-sided hop
past that merge point to reach the true champion — keeping the honest
asymmetry confined to that last small hop instead of the whole diagram.

### Playing a tournament out (`/tournament-start`)

Each pairing that's ready to play becomes its own row in
`tournament_matches`, holding the two teams, which round/bracket-node it
belongs to, and its own state — independent of any single guild-wide
"current game," since more than one of these can exist across a
tournament's lifetime (and, in simultaneous mode, within the same round).

Every match, either mode, also gets a **matchup graphic** posted alongside
its text announcement (`_renderMatchupImage`, called from `_postReadyCheck`/
`_postMatchReport`) — both teams' logos and rosters facing off, captain
starred and floated to the top of the list (`_orderedRoster`), and which
round of the tournament it is (`_matchRoundLabel`). It reuses the bracket
image's own canvas/header drawing code (`_createBracketCanvas`,
`_drawBracketHeader`) so it reads as the same product rather than a second
visual style bolted on.

**Sequential mode** genuinely reuses the ordinary game cycle rather than
reimplementing it: accepting a match's ready-check (✅, either captain)
sets `servers.team1`/`team2` to that match's two teams and calls
`_openBetting` — the exact function `/start` calls — so betting, the
🔵/🔴 winner report, and payouts all work unmodified. The only addition
is `active_tournament_match_id`, a column on `servers` that's `None` for
every ordinary game and only gets set while a tournament match is
borrowing the cycle; `recordResult` checks it once its normal work is
done and, if set, hands off to `_resolveTournamentMatch` to advance the
bracket. That's a small additive hook on otherwise heavily-tested shared
code, not a fork of it — zero behavior change for any non-tournament game.

**Simultaneous mode** can't reuse that cycle — `team1`/`team2` and
`betting_state` are guild-wide singletons, and simultaneous mode needs
several matches live at once — so it skips movement entirely and posts
every match's 🔵/🔴 report at once through its own lightweight reaction
path, scoped by each match's own row instead of guild state. Betting
still happens, just through a second, match-scoped path
(`_openConcurrentTournamentBetting`/`tournament_wagers`) instead of the
singleton `wagers` table `/start` uses — see "Concurrent tournament
betting" below.

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

### Concurrent tournament betting

The singleton `wagers` table (`PRIMARY KEY(guildId, userId)`) can only ever
represent one active bet per player per *guild* — fine for `/start` and
sequential-mode tournament matches, where there's only ever one game live
at a time, but structurally incapable of letting one player bet on several
matches at once, which simultaneous mode routinely has. Rather than bend
that table to fit, simultaneous-mode betting gets its own, genuinely
separate mechanism: a `tournament_wagers` table keyed by
`(matchId, userId)` instead of just `(guildId, userId)`, so the same
player can hold one bet per match across however many matches are open in
the round simultaneously.

`_openConcurrentTournamentBetting` opens one combined window covering
every match `_startRound`/`_startLosersRound`/`_startGrandFinals` just
queued for the round: the guild's configured per-match base
(`_getBettingTimerSeconds`, backing `/set-betting-timer`) times how many
matches are in the round, capped by `MAX_CONCURRENT_BETTING_SECONDS` so a
generous base times a big bracket's first round can't leave betting open
for an unreasonable stretch. `/wager` takes an optional `match_id` to say
which concurrently-open match a bet is for — omitted, it falls back to the
old singleton behavior for a casual/ranked game or a sequential-mode
match. Each match settles its own bets independently
(`_settleMatchWagers`, same pari-mutuel formula `computeGameDeltas` uses)
the instant it resolves, rather than waiting on the rest of the round —
one match finishing doesn't block or get blocked by any other
concurrently-open match's bettors getting paid.

### Team logos

`Team.logo_path` is a local file path, not image data — resolved against
`assets/clash-logos/` (Riot Games' official Clash-mode faction/region
logos, `/team-set-logo`'s autocomplete lists every file there by name) via
`_resolveLogoPath`. A team with no logo set gets one **assigned randomly**
the moment it's next loaded — `_ensureLogo`, called from every read path
(`getTeamRow`, `getTeamById`, `getTeamsForGuild`) as well as
`_saveNewTeam` — rather than needing a one-off migration for teams that
existed before this feature did; it just self-heals the first time each
one is touched again. `/team-stats` and `/my-teams` attach it as an embed
thumbnail via Discord's `attachment://<filename>` scheme (the file has to
be attached to the same message the embed references it from); the
matchup graphic (above) pastes it directly into the rendered image
instead.

`/report-correct-winner` fixes a specific tournament match via its
optional `match_id` — a narrower, separate path from the economy
correction described above. It flips the match's recorded winner and
re-propagates the bracket node, but refuses if the next round has already
started, rather than risk quietly corrupting a bracket that's moved on.
It only supports winners-bracket matches for now — correcting a losers-
bracket or Grand Finals match is refused with an explanatory message
rather than silently doing the wrong thing.

### Double elimination: losers bracket and Grand Finals

`/tournament-create-bracket`'s double-elimination option is a real losers
bracket, not just a flag — `buildLosersBracket` (`helper.py`) builds a
second tree wired to the winners bracket's own, and `/tournament-start`
plays winners bracket → losers bracket → Grand Finals (→ a bracket reset,
if needed) as one continuous sequence, entirely on its own once started,
the same "no repeat command needed" property single elimination already
had.

The losers bracket reuses `BracketNode` (two new fields: `loser`, the team
that lost a winners-bracket match, and `drop_to`, the losers-bracket leaf
that loser feeds into) rather than inventing a second node type. Losers-
bracket rounds alternate a fixed, well-known pattern for a *k*-round
winners bracket: round 1 pairs up winners-round-1's losers against each
other; odd rounds after that pair up last round's survivors against each
other; even rounds pair each survivor against a *fresh* loser dropping in
from winners round `r // 2 + 1`. A winners-bracket bye produces no loser
to drop down, so it's treated as a losers-bracket bye too — auto-advance,
no match created — and (rarely) two winners-bracket byes can land in the
same losers-bracket pairing, which just leaves that one slot permanently
unfilled, same as the equivalent winners-bracket edge case. Because losers-
bracket round sizes don't follow the winners bracket's simple halving
pattern, `Tournament` stores its rounds explicitly (`losers_bracket_rounds`)
rather than re-deriving them from the graph the way `_bracketRounds` does
for the winners side. Since the losers bracket is rendered as an image too,
this irregular shape doesn't need any special handling the way it would
have as text — a "fresh drop-in" leaf just gets positioned at its actual
round's x coordinate like any other node, landing in the right column
automatically without the leading-blank padding math a monospace-text
version would have needed. (See "Bracket images" above for how a large
losers bracket handles the two-sided layout differently from the winners
bracket, given that lopsided last round.) `/tournament-print-bracket` posts
the winners bracket and (for double elimination) the losers bracket as two
separate image attachments on the same message.

Grand Finals (winners-bracket champion vs losers-bracket champion) isn't
part of either bracket's own node graph — it's just another two rows in
`tournament_matches` (`bracketType='finals'`, `roundIndex` 0 for game one
and 1 for the reset), created once both brackets have produced a champion.
If the winners-bracket champion wins game one, the tournament's over
outright; if the losers-bracket champion wins instead, both sides now have
exactly one loss, so `_resolveFinalsMatch` posts a second, decider match
instead of ending things — whoever wins that one is champion regardless.
`_tournamentChampionName` is what actually knows how to read that state
back out (there's no single "is this tournament over" flag to check).

A 2-team double-elimination bracket is a genuine degenerate case: with
only one winners-bracket match total, its loser has nobody left to play,
so they become the "losers-bracket champion" with zero losers-bracket
matches — `buildLosersBracket` special-cases this rather than trying to
force the general round-alternation pattern through it.

#### Interleaved losers bracket scheduling

By default (`losers_bracket_timing="after_winners"`, `/tournament-create-
bracket`'s default), the losers bracket doesn't start at all until the
winners bracket has crowned its champion — the two brackets never overlap
in time. Passing `losers_bracket_timing="interleaved"` instead makes the
winners bracket pause after each of its own rounds and let any losers-
bracket round that round just unlocked play out first, before continuing
to the next winners round — "winners await the previous round's losers"
rather than "losers wait for winners to finish entirely."

Which losers round depends on which winners round isn't 1-to-1 (see the
round-alternation pattern above), so `buildLosersBracket` returns a third
value, `wb_dependency`, alongside `all_nodes`/`rounds`: `wb_dependency[i]`
is the winners-bracket `round_index` that losers round `i` needs fully
resolved before it can start (or `None` if it only depends on the previous
losers round finishing, no new winners-bracket input). `Tournament` stores
this list and the timing string alongside the losers bracket itself
(`set_losers_bracket`'s third argument, `set_losers_bracket_timing`).

The actual scheduling decision lives entirely in `_startRound` and
`_startLosersRound`'s own entry checks, not in the match-resolution tails
that call them (`_resolveTournamentMatch`/`_resolveLosersMatch` still just
call `_startRound(round_index + 1)` / `_startLosersRound(round_index + 1)`
unconditionally, exactly as in `after_winners` mode):

- `_startRound(round_index)` first checks (via `_readyUnstartedLosersRoundIndex`)
  whether a losers round is unstarted and unlocked; if so it starts that
  instead of `round_index`, leaving the winners bracket paused there until
  something calls back in.
- `_startLosersRound(round_index)` checks whether *its own* round is
  actually unlocked yet; if not, it defers to `_advanceInterleavedTournament`
  instead of starting a round whose dependency hasn't resolved.
- `_advanceInterleavedTournament` is the shared "what plays next" decision
  both of the above fall back to: start the next ready losers round if
  there is one, else resume the winners bracket if it still has a round to
  play, else attempt Grand Finals (safe to call unconditionally — it
  silently no-ops until both brackets actually have a champion).

`_nextUnstartedWinnersRoundIndex`/`_nextUnstartedLosersRoundIndex` (and
their `*RoundFullyResolved` counterparts) answer "how far has this bracket
actually gotten" purely from `tournament_matches` rows, correctly treating
an all-bye round that never got a row as resolved once play has moved past
it — the same ambiguity `buildLosersBracket`'s bye-collision handling
above already has to account for. Because `_startRound`'s terminal branch
(the "winners bracket complete" announcement) is only ever reached via the
one natural, unconditional call from `_resolveTournamentMatch`'s own tail —
never re-derived lazily from state — the announcement and the eventual
`_startGrandFinals` call each still only fire exactly once, the same
guarantee `after_winners` mode has always had.

### Data model

| Table | Scope | Holds |
|---|---|---|
| `servers` | one row per guild | current team rosters, channel names, betting state, `is_ranked`, `wager_channel`, `active_tournament_match_id`, `betting_timer_seconds` (`/set-betting-timer`) |
| `economy` | one row per (guild, player) | balance, elo, bet & game win/loss counts, gold wagered/won/lost |
| `wagers` | active team-game bets (singleton — one per (guild, player)) | cleared out (paid or refunded) once the game resolves |
| `tournament_wagers` | active simultaneous-tournament-match bets (one per (match, player)) | cleared out once that specific match resolves — see "Concurrent tournament betting" |
| `duels` | active `/wager-against` challenges | one row per challenge, several can be open at once |
| `leaderboards` | posted `/leaderboard` messages | which filter/order/page each message is currently showing |
| `my_team_views` | posted `/my-teams` messages | which page (and which caller's team list) each message is currently showing |
| `team_list_views` | posted `/team-list` messages | which filter/sort/page each message is currently showing |
| `last_result` | one row per guild | a snapshot of the most recently resolved game, for `/report-correct-winner` |
| `teams` | persistent named teams | one row per team — captain, roster, target size, voice channel, `logo_path` (see "Team logos") |
| `tournaments` | one row per guild | name, team/bracket size, elimination type, registered teams, the winners bracket, and (double elimination only) the losers bracket |
| `team_invites` | pending `/team-invite`s | one row per invitee per invite — several invitees from one `/team-invite` call share a `messageId`, each accepting independently |
| `tournament_matches` | every tournament match ever played | which bracket (`bracketType`: winners/losers/finals) and round/bracket-node it's for, its two teams, state, (once decided) its winner, and `bettingClosed` |
