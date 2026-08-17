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
Both commands check `ctx.user.voice`/`.channel` up front and reply with a
plain explanation ("You need to be in a voice channel to...") instead of
letting the `AttributeError` that used to happen reach the caller as a
silent failure; `/captains` additionally still needs at least two people
in that channel once the voice check itself passes.
Each `Team`'s `.name` comes from `_rosterTeamNames(guild_id)` — an admin's
configured `channel1`/`channel2` names (`/set`'s `team1`/`team2` params) if
there are any, otherwise the generic "Team 1"/"Team 2" fallback. That name
is what `printEmbed` titles the roster with, what `_renderMatchupImage`
labels the matchup graphic with, and — threaded through
`computeGameDeltas`/`recordResult`/`saveLastResult` all the way to
`formatResultMessage` and `reportCorrectWinnerHelper` — what the win
announcement, the elo-change line, and a later correction all say too, so
a server that's named its channels "Red"/"Blue" sees "Red"/"Blue"
everywhere a game touches, not a mix of that and "Team 1"/"Team 2".
`getRosterName(guild_id, column, fallback)` is the read-side counterpart —
`recordResult` and `/wager`'s confirmation both use it to recover the
*currently loaded* roster's name, while `saveLastResult` snapshots the
names actually shown at the time so a correction later still says the
right thing even if a newer roster (with different names) has since been
formed.
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

**BUG FIX:** `clearTeamsHelper` used to wipe `team1`/`team2`/
`original_channel` unconditionally, with no check for whether a game built
from them was still actively being bet on or played out. Since every
team-formation command *and* `/clear` itself all funnel through here,
simply starting a fresh roster while a previous one's bets hadn't
resolved yet — or running `/clear` for something as unrelated as
`clear_elo` — silently orphaned the game in progress: `getRosterPlayers`
would find nothing once a winner was finally reported, so no elo/game-
record/win-loss-gold ever applied (no "Elo:" line in the result message
either) and `moveMembersToOriginalChannel` would find `original_channel`
already blanked out (no "Moved everyone back" message), both without any
error or explanation. `clearTeamsHelper` now checks `betting_state`
first and calls `cancelGameHelper` (the same refund + move-back + "Game
cancelled" notice 🛑's own reaction triggers) before wiping anything, so
an in-flight game is always cleanly resolved rather than silently
destroyed.

`/reuse` (`reuseTeamsHelper`) re-posts whichever two rosters `/make-teams`,
`/captains`, or `/team-use` most recently produced, without drawing a
fresh random split, elo-balanced split, or captains draft. This works
because nothing clears `team1`/`team2` just because a game *resolved* —
only the next team-forming command's `clearTeamsHelper` call does that
(see the bug fix above) — so they already hold exactly the last game's
roster right up until something overwrites them. `/reuse` reads them
back, along with `mode`/`is_ranked`/`roster_use_roles`, and never writes
any of the three: a reused ranked game stays ranked, a casual one stays
casual, and a role-eligible roster keeps showing role labels, matching
whatever the original game actually was rather than some `/reuse`
default. If a game built from those same teams is still being bet on or
played, it's cancelled first (`cancelGameHelper` — refund + move back),
the same safety net `clearTeamsHelper` uses, just without the team1/team2
wipe that comes with it, since reusing them is the entire point.

### Voice moves & the betting window

There's no standalone `/start` command anymore — moving players and
opening betting both live behind a ▶️ reaction (`TEAM_START_EMOJI`) that
`_finalizeRoster` adds to the *second* of the two team embeds every
roster-forming path posts (`/make-teams`, ranked or not; `/captains` once
the draft actually finishes; `/team-use`). `printEmbed` now returns both
posted messages so its callers can hand them to `_finalizeRoster`, which
records `roster_team1_message_id`/`roster_team2_message_id`/
`roster_channel_id` on the guild's `servers` row — the same "remember
which message is still live" shape `betting_message_id` already used for
the winner-report reaction, and for the same reason: it's what lets
`handleRosterReaction` tell a stale roster's ▶️ apart from the current
one, since forming a new roster just overwrites those columns.
`_finalizeRoster` also adds a ⚡ reaction (`TEAM_START_NO_MOVE_EMOJI`)
right alongside ▶️ — same message, same guard columns — for a group
that's already elsewhere (a stage channel, another platform, in person)
and doesn't want Shockwave touching anyone's voice state at all.

Clicking either reaction runs `_startRosterViaReaction(guild_id, channel,
payload, move)`, `move=True` for ▶️ and `move=False` for ⚡ — the same
function either way, just with the whole channel-move block skipped for
⚡. For ▶️, since a reaction has no `ctx.user` the way an Interaction does,
the "channel to send everyone back to later" is found by scanning the
roster's own players for whichever one is *currently* sitting in a voice
channel (`_findRosterVoiceChannel`), rather than assuming the clicker
themselves is in voice — anyone can click it, not just someone at the
table. `channel1`/`channel2` (set by `/set`'s `team1`/`team2` params,
admin-only) are looked up next; if either is missing — `/set` was never
run, or the named channel got deleted — `_ensureDefaultTeamChannels`
self-heals onto `DEFAULT_TEAM_CHANNEL_NAMES` (`"Team-1"`/`"Team-2"`),
creating whichever one doesn't already exist and writing them back to
`channel1`/`channel2` so this only happens once per guild, rather than
refusing to start the game at all. ⚡ skips all of that — nobody has to be
in a voice channel at all to click it — and explicitly clears
`original_channel` back to `""` rather than leaving it alone, so
`moveMembersToOriginalChannel` no-ops once the game ends (winner reported
or cancelled): nothing moved at the start, nothing to move back either.
That clear matters even though ⚡ itself never *sets* `original_channel` —
`captainsHelper` captures the drafting caller's voice channel the moment a
`/captains` draft starts (in case everyone's since left voice by the time
a reaction is finally clicked), and a stale value from an earlier ▶️ game
is possible too, so ⚡ has to override whatever's already there rather than
just not touching it.
`roster_team2_message_id` is cleared
**synchronously**, before any `await`, the moment the (▶️-only) checks
above it pass — the same "flip before doing anything async" shape
`handleGameReportReaction`'s own `betting_message_id` clear uses, so two
near-simultaneous ▶️/⚡ clicks can't both pass the guard and start the game
twice. Once the moves are done (or skipped, for ⚡), it posts the same
matchup graphic a tournament match gets (`_sendMatchupImage` → the
tournament path's own `_renderMatchupImage`, just with no match id or
tournament name in the subtitle) and calls `_openBetting`. Betting has to
stay open for 60 seconds while the bot keeps responding to *other*
commands, so the countdown runs as its own `asyncio.create_task`
(`_bettingTimer`), tracked per-guild in `self.bettingTasks` so
CANCEL_GAME_EMOJI or a fresh ▶️/⚡ click can cancel it instead of leaving
it to fire later against a game that no longer exists. The headline text
comes from whichever `mode` string
(`"Normal"`/`"Ranked"`/`"Captains"`/`"Ranked Captains"`) the most recent
team-forming command left in `servers` (`_matchupLabelForMode`), so it
reads correctly no matter how the two teams got there.

A 🔄 reaction (`TEAM_ROLES_REROLL_EMOJI`) sits alongside ▶️/⚡ on that same
message, but only when the roster actually qualifies (`use_roles` was set
*and* both teams landed at exactly 5 — the same condition
`makeEmbedString` already used to decide whether to draw roles at all).
Clicking it (`_rerollRoster`) genuinely re-shuffles both teams' player
order and persists it back to `team1`/`team2`, then edits *both* posted
messages in place with freshly role-labeled embeds — a real fix over the
command this replaced (`/randomize-roles`/`randomRoleHelper`), which
shuffled into a `result1`/`result2` text pair nothing ever displayed and
never wrote the shuffle back to `team1`/`team2` at all, so the embeds
`/make-teams` itself posted never actually reflected a reroll no matter
how many times it ran. `_clearPagingReaction` removes the clicker's own
reaction afterward (same as leaderboard paging) so 🔄 stays clickable for
another reroll.

### Resolving a winner, or cancelling the game

Betting state for a guild is a finite state machine stored in the
`betting_state` column: `NONE → OPEN → CLOSED → NONE`. Unlike the old
flow, the winner-report message doesn't wait for betting to close —
`_openBetting` posts it immediately, in the same message as "betting is
open," pre-reacted with 🔵/🔴 (`TEAM_EMOJIS`) *and* 🛑
(`CANCEL_GAME_EMOJI`) right from the start, and stores that message's id
before returning. A real game doesn't wait for a 60-second countdown to
finish before anyone knows who won, so reporting (or cancelling) is valid
the whole time `betting_state` is `OPEN` *or* `CLOSED` — `_bettingTimer`
firing after the configured duration only flips `OPEN → CLOSED` and posts
a short "betting is now closed" notice; it doesn't touch the report
message or its reactions at all.

Any non-bot reaction anywhere goes through `on_raw_reaction_add` →
`handleGameReportReaction`, which checks the emoji (a `TEAM_EMOJIS` pick,
or `CANCEL_GAME_EMOJI`), the stored message id, and that
`betting_state` is `OPEN`/`CLOSED`, then **clears `betting_message_id`
synchronously before doing anything `await`-based** — that ordering
matters, since it's what stops two near-simultaneous reactions (e.g.
someone double-clicking, or two different people reacting within
milliseconds of each other) from both passing the check and double-
processing the same game. It's the stored message id that gets cleared
rather than `betting_state` itself, since the cancel path still needs to
read the real, un-flipped `betting_state` afterward to know whether
there's anything to refund. `_cancelBettingTimerTask` stops the running
timer either way, since a winner can now be reported (or the game
cancelled) while it's still counting down. A `TEAM_EMOJIS` pick calls
`recordResult`, exactly as before; `CANCEL_GAME_EMOJI` calls
`cancelGameHelper` instead — the reaction-driven replacement for the old
`/return` command, living on the exact same message. It refunds any open
bets and resets betting state via `cancelBettingHelper` (also clearing
`active_tournament_match_id`, so an abandoned tournament match's
bracket-advance hook can't fire against whatever unrelated game starts
next), then moves everyone back to the original channel the same way a
reported winner does. `cancelBettingHelper` itself is also what
`_openBetting` calls first, to silently clear out a stale unresolved round
before a fresh one opens — that path never moves anyone, since clearing a
stale round isn't the player-facing "the game was cancelled" event
`cancelGameHelper` handles.

The same flip-before-await-anything pattern shows up again in
`_acceptDuel` and `_resolveDuel` for `/wager-against`.

### The economy

Payouts are pari-mutuel: everyone who bet on the winning team splits the
losing team's pool, proportional to their own wager, on top of getting
their own wager back
(`payout = amount + (amount / winningPool) * rakedLosingPool`) — so a bet
on the side fewer people backed still pays out more than the same-sized
bet on the favorite. `computeGameDeltas` is a **pure function**: given the
wagers and rosters, it returns a plain dict of
`{user_id: {balance, wins, losses, …}}` deltas without touching the
database at all. `recordResult` is what actually calls `applyGameDeltas`
to write them. Keeping the math and the writing separate is what makes
`/report-correct-winner` possible (below).

`rakedLosingPool` isn't just `losingPool` — an unraked 100%-payout split
turned out to be too profitable for a "safe" bettor: anyone who could
reliably spot the favorite (visible elo, an obviously stacked roster, ...)
collected a low-risk, positive-EV income stream indefinitely, since
nothing was ever removed from circulation to offset it. `_imbalanceRakeFraction(winning_pool, losing_pool)`
takes a cut that scales with how lopsided the pool was — 0% at an even
50/50 split (a genuine coin-flip still pays full odds), up to
`MAX_IMBALANCE_RAKE` (50%) at a maximally one-sided pool (almost everyone
backed the winner) — so the tax lands specifically on "safe" betting,
never on real risk-taking; a pool where the eventual winners were actually
the *minority* (a real upset) isn't raked at all. The raked share isn't
paid to anyone — it was already deducted from losers' balances the moment
they placed those bets, so simply not crediting it to the winners removes
it from the economy outright, which also helps offset the inflation
`GAME_WIN_GOLD`/`GAME_LOSS_GOLD` (below) introduce on their own. The same
helper backs both `computeGameDeltas` (casual/ranked/sequential-tournament
games) and `_matchWagerDeltas` (simultaneous-tournament match wagers),
which used to duplicate the unraked formula independently.

Separately from wagering, every rostered player gets gold just for
finishing the game — `GAME_WIN_GOLD` (300) for being on the winning side,
`GAME_LOSS_GOLD` (150) for the losing side — the moment a game resolves,
ranked or casual, whether they bet on it or not. It's folded into the same
`balance` delta `computeGameDeltas` already produces per player (right
alongside `game_wins`/`game_losses`/`elo` in the team1_roster/team2_roster
loop, picking whichever constant matches `winning_team`) rather than being
its own separate write, so it rides along for free with
`applyGameDeltas`/`reportCorrectWinnerHelper`'s existing apply/reverse/
reapply cycle — undoing and correcting a misreported winner doesn't
double- or zero-out it by accident, and correctly *flips* which rostered
players get the win amount vs. the loss amount along with everything else
a correction re-derives. `gold_wagered`/`gold_won`/`gold_lost` stay
wager-only and never see it.

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
point. That 1000 is only the global fallback — an admin can move where new
players start with `/set`'s `default_elo` param (`_defaultEloForGuild`),
per guild; it only affects brand-new players (`ensureEconomyRow`) and
`/clear`'s `clear_elo` reset, never anyone's already-tracked rating.

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

`team` and `invalidate` are mutually exclusive — `reportCorrectWinnerHelper`
rejects giving both, or neither. `invalidate` stops after the undo step:
`_invalidateLastResult` reverses `last["deltas"]` the exact same way (bet
payouts, records, elo, `GAME_WIN_GOLD`/`GAME_LOSS_GOLD`), but never
recomputes or reapplies anything for either team, and then deletes the
`last_result` row outright rather than saving a new snapshot — there's no
"corrected winner" for a further correction to work from once a game's
been invalidated. Reversing the deltas alone isn't a *refund* for a
bettor, though: a winner's stored delta credited their whole payout
(stake plus winnings), so undoing it removes the payout entirely and
leaves them down by exactly their stake — the same state as if they'd
lost. `_invalidateLastResult` adds each wager's original `amount` back
afterward specifically to fix that, landing every bettor (winner or
loser) back at their exact pre-bet balance, the same "add the stake back"
refund `cancelBettingHelper` already does for a bet round that never
resolved at all. Not supported yet for a `match_id`-scoped tournament
match — invalidating one would also mean un-advancing whatever it fed
into the bracket, a bigger change than reversing a guild-wide economy
snapshot.

### Heads-up wagers (`/wager-against`)

A 1-on-1 side bet between two specific players, deliberately kept
independent of the team-game betting above (own table, own emoji, no
active game required). Unlike the single-active-game betting state stored
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

### Redirecting where bets get posted (`/set`'s `wager_channel`)

By default every betting message (the combined open+report message,
the closed notice, and either a reported result or a cancellation) goes
to wherever a game — or a tournament match — happened to start. Setting a
wager channel changes that: `_openBetting` (the shared core both
`_startRosterViaReaction`'s ▶️ handler and a sequential tournament match
call) resolves `servers.wager_channel`
by name right before anything else, and swaps it in for the channel it
was handed. Since every later step in the cycle (`_bettingTimer`,
`handleGameReportReaction`, `recordResult`, `cancelGameHelper`) just keeps
using whatever channel it was given, redirecting at that one entry point
is enough to redirect the whole thing.

### Admin resets and permissions

`/clear` requires the **Manage Server** permission outright
(`app_commands.checks.has_permissions`, same as `/report-correct-winner`).
Within it, `clear_elo`, `clear_economy`, `clear_achievements`, and
`clear_card_unlocks` additionally act on *every player* in the server, so
none of them run the moment the command is invoked — `/clear` posts a
`discord.ui.View` with "Confirm reset"/"Cancel" buttons
(`ConfirmResetView`), and the reset only happens from inside that view's
button callback. `interaction_check` on the view rejects anyone who isn't
the member who ran `/clear`, and the view times out after 30 seconds with
nothing changed if it's ignored. All four flags can be requested together
— `clear_economy` takes priority over `clear_elo` when both are set (the
whole-row wipe already resets elo too, so there'd be nothing left for
`clear_elo` to separately do), while `clear_achievements` and
`clear_card_unlocks` are independent of both (and of each other) and just
add their own extra sentence to the warning/confirmation text
(`confirmDestructiveClearHelper`/`ConfirmResetView.confirm` build these as
a list of per-flag sentences rather than one combined string, so any
combination reads cleanly without custom-casing grammar for every case).
`resetAchievementsHelper` only deletes `card_unlocks` rows whose `itemKey`
is a `CARD_ACHIEVEMENT_TITLES` key — every other unlock (tier rewards,
special grants, shop purchases) and the underlying `economy` stats those
achievements were computed from (`game_wins`, `current_win_streak`, ...)
are untouched, so a player who still qualifies will simply earn them back
the next time something self-heals (`/achievements`, `/stats`, their next
game) — this is a "clear the trophies off the shelf" reset, not a "make
everyone start over" one. `resetCardUnlocksHelper`, backing
`clear_card_unlocks`, goes further on purpose: it deletes *every*
`card_unlocks` row regardless of `itemType` (tier rewards, special
grants, shop purchases, and achievement titles alike) and resets the
equipped `trading_cards` row back to Shockwave's own defaults, since
leaving it pointed at a title/scheme/font that no longer resolves to
anything would just surface as a broken card the next time it renders.

`clear_achievements`/`clear_card_unlocks` each also take the same optional
`user` — narrows either from "every player in the server" down to just
that one member, still gated behind the exact same confirm/cancel view (a
single-player reset is still irreversible, so it gets no less confirmation
than a server-wide one). `clear_elo`/`clear_economy` always stay
whole-server regardless of `user` — only `clear_achievements`/
`clear_card_unlocks` read it — so a combined run (say, `clear_elo` +
`clear_achievements` + `user`) mixes an "every player" elo sentence with a
"for @member" achievements sentence in the same warning/confirmation
message rather than trying to force both onto one shared scope.
`resetAchievementsHelper(guild_id, user_id=None)`/
`resetCardUnlocksHelper(guild_id, user_id=None)` carry that same split
down to the SQL: `user_id=None` deletes every row for the guild, a real
one narrows the `DELETE` (and, for card unlocks, the `trading_cards`
reset) with an extra `AND userId=?`. Passing `user` without
`clear_achievements` or `clear_card_unlocks` is rejected outright, before
even the non-destructive team wipe runs — there's no other flag `user`
could mean anything for. `/tournament-create` follows a narrower
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
about team membership itself is exclusive. `/team-create` normally makes
the caller the captain, but an optional `captain` member argument lets
someone stand a team up on another player's behalf (e.g. an admin or
manager registering a team for someone else) — when given, that member
becomes the sole initial roster entry and captain instead of
`ctx.user`. Team names are unique per
guild **case-insensitively**: `getTeamRow` looks a team up with `name = ?
COLLATE NOCASE`, so "red" finds "Red" and `/team-create`'s (and
`/team-rename`'s) own uniqueness check rejects "red" as taken if "Red"
already exists. The one exception is renaming a team to a pure
capitalization change of its own current name ("Red" → "RED") —
`teamRenameHelper` special-cases that (comparing `.lower()` first) rather
than letting the collision check find the team colliding with *itself* and
wrongly calling it already taken. `/team-use` compares its two team-name
params the same case-insensitive way before ever calling `getTeamRow`, so
picking "Red" and "red" is still caught as "the same team twice" instead
of quietly resolving both to the same row. `/team-invite` uses the same
react-to-accept pattern as everything else that needs a specific person's
consent (`TEAM_INVITE_ACCEPT_EMOJI`, its own `team_invites` table keyed by
message id) — the captain check on both invites and voice-channel changes
relies on `Team.get_captain()` actually returning a real `Player` object,
which turned out to need its own fix (see below). `force` (Manage Server
only — checked separately from, and on top of, the ordinary captain-or-
admin gate every `/team-invite` call still has to pass first, so a captain
who isn't also an admin can't use it) skips the whole react-to-accept
dance: every valid member goes straight onto the roster via the same
`add_player`/`updateTeamData` pair `handleTeamInviteReaction` itself
commits once a real invite is accepted, just run immediately instead of
waiting on a reaction — no posted invite, no ✅, no `team_invites` row for
anyone to accept later.

`/team-leave` is the self-service opposite of `/team-invite` — removing
*yourself* needs nobody else's permission, so it's the one team command
with no captain/admin gate at all. The team's own captain is the one
exception: unlike every other command here, there's no "who's in charge
now" to fall back to (no transfer-captaincy command exists), so letting a
captain leave a non-empty team would strand it exactly the same way a
`None` `get_captain()` used to before that was fixed — `isTeamCaptain`
would fail for everyone, silently downgrading every other team command on
it to "Manage Server members only" until someone thought to check why.
`teamLeaveHelper` refuses outright instead, pointing the captain at
`/team-delete` — which already has to answer "what happens to this team"
regardless of roster size, so it's the one place that decision belongs.
`/team-use` is the
shortcut: it loads two persistent teams straight into `team1`/`team2` so
a casual or ranked game can start immediately, without cloning any state
back into the `teams` table — the in-memory copy gets `set_id(1)`/`set_id(2)`
purely for `_startRosterViaReaction`'s sake.

BUG FIX: a team name is free text, and Discord parses markdown emphasis
markers (`_`/`*`) across an *entire* message, not per line — an
unescaped underscore or asterisk in one team's name could pair up with
an unrelated marker later in the same message (most often the other
team's own name) and render everything in between in unintended
italics/bold, e.g. `/team-use`'s "**Fire_Squad** vs **Ice*Wolves**
loaded" confirmation, the win/elo announcement, a wager confirmation, or
a `/report-correct-winner` correction. Fixed by running every persistent
team name through `discord.utils.escape_markdown` right before it's
dropped into message text — `getRosterName` (the one place the roster's
stored name is read back out for display) and `/team-use`'s own messages
are the two spots this actually mattered, since the stored name itself
is left untouched and every other display path already reads it back
through one of those two.

`/team-rename`, `/team-set`, `/team-invite`, `/team-delete`, and
`/tournament-register` are all captain-gated the same way (`isTeamCaptain`),
but every one of them also lets any member with the **Manage Server**
permission through — `not isTeamCaptain(...) and not
ctx.user.guild_permissions.manage_guild`, same check repeated at each
command — so a team whose captain has gone inactive, left the server, or
just isn't around isn't stuck: an admin can rename it, change its voice
channel/logo, invite players, register it for a tournament, or delete it
without needing to be added to the roster first. `myCaptainedTeamAutocomplete`
(the suggestion list backing all five commands' `team` param) checks the
same permission and switches from `getTeamsCaptainedBy` to
`getTeamsForGuild` for an admin, so they can actually find a team they
don't captain to type in, rather than only being able to act on it by
typing the exact name from memory. `myTeamAutocomplete` (backing
`/team-stats` and `/team-use`, which don't require captaincy/rostering at
all — that scoping was only ever a suggestion-list convenience) gets the
same admin carve-out, swapping `getTeamsForPlayer` for `getTeamsForGuild`.
Renaming has to update
the `teams` row's own
`name` **column** and the `name` embedded in its serialized `data`
together (`_renameTeam`) — `getTeamRow` looks a team up by the column, so
letting the two drift apart would make the renamed team invisible under
its new name while a stale row still answered to the old one.
Deleting is destructive and irreversible, so it goes through the same
confirm/cancel button pattern `/clear` and `/tournament-create`'s overwrite
path use (`ConfirmTeamDeleteView`) rather than running immediately; on
confirm, it also deletes any pending `/team-invite` rows for that team
(`_deleteTeam`) so nobody can later "accept" an invite into a team that's
already gone (`handleTeamInviteReaction`'s own `getTeamById(...) is None`
guard would otherwise just eat the click silently instead of telling
them). A tournament this team is already registered in is untouched —
`register_team` snapshots a *copy* of the `Team` at registration time
(see below), not a live reference back into the `teams` table, so the
bracket entry plays out exactly as registered either way.

Both autocomplete functions above predate the admin carve-out — same
per-caller-scoped idea `/card-set`'s title/scheme/font params already use
(`cardTitleAutocomplete`, only ever suggesting what the caller has
personally unlocked); `myTeamAutocomplete` (`getTeamsForPlayer`)
specifically already existed for `/my-teams` before `/team-stats`/
`/team-use` needed it too. Either way, Discord's autocomplete is only a
suggestion list, not a hard restriction — typing a name that isn't
offered still submits fine, so this doesn't (and shouldn't) replace the
backing helpers' own captain/existence checks; it just means someone
usually doesn't have to remember exact spelling for their own teams.

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
`_openBetting` — the exact function ▶️'s own `_startRosterViaReaction`
calls — so betting, the
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
singleton `wagers` table a normal game uses — see "Concurrent tournament
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
represent one active bet per player per *guild* — fine for an ordinary
game and sequential-mode tournament matches, where there's only ever one game live
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
(`_getBettingTimerSeconds`, backing `/set`'s `betting_timer` param) times how many
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
logos, `/team-set`'s `logo` autocomplete lists every file there by name) via
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

`_ensureLogo` only ever runs for *persistent* teams (it needs a `team_id`
row to write the pick back to) — the ad-hoc `Team` objects `/make-teams`,
`/captains`, and ranked team formation build on the fly for a casual game
never go through it, so `team.get_logo_path()` is still `None` for them by
the time ▶️'s matchup graphic renders. Rather than draw a bare
accent-colored ring for those, `_drawMatchupColumn` picks a random
built-in logo right at render time and uses that instead — not persisted
anywhere (there's no stable row to persist it against), so a re-render can
land on a different one, which is fine for a team with no identity to keep
consistent in the first place. Falls back to the ring only if the
built-in set itself is unavailable.

### Trading cards

`/stats` posts two reactions alongside the embed (see `handleStatsReaction`):
🖼️ toggles the thumbnail between this server's own profile picture for
that player and their regular, account-wide one (`_resolveMemberAvatarUrl`
for the server half — `member.display_avatar`, which already resolves a
per-server override if one's set — and the new `_resolveGlobalAvatarUrl`
for the regular half, which deliberately fetches the plain `discord.User`
behind the member, bypassing any guild avatar override; comparing the
embed's current thumbnail URL against a freshly-resolved server URL is
what tells the handler which direction to flip), and 🃏 throws the whole
embed away and replaces it with
a rendered trading card (`_renderTradingCardImage`) — Shockwave's logo and
the server's name across the top (the exact same `_drawBracketHeader` every
other rendered image uses), the player's actual Discord username
(`member.name`, not their nickname) small in the header's top-right —
mirroring the logo/name block's own top-left placement — so the card
identifies exactly who it belongs to even for a player known mainly by a
nickname, the player's live avatar as a circular centerpiece, a
customizable title underneath it, then elo/ranked record/ranked win rate
as three stacked lines (each stat gets the card's full width rather than
sharing a column, so a long value never clips), and
finally — if they're rostered on any — their persistent teams, each with
its own logo pasted alongside its name (same self-healing `_ensureLogo`
every other team-logo display already relies on). The elo line's tier
"emoji" is a real, saved image of that tier's actual emoji
(`assets/elo-badges/<Tier>.png`, one PNG per `ELO_TIERS` entry, pasted by
`_drawEloBadge`/`_eloBadgeImage`) rather than the literal character
`eloRankLabel` uses in a real embed — PIL's bundled TTF fonts can't render
color emoji glyphs (`eloRankLabelPlain` strips it for exactly this
reason), and an earlier hand-drawn approximation (a small filled shape —
circle, diamond, crown, or medal depending on tier) kept drifting out of
sync with what the real emoji actually looks like. The assets themselves
were generated once, offline, by rendering each tier's real emoji
character through a color emoji font (Segoe UI Emoji), auto-cropping to
its glyph bounding box, and saving the result — a one-time step, not
something the bot does at runtime, so there's no color-emoji-font
dependency in production. `_eloBadgeImage` loads, resizes, and caches each
tier's PNG the first time it's needed (`_elo_badge_cache`, same
module-level "load once, reuse for the rest of the process" idea
`_bracket_logo_cache`/`_font_cache` already use) — every card only ever
needs one of a fixed handful of tier images, so repeat renders never hit
disk again for it.
🃏 itself doesn't apply anymore once the card is up (there's no "show the
card again" to offer), so `handleStatsReaction` removes just that one
reaction the moment it fires and adds a single 🪪 in its place; clicking
that rebuilds the plain `/stats` embed (`_buildStatsEmbed`, the same code
`statsHelper` itself calls) via `_swapTradingCardForStats`, sets
`stats_views.cardShown` back to 0, and restores 🃏 — a real back-and-forth
toggle rather than a one-way trip. 🖼️ is deliberately *not* touched by
either swap, since the avatar toggle applies on both sides of the
embed/card divide: `handleStatsReaction` branches on `cardShown` when 🖼️
fires, and once a card is up the toggle re-renders the whole card image
in place instead of swapping an embed thumbnail URL — the avatar is baked
into the PNG, not a swappable field. `_resolveCardAvatarImage` picks
between `member`'s per-server avatar and a plain `discord.User`'s
account-wide one (fetched the same way `_resolveGlobalAvatarUrl` does),
and `stats_views.cardAvatarGlobal` tracks which one is currently showing
so the next 🖼️ click knows which way to flip — reset to 0 every time the
card is (re-)entered, so it always starts on the server avatar, matching
the plain embed's own default.

A card's look lives in `trading_cards` (one row per (guild, player), same
self-healing "insert defaults on first read" shape `ensureEconomyRow` uses
for the economy table) — `title`, `accent_color`/`background_color`/
`text_color` as `"#RRGGBB"` hex, and `font_style` (a named preset
`_cardFontPaths` resolves to actual bundled font files; only `"default"` —
Chakra Petch + IBM Plex Sans, the same pairing every other image already
uses — exists today, but the column means more presets can be added later
with no schema change). Defaults are a deliberately more saturated purple
background than the site's own near-black `--ink` (`CARD_DEFAULT_
BACKGROUND_COLOR`, its own shade rather than reused from `BRACKET_*`),
"Rookie" as a placeholder title, and that default font pairing. There's no
`/customize-card`-style command yet — the table is meant to be edited
directly (or through a future command) — but `_renderTradingCardImage`
always reads through `getCardSettings` rather than hardcoding anything, so
a changed row shows up on the next card rendered with no code changes
needed.

`/report-correct-winner` fixes a specific tournament match via its
optional `match_id` — a narrower, separate path from the economy
correction described above. It flips the match's recorded winner and
re-propagates the bracket node, but refuses if the next round has already
started, rather than risk quietly corrupting a bracket that's moved on.
It only supports winners-bracket matches for now — correcting a losers-
bracket or Grand Finals match is refused with an explanatory message
rather than silently doing the wrong thing.

### Trading-card cosmetic unlocks

`card_unlocks` (one row per unlocked item — `(guildId, userId, itemType,
itemKey)`, `itemType` is `"title"` or `"color_scheme"`) permanently records
which trading-card cosmetics a player has earned, separately from
`trading_cards`' own currently-equipped settings — unlocking something and
actually wearing it are different concerns, the same way a game's
cosmetic inventory is separate from its loadout. `CARD_TIER_REWARD_TITLES`
is the whole catalog today: reaching Diamond, Master, Grandmaster, or
Challenger for the first time unlocks that tier's own title (e.g.
Diamond → "Diamond Mind") and a matching color scheme whose accent is
that tier's own `ELO_TIER_BADGE_COLORS` entry — looked up from `ELO_TIERS`
itself rather than duplicated, so a reward automatically tracks whatever
that tier's badge color currently is instead of freezing at whatever it
was the day it was unlocked (same reasoning `CARD_DEFAULT_*`'s own history
of stale-duplicate bugs already forced elsewhere in this file).
`_checkTierRewardUnlocks` checks every reward tier `elo` currently
qualifies for, not just whichever one it's presently sitting in — a big
enough single swing that jumps straight from Platinum to Grandmaster still
credits Diamond and Master along the way. `_unlockCardReward`'s `INSERT OR
IGNORE` makes checking idempotent, and since nothing anywhere deletes from
`card_unlocks`, a reward earned once stays unlocked even after the player
deranks back below the tier that earned it.

The unlock check runs from two places: `applyGameDeltas`, right after a
ranked result actually changes someone's elo (guarded to `sign > 0` only —
a tournament correction's reversal is "undo", not "grant a reward on the
way down"; the reapply against the corrected winner that follows calls
back in with `sign=1`, which checks properly), and lazily from
`_buildStatsEmbed` every time `/stats` runs for someone — the same
"self-heal on the next read" idea `ensureEconomyRow`/`ensureCardSettings`/
`_ensureLogo` already use elsewhere, so a player who was already sitting
at Diamond+ before this feature existed gets credited the first time
anything looks at their stats rather than never. `getUnlockedCardTitles`/
`getUnlockedCardColorSchemes` read back what's unlocked in
customization-ready form (display strings for titles; `{name,
accent_color, background_color}` hex dicts for schemes, background
derived the same darken-the-accent way `_renderTeamCardImage`'s own
background is).

`/card-set` is the one command that consumes `getUnlockedCardTitles`,
`getUnlockedCardColorSchemes`, and `getUnlockedCardFontStyles` together —
`title`, `color_scheme`, and `font_style` are all optional params on the
same command (each with its own autocomplete: `cardTitleAutocomplete` offers
`getAvailableCardTitles`, `cardColorSchemeAutocomplete` offers
`getAvailableCardColorSchemes`, `cardFontAutocomplete` offers
`getAvailableCardFontStyles` — each the default plus whatever the caller's
personally unlocked, so the picker never shows something they can't
actually equip), and any combination of the three can be set in one call.
`cardSetHelper` re-validates every *provided* field against its own
catalog before writing anything — the whole point of doing this validate-
first: a bad value in one field (a typo'd font, say) can't leave another,
genuinely valid field half-applied, since nothing gets written until every
given field has passed. `setCardTitle`/`setCardColorScheme`/
`setCardFontStyle` are the trusting internal setters `cardSetHelper` calls
for whichever fields were actually given — each also flips
`trading_cards.customized` to 1, the same flag `ensureCardSettings`'s own
resync-to-defaults check respects, since without it the very next
`/stats` call would silently revert an equipped field right back to its
`CARD_DEFAULT_*` value.

The color-scheme half of `cardSetHelper` (`getAvailableCardColorSchemes`)
is the one place this differs from titles/fonts in what it has to do
before offering a choice: `getUnlockedCardColorSchemes` runs
each scheme's `ELO_TIER_BADGE_COLORS` accent through `_ensureReadableAccent`
(`CARD_MIN_ACCENT_CONTRAST`, the same helper and threshold
`_renderTeamCardImage` uses for a team's sampled logo color) before ever
offering it — a badge color was picked for how it reads as a small
circle/diamond standing in for an emoji, not for driving a whole card's
header/label text against its own darkened background, and Master's and
Grandmaster's own badge colors are dim enough that offering them
unboosted would have reintroduced the exact readability problem the team
card already had to solve. Only the accent gets boosted; the background
stays the badge color's raw 28%-darkened shade either way, so a scheme's
overall mood still authentically reflects the tier that earned it —
`_renderTradingCardImage` itself is untouched and still trusts whatever's
stored in `trading_cards` exactly as given, whether that came from a
scheme selection or a hand-edited custom hex value.

Two bugs turned up once schemes actually started getting equipped instead
of just existing as unused plumbing. First, `CARD_MIN_ACCENT_CONTRAST`
(90, at the time) forced almost any accent to lighten into the 70-80%
HSL-lightness range against these cards' fairly bright vignette centers —
a fully saturated red's own average-channel brightness tops out around 85
even at 100% saturation, so a 90-point floor left it nowhere to go but
pastel. Turned saturating `CARD_SHOP_COLOR_SCHEMES`' raw accents into a
no-op regardless of how vivid they were on paper; dropped to 45, which
still rescues a genuinely too-dark color (see `RenderTeamCardImageTests`'
dark-navy regression test) without forcing an already-good one toward
white. Second, and more fundamental: `setCardColorScheme` only ever wrote
the *computed* hex pair, not which scheme it came from — so a later
change to a scheme's own colors (exactly what fixing the first bug
required) never reached a player who'd already equipped it, the identical
staleness problem `trading_cards.customized` was built to solve for the
default palette, just one layer down. `color_scheme_name` (nullable —
`NULL` for a hand-edited custom hex value with nothing to track) plus
`_resyncEquippedColorScheme` (called from `ensureCardSettings`, so every
`/stats` view re-checks it) fixes that the same way: an equipped scheme
now tracks its source of truth instead of freezing at equip time.

`grantSpecialCardTitle` is the escape hatch for a title with no elo tier
behind it at all (no matching color-scheme unlock alongside it, unlike a
rank reward) — a per-(guild, player) `card_unlocks` row, same shape as a
tier reward's own title unlock. Shockwave's own developer gets "Developer"
a different way, though: `SHOCKWAVE_DEVELOPER_ID` is a hardcoded Discord
user id `getUnlockedCardTitles` checks directly, so it's available in
every guild the bot is in — including ones with no `card_unlocks` row for
them at all, and future guilds the bot hasn't joined yet — rather than a
grant that would need repeating by hand every time.

### Shop

`/shop`/`/shop-buy` add a third, gold-priced path into `card_unlocks`
alongside reaching an elo tier and a special grant — `CARD_SHOP_TITLES`,
`CARD_SHOP_COLOR_SCHEMES`, and `CARD_SHOP_FONT_STYLES` are the three
catalogs (name → price, color schemes also carrying their own hex pair),
kept name-distinct from each other and from every `ELO_TIERS`/
`CARD_SPECIAL_TITLES` name so `shopBuyHelper`'s single `item` parameter can
resolve a purchase to its category (`_resolveShopItem`) without needing to
know it ahead of time. `CARD_SHOP_COLOR_SCHEMES` is the biggest of the
three — a handful of standalone themes (Crimson, Emerald, Azure, Sunset,
Fire) plus one per Runeterra region (Demacia, Noxus, Freljord, Ionia,
Piltover, Zaun, Shurima, Shadow Isles, Bilgewater, Bandle City, Targon)
— the same region set `assets/clash-logos/` already covers for
`/team-set`'s `logo` option, so a team using one of those crests has a matching
player-card scheme available too. A purchase is just `economy.balance -= price` plus
the exact same `INSERT OR IGNORE INTO card_unlocks` a tier reward or a
special grant writes — there's no separate "did I buy this" bookkeeping
anywhere, so a purchased item shows up through `getUnlockedCardTitles`/
`getUnlockedCardColorSchemes` automatically (`CARD_TITLE_CATALOG` folds
`CARD_SHOP_TITLES` in, and `getUnlockedCardColorSchemes` branches on
whether an `itemKey` is a tier name or a `CARD_SHOP_COLOR_SCHEMES` one).
Font styles are shop-only — there's no elo-tier path to one at all — so
`getUnlockedCardFontStyles` is a plain lookup against `CARD_SHOP_FONT_STYLES`
with no combining catalog needed. `shopHelper` lists every item grouped by
category with its price or an "✅ Owned" marker plus the caller's current
balance; `shopBuyHelper` refuses an unknown item, one already owned, or
one the caller can't afford, and on success tells them it's ready to
equip with `/card-set`.

The font half of `cardSetHelper`/`getAvailableCardFontStyles` is otherwise
the same shape as titles/color schemes for `trading_cards.font_style` —
the one thing that's different is `_cardFontPaths` itself, which resolves a `font_style` key
to a dict: `name_font`/`name_variation` and `title_font`/`title_variation`
for the card's two biggest typographic elements (the player's name, and
the title/epithet under it), `body_font` plus a `label_weight`/
`value_weight`/`team_weight` for everything smaller (stat labels, stat
values, team/roster rows — the header's username shares `team_weight`,
both being small secondary text).

`CARD_SHOP_FONT_STYLES`' six styles each back `name_font`/`title_font`
with a genuinely different bundled typeface — `"Bold"` is `RUSSO_ONE`,
`"Elegant"` is `CINZEL`, `"Cyber"` is `ORBITRON`, `"Retro"` is
`PRESS_START_2P`, `"Villain"` is `CREEPSTER`, `"Military"` is
`BLACK_OPS_ONE`, all pulled from Google Fonts (SIL Open Font License) into
`assets/fonts/`, alongside the already-bundled Chakra Petch/IBM Plex Sans
pairing `"default"` still uses. `body_font` stays `IBM_PLEX_SANS` for
every style — there's still only the one bundled body typeface — so a
style's effect on the smaller text is a different named `_loadFont`
weight/instance of that same file, same mechanism as `name_variation`/
`title_variation` for Cinzel/Orbitron (both variable fonts, like
`IBM_PLEX_SANS` itself); the other four (Russo One, Press Start 2P,
Creepster, Black Ops One) are each a single static weight by design, so
their own `_variation` fields are `None`.

`PRESS_START_2P`'s near-monospace, unusually-wide-per-glyph metrics turned
up a real bug the first five styles never touched: a long real Discord
username (up to 32 characters) rendered in it at the standard
`CARD_NAME_FONT_SIZE` could measure at or past `CARD_WIDTH` itself, with
nowhere left to draw the card's own border — every other bundled font
stays comfortably clear of the edge even at the same size. `_fitNameFont`
fixes this generally rather than special-casing the one font: it shrinks
whichever font/variation `_renderTradingCardImage` actually picked, in
`_loadFont`-sized steps, down toward `CARD_NAME_MIN_FONT_SIZE` until the
name's measured width clears `CARD_WIDTH - BRACKET_MARGIN * 2` — a no-op
for the other five styles, since none of them come close to the limit to
begin with. Only the drawn font size changes; `name_y`/`title_y` and the
rest of the card's layout stay anchored to the fixed `CARD_NAME_FONT_SIZE`
slot regardless, so a shrunk name just leaves a little extra breathing
room under it rather than needing the whole card re-laid-out.

Two rounds of BUG FIXes got the font-style feature to this point. First:
`_cardFontPaths` originally only varied `name_font`/`title_font`, and
even then only via different weights of the *same* Chakra Petch file —
every body element (stat labels, values, roster rows, the header's
username) always loaded the exact same three hardcoded `_loadFont`
weights regardless of `font_style` at all, so equipping "Bold"/"Elegant"
only visibly touched two smallish pieces of text and read as "nothing
changed" at a glance even where it did do something. `_renderTradingCardImage`
now reads every one of `_cardFontPaths`' weight keys instead of hardcoding
`"Bold"`/`"SemiBold"`/`"Medium"` inline, so a font style shifts the whole
card's typography together. Second: even with that fixed, "Bold" and
"Elegant" were still just different weights of the one Chakra Petch font
— a real but subtle difference, easy to miss at a glance. Downloading
Russo One/Cinzel/Orbitron (plus adding a third style, "Cyber") swapped in
actually distinct typefaces instead.

`/card-set` shows the card, not just confirms the change in text —
`_renderMemberTradingCardFile` (the caller's own current
title/scheme/font/stats/teams/avatar, rendered once, reflecting whichever
fields the call actually changed) and the thin `_cardPreviewEmbedAndFile`
wrapper around it render it, with the confirmation string (naming every
field that was set, joined into one sentence) passed as `content=`
alongside the same `embed=`/`file=` pair. Simpler than
`_swapStatsForTradingCard`'s own member-resolution: the caller of
`/card-set` is always a real, currently-present member (they're the one
running it), so there's no "member left the guild" fallback to handle the
way that function needs.

`/preview type:<Logos|Card Titles|Color Schemes|Fonts>` (`previewHelper`)
is the "what are my options" counterpart to `/card-set`/`/team-set` — a
single gallery image showing every option for one type, not just what a
given player has personally unlocked (unlike `getAvailableCard*`, which
are always per-player). Logos and Color Schemes are real
`PREVIEW_COLUMNS`-wide grids (`_renderPreviewGridPage`, shared by both —
they only differ in what `draw_cell` puts inside a cell: a pasted-in logo
image for Logos, a background-fill-plus-accent-circle swatch of the
scheme's own two colors for Color Schemes) with each item's exact
name/key underneath, so it doubles as a lookup table for what to actually
type. Fonts and Card Titles skip the grid entirely — a font style has a
real typeface to show (`_cardFontPaths`, the same lookup an equipped card
uses, so it's rendered in its own actual typeface rather than just
labeled) but nothing to lay out in columns, and a title has no visual
difference at all beyond the text — so both are simple one-column lists
instead. `_paginateGridItems` splits a grid onto more than one image if
its height would clear `PREVIEW_MAX_PAGE_HEIGHT`, though none of the four
types are anywhere close to that today (51 logos and ~21 schemes both
still fit on one page). Nothing here reads a specific player's unlocks —
Logos comes from `listAvailableLogos()`, Color Schemes from
`CARD_DEFAULT_SCHEME_NAME` plus every `CARD_SHOP_COLOR_SCHEMES` entry,
Fonts from `CARD_DEFAULT_FONT_STYLE` plus every `CARD_SHOP_FONT_STYLES`
key, Card Titles from `CARD_DEFAULT_TITLE` plus every `CARD_TITLE_CATALOG`
value (achievement titles included, since `CARD_ACHIEVEMENT_TITLES` folds
into that same catalog) — so the gallery always shows the complete
catalog, purchasable-but-not-yet-owned items included.

Each type is rendered once and cached to `PREVIEW_DIR`
(`assets/previews/`) as `<stem>-1.png`, `<stem>-2.png`, ... —
`_cachedPreviewFiles` just probes sequentially until the next page is
missing, so `/preview` never re-runs Pillow on a later call unless the
cached file(s) are deleted by hand (or the folder doesn't exist yet at
all). None of these four catalogs change without a code change, so
there's no cache-invalidation logic beyond that — deleting the relevant
file(s) is how a developer forces a regenerate after actually adding a
new logo/scheme/font/title.

### Achievements

`/achievements` and `CARD_ACHIEVEMENT_TITLES` add a fourth path into
`card_unlocks` (title only), alongside a tier reward, a special grant, and
a shop purchase — same `INSERT OR IGNORE`-shaped row (`_unlockAchievement`
handles it, using `rowcount` to tell a genuine first unlock from a
no-op repeat), so an achievement title shows up through
`getUnlockedCardTitles`/`/card-set` automatically like any other
(`CARD_TITLE_CATALOG` folds `CARD_ACHIEVEMENT_TITLES` in too). What's new
is *why* one unlocks: conditions tied to actual gameplay rather than rank
or gold spent — first win (First Blood); a single tournament win
(Tournament Champion); being rostered on `CARD_ACHIEVEMENT_TEAM_PLAYER_
TEAMS` (3)+ persistent teams at once (Team Player) or actually captaining
one of them (The Captain, checked via `isTeamCaptain`); owning
`CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS` (3)+ items bought from `/shop` (Big
Spender, counted by `_countShopPurchases` filtering `card_unlocks` down to
shop-catalog keys only — a tier reward or special grant doesn't count as
"purchased"); a single-match elo swing of `CARD_ACHIEVEMENT_UNDERDOG_
ELO_GAIN` (20)+ (Giant Slayer); winning a single `CARD_ACHIEVEMENT_HIGH_
ROLLER_GOLD` (5000)+ gold bet (High Roller) or one paying out
`CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER` (3)x+ the wager regardless of
its size (Jackpot); placing `CARD_ACHIEVEMENT_GAMBLER_BETS` (25)+ total
bets, win or lose (Frequent Bettor); and racking up `CARD_ACHIEVEMENT_
IRON_WILL_LOSSES` (20)+ game losses without quitting (Iron Will).

Veteran and On Fire are each a *ladder* rather than a single condition —
the same `game_wins`/`current_win_streak` column crossing further
thresholds for further, genuinely distinct titles (not just "Veteran
II"/"III"), so a card's epithet keeps meaning something as the number
climbs instead of just growing a suffix: Veteran (`CARD_ACHIEVEMENT_
VETERAN_WINS`, 10) → Elite (50) → Battle-Hardened (150) → Immortal (500)
career wins; On Fire (`CARD_ACHIEVEMENT_ON_FIRE_STREAK`, 5) → Unstoppable
(10) → Untouchable (20) win streak. Each ladder is walked as a
`(threshold, achievement_key)` list (`CARD_ACHIEVEMENT_VETERAN_LADDER`/
`CARD_ACHIEVEMENT_ON_FIRE_LADDER`) rather than one `if` per tier, so
crossing a big enough number in one jump unlocks every rung up to it in
the same pass, not just the one it technically landed past.

Gold-based achievements are deliberately keyed off a single transaction —
a single wager's own win, payout, or size — never a balance milestone:
`/daily` hands out `DAILY_GOLD_AMOUNT` (1000) for free every single day, so
"reach N gold saved" would just reward showing up regardless of size, not
anything skill- or risk-related, and "place N total bets" (Frequent
Bettor) is about activity, not any amount won or lost. High Roller,
Jackpot, and Giant Slayer are all checked inline inside `applyGameDeltas`'s
existing per-user loop for exactly that reason — each needs that specific
event's own context (this game's wager, this game's payout, this game's
elo swing) rather than a plain row snapshot. Every other achievement
(first_blood, the two ladders, team_player, captain, big_spender, gambler,
iron_will) lives in `_checkAchievements`, a single read against the
caller's current `economy` row (plus `getTeamsForPlayer`/
`_countShopPurchases` for the ones that need more than that) run from
three places — `applyGameDeltas` itself (every non-reversal delta
application), and a lazy self-heal call from `_buildStatsEmbed` (return
value discarded, no announcement) mirroring the same pattern
`ensureCardSettings`'s own self-heal already uses elsewhere in this file.
`current_win_streak` (new `economy` column) is maintained right alongside
those checks: incremented on a win, reset to 0 on a loss, and — like the
existing elo-tier check already sitting in this same function — skipped
entirely on `sign=-1` (a correction/reversal shouldn't move a streak
counter any more than it should re-fire an unlock).

Tournament Champion is the one condition with no natural home in
`applyGameDeltas` (a tournament win isn't a per-game delta at all), so
`_grantTournamentChampionAchievement` is a separate one-off hook called
from both places a tournament can actually end: single elimination's
`_startRound` and double elimination's `_resolveFinalsMatch` (Grand
Finals) — it unlocks the achievement for every rostered player on the
winning team in one pass.

Unlike the other three unlock paths, earning an achievement also posts a
notification — these are meant to feel like a moment worth noticing, not
just another option quietly waiting in `/card-set`'s title autocomplete.
`applyGameDeltas` returns the list of newly-unlocked `(user_id,
achievement_key)` pairs (a mechanical `rowcount`-based fact, not an I/O
side effect, keeping the same "helper does data, caller does I/O" split
this file uses elsewhere) for its callers to hand to `_announceAchievements`
— `_settleMatchWagers`, `recordResult`, and both tournament-completion
hooks all do; `reportCorrectWinnerHelper`/`_correctTournamentMatchHelper`'s
own *reapply* half does too (their reversal half never unlocks anything,
since `sign=-1` skips every check above), so a correction can still
retroactively earn an achievement it should have. `/achievements` itself
(`achievementsHelper`/`getAchievementCatalog`, no permission gate) lists
every achievement with its description and a ✅/🔒 marker for the caller,
self-healing via a `_checkAchievements` call first so an achievement earned
before this feature existed (e.g. an existing win count already past
`CARD_ACHIEVEMENT_VETERAN_WINS`) shows up correctly the first time it's
checked rather than waiting on the caller's next game. The embed itself is
grouped into fields the same way `/shop`'s own `shopHelper` groups by item
type (`embed.add_field` per category, not one flat description) —
Veteran and On Fire each get their own field, tiers listed lowest-to-
highest, so a four-rung ladder reads as one clear progression instead of
its rungs being scattered alphabetically-by-insertion-order alongside
every unrelated achievement; everything else lands in a shared `__Other__`
field.

### Team cards

`/team-stats` gets the same card treatment as `/stats`, via its own single
reaction (see `handleTeamStatsReaction`): `TEAM_CARD_EMOJI` (🛡️) throws the
embed away for a portrait card (`_renderTeamCardImage`) built around the
team's own logo as the focal point — big, framed, centered, right below
the usual Shockwave-logo-and-server-name header every rendered image
opens with. Unlike the player trading card, there's no `trading_cards`-
style settings row backing this: "color scheme matches the logo" means
sampling a color straight off the logo file itself on every render
(`_dominantLogoColor`) rather than reading a stored customization — there's
nothing to customize independently of the logo in the first place. That
function buckets a downscaled copy of the logo into 16-value RGB groups
and returns the most common one, skipping near-transparent pixels (the
background) and near-white/near-black ones (padding/outlines), so a
logo's actual identifying color wins out over whatever frames it. The
sampled color becomes the card's accent (header title, frame, rule, stat
labels); the background is that same color darkened to 28% and lightened
back up 30% for the vignette center, the same darken-then-relighten
relationship the player card's customizable `background_color` has to its
own center, just derived instead of stored.

A logo's dominant color has no readability guarantee at all — a deep navy
or forest green passes `_dominantLogoColor`'s own brightness filter just
fine, and since that same color also drives the background, drawing it
as-is for header/label text risked it nearly disappearing against the
vignette's own lightened center. `_ensureReadableAccent` fixes this: it
lightens the sampled color toward white (closed-form, since
`_lightenColor`'s blend is linear in its `amount` — no search needed) just
enough to clear `TEAM_CARD_MIN_ACCENT_CONTRAST` average-brightness units
above that center, and leaves an already-readable color untouched. Only
the *background* derivation above uses the true, unboosted sample — every
drawn accent element (header title, frame, rule, stat labels, captain
star, roster overflow line) uses the boosted version, so the card still
visibly carries the logo's hue without any of its text becoming hard to
read. Below the logo: the team's
name, then three stat rows (captain, record, win rate — same bolded-label
layout the player card's own stat rows use), then its full roster
(`_orderedRoster`, captain floated to the front) with the captain marked
by the same drawn star `_drawMatchupColumn` uses, capped at
`TEAM_CARD_MAX_ROSTER_ROWS` with a "+N more players" overflow line past
that. A team with no logo file at all falls back to `TEAM_CARD_FALLBACK_
ACCENT_COLOR` (the same gold the player card defaults to) rather than a
bare/colorless frame. `TEAM_CARD_RETURN_EMOJI` (↩️) is the one action that
applies once the card is up — swaps back to the plain embed
(`_renderTeamStatsEmbed`, the same one `/team-stats` itself posts) via
`_swapTeamCardForStats` and restores 🛡️, mirroring `STATS_RETURN_EMOJI`'s
real back-and-forth toggle rather than `/stats`' card being a one-way
trip. Tracked in its own `team_stats_views` table (`teamId` instead of a
player's `targetUserId`) rather than reusing `stats_views`, since a team
and a player are different things to look back up on a reaction.

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
| `servers` | one row per guild | current team rosters, channel names, betting state, `is_ranked`, `wager_channel`, `active_tournament_match_id`, `betting_timer_seconds` (all admin-configurable via `/set`) |
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
