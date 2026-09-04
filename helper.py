import discord
from TourneyClasses import (
    Team, Tournament, Player, BracketNode, serialize_bracket, deserialize_bracket,
    serialize_losers_rounds, deserialize_losers_rounds,
)
import random
import asyncio
import datetime
import io
import itertools
import json
import logging
import math
import os
import time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# This is the same logger bot.py sets up file/console handlers for.
# logging.basicConfig there attaches to the root logger, so anything logged
# here under this same name also lands in shockwave.log. Used sparingly,
# only where a caught exception would otherwise vanish silently (see
# _updateDraftEmbeds/_editRosterTeamEmbeds).
logger = logging.getLogger("shockwave")

# Every bracket/matchup image is drawn at BRACKET_SUPERSAMPLE times its
# final size, then downscaled with LANCZOS resampling in _imageToFile.
# PIL's ImageDraw has no antialiasing of its own, so lines and glyph edges
# come out visibly jagged at 1x. Rendering bigger and then shrinking down
# is the standard way around that. Every pixel-valued constant below is
# already expressed at supersampled scale (hence "* BRACKET_SUPERSAMPLE"
# throughout), so the drawing code just uses them as-is and never has to
# think about the scale factor itself.
BRACKET_SUPERSAMPLE = 2

# Bracket-image layout constants (see _renderTreeImage and friends), plain
# pixel values, not something a server would ever need to tune.
BRACKET_FONT_SIZE = 16 * BRACKET_SUPERSAMPLE
BRACKET_TITLE_FONT_SIZE = 22 * BRACKET_SUPERSAMPLE
BRACKET_SUBTITLE_FONT_SIZE = 12 * BRACKET_SUPERSAMPLE
BRACKET_ROUND_LABEL_FONT_SIZE = 13 * BRACKET_SUPERSAMPLE
BRACKET_ROUND_LABEL_HEIGHT = 22 * BRACKET_SUPERSAMPLE
BRACKET_ROW_HEIGHT = 28 * BRACKET_SUPERSAMPLE
BRACKET_PADDING = 10 * BRACKET_SUPERSAMPLE
BRACKET_MARGIN = 20 * BRACKET_SUPERSAMPLE
BRACKET_CORNER_RADIUS = 6 * BRACKET_SUPERSAMPLE
BRACKET_CHAMPION_STAR_RADIUS = 7 * BRACKET_SUPERSAMPLE
# Extra room reserved before a champion/final-result label for its star
# badge (see _drawChampionLabel): the star itself plus a little breathing
# room on each side of it.
BRACKET_CHAMPION_BADGE_GAP = BRACKET_CHAMPION_STAR_RADIUS * 2 + BRACKET_PADDING * 2
BRACKET_LOGO_HEIGHT = 26 * BRACKET_SUPERSAMPLE
BRACKET_SUBTITLE_GAP = 6 * BRACKET_SUPERSAMPLE           # between the title row and the subtitle
BRACKET_HEADER_RULE_GAP = 10 * BRACKET_SUPERSAMPLE       # between the header text block and its accent rule
BRACKET_HEADER_RULE_MARGIN = 12 * BRACKET_SUPERSAMPLE    # between the accent rule and whatever's drawn below it
BRACKET_BORDER_RADIUS = 14 * BRACKET_SUPERSAMPLE
# Connector lines, the accent rule, and the outer frame all used bare
# hardcoded width= literals before. They're named and scaled now so they
# shrink back down to the same relative thickness after the downscale,
# instead of staying full-width on a now-bigger canvas.
BRACKET_LINE_WIDTH = 2 * BRACKET_SUPERSAMPLE
BRACKET_RULE_WIDTH = 1 * BRACKET_SUPERSAMPLE
BRACKET_LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "logo-mark.png")
# Built-in Clash faction/region logos a team can pick from (see
# /team set and _ensureLogo). One file per available logo, named after
# it (e.g. "Demacia.png"), no subfolders.
TEAM_LOGO_DIR = os.path.join(os.path.dirname(__file__), "assets", "clash-logos")

# /shop preview's cache. Logos/color-schemes/fonts/titles never change
# without a code change, so each type is rendered once and reused from disk
# on every later call instead of re-running Pillow on every single request
# (see previewHelper/_cachedPreviewFiles). Deleting a file here (or the
# whole folder) just makes the next /shop preview for that type regenerate
# it. Same "no source of truth but the assets folder itself" idea
# TEAM_LOGO_DIR already relies on.
PREVIEW_DIR = os.path.join(os.path.dirname(__file__), "assets", "previews")

# Real TTF fonts instead of PIL's built-in default font, which is a small
# bitmap face that looks noticeably rough and pixelated once scaled up to
# heading sizes. These are the same two families shockwave-site's own CSS
# uses (--font-display / --font-body, see styles.css), so the images read
# as the same brand as the site, not just the same color palette. Chakra
# Petch is for anything headline-ish (titles, team names, "VS"). IBM Plex
# Sans is for body text (roster names, round headers). Both are
# Google/SIL-OFL-licensed and bundled under assets/fonts rather than
# linked, so rendering doesn't depend on network access or the host having
# them installed.
FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
CHAKRA_PETCH_SEMIBOLD = os.path.join(FONTS_DIR, "ChakraPetch-SemiBold.ttf")
CHAKRA_PETCH_BOLD = os.path.join(FONTS_DIR, "ChakraPetch-Bold.ttf")
IBM_PLEX_SANS = os.path.join(FONTS_DIR, "IBMPlexSans.ttf")  # variable weight, see _loadFont
# CARD_SHOP_FONT_STYLES' own typefaces: genuinely different fonts (not
# just different weights of Chakra Petch), all from Google Fonts, all SIL
# Open Font License. Russo One is a single static weight by design (no
# variation axis). Cinzel and Orbitron are variable fonts like
# IBM_PLEX_SANS itself, see _loadFont's own variation-name mechanism.
RUSSO_ONE = os.path.join(FONTS_DIR, "RussoOne-Regular.ttf")
CINZEL = os.path.join(FONTS_DIR, "Cinzel-Variable.ttf")
ORBITRON = os.path.join(FONTS_DIR, "Orbitron-Variable.ttf")
# Second wave of shop fonts, meeting the same "genuinely distinct
# typeface, not just a weight" bar the three above set. All are
# single-static-weight Google/SIL-OFL fonts (no `_variation` to pass, same
# as Russo One). Each was chosen to read as a completely different mood at
# a glance: Press Start 2P is an actual pixel-grid bitmap-style face
# (8-bit/arcade), Creepster is a dripping horror-poster face, and Black
# Ops One is a blocky military stencil face.
PRESS_START_2P = os.path.join(FONTS_DIR, "PressStart2P-Regular.ttf")
CREEPSTER = os.path.join(FONTS_DIR, "Creepster-Regular.ttf")
BLACK_OPS_ONE = os.path.join(FONTS_DIR, "BlackOpsOne-Regular.ttf")
# Third wave, same single-static-weight Google/SIL-OFL bar as the second
# wave. Bungee is a chunky neon-sign/urban display face. Rye is a
# wanted-poster western face. Permanent Marker is a casual handwritten
# marker face, the one "quiet" font of this wave, priced with Bold/Elegant
# rather than the loud ones (see CARD_SHOP_FONT_STYLES).
BUNGEE = os.path.join(FONTS_DIR, "Bungee-Regular.ttf")
RYE = os.path.join(FONTS_DIR, "Rye-Regular.ttf")
PERMANENT_MARKER = os.path.join(FONTS_DIR, "PermanentMarker-Regular.ttf")

# Colors lifted straight from shockwave-site/assets/styles.css's :root
# palette, so the bracket image reads as part of the same brand instead of
# a plain black-on-white chart dropped into a dark-themed Discord client.
BRACKET_BACKGROUND = (21, 11, 34)      # --ink
BRACKET_BACKGROUND_CENTER = (30, 19, 48)   # --surface, lighter center of the canvas's radial vignette
BRACKET_TEXT_COLOR = (243, 239, 250)   # --text
BRACKET_TITLE_COLOR = (237, 198, 67)   # --gold
BRACKET_LINE_COLOR = (118, 106, 148)   # --muted-dim
# The losers bracket's own accent, standing in for gold everywhere a
# winners-bracket image would use it (title, champion label, frame). This
# makes the two images readable as "which one is this" at a glance,
# without relying on remembering which caption belongs to which
# attachment.
BRACKET_LOSERS_ACCENT_COLOR = (231, 76, 60)   # --team-red

# /matchup image (see _renderMatchupImage), posted alongside the existing
# text announcement whenever a tournament match is created
# (_postMatchReport, _postReadyCheck). Team 1/2's accent colors come
# straight from shockwave-site's own --team-blue/--team-red palette (see
# the .discord-embed.blue/.discord-embed.red rules in styles.css). This is
# a different pairing than the bracket image's gold/red winners/losers
# split, since this is about telling team 1 from team 2, not winners
# bracket from losers bracket. (TEAM2_ACCENT_COLOR's value happens to
# match BRACKET_LOSERS_ACCENT_COLOR only because both trace back to the
# same site palette, not because they mean the same thing.)
TEAM1_ACCENT_COLOR = (52, 152, 219)   # --team-blue
TEAM2_ACCENT_COLOR = (231, 76, 60)    # --team-red
MATCHUP_LOGO_SIZE = 96 * BRACKET_SUPERSAMPLE
MATCHUP_COLUMN_GAP = 56 * BRACKET_SUPERSAMPLE   # width reserved for the "VS" divider between columns
MATCHUP_VS_FONT_SIZE = 30 * BRACKET_SUPERSAMPLE
# One icon per SETUP_ROLE_NAMES entry (see _roleIconImage), in
# assets/role-icons/. Not bundled here: drawing degrades to "no icon, no
# extra row width" if a file is missing, the same "off by default until
# the assets exist" shape ELO_BADGE_DIR/TEAM_LOGO_DIR already use.
ROLE_ICON_DIR = os.path.join(os.path.dirname(__file__), "assets", "role-icons")
# _roleIconImage tries each of these filename stems (+ ".png") in order
# for a given role, so either the plain name we ask for, or whatever
# naming an icon set was actually exported with (e.g. "Top_icon.png",
# "Mid" spelled "Middle"), just works without renaming anything.
ROLE_ICON_FILENAME_CANDIDATES = {
    "Top": ["top", "Top_icon"],
    "Jungle": ["jungle", "Jungle_icon"],
    "Mid": ["mid", "Middle_icon", "middle"],
    "Bottom": ["bottom", "Bottom_icon"],
    "Support": ["support", "Support_icon"],
}
MATCHUP_ROLE_ICON_SIZE = 20 * BRACKET_SUPERSAMPLE
MATCHUP_ROLE_ICON_GAP = 6 * BRACKET_SUPERSAMPLE  # space between the icon and the player's name

# A bracket this many rounds deep (16+ teams) splits into two halves that
# grow toward the center instead of one long strip growing left-to-right.
# It's the same idea as a printed tournament bracket poster, and a lot
# more compact since each side only stacks half the leaves (half the
# height). Only the winners bracket ever uses this: its champion's two
# children are always exactly even halves (buildBracket produces a
# perfectly balanced tree), which is what makes a symmetric two-sided
# split look right. The losers bracket has no such guarantee. Its final
# round pairs a whole survivor subtree against a single fresh drop-in leaf
# (see buildLosersBracket), so splitting it the same way would just look
# lopsided.
BRACKET_TWO_SIDED_MIN_ROUNDS = 4

# Lazily loaded and resized once, then reused for every bracket image for
# the rest of the process. It's module-level (not per-`helpers` instance)
# since it's a static asset every instance would otherwise reload
# identically. False (not None) means loading was already tried and
# failed, so it isn't retried on every single render.
_bracket_logo_cache = None

# TrueType fonts loaded once per (path, size, variation) and reused for
# every image rendered for the rest of the process. Same idea as
# _bracket_logo_cache: module-level since it's static. There's no
# reassignment involved here, so this dict can just be mutated directly
# (no `global` needed, unlike _bracket_logo_cache's None/False swap).
_font_cache = {}

# ELO tier badge images (see ELO_BADGE_DIR), loaded and resized once per
# (path, size) and reused for the rest of the process. Every trading card
# draws exactly one of these, so this avoids re-opening and re-resizing
# the same handful of small PNGs from disk on every single render. Same
# "mutate the dict directly" reasoning as _font_cache's comment above.
_elo_badge_cache = {}

# Role icons (see ROLE_ICON_DIR), same load-once-per-(path, size)-and-reuse
# shape _elo_badge_cache already uses.
_role_icon_cache = {}

BETTING_DURATION_SECONDS = 60
# How long a /team invite or /wager against challenge sits waiting on the
# other side before expireStalePendingInvites cleans it up (see that
# method's own comment). Both are otherwise indefinite - the posted view
# is persistent (timeout=None) and nothing else ever revisits a row nobody
# acted on.
PENDING_INVITE_EXPIRY_SECONDS = 24 * 60 * 60
# _openConcurrentTournamentBetting multiplies a guild's configured
# per-match timer by however many matches are in the round. This caps
# the result so a generous base times a big bracket's first round can't
# leave betting open for an unreasonable stretch.
MAX_CONCURRENT_BETTING_SECONDS = 1800
DAILY_GOLD_AMOUNT = 1000
# Reward every rostered player gets in computeGameDeltas for simply
# finishing a game (casual or ranked, regardless of anything they bet
# themselves), split by whether their side won or lost. This is separate
# from gold_won/gold_wagered/gold_lost (those are wager-specific); it's
# just balance.
GAME_WIN_GOLD = 300
GAME_LOSS_GOLD = 150
# The pari-mutuel payout (see _imbalanceRakeFraction) used to hand winners
# 100% of the losing pool. That was free money for anyone who could
# reliably spot the favorite (visible elo, an obviously stacked roster,
# etc.), since nothing was ever removed from circulation to offset it.
# This caps how much of the losing pool gets raked off before the split,
# scaling with how lopsided the pool was: 0 at an even 50/50 split (a
# genuine coin-flip pays full odds), and this fraction at a maximally
# one-sided pool (almost everyone backed the winner). That taxes the
# "safe" bets specifically, rather than genuine risk-taking. The raked
# share isn't paid to anyone: it was already deducted from losers'
# balances at bet time, so simply not crediting it to the winners removes
# it from the economy outright.
MAX_IMBALANCE_RAKE = 0.5
# Blue for team 1, red for team 2. Matches TEAM1_ACCENT_COLOR/
# TEAM2_ACCENT_COLOR, and decorates WinnerReportView/
# TournamentMatchReportView's own Team 1/Team 2 button labels.
TEAM_EMOJIS = {1: "🔵", 2: "🔴"}
# Decorates the "Game cancelled" message WinnerReportView's own Cancel
# Game button posts (see cancelGameHelper), the button replacement for
# the old /return command.
CANCEL_GAME_EMOJI = "\U0001F6D1"  # 🛑
DEFAULT_ELO = 1000
ELO_K_FACTOR = 32
# +/- range randomly added to each player's elo before balancing ranked
# teams. Keeps matchups from being the exact same optimal split every
# time, at the cost of the balance being only "roughly" fair.
ELO_BALANCE_JITTER = 100
# Role-aware ranked balancing (/make-teams ranked:true use_roles:true, 5v5
# only, see _assignRolesForBalance). Neither penalty touches a player's
# real elo at all; both only shape which role/team split the balancer
# picks, the same way ELO_BALANCE_JITTER's own nudge does.
#
# A player sitting in a role they didn't mark as liked (and didn't mark
# disliked either, no preference on record either way) is assumed to
# perform somewhat below their raw elo there.
ROLE_BALANCE_OFF_ROLE_PENALTY = 100
# A player forced into a role they specifically marked as disliked is
# assumed to underperform a lot more than just "unfamiliar". Someone who
# said they don't want to jungle, jungling, is a bigger gap than someone
# with no stated opinion on it either way.
ROLE_BALANCE_DISLIKED_ROLE_PENALTY = 200
# Fill order _assignRolesForBalance walks SETUP_ROLE_NAMES' five roles in:
# Jungle first, then the rest. This way a player who'd fit multiple roles
# well doesn't get claimed by an easier-to-fill role before the scarcer
# one (junglers are typically the harder role to find genuine takers for)
# ever gets a look at them. Final rosters still get reordered back to
# SETUP_ROLE_NAMES' own Top/Jungle/Mid/Bottom/Support order before being
# handed to a Team, since that's the position makeEmbedString reads each
# row's label from.
ROLE_BALANCE_FILL_ORDER = ["Jungle", "Top", "Mid", "Bottom", "Support"]
# _refineRoleBalance's cap on hill-climb passes over every pairwise role
# swap. It already stops as soon as a full pass finds no improving swap;
# this only guards against a pathological input oscillating forever.
ROLE_BALANCE_MAX_REFINE_PASSES = 5
# Unlike the two penalties above, this one DOES touch real elo. A player
# who wins while playing a role they marked disliked gets their normal
# win delta multiplied up by this much (see computeGameDeltas), a reward
# for actually pulling off a win on a less-wanted assignment, on top of
# whatever the normal team-average swing already gave them. Losing on a
# disliked role earns no such bonus. Only a win counts.
ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER = 1.5
# How long the /clear confirmation buttons stay clickable before the
# reset is abandoned on its own.
CLEAR_CONFIRM_TIMEOUT_SECONDS = 30
# Same idea for /tournament create's overwrite confirmation.
TOURNAMENT_CONFIRM_TIMEOUT_SECONDS = 30
# ...and for /team set's already-in-use voice channel confirmation.
TEAM_CONFIRM_TIMEOUT_SECONDS = 30
# ...and for confirming a reported game winner (see ConfirmWinnerReportView)
# before it's actually recorded.
WINNER_REPORT_CONFIRM_TIMEOUT_SECONDS = 30
# ...and for /set correct-winner's own confirmation (see
# ConfirmCorrectWinnerView) before it reverses/reapplies a game's payouts.
CORRECT_WINNER_CONFIRM_TIMEOUT_SECONDS = 30
# How long /shop browse's own sort buttons (see ShopSortView) stay
# clickable before they freeze in place. Longer than the confirm/cancel
# views above since this isn't gating anything destructive, just a display
# preference someone might sit on while comparing prices.
SHOP_SORT_TIMEOUT_SECONDS = 180
# ...and for confirming a reported duel result (see ConfirmDuelResultView)
# before gold actually changes hands.
DUEL_CONFIRM_TIMEOUT_SECONDS = 30
# How long /wager team's own "Cancel bet" button (see WagerCancelView)
# stays clickable. Not a confirm/cancel dialog like the ones above - just
# the button's own lifetime - so it's set to /set betting-timer's own max
# (600s) rather than a short 30s window: a bet should stay cancellable for
# as long as betting could plausibly still be open. Server-side (see
# _handleWagerCancelClick) is what actually decides whether betting's
# still open on a given click, this just bounds how long Discord itself
# keeps the button interactive.
WAGER_CANCEL_VIEW_TIMEOUT_SECONDS = 600

# /stats: press to toggle the shown avatar between this server's own
# per-server profile picture (if the player has set one, same as the
# card/embed shows by default) and their regular, account-wide avatar (see
# StatsView/_resolveGlobalAvatarUrl). It's the same button either
# direction, flipping based on whichever's currently showing. Both are
# resolved live (not snapshotted at /stats time), so a player who changes
# either avatar later and toggles sees their current one, same as a fresh
# /stats would.
STATS_AVATAR_TOGGLE_EMOJI = "\U0001f5bc️"  # 🖼️ decorates StatsView's Avatar button
# /stats: press to blow the whole embed away and replace it with the
# player's trading card (see _renderTradingCardImage). Both this and the
# avatar toggle above only make sense on the plain /stats embed, so
# StatsView swaps Card out for STATS_RETURN_EMOJI below the moment the
# card goes up. A card isn't shaped like a normal /stats embed, so neither
# toggle applies to it anymore.
STATS_CARD_EMOJI = "\U0001F0CF"  # 🃏 decorates StatsView's Card button
# Shown only once the trading card is up, in place of
# STATS_AVATAR_TOGGLE_EMOJI / STATS_CARD_EMOJI. It's the one action that
# makes sense from the card view: swapping back to the plain /stats embed
# (which then gets its own Card button restored, so the whole thing is a
# real back-and-forth toggle rather than a one-way trip).
STATS_RETURN_EMOJI = "↩️"  # ↩️ decorates StatsView's Back button, was 🪪
# (U+1FAAA IDENTIFICATION CARD), which several Discord clients render as a
# blank or missing glyph despite being valid Unicode. ↩️ is the same
# long-established arrow TEAM_CARD_RETURN_EMOJI already uses for the
# identical "back to the plain view" role on TeamStatsView.

# Trading-card layout (see _renderTradingCardImage), a portrait card
# roughly the shape of a real trading card. Reuses the same canvas/header
# building blocks (_createBracketCanvas, _drawBracketHeader) every other
# rendered image in this file already uses, so it reads as the same
# product rather than a bolted-on fourth visual style.
CARD_WIDTH = 420 * BRACKET_SUPERSAMPLE
CARD_AVATAR_SIZE = 176 * BRACKET_SUPERSAMPLE
CARD_AVATAR_BORDER = 3 * BRACKET_SUPERSAMPLE
CARD_NAME_FONT_SIZE = 20 * BRACKET_SUPERSAMPLE
# Floor _fitNameFont shrinks toward. See that method's own comment for why
# a floor is needed at all now (PRESS_START_2P's unusually wide,
# near-monospace glyphs). Low enough that even Discord's absolute worst
# case, a full 32-character username in PRESS_START_2P, still actually
# clears the card's width once shrunk this far (768px at 24pt, still over
# CARD_WIDTH's ~760px usable width, comfortably under it by 20pt).
CARD_NAME_MIN_FONT_SIZE = 10 * BRACKET_SUPERSAMPLE
CARD_TITLE_FONT_SIZE = 14 * BRACKET_SUPERSAMPLE
CARD_STAT_LABEL_FONT_SIZE = 12 * BRACKET_SUPERSAMPLE
CARD_STAT_VALUE_FONT_SIZE = 15 * BRACKET_SUPERSAMPLE
# One stacked stat line (label + value, see _renderTradingCardImage).
# Every stat gets its own row now instead of sharing a 3-column line, so
# a long value (e.g. a full elo/rank string) has the card's whole width to
# work with instead of a third of it.
CARD_STAT_LINE_HEIGHT = 30 * BRACKET_SUPERSAMPLE
CARD_ELO_BADGE_RADIUS = 6 * BRACKET_SUPERSAMPLE
CARD_TEAM_LOGO_SIZE = 28 * BRACKET_SUPERSAMPLE
CARD_TEAM_ROW_HEIGHT = 34 * BRACKET_SUPERSAMPLE
CARD_TEAM_ROW_GAP = 4 * BRACKET_SUPERSAMPLE
# How many of a player's teams get their own row before the rest just add
# to a "+N more" line. A card is only so tall, and a player stacked on a
# dozen teams shouldn't turn it into a scroll.
CARD_MAX_TEAM_ROWS = 4

# trading_cards' defaults: Shockwave's own site palette (see BRACKET_*
# above) and font pairing, so a player who's never customized their card
# gets exactly the same look every other rendered image already has,
# rather than something generic. Colors are stored as "#RRGGBB" hex in the
# table (portable, human-editable) and converted back to RGB tuples at
# render time (see _hexToRgb).
CARD_DEFAULT_TITLE = "Rookie"
CARD_DEFAULT_ACCENT_COLOR = "#EDC643"      # --gold, same as BRACKET_TITLE_COLOR
# A dark indigo, between the earlier purple family (#2A1245, #4A148C,
# #5B21B6, #4C2287) and pure navy blue (#1A2B5B), landing on a dark
# blue-purple rather than committing fully to either. Deliberately its own
# shade rather than reused from the site's own near-black --ink
# (assets/styles.css), so a default-looking card still reads as its own
# distinct background rather than just "dark".
CARD_DEFAULT_BACKGROUND_COLOR = "#251A5B"
CARD_DEFAULT_TEXT_COLOR = "#F3EFFA"        # --text, same as BRACKET_TEXT_COLOR
CARD_DEFAULT_FONT_STYLE = "Default"        # Chakra Petch + IBM Plex Sans - see _cardFontPaths
# /card-set's name for reverting to the palette above. Always offered
# (see getAvailableCardColorSchemes) the same way CARD_DEFAULT_TITLE
# always is, since it needs no unlocking either.
CARD_DEFAULT_SCHEME_NAME = "Default"

# /team stats: press to swap the embed for a team card (see
# _renderTeamCardImage), the team's own counterpart to /stats' trading
# card. Same one-card-view-with-a-way-back shape as STATS_CARD_EMOJI/
# STATS_RETURN_EMOJI, tracked in its own team_stats_views table rather
# than reusing stats_views, since a team, not a player, is what's shown on
# the card.
TEAM_CARD_EMOJI = "\U0001f6e1️"    # 🛡️ decorates TeamStatsView's Card button
TEAM_CARD_RETURN_EMOJI = "↩️"  # ↩️ decorates TeamStatsView's Back button

# RosterActionView's own button decorations, replacing the old standalone
# /start and /randomize-roles commands. Posted on the SECOND team embed
# only (see printEmbed/_finalizeRoster) once a roster is actually final
# (not mid-draft). Tracked via roster_team1_message_id/
# roster_team2_message_id on `servers`, so a stale click on an earlier
# roster can't act on whatever team1/team2 happen to be loaded now. Both
# role-assignment buttons (Random Roles, Balanced Roles) only get
# included when the roster is exactly 5v5 (see _finalizeRoster). Which
# roles a ranked-with-roles roster was posted showing is tracked
# separately via roster_use_roles.
TEAM_ROLES_REROLL_EMOJI = "\U0001f504"  # 🔄
# Balanced Roles' own button, using the same elo+preference logic
# /make-teams ranked use_roles:true uses (see
# _assignRolesForFixedTeams), just applied after the fact to whatever
# roster is already posted.
TEAM_ROLES_BALANCE_EMOJI = "⚖️"  # ⚖️
TEAM_START_EMOJI = "▶️"  # ▶️
# Same as TEAM_START_EMOJI (posts the matchup image, opens betting) but
# skips moving anyone into team channels. For a group that's already
# elsewhere (a stage channel, external voice, in person, etc.) and
# doesn't want Shockwave touching anyone's voice state. See
# _startRosterViaReaction.
TEAM_START_NO_MOVE_EMOJI = "⚡"  # ⚡
# Fallback voice channel names ▶️ self-heals onto a guild's `channel1`/
# `channel2` (see _ensureDefaultTeamChannels) if a game is started before
# /set channels' team1/team2 have ever been given. Created on demand the
# same way /set channels itself creates a missing channel.
DEFAULT_TEAM_CHANNEL_NAMES = ("Team-1", "Team-2")

# Team-card layout (see _renderTeamCardImage), same card shape/width as
# the player trading card above (CARD_WIDTH, CARD_NAME_FONT_SIZE,
# CARD_STAT_*), just with the team's own logo as the focal point in place
# of a player's avatar, so the two read as the same product rather than
# two unrelated visual styles.
TEAM_CARD_LOGO_SIZE = 200 * BRACKET_SUPERSAMPLE
TEAM_CARD_LOGO_BORDER = CARD_AVATAR_BORDER
TEAM_CARD_LOGO_RADIUS = 20 * BRACKET_SUPERSAMPLE
TEAM_CARD_ROSTER_ROW_HEIGHT = 26 * BRACKET_SUPERSAMPLE
TEAM_CARD_STAR_RADIUS = 5 * BRACKET_SUPERSAMPLE
# How many roster rows get their own line before the rest just add to a
# "+N more" line. Same reasoning as CARD_MAX_TEAM_ROWS, just sized for a
# team roster (typically bigger than one player's team list) rather than
# a player's own team memberships.
TEAM_CARD_MAX_ROSTER_ROWS = 8
# Fallback accent when a team has no usable logo file to sample a color
# from at all (see _dominantLogoColor). Every persistent team gets a logo
# assigned on load (_ensureLogo), so this only matters if the assets
# folder itself went missing since. Same gold as the player card's own
# default accent, for the same "still recognizably Shockwave" reason.
TEAM_CARD_FALLBACK_ACCENT_COLOR = (237, 198, 67)
# How much brighter (0-255 average-channel "brightness") a card's accent
# needs to be than the background behind it. See _ensureReadableAccent.
# Originally just for the team card, where a logo's sampled dominant color
# has no readability guarantee at all (a deep navy or forest green passes
# _dominantLogoColor's own brightness filter just fine), and that same
# color also drives the background (see _renderTeamCardImage). Without
# this, a dark-logoed team's header title and stat labels could end up
# close in brightness to the vignette's own lightened center: legible in
# the dark corners, but nearly invisible in the middle of the card. Reused
# by getUnlockedCardColorSchemes for the same reason: those schemes'
# accents aren't vetted against the darkened-background card they'd be
# driving once equipped as a full color scheme.
#
# Calibrated deliberately low. Against these cards' fairly bright vignette
# centers (~80-120 brightness already), a higher value would force almost
# any color to lighten into the 70-80% HSL-lightness range to clear the
# gap, fully saturated reds and pinks especially. A pure red's own
# average-channel brightness tops out around 85 even at 100% saturation,
# so it would read as washed-out pastel regardless of how saturated
# CARD_SHOP_COLOR_SCHEMES' own raw accents were, undoing any saturation
# tuning there. 45 still rescues a genuinely too-dark color (see
# RenderTeamCardImageTests' own dark-navy regression test) without forcing
# an already-vivid one toward white.
CARD_MIN_ACCENT_CONTRAST = 45

# League-style rank tiers for /stats. Each tier spans 250 elo, with
# DEFAULT_ELO (1000) landing every new player in the middle at Platinum.
# That's a global fallback; a guild can override its own starting elo via
# /set default-elo (see _defaultEloForGuild) without changing this ladder.
# Ascending order, each entry is (elo threshold, tier name, emoji, badge
# color). The trading card (_drawEloBadge) pastes the tier's own real
# emoji artwork (see ELO_BADGE_DIR) instead of the literal character,
# since PIL's bundled TTF fonts can't render color emoji glyphs (the same
# class of issue the roster's captain star ran into). So there's no shape
# to hand-approximate here anymore. badge_color now exists purely for the
# tier-reward trading-card color scheme (see
# ELO_TIER_BADGE_COLORS/getUnlockedCardColorSchemes), independent of
# whatever the badge image itself looks like.
ELO_TIERS = [
    (0, "Iron", "⚙️", (153, 170, 181)),
    (250, "Bronze", "\U0001f949", (205, 127, 50)),
    (500, "Silver", "\U0001f948", (192, 192, 192)),
    (750, "Gold", "\U0001f947", (255, 204, 51)),
    # A clear blue (matching \U0001f537 itself), not cyan/teal; cyan reads
    # as "Diamond" at a glance.
    (1000, "Platinum", "\U0001f537", (41, 121, 255)),
    (1250, "Diamond", "\U0001f48e", (137, 207, 240)),
    (1500, "Master", "\U0001f7e3", (155, 60, 200)),
    (1750, "Grandmaster", "\U0001f534", (221, 46, 68)),
    (2000, "Challenger", "\U0001f451", (255, 199, 44)),
]

# Divisions within a tier, lowest to highest, the same I/II/III/IV split
# League uses, with "I" nearest promotion into the next tier up.
ELO_DIVISIONS = ["IV", "III", "II", "I"]

# Only the first this-many tiers (Iron through Diamond) show a division;
# Master and above show just the tier, same as League showing raw LP
# instead of I-IV once you hit Master.
ELO_DIVISIONED_TIER_COUNT = 6

# Derived, not duplicated, from ELO_TIERS itself: a tier's threshold/badge
# color looked up by name rather than re-typed as separate constants. That
# way nothing here can drift out of sync if ELO_TIERS' own values ever
# change (the exact "stale duplicated constant" bug CARD_DEFAULT_* ran
# into earlier is what this sidesteps).
ELO_TIER_THRESHOLDS = {name: threshold for threshold, name, _emoji, _badge in ELO_TIERS}
ELO_TIER_BADGE_COLORS = {name: badge for _threshold, name, _emoji, badge in ELO_TIERS}
# Each tier's real emoji artwork (see the generation note above), one PNG
# per ELO_TIERS name (e.g. assets/elo-badges/Challenger.png), pasted onto
# the trading card by eloRankBadgeImagePath/_drawEloBadge instead of a
# hand-drawn approximation.
ELO_BADGE_DIR = os.path.join(os.path.dirname(__file__), "assets", "elo-badges")

# Trading-card rewards permanently unlocked (see card_unlocks,
# _checkTierRewardUnlocks) the first time a player reaches each of these
# tiers: a title (equippable as the card's epithet) and a matching color
# scheme (accent sampled from that tier's own ELO_TIERS badge color, same
# "derive, don't duplicate" reasoning as the dicts above). Only Diamond and
# up reward anything. Iron through Platinum are the "everyone passes
# through these" tiers with nothing special to commemorate.
CARD_TIER_REWARD_TITLES = {
    "Diamond": "Diamond Mind",
    "Master": "Mastermind",
    "Grandmaster": "Grandmaster",
    "Challenger": "The Challenger",
}
# Titles granted directly rather than earned by reaching an elo tier - not
# something _checkTierRewardUnlocks ever awards on its own. Right now the
# only grant is SHOCKWAVE_DEVELOPER_ID's own "Developer" title (see
# getUnlockedCardTitles). Kept as its own small catalog rather than folded
# into CARD_TIER_REWARD_TITLES since these have no elo threshold backing
# them at all.
CARD_SPECIAL_TITLES = {
    "Developer": "Developer",
}
# /shop browse: trading-card cosmetics purchasable with gold
# (economy.balance) rather than earned by rank. Same card_unlocks table,
# same itemType/itemKey shape as a tier reward or a special grant, just a
# different unlock path (see shopBuyHelper). Names are kept distinct
# across all three catalogs below (and distinct from every
# ELO_TIERS/CARD_SPECIAL_TITLES name too) so /shop buy's own `item`
# parameter can look one up by name alone without needing to know its
# category ahead of time.
CARD_SHOP_TITLES = {
    "Legend": 5000,
    "Ace": 3000,
    "Champion": 7500,
    "Contender": 1000,
    "Rival": 2000,
    "Vanguard": 3500,
    "Warlord": 4500,
    "Phantom": 5500,
    "Executioner": 6500,
    "Overlord": 8500,
}
# Hand-picked accent/background pairs rather than derived from anything
# else. Unlike a tier reward's scheme, there's no ELO_TIERS badge color
# backing these, just a curated catalog. Still run through
# _ensureReadableAccent before ever being offered (see
# getUnlockedCardColorSchemes) as the same safety net, even though these
# were chosen to already read well against their own background. Accents
# are kept deliberately vivid (at least ~93% HSL saturation, lightness
# clamped to a punchy 48-58% band rather than drifting pastel), so a
# theme's color still reads clearly even on the smaller badge/line
# elements it drives, not just the big ones. The region-named entries pair
# with assets/clash-logos' own region crests (Demacia.png, Noxus.png,
# etc.), the same Runeterra region set, so a team that's already using one
# of those logos has a matching player-card scheme available too.
CARD_SHOP_COLOR_SCHEMES = {
    "Crimson": {"price": 4000, "accent_color": "#F72837", "background_color": "#3D0F14"},
    "Emerald": {"price": 4000, "accent_color": "#09F16B", "background_color": "#0F3D1F"},
    "Azure": {"price": 4000, "accent_color": "#189DF7", "background_color": "#0F2A3D"},
    "Sunset": {"price": 4500, "accent_color": "#FF7D29", "background_color": "#3D1F0F"},
    "Fire": {"price": 4000, "accent_color": "#FF0800", "background_color": "#472100"},
    "Demacia": {"price": 4000, "accent_color": "#F8B530", "background_color": "#0A1D3D"},
    "Noxus": {"price": 4000, "accent_color": "#EC092F", "background_color": "#2B0A0F"},
    "Freljord": {"price": 4000, "accent_color": "#30F0F8", "background_color": "#0D2B3E"},
    "Ionia": {"price": 4000, "accent_color": "#F83074", "background_color": "#2E1A3D"},
    "Piltover": {"price": 4000, "accent_color": "#F8BB30", "background_color": "#0D3B3E"},
    "Zaun": {"price": 4000, "accent_color": "#A6FF00", "background_color": "#1F1A2E"},
    "Shurima": {"price": 4000, "accent_color": "#F7A62E", "background_color": "#3D2A14"},
    "Shadow Isles": {"price": 4000, "accent_color": "#29FF8A", "background_color": "#0A1F16"},
    "Bilgewater": {"price": 4000, "accent_color": "#09ECD7", "background_color": "#2E1A0D"},
    # Accent (lavender, from the region's own purple hue) and background
    # (darkened yellow, the same 28%-of-a-full-saturation-base approach
    # every other entry's background uses) are deliberately swapped from a
    # plain yellow-on-purple pick, since that read too close to
    # CARD_DEFAULT_ACCENT_COLOR's own gold-on-indigo look at a glance.
    "Bandle City": {"price": 4000, "accent_color": "#B677F8", "background_color": "#473700"},
    # Brightened for a comfortable margin above CARD_MIN_ACCENT_CONTRAST,
    # not just scraping past the floor.
    "Targon": {"price": 4000, "accent_color": "#AE77F8", "background_color": "#150A2E"},
}
# Genuinely different typefaces for the card's name/title (see
# _cardFontPaths): RUSSO_ONE/CINZEL/ORBITRON, not just different weights
# of the same Chakra Petch font as before. Price only tracks how loud or
# stylized a face reads, not arrival order. Bold and Elegant are the two
# "quiet" faces (a plain display face, a plain serif) and stay at the
# original baseline price. Every other style commits hard to one specific
# loud aesthetic (futuristic, pixel-arcade, horror, military stencil, neon
# sign, wanted-poster western) and costs more for it. Handwritten
# (Permanent Marker) is the one later addition that reads closer to
# "quiet" than "loud", so it's priced at the baseline too.
CARD_SHOP_FONT_STYLES = {
    "Bold": 2500,
    "Elegant": 2500,
    "Handwritten": 2500,
    "Cyber": 3500,
    "Retro": 3500,
    "Villain": 3500,
    "Military": 3500,
    "Neon": 3500,
    "Western": 3500,
}
# Every card_unlocks itemKey that resolves to a real title (tier-earned,
# specially-granted, or purchased alike). This is the one place
# getUnlockedCardTitles and /card-set's own validation both read from, so
# all three catalogs above only ever need combining in one place. Shop
# titles have no separate display text of their own (unlike a tier's
# flavor title), so each just maps to itself.
CARD_TITLE_CATALOG = {
    **CARD_TIER_REWARD_TITLES, **CARD_SPECIAL_TITLES, **{name: name for name in CARD_SHOP_TITLES},
}

# /shop preview's grid layout (Logos, Color Schemes): cell footprint plus
# the label under it, gapped and margined the same way
# _renderTeamCardImage's own BRACKET_* spacing constants shape everything
# else this file renders. Titles/Fonts don't use a grid at all (see
# _renderCardTitlePreviewImage/_renderFontPreviewImage). There's nothing
# image-shaped to lay out in columns for either, just one row of text per
# option.
PREVIEW_COLUMNS = 6
PREVIEW_CELL_SIZE = 140
PREVIEW_CELL_LABEL_HEIGHT = 30
PREVIEW_CELL_GAP = 18
PREVIEW_MARGIN = 30
PREVIEW_TITLE_FONT_SIZE = 32
PREVIEW_LABEL_FONT_SIZE = 15
# Soft cap on a single page's height before _paginatePreviewItems splits
# the rest onto another image entirely. Discord still renders a much
# taller image fine, but past this it stops reading as "one glanceable
# grid" and starts needing real scrolling to take in.
PREVIEW_MAX_PAGE_HEIGHT = 2200

# Achievements: a fourth path into card_unlocks (title only) alongside a
# tier reward, a special grant, and a shop purchase. Same table, same
# itemType='title' shape (see _unlockAchievement), just a different set of
# trigger conditions checked from gameplay itself (_checkAchievements,
# applyGameDeltas) rather than earned by rank or bought with gold. Unlike
# the other three, unlocking one also posts a Discord notification (see
# _announceAchievements). These are meant to feel like a moment worth
# noticing, not just another option quietly waiting in /card-set's own
# autocomplete.
#
# Gold-based achievements are deliberately keyed off a single transaction
# (a single wager win), never a balance milestone. /daily hands out
# DAILY_GOLD_AMOUNT (1000) for free every single day, so "reach N gold
# saved" would just reward showing up, not anything skill- or
# risk-related, no matter how big N is.
CARD_ACHIEVEMENT_VETERAN_WINS = 10
# Veteran's own ladder, same game_wins column, three further thresholds
# each with their own distinct title (not just "Veteran II"/"III"/"IV")
# so a card's epithet keeps meaning something as the number climbs instead
# of just growing a suffix.
CARD_ACHIEVEMENT_VETERAN_ELITE_WINS = 50
CARD_ACHIEVEMENT_VETERAN_MASTER_WINS = 150
CARD_ACHIEVEMENT_VETERAN_IMMORTAL_WINS = 500
CARD_ACHIEVEMENT_ON_FIRE_STREAK = 5
# On Fire's own ladder, same shape as Veteran's above.
CARD_ACHIEVEMENT_ON_FIRE_UNSTOPPABLE_STREAK = 10
CARD_ACHIEVEMENT_ON_FIRE_UNTOUCHABLE_STREAK = 20
CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD = 5000
# Jackpot: same single-transaction reasoning as High Roller (see the
# module comment above). This is a payout ratio, not an absolute amount,
# so it's really testing "won as a big underdog on the betting side"
# rather than anything balance-related at all.
CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER = 3
CARD_ACHIEVEMENT_UNDERDOG_ELO_GAIN = 20
CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS = 3
CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS = 3
CARD_ACHIEVEMENT_GAMBLER_BETS = 25
CARD_ACHIEVEMENT_IRON_WILL_LOSSES = 20
CARD_ACHIEVEMENT_TITLES = {
    "first_blood": "First Blood",
    "veteran": "Veteran",
    "veteran_elite": "Elite",
    "veteran_master": "Battle-Hardened",
    "veteran_immortal": "Immortal",
    "on_fire": "On Fire",
    "on_fire_unstoppable": "Unstoppable",
    "on_fire_untouchable": "Untouchable",
    "high_roller": "High Roller",
    "jackpot": "Jackpot",
    "underdog": "Giant Slayer",
    "team_player": "Team Player",
    "captain": "The Captain",
    "big_spender": "Big Spender",
    "gambler": "Frequent Bettor",
    "iron_will": "Iron Will",
    "tournament_champion": "Tournament Champion",
    "onboarded": "Onboarded",
}
# /achievements' own descriptions, kept next to the thresholds above that
# they each read from, so the two can't drift out of sync with each other.
CARD_ACHIEVEMENT_DESCRIPTIONS = {
    "first_blood": "Win your first game.",
    "veteran": f"Win {CARD_ACHIEVEMENT_VETERAN_WINS} games.",
    "veteran_elite": f"Win {CARD_ACHIEVEMENT_VETERAN_ELITE_WINS} games.",
    "veteran_master": f"Win {CARD_ACHIEVEMENT_VETERAN_MASTER_WINS} games.",
    "veteran_immortal": f"Win {CARD_ACHIEVEMENT_VETERAN_IMMORTAL_WINS} games.",
    "on_fire": f"Win {CARD_ACHIEVEMENT_ON_FIRE_STREAK} games in a row.",
    "on_fire_unstoppable": f"Win {CARD_ACHIEVEMENT_ON_FIRE_UNSTOPPABLE_STREAK} games in a row.",
    "on_fire_untouchable": f"Win {CARD_ACHIEVEMENT_ON_FIRE_UNTOUCHABLE_STREAK} games in a row.",
    "high_roller": f"Win a single bet of {CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD}+ gold.",
    "jackpot": f"Win a single bet paying out {CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER}x+ your wager.",
    "underdog": "Win a ranked game as a significant underdog (a big single-match elo swing).",
    "team_player": f"Be rostered on {CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS}+ persistent teams at once.",
    "captain": "Be the captain of a persistent team.",
    "big_spender": f"Own {CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS}+ items purchased from /shop buy.",
    "gambler": f"Place {CARD_ACHIEVEMENT_GAMBLER_BETS}+ total bets.",
    "iron_will": f"Rack up {CARD_ACHIEVEMENT_IRON_WILL_LOSSES}+ game losses without giving up.",
    "tournament_champion": "Win a tournament.",
    "onboarded": "Run /setup for the first time.",
}
CARD_TITLE_CATALOG = {**CARD_TITLE_CATALOG, **CARD_ACHIEVEMENT_TITLES}

# Shockwave's own developer. Always has the "Developer" title available
# in every guild the bot is in (see getUnlockedCardTitles), not just ones
# with a card_unlocks row for them. A single id rather than a per-guild
# grant, since the alternative would mean re-granting it by hand every
# time the bot joins a new guild, for something that should just always be
# true, everywhere, for this one account. Set from token.txt's second
# line (see bot.py, right after it reads the token from the first line),
# not hardcoded here, so this default only applies before bot.py has had
# a chance to override it (e.g. in tests, or if token.txt lacks the line).
SHOCKWAVE_DEVELOPER_ID = None
# Same darken-for-background ratio _renderTeamCardImage uses to derive a
# team card's background from its sampled logo accent. Reused here so a
# reward scheme's background relates to its accent the same visual way
# every other card's does.
CARD_BACKGROUND_DARKEN_RATIO = 0.28

# /wager against: a heads-up gold wager between two specific players,
# independent of the team-game betting above. The challenged player
# accepts with a button (DuelAcceptView). Once accepted, anyone can press
# a button to report who actually won (DuelResultView), then confirm it
# (ConfirmDuelResultView) before gold actually changes hands, same shape
# as the team-game winner report.

# /leaderboard: paged via LeaderboardPagingView's buttons rather than
# re-running the command. Clicking one edits the existing message instead
# of posting a new one. These also decorate MyTeamsPagingView/
# TeamListPagingView's own button labels, the same First/Prev/Next/Last
# shape reused for /team lookup and /team list.
LEADERBOARD_PAGE_SIZE = 10
LEADERBOARD_FIRST_EMOJI = "⏮️"  # ⏮️ jump to the first page
LEADERBOARD_PREV_EMOJI = "◀️"   # ◀️ previous page
LEADERBOARD_NEXT_EMOJI = "▶️"   # ▶️ next page
LEADERBOARD_LAST_EMOJI = "⏭️"   # ⏭️ jump to the last page
# First/Prev/Next/Last only move one step (or to either end). This opens
# _PageJumpModal instead, for going straight to an arbitrary page without
# clicking Next N times. Shared the same way the four above are.
LEADERBOARD_JUMP_EMOJI = "\U0001F522"  # 🔢 jump to a specific page
# The ranked-list view's own entry point into cards mode (one player's
# stats card per page, see _renderLeaderboardEntryStatsEmbed), and cards
# mode's own way back out to the list. Distinct from STATS_CARD_EMOJI/
# STATS_RETURN_EMOJI, which toggle the stats-card/trading-card swap
# *within* cards mode, a different action from switching modes entirely.
LEADERBOARD_CARDS_EMOJI = "\U0001F3B4"  # 🎴 enter cards mode from the list
LEADERBOARD_LIST_EMOJI = "\U0001F4CB"  # 📋 leave cards mode back to the list

# /make-teams draft's button-based picker (CaptainsDraftPickView). A
# message tops out at 5 rows of 5 buttons (25 total). One slot is always
# reserved for Random, so a pool of DRAFT_PICK_MAX_UNPAGINATED (24) or
# fewer fits on a single page with no First/Prev/Next/Last row at all.
# Past that, pagination kicks in at DRAFT_PICK_PAGE_SIZE (20) players per
# page, freeing up row 4 entirely for First/Prev/Next/Last/Random.
DRAFT_PICK_MAX_UNPAGINATED = 24
DRAFT_PICK_PAGE_SIZE = 20
DRAFT_PICK_RANDOM_EMOJI = "🎲"

# /team list: what it can sort by, and its display label, same paging
# shape and page size as /leaderboard, just over teams instead of players.
TEAM_LIST_SORT_LABELS = {
    "name": "Name",
    "wins": "Wins",
    "losses": "Losses",
    "win_rate": "Win Rate",
    "roster_size": "Roster Size",
}

# Every stat /leaderboard can filter/sort by, and its display label. "elo"
# doubles as the default sort when no filter is given (the overview view).
LEADERBOARD_STAT_LABELS = {
    "elo": "Elo",
    "balance": "Balance",
    "game_wins": "Game Wins",
    "game_losses": "Game Losses",
    "game_win_rate": "Game Win Rate",
    "ranked_wins": "Ranked Wins",
    "ranked_losses": "Ranked Losses",
    "ranked_win_rate": "Ranked Win Rate",
    "casual_wins": "Casual Wins",
    "casual_losses": "Casual Losses",
    "casual_win_rate": "Casual Win Rate",
    "bet_wins": "Bet Wins",
    "bet_losses": "Bet Losses",
    "bet_win_rate": "Bet Win Rate",
    "net_gold": "Net Gold",
    "gold_wagered": "Gold Wagered",
}

# Which (wins, losses) entry keys define a 0W-0L "hasn't done anything in
# this category yet" player for a given /leaderboard stat, so that view
# can drop them instead of listing them alongside people with an actual
# record. None (the Overview default) and "elo" both use the combined
# game record rather than the narrower ranked one, since a casual-only
# player still has a meaningful elo/overview entry to show (their ranked
# record legitimately reads 0W-0L). Only someone who's never played a game
# at all, ranked or casual, gets dropped here. Stats with no wins/losses
# concept (balance, net_gold, gold_wagered) map to None and never filter
# anyone out.
LEADERBOARD_RECORD_KEYS = {
    None: ("game_wins", "game_losses"),
    "elo": ("game_wins", "game_losses"),
    "balance": None,
    "game_wins": ("game_wins", "game_losses"),
    "game_losses": ("game_wins", "game_losses"),
    "game_win_rate": ("game_wins", "game_losses"),
    "ranked_wins": ("ranked_wins", "ranked_losses"),
    "ranked_losses": ("ranked_wins", "ranked_losses"),
    "ranked_win_rate": ("ranked_wins", "ranked_losses"),
    "casual_wins": ("casual_wins", "casual_losses"),
    "casual_losses": ("casual_wins", "casual_losses"),
    "casual_win_rate": ("casual_wins", "casual_losses"),
    "bet_wins": ("bet_wins", "bet_losses"),
    "bet_losses": ("bet_wins", "bet_losses"),
    "bet_win_rate": ("bet_wins", "bet_losses"),
    "net_gold": None,
    "gold_wagered": None,
}

roles = {
    0: "Top - ",
    1: "Jungle - ",
    2: "Mid - ",
    3: "Bottom - ",
    4: "Support - "
}
# /setup's own role vocabulary, the same five roles as `roles` above,
# just as bare names rather than "<Name> - " embed-line prefixes. What
# gets stored in player_role_preferences.
SETUP_ROLE_NAMES = ["Top", "Jungle", "Mid", "Bottom", "Support"]
SETUP_ROLE_TIMEOUT_SECONDS = 120


# Confirm/cancel buttons for /clear teams, /clear channels, and
# /clear tournament. None of the three ran a confirmation at all before,
# unlike their /clear elo/economy/achievements/card-unlocks siblings
# below, even though /clear tournament in particular is just as
# irreversible (it wipes the bracket, registrations, and match history
# outright). `action` picks which extra step (if any) Confirm takes
# beyond the clearTeamsHelper() every /clear subcommand already does.
# "teams" needs nothing beyond that, so it's the implicit default.
class ConfirmClearActionView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, action="teams"):
        super().__init__(timeout=CLEAR_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.action = action
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /clear can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()
        if self.action == "channels":
            self.helperObj.update(self.guild_id, "channel1", "")
            self.helperObj.update(self.guild_id, "channel2", "")
            result = "Cleared! The saved team channel names have been forgotten too."
        elif self.action == "tournament":
            self.helperObj.deleteTournamentHelper(self.guild_id)
            result = (
                "Cleared! This server's tournament (bracket, registrations, and match history) "
                "has been deleted."
            )
        else:
            result = "Cleared!"
        await self.helperObj.clearTeamsHelper(interaction)
        await interaction.response.edit_message(content=result, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was cleared.", view=self)

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Confirmation expired. Run /clear again if you still want to do this.",
                    view=self,
                )
            except discord.HTTPException:
                pass


# Confirm/cancel buttons for /clear elo, /clear economy,
# /clear achievements, and /clear card-unlocks. /clear elo and
# /clear economy always reset state for every player in the server.
# /clear achievements and /clear card-unlocks normally do too, but both
# can instead target the same optional `target` member (see their own
# `user` parameter). None of the four actually run until whoever ran the
# command clicks "Confirm reset" on this view.
class ConfirmResetView(discord.ui.View):
    def __init__(
        self, helperObj, guild_id, guild_name, invoker_id,
        clear_economy, clear_elo, clear_achievements, clear_card_unlocks=False, target=None,
    ):
        super().__init__(timeout=CLEAR_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.invoker_id = invoker_id
        self.clear_economy = clear_economy
        self.clear_elo = clear_elo
        self.clear_achievements = clear_achievements
        self.clear_card_unlocks = clear_card_unlocks
        self.target = target
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /clear can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        results = []
        if self.clear_economy:
            if self.target is not None:
                self.helperObj.resetEconomyHelper(self.guild_id, user_id=self.target.id)
                results.append(
                    "Economy data (balance, elo, game record, betting record, gold "
                    f"wagered/won/lost) has been reset for {self.target.mention}."
                )
            else:
                self.helperObj.resetEconomyHelper(self.guild_id)
                results.append(
                    "Economy data (balance, elo, game record, betting record, gold "
                    f"wagered/won/lost) has been reset for every player in **{self.guild_name}**."
                )
        elif self.clear_elo:
            reset_elo = self.helperObj._defaultEloForGuild(self.guild_id)
            if self.target is not None:
                self.helperObj.resetEloHelper(self.guild_id, user_id=self.target.id)
                results.append(f"Elo has been reset to {reset_elo} for {self.target.mention}.")
            else:
                self.helperObj.resetEloHelper(self.guild_id)
                results.append(f"Elo has been reset to {reset_elo} for every player in **{self.guild_name}**.")
        if self.clear_achievements:
            if self.target is not None:
                self.helperObj.resetAchievementsHelper(self.guild_id, user_id=self.target.id)
                results.append(f"Every earned achievement has been reset for {self.target.mention}.")
            else:
                self.helperObj.resetAchievementsHelper(self.guild_id)
                results.append(
                    f"Every earned achievement has been reset for every player in **{self.guild_name}**."
                )
        if self.clear_card_unlocks:
            if self.target is not None:
                self.helperObj.resetCardUnlocksHelper(self.guild_id, user_id=self.target.id)
                results.append(
                    f"Every trading-card unlock has been reset for {self.target.mention}, and their card "
                    "restored to Shockwave's defaults."
                )
            else:
                self.helperObj.resetCardUnlocksHelper(self.guild_id)
                results.append(
                    f"Every trading-card unlock has been reset for every player in **{self.guild_name}**, "
                    "and their cards restored to Shockwave's defaults."
                )
        result = " ".join(results)
        self._disable_buttons()
        self.stop()
        # Also clears the current teams/draft (and cancels/refunds any
        # in-progress game first), same as ConfirmClearActionView's own
        # Confirm does - only now, not before this prompt even posted, so
        # Cancel below genuinely leaves everything untouched.
        await self.helperObj.clearTeamsHelper(interaction)
        await interaction.response.edit_message(content=result, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was reset.", view=self)

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Confirmation expired. Run /clear again if you still want to do this.",
                    view=self,
                )
            except discord.HTTPException:
                pass


# Confirm/cancel buttons for /tournament create when a tournament already
# exists for the server; creating one is destructive (it replaces the
# only tournament a server can have), so it doesn't happen until whoever
# ran the command clicks "Overwrite tournament" here.
class ConfirmTournamentOverwriteView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, name, team_size, num_teams, double_elimination):
        super().__init__(timeout=TOURNAMENT_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.name = name
        self.team_size = team_size
        self.num_teams = num_teams
        self.double_elimination = double_elimination
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /tournament create can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Overwrite tournament", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        tournament = Tournament(self.name, self.team_size, self.num_teams, self.double_elimination)
        self.helperObj.saveTournament(self.guild_id, tournament)
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content=f"Tournament **{self.name}** created, replacing the previous one.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. The existing tournament was kept.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /tournament create again if you still want to "
                        "overwrite the existing tournament."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# Confirm/cancel for /tournament create-bracket when this guild already has
# match history (tournament_matches rows) from a previous bracket -
# rebuilding erases it outright (results, and any bets that were never
# settled). Same "only gate what's actually destructive" reasoning
# ConfirmTournamentOverwriteView's own ownership check applies: a bracket
# built (or rerolled) before /tournament start has ever run has no history
# to lose, so createBracketHelper skips this (and the Manage Server check)
# entirely in that case.
class ConfirmBracketOverwriteView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, double_elimination, losers_bracket_timing):
        super().__init__(timeout=TOURNAMENT_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.double_elimination = double_elimination
        self.losers_bracket_timing = losers_bracket_timing
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /tournament create-bracket can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Rebuild bracket", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()
        tournament = self.helperObj.getTournament(self.guild_id)
        if tournament is None:
            await interaction.response.edit_message(
                content="This server's tournament no longer exists.", view=self
            )
            return
        teams = tournament.get_teams()
        self.helperObj._rebuildBracket(
            self.guild_id, tournament, teams, self.double_elimination, self.losers_bracket_timing
        )
        elim_style = "double" if self.double_elimination else "single"
        timing_note = self.helperObj._bracketTimingNote(self.double_elimination, self.losers_bracket_timing)
        await interaction.response.edit_message(
            content=(
                f"Bracket rebuilt for **{tournament.get_name()}** - {len(teams)} teams, "
                f"{elim_style} elimination.{timing_note}"
            ),
            view=self,
        )
        await self.helperObj._sendBracketText(interaction.channel, tournament, self.guild_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. The existing bracket and match history were kept.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /tournament create-bracket again if you still want to "
                        "rebuild the bracket."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# Confirm/cancel buttons for /team set when the requested voice channel is
# already another team's. "Yes" assigns it to this team anyway. (The other
# team's own assignment is left alone, this doesn't enforce exclusivity,
# just warns.) "No" leaves everything as it was and tells the invoker to
# run the command again with a different channel.
class ConfirmVoiceChannelOverwriteView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, team_id, team_name, channel):
        super().__init__(timeout=TEAM_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.team_id = team_id
        self.team_name = team_name
        self.channel = channel
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /team set can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Yes, use it anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        team = self.helperObj.getTeamById(self.guild_id, self.team_id)
        team.set_voice_channel(self.channel)
        self.helperObj.updateTeamData(self.team_id, team)
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content=f"**{self.team_name}**'s voice channel is now {self.channel.mention}.", view=self
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. Run `/team set` again with a different channel.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /team set again if you still want to use this channel."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# Confirm/cancel buttons for /team delete. Deleting a persistent team is
# destructive (its roster/record/logo are gone, and any pending
# /team invite for it becomes unacceptable), so it doesn't happen until the
# captain who ran the command clicks "Delete team" here, the same pattern
# ConfirmResetView established for /clear. This doesn't touch a tournament
# this team may already be registered in. register_team snapshots a copy
# of the Team at registration time (see registerTeamHelper), not a live
# reference, so a deleted team's bracket entry plays out exactly as
# registered either way.
class ConfirmTeamDeleteView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, team_id, team_name):
        super().__init__(timeout=TEAM_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.team_id = team_id
        self.team_name = team_name
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /team delete can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Delete team", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.helperObj._deleteTeam(self.guild_id, self.team_id)
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content=f"**{self.team_name}** has been deleted.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content=f"Cancelled. **{self.team_name}** was kept.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /team delete again if you still want to delete "
                        "this team."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# One SETUP_ROLE_NAMES entry's own toggle button: primary (highlighted)
# when currently selected, secondary otherwise, so the live selection is
# visible at a glance without any separate summary text. There's no
# separate "un-click" the way a reaction's remove event was. Clicking an
# already-selected role's button toggles it off the exact same way
# clicking an unselected one toggles it on, so this one button fully
# replaces the old add-reaction/remove-reaction pair for that role.
class SetupRoleToggleButton(discord.ui.Button):
    def __init__(self, role, selected):
        super().__init__(
            label=role, style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary, row=0,
        )
        self.role_name = role

    async def callback(self, interaction):
        await self.view.helperObj._handleSetupRoleToggleClick(interaction, self.role_name)


# /setup's role-picking step: press a role to toggle it, then press
# Confirm. The same view SHAPE serves both the liked-roles step and the
# disliked-roles step that follows it. confirm's callback
# (helpers._confirmSetupRoleStep) reads which step is current from
# setup_role_sessions itself, and a fresh instance (selected_roles=()) is
# built for the disliked round rather than reusing the liked round's own
# stale button states. Every toggle click also rebuilds a fresh instance
# (see helpers._handleSetupRoleToggleClick) reflecting the DB's current
# selectedRoles, since there's no per-instance state to mutate in place.
# A button's own selected/unselected style is derived fresh every render,
# never tracked on self.
class SetupRoleSelectionView(discord.ui.View):
    def __init__(self, helperObj, guild_id, user_id, selected_roles=()):
        super().__init__(timeout=SETUP_ROLE_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.user_id = user_id
        self.message = None
        for role in SETUP_ROLE_NAMES:
            self.add_item(SetupRoleToggleButton(role, role in selected_roles))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran /setup can use this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction, button):
        await self.helperObj._confirmSetupRoleStep(interaction, self)

    async def on_timeout(self):
        if self.message is None:
            return
        self.helperObj._expireSetupRoleSession(self.guild_id, self.message.id)
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(
                content="Role selection timed out. Run /setup again if you'd like to set your roles.",
                view=self,
            )
        except discord.HTTPException:
            pass


# Discord caps a button's label at 80 characters. A team name is free
# text (/team create, /team rename) with no length limit of its own, so
# this truncates rather than risking an edit/send outright failing on an
# unusually long name.
def _teamButtonLabel(team_name, team_number):
    return f"{team_name} {TEAM_EMOJIS[team_number]}"[:80]


# Team 1/Team 2/Cancel Game are built per-message (dynamic add_item)
# rather than via decorator, so each report can show the game's actual
# team names instead of a fixed "Team 1"/"Team 2" label. custom_id stays
# fixed either way for persistent routing after a restart, same shape as
# SetupRoleToggleButton. team_number is None for Cancel Game.
class _WinnerReportButton(discord.ui.Button):
    def __init__(self, label, style, custom_id, team_number=None):
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.team_number = team_number

    async def callback(self, interaction):
        if self.team_number is None:
            await self.view.helperObj._handleWinnerReportCancelClick(interaction)
        else:
            await self.view.helperObj._handleWinnerReportPick(interaction, self.team_number)


# The winner-report message's own buttons. Persistent (custom_id on every
# item, timeout=None, registered once via client.add_view in bot.py's
# on_ready) rather than a normal per-message View, so an open betting
# window (up to WAGER_TIMER_SECONDS_MAX, or far longer for a big
# tournament's own betting_timer setting) keeps accepting clicks across a
# bot restart or redeploy, the same way the reactions it replaces always
# did. A persistent view is a single shared instance covering every
# guild's open betting window at once, so unlike a normal ConfirmXView
# there's no per-game state on self at all. Every callback below
# re-derives guild_id and the report message id from the interaction
# itself and looks up game state fresh, exactly the "look everything up
# by id, trust nothing stored on an object" shape
# handleGameReportReaction (the reaction handler this replaces) already
# used. team1_name/team2_name default to "Team 1"/"Team 2" for the
# generic instance client.add_view registers at startup (routing only,
# never actually shown); every real send passes the game's actual names
# in.
class WinnerReportView(discord.ui.View):
    def __init__(self, helperObj, team1_name="Team 1", team2_name="Team 2"):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        self.team1 = _WinnerReportButton(
            _teamButtonLabel(team1_name, 1), discord.ButtonStyle.primary,
            "shockwave:winner_report:team1", team_number=1,
        )
        self.team2 = _WinnerReportButton(
            _teamButtonLabel(team2_name, 2), discord.ButtonStyle.danger,
            "shockwave:winner_report:team2", team_number=2,
        )
        self.cancelGame = _WinnerReportButton(
            "Cancel Game", discord.ButtonStyle.secondary, "shockwave:winner_report:cancel",
        )
        self.add_item(self.team1)
        self.add_item(self.team2)
        self.add_item(self.cancelGame)


# A Team 1/Team 2 click on the winner-report message posts this instead of
# recording the result immediately. A real game/economy change (elo,
# payouts, game record) shouldn't hinge on a single accidental click the
# way the roster start/reroll buttons reasonably can, since a fresh
# roster or /clear cleanly undoes those, while a recorded result only has
# the heavier /set correct-winner as its way back. Unlike the initial
# report (still open to anyone at the table, same as the winner-report
# buttons themselves), actually CONFIRMING it is gated to a player
# rostered in this game or a Manage Server admin (see interaction_check /
# _isAdminOrInCurrentGame), so a bystander can't finalize a result for a
# game they were never part of. On Confirm, both the original
# report_message (if handed one) and this confirmation prompt itself are
# deleted once the result is recorded, so nothing stale lingers once
# formatResultMessage's own message has said the same thing for good.
class ConfirmWinnerReportView(discord.ui.View):
    def __init__(self, helperObj, guild_id, winning_team, report_message_id, report_message=None):
        super().__init__(timeout=WINNER_REPORT_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.winning_team = winning_team
        self.report_message_id = report_message_id
        self.report_message = report_message
        self.message = None

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    # See _isAdminOrInCurrentGame's own comment for why this is gated at
    # all now.
    async def interaction_check(self, interaction):
        if self.helperObj._isAdminOrInCurrentGame(interaction):
            return True
        await interaction.response.send_message(
            "Only a player in this game, or a member with the Manage Server permission, can confirm this.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        await self.helperObj.recordResult(
            self.guild_id, self.winning_team, interaction.channel, interaction.guild
        )
        # Both the original betting-open/winner-report message and this
        # confirmation prompt itself are done saying anything useful once
        # the result is actually recorded. formatResultMessage's own
        # message above is what the channel keeps instead.
        await self.helperObj._deleteMessageSafely(self.report_message)
        await self.helperObj._deleteMessageSafely(interaction.message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        self.helperObj._restoreWinnerReportMessage(self.guild_id, self.report_message_id)
        await interaction.response.edit_message(
            content="Report cancelled. Use the buttons on the original message to report the correct winner.",
            view=self,
        )

    async def on_timeout(self):
        self._disable_buttons()
        self.helperObj._restoreWinnerReportMessage(self.guild_id, self.report_message_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation timed out. Use the buttons on the original message to report "
                        "the winner."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# /set correct-winner's own confirmation, for its last-game path (the
# match_id path gets its own ConfirmTournamentMatchCorrectionView instead,
# a different enough shape - bracket propagation, wager-only reversal - not
# to share this one). Reverses and reapplies a real game's payouts/elo/
# records the same way ConfirmWinnerReportView's own Confirm applies them
# in the first place, so this shouldn't hinge on one accidental click
# either, even though it's a typed command rather than a stray button - the
# blast radius (every player in the game, potentially) is the same. Stores
# the exact last_result snapshot the warning was built from (not just a
# guild_id) so Confirm can detect a newer game having resolved in between
# and refuse instead of corrupting it.
class ConfirmCorrectWinnerView(discord.ui.View):
    def __init__(self, helperObj, guild_id, invoker_id, snapshot, correct_team, invalidate):
        super().__init__(timeout=CORRECT_WINNER_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.snapshot = snapshot
        self.correct_team = correct_team
        self.invalidate = invalidate
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /set correct-winner can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm correction", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()

        current = self.helperObj.getLastResult(self.guild_id)
        if current is None or current != self.snapshot:
            await interaction.response.edit_message(
                content=(
                    "The last game has changed since this correction was requested (a newer game "
                    "probably resolved in between). Run /set correct-winner again if it still needs "
                    "correcting."
                ),
                view=self,
            )
            return

        result_text, summary, newly_unlocked = self.helperObj._applyCorrectWinner(
            self.guild_id, self.snapshot, self.correct_team, self.invalidate
        )
        await interaction.response.edit_message(content=result_text, view=self)
        if summary is not None:
            await interaction.channel.send(self.helperObj.formatResultMessage(self.correct_team, summary))
            await self.helperObj._announceAchievements(interaction.channel, newly_unlocked)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was corrected.", view=self)

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /set correct-winner again if it still needs correcting."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# Cancel Game click posts this instead of cancelling immediately, same
# reasoning as ConfirmWinnerReportView. A real refund-and-move-everyone-
# back action shouldn't hinge on one accidental click, so both of the
# winner-report message's consequential buttons now share the same
# two-step shape. Only roster start/reroll stay single-click, since those
# are still cleanly reversible afterward. On Confirm, the original
# report_message (if handed one) has its buttons stripped too, matching
# ConfirmWinnerReportView.
class ConfirmCancelGameView(discord.ui.View):
    def __init__(self, helperObj, guild_id, report_message_id, report_message=None):
        super().__init__(timeout=WINNER_REPORT_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.report_message_id = report_message_id
        self.report_message = report_message
        self.message = None

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    # See _isAdminOrInCurrentGame's own comment for why this is gated at
    # all now.
    async def interaction_check(self, interaction):
        if self.helperObj._isAdminOrInCurrentGame(interaction):
            return True
        await interaction.response.send_message(
            "Only a player in this game, or a member with the Manage Server permission, can confirm this.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        await self.helperObj._finishGameCancel(
            self.guild_id, interaction.channel, interaction.guild, self.report_message
        )
        # Both the original report message and this confirmation prompt
        # itself are done saying anything useful once the game's actually
        # cancelled. cancelGameHelper's own "Game cancelled." message is
        # what the channel keeps instead, matching how
        # ConfirmWinnerReportView cleans up its own two messages.
        await self.helperObj._deleteMessageSafely(interaction.message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        self.helperObj._restoreWinnerReportMessage(self.guild_id, self.report_message_id)
        await interaction.response.edit_message(
            content="Game kept. Use the buttons on the original message to report the winner or cancel again.",
            view=self,
        )

    async def on_timeout(self):
        self._disable_buttons()
        self.helperObj._restoreWinnerReportMessage(self.guild_id, self.report_message_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Cancellation confirmation timed out. Use the buttons on the original message."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# Same idea as ConfirmWinnerReportView, for a simultaneous-mode tournament
# match's own Team 1/Team 2 report instead of the guild-wide singleton
# one. This is a separate view since a simultaneous round can have
# several matches (and so several pending confirmations) live at once,
# each needing its own match_id/channel_id rather than the one guild_id a
# normal game's report has. Confirm calls _resolveTournamentMatch directly
# (the same function recordResult's own tournament hook calls for
# sequential mode) and then deletes the original match message along with
# this confirmation prompt, matching ConfirmWinnerReportView. Cancel or
# timeout puts the match back to AWAITING_RESULT via
# _restoreTournamentMatchAwaitingResult so it can be reacted on again.
class ConfirmTournamentMatchReportView(discord.ui.View):
    def __init__(self, helperObj, guild_id, match_id, winning_team, channel_id, report_message=None):
        super().__init__(timeout=WINNER_REPORT_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.match_id = match_id
        self.winning_team = winning_team
        self.channel_id = channel_id
        self.report_message = report_message
        self.message = None

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    # Scoped to this specific match's own two rosters
    # (_isPlayerInTournamentMatch), not the guild-wide game, since several
    # matches can be live at once.
    async def interaction_check(self, interaction):
        if (
            interaction.user.guild_permissions.manage_guild
            or self.helperObj._isPlayerInTournamentMatch(self.match_id, interaction.user.id)
        ):
            return True
        await interaction.response.send_message(
            "Only a player in this match, or a member with the Manage Server permission, can confirm this.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        await self.helperObj._resolveTournamentMatch(
            self.guild_id, self.match_id, self.winning_team, self.channel_id
        )
        await self.helperObj._deleteMessageSafely(self.report_message)
        await self.helperObj._deleteMessageSafely(interaction.message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        self.helperObj._restoreTournamentMatchAwaitingResult(self.match_id)
        await interaction.response.edit_message(
            content=(
                "Report cancelled. Use the buttons on the original match message to report the "
                "correct winner."
            ),
            view=self,
        )

    async def on_timeout(self):
        self._disable_buttons()
        self.helperObj._restoreTournamentMatchAwaitingResult(self.match_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation timed out. Use the buttons on the original match message to "
                        "report the winner."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# /set correct-winner's match_id path (see _correctTournamentMatchHelper),
# same "a real payout/bracket change shouldn't hinge on one click" reasoning
# ConfirmCorrectWinnerView applies to the last-game path, just with this
# path's own state to re-verify: the expected old winner and "next round
# hasn't started" check both get re-checked at Confirm time too, not just
# when the prompt was first built, in case either changed in between.
class ConfirmTournamentMatchCorrectionView(discord.ui.View):
    def __init__(
        self, helperObj, guild_id, invoker_id, match_id, round_index, node_index, expected_winner, correct_team
    ):
        super().__init__(timeout=CORRECT_WINNER_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.match_id = match_id
        self.round_index = round_index
        self.node_index = node_index
        self.expected_winner = expected_winner
        self.correct_team = correct_team
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /set correct-winner can confirm this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm correction", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()

        self.helperObj.cursor.execute(
            "SELECT state, winner FROM tournament_matches WHERE guildId=? AND id=?",
            (self.guild_id, self.match_id)
        )
        row = self.helperObj.cursor.fetchone()
        stale = (
            row is None or row[0] != "RESOLVED" or row[1] != self.expected_winner
            or self.helperObj._nextTournamentRoundStarted(self.guild_id, self.round_index)
        )
        if stale:
            await interaction.response.edit_message(
                content=(
                    f"Match #{self.match_id} has changed since this correction was requested. Run "
                    "/set correct-winner again if it still needs correcting."
                ),
                view=self,
            )
            return

        applied = self.helperObj._applyTournamentMatchCorrection(
            self.guild_id, self.match_id, self.node_index, self.correct_team
        )
        if applied is None:
            await interaction.response.edit_message(
                content="This server's tournament no longer exists.", view=self
            )
            return
        result_text, tournament, newly_unlocked = applied
        await interaction.response.edit_message(content=result_text, view=self)
        await self.helperObj._sendBracketText(interaction.channel, tournament, self.guild_id)
        await self.helperObj._announceAchievements(interaction.channel, newly_unlocked)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. The match's recorded winner was kept.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation expired. Run /set correct-winner again if it still needs correcting."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# The sequential-mode ready-check message's own button, persistent
# (custom_id, timeout=None, registered once via client.add_view) since a
# match can sit waiting on a captain indefinitely, same reasoning as
# WinnerReportView/DuelAcceptView. The callback re-derives which match
# (and whether the clicker is actually one of its captains) from
# interaction.guild_id and interaction.message.id.
class TournamentReadyView(discord.ui.View):
    def __init__(self, helperObj):
        super().__init__(timeout=None)
        self.helperObj = helperObj

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success, custom_id="shockwave:tournament:ready")
    async def ready(self, interaction, button):
        await self.helperObj._handleReadyClick(interaction)


# Team 1/Team 2 are built per-message (dynamic add_item) rather than via
# decorator so each match report can show the match's actual team names,
# the same reasoning/shape _WinnerReportButton/WinnerReportView use.
class _TournamentReportButton(discord.ui.Button):
    def __init__(self, label, style, custom_id, team_number):
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.team_number = team_number

    async def callback(self, interaction):
        await self.view.helperObj._handleTournamentMatchReportClick(interaction, self.team_number)


# The simultaneous-mode match report message's own buttons, same
# persistent shape as TournamentReadyView, for the same "can sit
# AWAITING_RESULT indefinitely" reason. A press posts a
# ConfirmTournamentMatchReportView instead of resolving immediately,
# matching WinnerReportView/ConfirmWinnerReportView's two-step shape.
# team1_name/team2_name default to "Team 1"/"Team 2" for the generic
# instance client.add_view registers at startup (routing only). Every
# real send passes the match's actual names in.
class TournamentMatchReportView(discord.ui.View):
    def __init__(self, helperObj, team1_name="Team 1", team2_name="Team 2"):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        self.team1 = _TournamentReportButton(
            _teamButtonLabel(team1_name, 1), discord.ButtonStyle.primary,
            "shockwave:tournament:team1", team_number=1,
        )
        self.team2 = _TournamentReportButton(
            _teamButtonLabel(team2_name, 2), discord.ButtonStyle.danger,
            "shockwave:tournament:team2", team_number=2,
        )
        self.add_item(self.team1)
        self.add_item(self.team2)


# The posted roster's own buttons (team2_message only, see
# _finalizeRoster), persistent (custom_id, timeout=None, registered once
# via client.add_view) since a roster can sit un-started indefinitely,
# same reasoning as WinnerReportView. Random Roles/Balanced Roles always
# show together, since a single shared instance can't conditionally omit
# a button per-message the way adding a reaction conditionally once
# could. Their own callbacks re-check the roster's actual team sizes and
# politely no-op or reject if it isn't exactly 5v5, the same guard the
# old reaction handler already had for "a prankster reacts 🔄 on a
# message that never earned it".
class RosterActionView(discord.ui.View):
    # include_role_buttons=False (a roster that isn't exactly 5v5) omits
    # both buttons from THIS message entirely, matching the reaction they
    # replace (which was only ever added when _finalizeRoster's own
    # size-eligibility check passed). The generic instance registered once
    # at startup still has all four, since persistent-view registration is
    # only about routing a custom_id's clicks, not about which buttons any
    # one message actually shows.
    def __init__(self, helperObj, include_role_buttons=True):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if not include_role_buttons:
            self.remove_item(self.reroll)
            self.remove_item(self.balanceRoles)

    @discord.ui.button(
        label=f"Start {TEAM_START_EMOJI}", style=discord.ButtonStyle.success,
        custom_id="shockwave:roster:start",
    )
    async def start(self, interaction, button):
        await self.helperObj._handleRosterStartClick(interaction, move=True)

    @discord.ui.button(
        label=f"Start (no move) {TEAM_START_NO_MOVE_EMOJI}", style=discord.ButtonStyle.primary,
        custom_id="shockwave:roster:start_no_move",
    )
    async def startNoMove(self, interaction, button):
        await self.helperObj._handleRosterStartClick(interaction, move=False)

    # Also doubles as the very first role assignment, not just a
    # re-shuffle of an already role-labelled roster. A plain (non-ranked)
    # 5v5 make-teams split never had roles shown at all until this is
    # clicked once.
    @discord.ui.button(
        label=f"Random Roles {TEAM_ROLES_REROLL_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:roster:reroll",
    )
    async def reroll(self, interaction, button):
        await self.helperObj._handleRosterRerollClick(interaction)

    @discord.ui.button(
        label=f"Balanced Roles {TEAM_ROLES_BALANCE_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:roster:balance_roles",
    )
    async def balanceRoles(self, interaction, button):
        await self.helperObj._handleRosterBalanceRolesClick(interaction)


# /team invite's own posted message, persistent (custom_id, timeout=None,
# registered once via client.add_view) since an invite can sit unanswered
# indefinitely, same reasoning as WinnerReportView. One shared Accept
# button covers every invitee on the message (see
# _handleTeamInviteAcceptClick). The DB lookup itself, scoped to
# targetId=interaction.user.id, is what tells several different invited
# members' clicks apart, not anything about the button or view. Cancel is
# the odd one out of the three: unlike Accept/Decline (each scoped to
# whichever invitee clicked), it's for the team's own captain/admin side -
# retracting the whole invite, every remaining invitee on the message at
# once, not just one of them (see _handleTeamInviteCancelClick).
class TeamInviteAcceptView(discord.ui.View):
    def __init__(self, helperObj):
        super().__init__(timeout=None)
        self.helperObj = helperObj

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="shockwave:team_invite:accept")
    async def accept(self, interaction, button):
        await self.helperObj._handleTeamInviteAcceptClick(interaction)

    @discord.ui.button(
        label="Decline", style=discord.ButtonStyle.secondary, custom_id="shockwave:team_invite:decline"
    )
    async def decline(self, interaction, button):
        await self.helperObj._handleTeamInviteDeclineClick(interaction)

    @discord.ui.button(
        label="Cancel invite", style=discord.ButtonStyle.danger, custom_id="shockwave:team_invite:cancel"
    )
    async def cancelInvite(self, interaction, button):
        await self.helperObj._handleTeamInviteCancelClick(interaction)


# /team transfer's own posted message, same persistent shape as
# TeamInviteAcceptView and for the same reason: an offer can sit
# unanswered indefinitely. Accept/Decline are scoped to the one player
# being offered the captaincy (toCaptainId), Cancel transfer to whoever's
# captain right now (or a Manage Server admin) - the exact same
# three-button shape TeamInviteAcceptView already uses, just for handing
# off captaincy instead of joining a roster.
class TeamTransferAcceptView(discord.ui.View):
    def __init__(self, helperObj):
        super().__init__(timeout=None)
        self.helperObj = helperObj

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="shockwave:team_transfer:accept")
    async def accept(self, interaction, button):
        await self.helperObj._handleTeamTransferAcceptClick(interaction)

    @discord.ui.button(
        label="Decline", style=discord.ButtonStyle.secondary, custom_id="shockwave:team_transfer:decline"
    )
    async def decline(self, interaction, button):
        await self.helperObj._handleTeamTransferDeclineClick(interaction)

    @discord.ui.button(
        label="Cancel transfer", style=discord.ButtonStyle.danger, custom_id="shockwave:team_transfer:cancel"
    )
    async def cancelTransfer(self, interaction, button):
        await self.helperObj._handleTeamTransferCancelClick(interaction)


# /stats' own posted message, persistent (custom_id, timeout=None,
# registered once via client.add_view) since nothing ever expires a stats
# view on its own, the same open-ended reasoning as WinnerReportView.
# card_shown picks which of Card/Back is actually attached to THIS
# message (see RosterActionView's own include_reroll for why a persistent
# view's registered template and any one message's real button set don't
# have to match). Avatar always shows either way, since both the embed
# and the trading card have their own avatar to toggle.
class StatsView(discord.ui.View):
    def __init__(self, helperObj, card_shown=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if card_shown:
            self.remove_item(self.showCard)
        else:
            self.remove_item(self.returnToStats)

    @discord.ui.button(
        label=f"Avatar {STATS_AVATAR_TOGGLE_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:stats:avatar_toggle",
    )
    async def avatarToggle(self, interaction, button):
        await self.helperObj._handleStatsAvatarToggleClick(interaction)

    @discord.ui.button(
        label=f"Card {STATS_CARD_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:stats:show_card",
    )
    async def showCard(self, interaction, button):
        await self.helperObj._handleStatsShowCardClick(interaction)

    @discord.ui.button(
        label=f"Back {STATS_RETURN_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:stats:return",
    )
    async def returnToStats(self, interaction, button):
        await self.helperObj._handleStatsReturnClick(interaction)


# /team stats' own posted message, same persistent, state-dependent-
# button-set shape as StatsView, just for a team (no avatar toggle, since
# a team card has no per-player avatar to flip).
class TeamStatsView(discord.ui.View):
    def __init__(self, helperObj, card_shown=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if card_shown:
            self.remove_item(self.showCard)
        else:
            self.remove_item(self.returnToStats)

    @discord.ui.button(
        label=f"Card {TEAM_CARD_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:team_stats:show_card",
    )
    async def showCard(self, interaction, button):
        await self.helperObj._handleTeamStatsShowCardClick(interaction)

    @discord.ui.button(
        label=f"Back {TEAM_CARD_RETURN_EMOJI}", style=discord.ButtonStyle.primary,
        custom_id="shockwave:team_stats:return",
    )
    async def returnToStats(self, interaction, button):
        await self.helperObj._handleTeamStatsReturnClick(interaction)


# A button can't take free text, so the "Page #" button on each of the
# three paging views below opens this instead. Whichever handler_name
# names (one of _handleLeaderboardPageClick/_handleMyTeamsPageClick/
# _handleTeamListPageClick) gets called with target_page set to the
# 0-based page the user typed, the same call shape a Prev/Next click
# already uses (see _computeNewPage's own target= branch). total_pages
# here is only a snapshot from whenever "Page #" was clicked, used for
# the label's range hint and to reject obvious nonsense early. The
# handler re-derives the CURRENT total_pages itself before actually
# jumping, so anything typed past it just clamps to the last page, same
# as Next already does when there's nowhere further to go, in case
# entries changed while the modal was open.
class _PageJumpModal(discord.ui.Modal):
    def __init__(self, helperObj, handler_name, total_pages):
        super().__init__(title="Jump to Page")
        self.helperObj = helperObj
        self.handler_name = handler_name
        self.page_input = discord.ui.TextInput(
            label=f"Page number (1-{total_pages})", placeholder="e.g. 3", max_length=6,
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction):
        raw = self.page_input.value.strip()
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                f"'{raw}' isn't a valid page number. Enter a whole number, 1 or higher.", ephemeral=True
            )
            return
        handler = getattr(self.helperObj, self.handler_name)
        await handler(interaction, target_page=int(raw) - 1)


# /leaderboard, /team lookup, and /team list all page the exact same way:
# First/Prev/Next/Last/Page#, one shared view per guild/caller/search
# rather than re-running the command. So all three views below are the
# same button shape, just wired to a different helper.py handler and
# table. Persistent (custom_id, timeout=None, registered once via
# client.add_view) since nothing ever expires one of these pages on its
# own, the same open-ended reasoning as WinnerReportView.
class LeaderboardPagingView(discord.ui.View):
    def __init__(self, helperObj, cards=False, card_shown=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if not cards:
            self.remove_item(self.showCard)
            self.remove_item(self.returnToStats)
            self.remove_item(self.backToList)
        else:
            self.remove_item(self.viewCards)
            if card_shown:
                self.remove_item(self.showCard)
            else:
                self.remove_item(self.returnToStats)

    @discord.ui.button(label=LEADERBOARD_FIRST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:first")
    async def first(self, interaction, button):
        await self.helperObj._handleLeaderboardPageClick(interaction, "first")

    @discord.ui.button(label=LEADERBOARD_PREV_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:prev")
    async def prev(self, interaction, button):
        await self.helperObj._handleLeaderboardPageClick(interaction, "prev")

    @discord.ui.button(label=LEADERBOARD_NEXT_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:next")
    async def next(self, interaction, button):
        await self.helperObj._handleLeaderboardPageClick(interaction, "next")

    @discord.ui.button(label=LEADERBOARD_LAST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:last")
    async def last(self, interaction, button):
        await self.helperObj._handleLeaderboardPageClick(interaction, "last")

    @discord.ui.button(
        label=f"Page # {LEADERBOARD_JUMP_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:leaderboard:jump",
    )
    async def jump(self, interaction, button):
        await self.helperObj._handleLeaderboardJumpClick(interaction)

    # Re-sorts the same filter in the other direction without re-running
    # the command, the same "independent toggle buttons, not one cycling
    # button" shape ShopSortView already established. Persisted in
    # `leaderboards` (this view is timeout=None/persistent) rather than
    # held as plain instance state the way ShopSortView's own short-lived
    # view can get away with.
    @discord.ui.button(label="Ascending", style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:asc")
    async def ascending(self, interaction, button):
        await self.helperObj._handleLeaderboardOrderClick(interaction, "asc")

    @discord.ui.button(label="Descending", style=discord.ButtonStyle.secondary, custom_id="shockwave:leaderboard:desc")
    async def descending(self, interaction, button):
        await self.helperObj._handleLeaderboardOrderClick(interaction, "desc")

    # The ranked list's own entry point into cards mode: one player's full
    # stats card per page instead of everyone at once (see
    # _renderLeaderboardEntryStatsEmbed), starting from whichever entry is
    # first on the currently-shown list page. Only shown outside cards
    # mode.
    @discord.ui.button(
        label=f"Cards {LEADERBOARD_CARDS_EMOJI}", style=discord.ButtonStyle.primary,
        custom_id="shockwave:leaderboard:view_cards",
    )
    async def viewCards(self, interaction, button):
        await self.helperObj._handleLeaderboardViewCardsClick(interaction)

    # Cards mode's own way back to the ranked list, the reverse of
    # viewCards above. Shown throughout cards mode, alongside whichever of
    # showCard/returnToStats also applies.
    @discord.ui.button(
        label=f"List {LEADERBOARD_LIST_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:leaderboard:back_to_list",
    )
    async def backToList(self, interaction, button):
        await self.helperObj._handleLeaderboardBackToListClick(interaction)

    # /team list's own Card/Back toggle (see TeamListPagingView), carried
    # over here; cards mode swaps the summary list for one player's full
    # /stats embed per page, and this additionally lets that flip over to
    # their actual trading card. Never shown at all outside cards mode.
    @discord.ui.button(
        label=f"Card {STATS_CARD_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:leaderboard:show_card",
    )
    async def showCard(self, interaction, button):
        await self.helperObj._handleLeaderboardShowCardClick(interaction)

    @discord.ui.button(
        label=f"Back {STATS_RETURN_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:leaderboard:return",
    )
    async def returnToStats(self, interaction, button):
        await self.helperObj._handleLeaderboardReturnClick(interaction)


# See LeaderboardPagingView, same shape, /team lookup's own table/handler.
# Also offers the same Card/Back toggle TeamListPagingView's cards:true mode
# does (see that class's own comment), so a team's actual trading card is
# reachable from here too, not just from /team list cards:true.
class MyTeamsPagingView(discord.ui.View):
    def __init__(self, helperObj, card_shown=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if card_shown:
            self.remove_item(self.showCard)
        else:
            self.remove_item(self.returnToStats)

    @discord.ui.button(label=LEADERBOARD_FIRST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:my_teams:first")
    async def first(self, interaction, button):
        await self.helperObj._handleMyTeamsPageClick(interaction, "first")

    @discord.ui.button(label=LEADERBOARD_PREV_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:my_teams:prev")
    async def prev(self, interaction, button):
        await self.helperObj._handleMyTeamsPageClick(interaction, "prev")

    @discord.ui.button(label=LEADERBOARD_NEXT_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:my_teams:next")
    async def next(self, interaction, button):
        await self.helperObj._handleMyTeamsPageClick(interaction, "next")

    @discord.ui.button(label=LEADERBOARD_LAST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:my_teams:last")
    async def last(self, interaction, button):
        await self.helperObj._handleMyTeamsPageClick(interaction, "last")

    @discord.ui.button(
        label=f"Page # {LEADERBOARD_JUMP_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:my_teams:jump",
    )
    async def jump(self, interaction, button):
        await self.helperObj._handleMyTeamsJumpClick(interaction)

    @discord.ui.button(
        label=f"Card {TEAM_CARD_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:my_teams:show_card",
    )
    async def showCard(self, interaction, button):
        await self.helperObj._handleMyTeamsShowCardClick(interaction)

    @discord.ui.button(
        label=f"Back {TEAM_CARD_RETURN_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:my_teams:return",
    )
    async def returnToStats(self, interaction, button):
        await self.helperObj._handleMyTeamsReturnClick(interaction)


# See LeaderboardPagingView for the paging buttons, /team list's own
# table/handler. `cards` (only ever True for a /team list cards:true
# message) additionally offers TeamStatsView's own Card/Back toggle, so
# the currently-paged team's plain stats card can be swapped for its
# actual trading card. `card_shown` picks which of the two is attached to
# THIS message, the same "registered template and one message's real
# button set don't have to match" reasoning RosterActionView's own
# include_reroll already established. Never shown at all for a plain
# (non-cards) list.
class TeamListPagingView(discord.ui.View):
    def __init__(self, helperObj, cards=False, card_shown=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        if not cards:
            self.remove_item(self.showCard)
            self.remove_item(self.returnToStats)
        elif card_shown:
            self.remove_item(self.showCard)
        else:
            self.remove_item(self.returnToStats)

    @discord.ui.button(label=LEADERBOARD_FIRST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:team_list:first")
    async def first(self, interaction, button):
        await self.helperObj._handleTeamListPageClick(interaction, "first")

    @discord.ui.button(label=LEADERBOARD_PREV_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:team_list:prev")
    async def prev(self, interaction, button):
        await self.helperObj._handleTeamListPageClick(interaction, "prev")

    @discord.ui.button(label=LEADERBOARD_NEXT_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:team_list:next")
    async def next(self, interaction, button):
        await self.helperObj._handleTeamListPageClick(interaction, "next")

    @discord.ui.button(label=LEADERBOARD_LAST_EMOJI, style=discord.ButtonStyle.secondary, custom_id="shockwave:team_list:last")
    async def last(self, interaction, button):
        await self.helperObj._handleTeamListPageClick(interaction, "last")

    @discord.ui.button(
        label=f"Page # {LEADERBOARD_JUMP_EMOJI}", style=discord.ButtonStyle.secondary,
        custom_id="shockwave:team_list:jump",
    )
    async def jump(self, interaction, button):
        await self.helperObj._handleTeamListJumpClick(interaction)

    @discord.ui.button(
        label=f"Card {TEAM_CARD_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:team_list:show_card",
    )
    async def showCard(self, interaction, button):
        await self.helperObj._handleTeamListShowCardClick(interaction)

    @discord.ui.button(
        label=f"Back {TEAM_CARD_RETURN_EMOJI}", style=discord.ButtonStyle.primary, custom_id="shockwave:team_list:return",
    )
    async def returnToStats(self, interaction, button):
        await self.helperObj._handleTeamListReturnClick(interaction)


# Lets whoever ran /shop browse re-sort the listing by price or by owned
# status, either direction, without re-running the command. Four
# independent toggle buttons (not a single cycling one), so the current
# sort is always visible at a glance from which two are "pressed". Purely
# a display preference: clicking any of these only re-renders the same
# embed with a different sort_key/descending combination (see
# helpers._buildShopEmbed) and never touches gold, ownership, or the
# catalog itself. So unlike every other View in this file, there's
# nothing to restore on cancel or timeout, just stop taking input once it
# expires.
class ShopSortView(discord.ui.View):
    def __init__(self, helperObj, guild_id, user_id):
        super().__init__(timeout=SHOP_SORT_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.user_id = user_id
        self.sort_key = None
        # Named `sort_descending`, not `descending`. That would collide
        # with the `descending` button method below, which discord.py's
        # button decorator turns into a class-level ui.Item descriptor
        # that an instance attribute of the same name would shadow.
        self.sort_descending = False
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran /shop browse can sort this.", ephemeral=True
            )
            return False
        return True

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    async def _reRender(self, interaction):
        embed = self.helperObj._buildShopEmbed(
            self.guild_id, self.user_id, self.sort_key, self.sort_descending
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Sort: Price", style=discord.ButtonStyle.secondary)
    async def sortByPrice(self, interaction, button):
        self.sort_key = "price"
        await self._reRender(interaction)

    @discord.ui.button(label="Sort: Owned", style=discord.ButtonStyle.secondary)
    async def sortByOwned(self, interaction, button):
        self.sort_key = "owned"
        await self._reRender(interaction)

    @discord.ui.button(label="Ascending", style=discord.ButtonStyle.secondary)
    async def ascending(self, interaction, button):
        self.sort_descending = False
        await self._reRender(interaction)

    @discord.ui.button(label="Descending", style=discord.ButtonStyle.secondary)
    async def descending(self, interaction, button):
        self.sort_descending = True
        await self._reRender(interaction)

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# /wager team's own bet-confirmation message. Not persistent (no
# custom_id, not registered via client.add_view): a bet only needs
# cancelling for as long as betting could still be open
# (WAGER_CANCEL_VIEW_TIMEOUT_SECONDS), so surviving a restart isn't worth
# the same custom_id-routing machinery the genuinely long-lived views
# above need. guild_id/user_id/match_id are captured on self at
# construction instead, since (unlike a persistent view) this one's never
# reconstructed from a custom_id alone. match_id is None for a bet on the
# current game, or a tournament match's own id for one placed via
# match_id= (see wagerHelper/_placeTournamentWager).
class WagerCancelView(discord.ui.View):
    def __init__(self, helperObj, guild_id, user_id, match_id=None):
        super().__init__(timeout=WAGER_CANCEL_VIEW_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.user_id = user_id
        self.match_id = match_id

    @discord.ui.button(label="Cancel bet", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        await self.helperObj._handleWagerCancelClick(interaction, self.guild_id, self.user_id, self.match_id)


# /wager against's challenge message's own buttons, persistent (custom_id,
# timeout=None, registered once via client.add_view) since a challenge can
# sit unanswered indefinitely, same reasoning as WinnerReportView. A
# single shared instance covers every pending challenge in every guild,
# so each callback re-derives which duel (and whether this clicker is
# actually the challenged player) from interaction.guild_id and
# interaction.message.id rather than anything stored on self. Decline
# mirrors TeamInviteAcceptView's own Accept/Decline pair: the challenged
# player can make an unwanted challenge go away instead of it just sitting
# there forever (no gold is ever escrowed until Accept, so there's nothing
# to refund on a decline). Cancel challenge is the challenger's own side of
# that same idea - retracting a challenge they regret sending, mirroring
# TeamInviteAcceptView's Cancel invite button - distinct from
# DuelResultView's "Cancel Duel" below (custom_id
# shockwave:duel:cancel, note the different id), which only applies once a
# duel's already been accepted and gold's actually at stake.
class DuelAcceptView(discord.ui.View):
    def __init__(self, helperObj):
        super().__init__(timeout=None)
        self.helperObj = helperObj

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="shockwave:duel:accept")
    async def accept(self, interaction, button):
        await self.helperObj._handleDuelAcceptClick(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, custom_id="shockwave:duel:decline")
    async def decline(self, interaction, button):
        await self.helperObj._handleDuelDeclineClick(interaction)

    @discord.ui.button(
        label="Cancel challenge", style=discord.ButtonStyle.danger, custom_id="shockwave:duel:cancel_challenge"
    )
    async def cancelChallenge(self, interaction, button):
        await self.helperObj._handleDuelRetractClick(interaction)


# The accepted duel's own result-report message's buttons, same
# persistent shape as DuelAcceptView, for the same "a duel can sit
# AWAITING_RESULT indefinitely" reason. A press posts a
# ConfirmDuelResultView instead of paying out immediately, matching
# WinnerReportView/ConfirmWinnerReportView's two-step shape, a real gold
# transfer shouldn't hinge on a single accidental click. Cancel Duel
# mirrors WinnerReportView's own Cancel Game button (see
# ConfirmDuelCancelView below): once gold is actually escrowed (see
# DuelAcceptView's own Decline, which only covers before that point),
# there was otherwise no way back for a duel neither side wants finished -
# a disagreement, someone leaving, or it just being forgotten would leave
# both stakes stuck with no refund and no admin override either.
class DuelResultView(discord.ui.View):
    def __init__(self, helperObj):
        super().__init__(timeout=None)
        self.helperObj = helperObj

    @discord.ui.button(
        label="Challenger Won 🔵", style=discord.ButtonStyle.primary,
        custom_id="shockwave:duel:challenger_won",
    )
    async def challengerWon(self, interaction, button):
        await self.helperObj._handleDuelResultClick(interaction, winner_is_challenger=True)

    @discord.ui.button(
        label="Target Won 🔴", style=discord.ButtonStyle.danger,
        custom_id="shockwave:duel:target_won",
    )
    async def targetWon(self, interaction, button):
        await self.helperObj._handleDuelResultClick(interaction, winner_is_challenger=False)

    @discord.ui.button(
        label="Cancel Duel", style=discord.ButtonStyle.secondary, custom_id="shockwave:duel:cancel",
    )
    async def cancelDuel(self, interaction, button):
        await self.helperObj._handleDuelCancelClick(interaction)


# A Challenger Won/Target Won click posts this instead of paying out the
# pot immediately. Confirm actually pays out (via _finishDuelResolution,
# which re-fetches the duel's own row by id rather than trusting anything
# stored here besides the id itself) and then strips the original duel
# message's own buttons via _clearMessageButtons, matching
# ConfirmWinnerReportView. Cancel or timeout restores the duel to
# AWAITING_RESULT via _restoreDuelAwaitingResult so its buttons work
# again. Not persistent: a short, one-off confirmation window, same as
# ConfirmWinnerReportView/ConfirmTournamentMatchReportView.
class ConfirmDuelResultView(discord.ui.View):
    def __init__(self, helperObj, duel_id, winner_is_challenger, report_message=None):
        super().__init__(timeout=DUEL_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.duel_id = duel_id
        self.winner_is_challenger = winner_is_challenger
        self.report_message = report_message
        self.message = None

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    # Scoped to this specific duel's own two participants
    # (_isPlayerInDuel), same reasoning as the other confirm views.
    async def interaction_check(self, interaction):
        if (
            interaction.user.guild_permissions.manage_guild
            or self.helperObj._isPlayerInDuel(self.duel_id, interaction.user.id)
        ):
            return True
        await interaction.response.send_message(
            "Only a participant in this duel, or a member with the Manage Server permission, can confirm this.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Result confirmed, paying out the wager...", view=self
        )
        await self.helperObj._finishDuelResolution(self.duel_id, self.winner_is_challenger)
        await self.helperObj._clearMessageButtons(self.report_message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        self.helperObj._restoreDuelAwaitingResult(self.duel_id)
        await interaction.response.edit_message(
            content="Report cancelled. Use the buttons on the original message to report the correct result.",
            view=self,
        )

    async def on_timeout(self):
        self._disable_buttons()
        self.helperObj._restoreDuelAwaitingResult(self.duel_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Confirmation timed out. Use the buttons on the original message to report "
                        "the result."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# DuelResultView's Cancel Duel click posts this instead of refunding
# immediately, same two-step shape ConfirmDuelResultView uses for the
# opposite action (paying out) and for the same reason. Shares
# _restoreDuelAwaitingResult with ConfirmDuelResultView on Cancel/timeout:
# both views only ever move a duel INTO the shared 'CONFIRMING' state from
# 'AWAITING_RESULT', so restoring back to 'AWAITING_RESULT' is correct
# regardless of which of the two prompted it.
class ConfirmDuelCancelView(discord.ui.View):
    def __init__(self, helperObj, duel_id, report_message=None):
        super().__init__(timeout=DUEL_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.duel_id = duel_id
        self.report_message = report_message
        self.message = None

    def _disable_buttons(self):
        for item in self.children:
            item.disabled = True

    # Scoped to this specific duel's own two participants
    # (_isPlayerInDuel), same reasoning as the other confirm views.
    async def interaction_check(self, interaction):
        if (
            interaction.user.guild_permissions.manage_guild
            or self.helperObj._isPlayerInDuel(self.duel_id, interaction.user.id)
        ):
            return True
        await interaction.response.send_message(
            "Only a participant in this duel, or a member with the Manage Server permission, can confirm this.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(
            content="Duel cancelled, refunding both players...", view=self
        )
        await self.helperObj._finishDuelCancellation(self.duel_id)
        await self.helperObj._clearMessageButtons(self.report_message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        self.helperObj._restoreDuelAwaitingResult(self.duel_id)
        await interaction.response.edit_message(
            content="Duel kept. Use the buttons on the original message to report a result or cancel again.",
            view=self,
        )

    async def on_timeout(self):
        self._disable_buttons()
        self.helperObj._restoreDuelAwaitingResult(self.duel_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "Cancellation confirmation timed out. Use the buttons on the original message."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# One remaining draft-pool player's own button on CaptainsDraftPickView.
# DynamicItem (not a plain discord.ui.Button) since the pool is a
# variable-length, per-guild list. A fixed custom_id per possible player,
# the way WinnerReportView's team1/team2 buttons work, can't cover
# "however many people happen to be drafting this time" without
# pre-registering far more distinct custom_ids than a single View can
# ever hold (25). The template below encodes only a slot POSITION (0-23,
# this page's index into the pool), never a player id.
# _handleDraftPickSlotClick re-resolves that position against the guild's
# current pool fresh at click time, the same "trust nothing stored on the
# object" discipline WinnerReportView already uses. label/style come
# straight off the reconstructed `item` Discord already parsed from the
# raw component (from_custom_id), so a restart never needs a DB
# round-trip just to redraw a button that's about to be replaced by a
# fresh render the moment it's clicked anyway.
class _DraftPickSlotButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shockwave:draft_pick:slot:(?P<index>[0-9]+)"):
    def __init__(self, index, label, style, row):
        super().__init__(
            discord.ui.Button(
                label=label[:80], style=style, row=row,
                custom_id=f"shockwave:draft_pick:slot:{index}",
            )
        )
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["index"]), item.label, item.style, item.row)

    async def interaction_check(self, interaction):
        return await interaction.client.helperObj._isDraftPickTurn(interaction)

    async def callback(self, interaction):
        await interaction.client.helperObj._handleDraftPickSlotClick(interaction, self.index)


# The draft picker's own posted message: one _DraftPickSlotButton per
# player on the current page (blue while Team 1 is picking, red for Team
# 2, recomputed fresh every render, never toggled in place), a Random
# button that's always present, and First/Prev/Next/Last only once the
# pool no longer fits on one page (see DRAFT_PICK_MAX_UNPAGINATED). No
# on_timeout at all; timeout=None like WinnerReportView, since a draft
# waiting on a captain shouldn't quietly lock up mid-pick the way a
# confirm dialog reasonably can.
class CaptainsDraftPickView(discord.ui.View):
    def __init__(self, helperObj, pool_page=(), turn=1, paginated=False):
        super().__init__(timeout=None)
        self.helperObj = helperObj
        # Random/First/Prev/Next/Last already exist as children the moment
        # super().__init__() runs, since decorator-registered buttons are
        # added by the base View before this body even starts. So the
        # unwanted nav buttons have to come out BEFORE the slot buttons go
        # in, or a full 24-slot non-paginated page plus all 5 fixed
        # buttons would overflow the 25-child cap.
        if not paginated:
            self.remove_item(self.first)
            self.remove_item(self.prev)
            self.remove_item(self.next)
            self.remove_item(self.last)
        style = discord.ButtonStyle.primary if turn == 1 else discord.ButtonStyle.danger
        for i, player in enumerate(pool_page):
            self.add_item(_DraftPickSlotButton(i, player.get_name(), style, i // 5))

    async def interaction_check(self, interaction):
        return await self.helperObj._isDraftPickTurn(interaction)

    @discord.ui.button(
        label=f"Random {DRAFT_PICK_RANDOM_EMOJI}", style=discord.ButtonStyle.success, row=4,
        custom_id="shockwave:draft_pick:random",
    )
    async def random(self, interaction, button):
        await self.helperObj._handleDraftPickRandomClick(interaction)

    @discord.ui.button(label=LEADERBOARD_FIRST_EMOJI, style=discord.ButtonStyle.secondary, row=4, custom_id="shockwave:draft_pick:first")
    async def first(self, interaction, button):
        await self.helperObj._handleDraftPickPageClick(interaction, "first")

    @discord.ui.button(label=LEADERBOARD_PREV_EMOJI, style=discord.ButtonStyle.secondary, row=4, custom_id="shockwave:draft_pick:prev")
    async def prev(self, interaction, button):
        await self.helperObj._handleDraftPickPageClick(interaction, "prev")

    @discord.ui.button(label=LEADERBOARD_NEXT_EMOJI, style=discord.ButtonStyle.secondary, row=4, custom_id="shockwave:draft_pick:next")
    async def next(self, interaction, button):
        await self.helperObj._handleDraftPickPageClick(interaction, "next")

    @discord.ui.button(label=LEADERBOARD_LAST_EMOJI, style=discord.ButtonStyle.secondary, row=4, custom_id="shockwave:draft_pick:last")
    async def last(self, interaction, button):
        await self.helperObj._handleDraftPickPageClick(interaction, "last")


class helpers():
    def __init__(self, cursor, db) -> None:
        self.cursor = cursor
        self.db = db
        # Set by bot.py once the discord.Client exists. Needed so the
        # background betting timer and the raw-reaction handler (neither
        # of which run inside an Interaction) can still fetch channels and
        # send messages.
        self.client = None
        # guildId -> asyncio.Task for the currently running betting timer,
        # so CANCEL_GAME_EMOJI (or a fresh ▶️ click) mid-game can cancel it
        # instead of letting a stale "betting closed" message fire later.
        self.bettingTasks = {}

    # SQL get template function
    def get(self, guild_id, column):
        self.cursor.execute("SELECT " + column +
                    " FROM servers WHERE guildId=?", (guild_id,))
        return self.cursor.fetchone()[0]

    # SQL update template function
    def update(self, guild_id, column, value):
        self.cursor.execute("UPDATE servers SET " + column +
                    "=? WHERE guildId=?", (value, guild_id))
        self.db.commit()

    # Team objects formed by /make-teams random, /make-teams draft, etc.
    # use these as their .name, read by printEmbed (the roster embed
    # titles) and _renderMatchupImage (the matchup graphic), and later
    # handed to computeGameDeltas/formatResultMessage so the win/elo-change
    # summary says the same names too. An admin-configured channel1/channel2
    # name (see /set channels' team1/team2 params) reads a lot better than
    # a bare "Team 1"/"Team 2". Falls back to that for a guild that's
    # never named its channels.
    def _rosterTeamNames(self, guild_id):
        name1 = self.get(guild_id, "channel1")
        name2 = self.get(guild_id, "channel2")
        return name1 or "Team 1", name2 or "Team 2"

    async def randomizeTeamHelper(self, ctx):
        await self.clearTeamsHelper(ctx)

        members = []
        team1 = Team()
        team2 = Team()
        team1.name, team2.name = self._rosterTeamNames(ctx.guild.id)

        channel = ctx.user.voice.channel

        for i in channel.members:
            members.append(i)

        m = np.array(members, dtype=object)
        np.random.shuffle(m)

        for i in range(len(members)):
            newPlayer = Player()
            newPlayer.name = m[i].name
            newPlayer.id = m[i].id

            if i < len(members) / 2:
                team1.add_player(newPlayer)
            else:
                team2.add_player(newPlayer)

        serialzedTeam1 = team1.serializeTeam()
        serialzedTeam2 = team2.serializeTeam()

        self.update(ctx.guild.id, "team1", serialzedTeam1)
        self.update(ctx.guild.id, "team2", serialzedTeam2)
        self.update(ctx.guild.id, "mode", "Normal")
        self.update(ctx.guild.id, "game", self._currentGame(ctx.guild.id))

    # Splits members into two roughly elo-balanced teams. Each player's elo
    # gets a random +/-ELO_BALANCE_JITTER nudge before sorting, so the
    # split isn't the exact same optimal matchup every time. Then a snake
    # draft (strongest pick alternates sides each round: 1,2,2,1,1,2,2,1,...)
    # keeps team sizes within one of each other while spreading strong and
    # weak picks across both sides, rather than stacking every top player
    # on one team.
    def formBalancedTeams(self, members_with_elo):
        jittered = [
            (member, elo + random.uniform(-ELO_BALANCE_JITTER, ELO_BALANCE_JITTER))
            for member, elo in members_with_elo
        ]
        jittered.sort(key=lambda pair: pair[1], reverse=True)

        team1, team2 = [], []
        for i, (member, _elo) in enumerate(jittered):
            round_num, pos = divmod(i, 2)
            first_pick_is_team1 = round_num % 2 == 0
            goes_to_team1 = (pos == 0) == first_pick_is_team1
            (team1 if goes_to_team1 else team2).append(member)

        return team1, team2

    # One of the three tiers ROLE_BALANCE_OFF_ROLE_PENALTY/
    # ROLE_BALANCE_DISLIKED_ROLE_PENALTY are keyed off of, given the
    # (liked, disliked) lists getRolePreferences returns for a player.
    def _roleTier(self, liked, disliked, role):
        if role in liked:
            return "liked"
        if role in disliked:
            return "disliked"
        return "neutral"

    def _roleBalancePenalty(self, tier):
        return {
            "liked": 0,
            "neutral": ROLE_BALANCE_OFF_ROLE_PENALTY,
            "disliked": ROLE_BALANCE_DISLIKED_ROLE_PENALTY,
        }[tier]

    # Builds one (member, elo, role, tier, effective_elo) entry.
    # effective_elo is elo minus whichever penalty `tier` earns, 0 for
    # "liked". Shared by _assignRolesForBalance's initial fill and
    # _refineRoleBalance's swaps so both ways a player ends up on a role
    # score it the same way.
    def _reassignRole(self, guild_id, member, elo, role):
        liked, disliked = self.getRolePreferences(guild_id, member.id)
        tier = self._roleTier(liked, disliked, role)
        return (member, elo, role, tier, elo - self._roleBalancePenalty(tier))

    # Greedily assigns each of `members_with_elo` (must be exactly 10,
    # 5v5, the only shape roles apply to, see formRoleBalancedTeams) one
    # of SETUP_ROLE_NAMES' five roles, two players per role (one per
    # eventual team, _splitRoleBalancedTeams decides which). Walks
    # ROLE_BALANCE_FILL_ORDER (jungle first) and, for each role in turn,
    # fills its two slots from whichever unassigned players actually like
    # it first, then unassigned players with no stated preference either
    # way, and only reaches for someone who marked the role disliked if
    # nothing else is left for it.
    #
    # Returns a list of 10 (member, elo, role, tier, effective_elo) tuples
    # in ROLE_BALANCE_FILL_ORDER order, not SETUP_ROLE_NAMES' on-screen
    # order. Callers that need Top/Jungle/Mid/Bottom/Support order (i.e.
    # _splitRoleBalancedTeams) reorder for themselves.
    # per_role=2 is the ranked 10-player shape (two players per role, one
    # per eventual side). _assignRolesForFixedTeams reuses this same fill
    # with per_role=1 to place a single already-fixed 5-player team's own
    # five members across the five roles.
    def _assignRolesForBalance(self, guild_id, members_with_elo, per_role=2):
        elo_by_id = {member.id: elo for member, elo in members_with_elo}
        preferences = {
            member.id: self.getRolePreferences(guild_id, member.id) for member, _elo in members_with_elo
        }
        remaining = sorted((member for member, _elo in members_with_elo), key=lambda m: m.id)

        assigned = []
        for role in ROLE_BALANCE_FILL_ORDER:
            liking, neutral, disliking = [], [], []
            for member in remaining:
                liked, disliked = preferences[member.id]
                tier = self._roleTier(liked, disliked, role)
                (liking if tier == "liked" else disliking if tier == "disliked" else neutral).append(member)

            for member in (liking + neutral + disliking)[:per_role]:
                remaining.remove(member)
                assigned.append(self._reassignRole(guild_id, member, elo_by_id[member.id], role))

        return assigned

    # Brute-forces which of each role's two players lands on which side
    # (2**5 = 32 combinations, cheap enough to just try all of them) and
    # keeps whichever split minimizes the gap between the two sides' total
    # effective_elo. `assigned` is 10 (member, elo, role, tier,
    # effective_elo) tuples, two per SETUP_ROLE_NAMES role (the shape
    # _assignRolesForBalance/_refineRoleBalance both produce). Returns
    # (side_a, side_b), each a list of 5 such tuples sorted into
    # SETUP_ROLE_NAMES' own Top/Jungle/Mid/Bottom/Support order, matching
    # the position makeEmbedString reads each roster row's role label from.
    def _splitRoleBalancedTeams(self, assigned):
        by_role = {}
        for entry in assigned:
            by_role.setdefault(entry[2], []).append(entry)
        role_pairs = [by_role[role] for role in SETUP_ROLE_NAMES]

        best_diff, best_combo = None, None
        for combo in itertools.product((0, 1), repeat=len(role_pairs)):
            side_a = [pair[flip] for pair, flip in zip(role_pairs, combo)]
            side_b = [pair[1 - flip] for pair, flip in zip(role_pairs, combo)]
            diff = abs(sum(e[4] for e in side_a) - sum(e[4] for e in side_b))
            if best_diff is None or diff < best_diff:
                best_diff, best_combo = diff, (side_a, side_b)

        return best_combo

    def _roleSplitDiff(self, assigned):
        side_a, side_b = self._splitRoleBalancedTeams(assigned)
        return abs(sum(e[4] for e in side_a) - sum(e[4] for e in side_b))

    # Hill-climbs on top of _assignRolesForBalance's initial
    # preference-first fill: repeatedly tries swapping which role each of
    # two players is assigned to (any two players holding different
    # roles, not just ones _splitRoleBalancedTeams currently has on
    # opposite sides, since it re-decides sides fresh every time anyway),
    # keeping a swap only if it lets _splitRoleBalancedTeams find a
    # tighter effective-elo split than the best one seen so far. A swap
    # that only makes preference fit worse without ever improving balance
    # is never kept. This only refines the balance on top of whatever
    # _assignRolesForBalance already prioritized for preference, it never
    # fights it for its own sake. Stops once a full pass finds no
    # improving swap, or after ROLE_BALANCE_MAX_REFINE_PASSES passes
    # either way.
    def _refineRoleBalance(self, guild_id, assigned):
        best_assigned = list(assigned)
        best_diff = self._roleSplitDiff(best_assigned)

        for _pass in range(ROLE_BALANCE_MAX_REFINE_PASSES):
            improved = False
            for i, j in itertools.combinations(range(len(best_assigned)), 2):
                member_i, elo_i, role_i, _tier_i, _eff_i = best_assigned[i]
                member_j, elo_j, role_j, _tier_j, _eff_j = best_assigned[j]
                if role_i == role_j:
                    continue

                candidate = list(best_assigned)
                candidate[i] = self._reassignRole(guild_id, member_i, elo_i, role_j)
                candidate[j] = self._reassignRole(guild_id, member_j, elo_j, role_i)

                diff = self._roleSplitDiff(candidate)
                if diff < best_diff:
                    best_assigned, best_diff, improved = candidate, diff, True
                    break
            if not improved:
                break

        return best_assigned

    # Entry point for /make-teams ranked:true use_roles:true, returns
    # None for anything other than exactly 10 players (rankedTeamHelper
    # falls back to the roleless formBalancedTeams split in that case,
    # same as the casual /make-teams path does for non-5v5 rosters).
    # Otherwise runs _assignRolesForBalance's preference-first fill,
    # _refineRoleBalance's balance-improving swap pass on top of it, and
    # returns the final (side_a, side_b) split, each already ordered
    # Top/Jungle/Mid/Bottom/Support for makeEmbedString.
    def formRoleBalancedTeams(self, guild_id, members_with_elo):
        if len(members_with_elo) != 10:
            return None
        assigned = self._assignRolesForBalance(guild_id, members_with_elo)
        refined = self._refineRoleBalance(guild_id, assigned)
        return self._splitRoleBalancedTeams(refined)

    # Hill-climbs the same way _refineRoleBalance does, just constrained
    # to swaps within one already-fixed team at a time (own/other pairs
    # cover both directions each pass). Membership can't move between
    # sides here, the way it can for ranked's own from-scratch split, so a
    # role swap can only change the team it's made within. Still able to
    # shrink the gap between the two teams' effective-elo totals though,
    # since each team's own total shifts with how many
    # liked/neutral/disliked penalties its particular fill landed it with.
    def _refineFixedTeamRoleBalance(self, guild_id, team1_assigned, team2_assigned):
        team1_assigned, team2_assigned = list(team1_assigned), list(team2_assigned)
        for _pass in range(ROLE_BALANCE_MAX_REFINE_PASSES):
            improved = False
            for own, other in ((team1_assigned, team2_assigned), (team2_assigned, team1_assigned)):
                other_sum = sum(e[4] for e in other)
                best_diff = abs(sum(e[4] for e in own) - other_sum)
                for i, j in itertools.combinations(range(len(own)), 2):
                    member_i, elo_i, role_i, _tier_i, _eff_i = own[i]
                    member_j, elo_j, role_j, _tier_j, _eff_j = own[j]
                    if role_i == role_j:
                        continue

                    candidate = list(own)
                    candidate[i] = self._reassignRole(guild_id, member_i, elo_i, role_j)
                    candidate[j] = self._reassignRole(guild_id, member_j, elo_j, role_i)

                    diff = abs(sum(e[4] for e in candidate) - other_sum)
                    if diff < best_diff:
                        own[:] = candidate
                        best_diff, improved = diff, True
                        break
            if not improved:
                break

        return team1_assigned, team2_assigned

    # Entry point for the roster's own Balanced Roles button: the same
    # preference-first fill and elo-diff-minimizing refinement ranked
    # roles uses, just run independently against two ALREADY-fixed
    # 5-player rosters (any /make-teams split, a captains draft, etc.)
    # instead of building the team split itself. Callers must already
    # have checked both lists have exactly 5 entries (the only shape
    # roles apply to). Returns (team1_entries, team2_entries), each 5
    # (member, elo, role, tier, effective_elo) tuples in SETUP_ROLE_NAMES
    # order, matching formRoleBalancedTeams' own per-side shape so callers
    # build Team objects from either the same way.
    def _assignRolesForFixedTeams(self, guild_id, team1_members_with_elo, team2_members_with_elo):
        team1_assigned = self._assignRolesForBalance(guild_id, team1_members_with_elo, per_role=1)
        team2_assigned = self._assignRolesForBalance(guild_id, team2_members_with_elo, per_role=1)
        team1_assigned, team2_assigned = self._refineFixedTeamRoleBalance(guild_id, team1_assigned, team2_assigned)

        role_order = {role: i for i, role in enumerate(SETUP_ROLE_NAMES)}
        team1_assigned.sort(key=lambda entry: role_order[entry[2]])
        team2_assigned.sort(key=lambda entry: role_order[entry[2]])
        return team1_assigned, team2_assigned

    # What a brand new player's elo starts at in this guild: DEFAULT_ELO
    # (1000) unless an admin has overridden it with /set default-elo's
    # param (see adminSetHelper), in which case that value wins instead.
    # This is the one place every other elo-defaulting call site in this
    # file goes through, so a guild's configured default never has to be
    # re-looked-up or duplicated by hand.
    def _defaultEloForGuild(self, guild_id):
        value = self.get(guild_id, "default_elo")
        return value if value is not None else DEFAULT_ELO

    def averageElo(self, members, elo_by_id, default_elo=DEFAULT_ELO):
        if not members:
            return default_elo
        return round(sum(elo_by_id[m.id] for m in members) / len(members))

    # The (emoji, plain-text label, badge color, bare tier name) behind
    # eloRankLabel/eloRankLabelPlain/eloRankBadgeImagePath, e.g.
    # ("\U0001f537", "Platinum III", (41, 121, 255), "Platinum") once
    # divisions stop applying. ELO_TIERS is sorted
    # ascending, so the last threshold at or below elo wins: exactly 1000
    # is Platinum, not Gold, and anything above the top tier's threshold
    # is still Challenger. `tier_name` is always the bare name (no
    # division suffix), unlike `label`. It's what eloRankBadgeImagePath
    # looks the asset file up by.
    def _eloRankParts(self, elo):
        tier_index = 0
        for i, (threshold, _name, _emoji, _badge) in enumerate(ELO_TIERS):
            if elo >= threshold:
                tier_index = i
            else:
                break

        threshold, name, emoji, badge_color = ELO_TIERS[tier_index]

        if tier_index >= ELO_DIVISIONED_TIER_COUNT:
            return emoji, name, badge_color, name

        span = ELO_TIERS[tier_index + 1][0] - threshold
        offset = max(elo - threshold, 0)
        segment_size = span / len(ELO_DIVISIONS)
        division_index = min(int(offset // segment_size), len(ELO_DIVISIONS) - 1)

        return emoji, f"{name} {ELO_DIVISIONS[division_index]}", badge_color, name

    # Maps a raw elo number to a League-style "emoji tier division" label,
    # e.g. "\U0001f537 Platinum III", what /stats and /leaderboard show,
    # since Discord's own client renders the emoji fine in embed text.
    def eloRankLabel(self, elo):
        emoji, label, _badge, _tier_name = self._eloRankParts(elo)
        return f"{emoji} {label}"

    # Same tier/division text, without the leading emoji, for the trading
    # card (_renderTradingCardImage), which draws its stats with PIL, and
    # the bundled TTF fonts don't have these glyphs (the same class of
    # issue the roster's captain star and the matchup header's bullet
    # separator ran into). Discord's client-side emoji rendering isn't
    # available there the way it is for a normal embed field.
    def eloRankLabelPlain(self, elo):
        return self._eloRankParts(elo)[1]

    # Path to this tier's real emoji artwork (see ELO_BADGE_DIR), the
    # trading card's stand-in for the emoji eloRankLabel shows in a real
    # embed. Actual saved emoji images rather than a hand-drawn PIL
    # approximation, so there's no shape/color to keep in sync with the
    # real glyph by hand.
    def eloRankBadgeImagePath(self, elo):
        tier_name = self._eloRankParts(elo)[3]
        return os.path.join(ELO_BADGE_DIR, f"{tier_name}.png")

    # Forms elo-balanced teams from the caller's voice channel and marks
    # the game as ranked, so elo actually gets updated when the winner is
    # eventually reported (see computeGameDeltas/recordResult). Everything
    # else (moving players, opening betting) is still the posted roster's
    # ▶️ reaction's job, same as /make-teams.
    #
    # use_roles=True additionally tries to assign Top/Jungle/Mid/Bottom/
    # Support (see formRoleBalancedTeams), only possible with exactly 10
    # players, since roles only make sense for two full 5-player sides.
    # Anything else falls back to the same roleless formBalancedTeams
    # split everyone else gets, with a note explaining why roles weren't
    # applied.
    async def rankedTeamHelper(self, ctx, use_roles=False, not_setup_note=None):
        await self.clearTeamsHelper(ctx)

        guild_id = ctx.guild.id
        channel = ctx.user.voice.channel
        default_elo = self._defaultEloForGuild(guild_id)
        game = self._currentGame(guild_id)

        members_with_elo = []
        for member in channel.members:
            self.ensureEconomyRow(guild_id, member.id, member.name)
            self.ensureGameStatsRow(guild_id, member.id, member.name, game)
            elo = self.getGameStat(guild_id, member.id, game, "elo")
            members_with_elo.append((member, elo if elo is not None else default_elo))

        elo_by_id = {member.id: elo for member, elo in members_with_elo}
        name1, name2 = self._rosterTeamNames(guild_id)

        roles_requested = use_roles
        use_roles = use_roles and self._gameSupportsRoles(game)
        role_split = self.formRoleBalancedTeams(guild_id, members_with_elo) if use_roles else None

        team1 = Team()
        team1.name = name1
        team2 = Team()
        team2.name = name2

        disliked_role_ids = []
        if role_split is not None:
            side_a, side_b = role_split
            for member, _elo, _role, tier, _eff in side_a:
                team1.add_player(Player(member.id, member.name))
                if tier == "disliked":
                    disliked_role_ids.append(member.id)
            for member, _elo, _role, tier, _eff in side_b:
                team2.add_player(Player(member.id, member.name))
                if tier == "disliked":
                    disliked_role_ids.append(member.id)
            team1_members = [entry[0] for entry in side_a]
            team2_members = [entry[0] for entry in side_b]
        else:
            team1_members, team2_members = self.formBalancedTeams(members_with_elo)
            for member in team1_members:
                team1.add_player(Player(member.id, member.name))
            for member in team2_members:
                team2.add_player(Player(member.id, member.name))

        # Read back by recordResult/reportCorrectWinnerHelper to credit
        # the ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER bonus. Stored
        # as plain comma-separated text since it's never queried, only
        # ever read back whole (see _dislikedRoleUserIds).
        self.update(guild_id, "disliked_role_user_ids", ",".join(str(i) for i in disliked_role_ids))

        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "mode", "Ranked")
        self.update(guild_id, "is_ranked", 1)
        self.update(guild_id, "game", game)

        team1_avg = self.averageElo(team1_members, elo_by_id, default_elo)
        team2_avg = self.averageElo(team2_members, elo_by_id, default_elo)

        message = (
            f"Ranked teams created! Team 1 avg elo **{team1_avg}**, Team 2 avg elo **{team2_avg}**. "
        )
        if roles_requested and not self._gameSupportsRoles(game):
            message += (
                f"Role-based team balancing is League-only, so it's off for **{game}**. Showing the "
                f"roster as normal instead. "
            )
        elif use_roles and role_split is None:
            message += (
                "Roles need exactly 10 players (5 a side) to assign, so no roles were assigned this "
                "time. Showing the roster as normal instead. "
            )
        elif role_split is not None and not_setup_note:
            # Only relevant once roles actually got assigned; the
            # exactly-10 fallback above already explains why nobody's
            # preferences mattered this time.
            message += f"{not_setup_note} "
        message += (
            "Press Start on the roster below when you're ready to move everyone and open betting, or "
            "Start (no move) to open betting without moving anyone.\n"
        )
        message += self._gameNote(guild_id)
        await ctx.response.send_message(message)
        intro_message = await ctx.original_response()
        team1_message, team2_message, _ = await self.printEmbed(ctx, team1, team2, useRoles=role_split is not None)
        await self._finalizeRoster(
            ctx.guild.id, team1_message, team2_message, team1, team2, use_roles=role_split is not None,
            intro_messages=[intro_message],
        )

    def makeEmbedString(self, team: Team, useRoles=False):
        teamString = ""

        # Usernames can contain markdown characters (_, *, ~, `, etc.).
        # Escape them so e.g. "under_score" doesn't get parsed as italics
        # once dropped into an embed description alongside every other
        # name.
        if useRoles and len(team.players) == 5:
            for i in range(5):
                teamString += roles.get(i) + discord.utils.escape_markdown(team.players[i].name) + "\n"
        else:
            for player in team.players:
                teamString += discord.utils.escape_markdown(player.name) + "\n"

        return teamString

    # Prints teams in the discord channel.
    # DO NOT PASS NULL TEAMS.
    # Returns (team1_message, team2_message) so a caller whose roster is
    # actually final (not a captains draft still in progress) can pass
    # them to _finalizeRoster to turn the second one into a live
    # reroll/start control. Callers that don't care (a draft's own
    # in-progress reposts) just discard the return value.
    # Shared by printEmbed's own initial post and _updateDraftEmbeds'
    # later in-place edits, so a picked player disappearing from PLAYERS
    # (or the roster gaining one) always renders identically regardless
    # of which of the two ever built it.
    def _buildTeamEmbeds(self, team1: Team, team2: Team, useRoles=False):
        team1_embed = discord.Embed(
            title=team1.get_name(), description=self.makeEmbedString(team1, useRoles), color=discord.Color.blue()
        )
        team2_embed = discord.Embed(
            title=team2.get_name(), description=self.makeEmbedString(team2, useRoles), color=discord.Color.red()
        )
        return team1_embed, team2_embed

    # None when there's nothing left to show at all (playersTeam wasn't
    # given, or the pool was never non-empty to begin with). A live
    # draft's pool hitting zero mid-picking is handled by the caller
    # instead (see _updateDraftEmbeds), since that's "everyone's been
    # drafted", a real state worth still showing, not "there was never a
    # pool here".
    def _buildPlayersEmbed(self, playersTeam):
        if playersTeam is None or len(playersTeam.get_players()) == 0:
            return None
        return discord.Embed(
            title="PLAYERS", description=self.makeEmbedString(playersTeam), color=discord.Color.purple()
        )

    async def printEmbed(self, ctx, team1: Team, team2: Team, playersTeam=None, useRoles=False):
        team1_embed, team2_embed = self._buildTeamEmbeds(team1, team2, useRoles)

        # ctx.response.send_message can only be called once per
        # interaction, and printEmbed is sometimes called from a place
        # (captainsHelper) where the interaction was already responded to
        # earlier in the flow. channel.send for both embeds here lets the
        # caller decide if/when to do the initial interaction response.
        team1_message = await ctx.channel.send(embed=team1_embed)
        team2_message = await ctx.channel.send(embed=team2_embed)

        players_embed = self._buildPlayersEmbed(playersTeam)
        players_message = await ctx.channel.send(embed=players_embed) if players_embed is not None else None

        return team1_message, team2_message, players_message

    # A live captain draft's own team1/team2/PLAYERS embeds, edited in
    # place for every pick instead of _applyDraftPick reposting all three
    # via printEmbed each time. captainsHelper's own initial printEmbed
    # call is what actually posts them, storing their ids on
    # roster_team1_message_id/roster_team2_message_id (the same fields
    # _finalizeRoster reuses once the draft finishes) and
    # draft_players_message_id. Same "fetch by stored id, rebuild, edit"
    # shape _rerollRoster already uses for the equivalent post-draft
    # Reroll click. draft_players_message_id can be None if the pool was
    # already empty the moment the draft started (nothing was ever posted
    # to edit). Everything else is guaranteed set by the time a pick can
    # happen at all, so no None-guard needed for
    # team1_msg_id/team2_msg_id here.
    async def _updateDraftEmbeds(self, guild_id, channel, team1, team2, players):
        team1_msg_id = self.get(guild_id, "roster_team1_message_id")
        team2_msg_id = self.get(guild_id, "roster_team2_message_id")
        players_msg_id = self.get(guild_id, "draft_players_message_id")

        team1_embed, team2_embed = self._buildTeamEmbeds(team1, team2)

        # Each edit is independent: a transient HTTPException on one
        # message (rate limit, brief API hiccup) must not skip the others
        # or abort the caller before it reaches _finalizeRoster on the
        # last pick. Previously an unhandled exception here (e.g. failing
        # only on the PLAYERS edit) would propagate out and leave that
        # embed stuck showing the just-drafted player forever on the final
        # pick, since nothing else ever re-edits it once the draft is
        # over. Logged (not silently swallowed) since the DB write
        # underneath is always correct regardless: a failure here only
        # ever means the embed on screen fell behind, and that's otherwise
        # invisible, with nothing else surfacing it to the user or to
        # shockwave.log.
        team1_message = await channel.fetch_message(int(team1_msg_id))
        try:
            await team1_message.edit(embed=team1_embed)
        except discord.HTTPException:
            logger.exception("_updateDraftEmbeds: team1 embed edit failed (guild %s)", guild_id)

        team2_message = await channel.fetch_message(int(team2_msg_id))
        try:
            await team2_message.edit(embed=team2_embed)
        except discord.HTTPException:
            logger.exception("_updateDraftEmbeds: team2 embed edit failed (guild %s)", guild_id)

        if players_msg_id is not None:
            players_embed = self._buildPlayersEmbed(players) or discord.Embed(
                title="PLAYERS", description="Everyone has been drafted!", color=discord.Color.purple()
            )
            try:
                players_message = await channel.fetch_message(int(players_msg_id))
                await players_message.edit(embed=players_embed)
            except discord.HTTPException:
                logger.exception("_updateDraftEmbeds: PLAYERS embed edit failed (guild %s)", guild_id)

        return team1_message, team2_message

    # /set (admin-only, manage_guild, see bot.py): a single entry point
    # for every server-tunable knob an admin might want to change (team
    # channels/size, the betting timer, and a direct elo correction), so
    # tweaking one doesn't mean hunting down a handful of different
    # commands. Every given field is validated before ANY of them is
    # applied, the same validate-then-apply-all pattern /card-set and
    # /team set use, so a bad value in one field can't leave another,
    # genuinely valid field half-applied. team1/team2 and member/elo are
    # each pairs (either both given or neither). size and betting_timer
    # each stand alone. wager_channel/matchup_channel are their own
    # dedicated commands/helpers instead (setWagerChannelHelper/
    # setMatchupChannelHelper), not folded in here, since both can be run
    # with no channel given at all (meaning "here"), a shape that doesn't
    # fit this function's shared "was this field even touched" None-check.
    async def adminSetHelper(
        self, ctx, team1, team2, size, betting_timer, member, elo, default_elo,
    ):
        guild = ctx.guild
        guild_id = guild.id

        if all(
            v is None
            for v in (team1, team2, size, betting_timer, member, elo, default_elo)
        ):
            await ctx.response.send_message(
                "Give at least one setting to change: team1+team2, size, betting_timer, "
                "member+elo, or default_elo.",
                ephemeral=True,
            )
            return

        if (team1 is None) != (team2 is None):
            await ctx.response.send_message(
                "Give both team1 and team2 together, or neither.", ephemeral=True
            )
            return

        if (member is None) != (elo is None):
            await ctx.response.send_message(
                "Give both member and elo together, or neither.", ephemeral=True
            )
            return

        if betting_timer is not None:
            if betting_timer <= 0:
                await ctx.response.send_message(
                    "betting_timer must be greater than 0 seconds.", ephemeral=True
                )
                return
            if betting_timer > 600:
                await ctx.response.send_message(
                    "betting_timer can't be more than 600 seconds (10 minutes).", ephemeral=True
                )
                return

        if default_elo is not None and default_elo <= 0:
            await ctx.response.send_message("default_elo must be greater than 0.", ephemeral=True)
            return

        applied = []

        if team1 is not None:
            channel1 = discord.utils.get(guild.channels, name=team1)
            if channel1 is None:
                channel1 = await guild.create_voice_channel(name=team1)
            channel2 = discord.utils.get(guild.channels, name=team2)
            if channel2 is None:
                channel2 = await guild.create_voice_channel(name=team2)
            self.update(guild_id, "channel1", str(team1))
            self.update(guild_id, "channel2", str(team2))
            applied.append(f"team channels to {channel1.mention}/{channel2.mention}")

        if size is not None:
            self.update(guild_id, "team_size", size)
            applied.append(f"team size to **{size}**")

        # For a simultaneous-mode tournament round with several matches
        # open at once, betting_timer is the PER-MATCH base. See
        # _openConcurrentTournamentBetting, which multiplies it by however
        # many matches are in that round (capped so a big base times a big
        # bracket's first round can't leave betting open for absurdly
        # long).
        if betting_timer is not None:
            self.update(guild_id, "betting_timer_seconds", betting_timer)
            applied.append(f"the betting window to **{betting_timer} seconds**")

        # Sets `member`'s elo to an exact value rather than a +/- delta,
        # for correcting a broken rating directly rather than fighting the
        # match-result math to get there. Still runs
        # _checkTierRewardUnlocks afterward, the same as any other path
        # that changes elo (applyGameDeltas, the lazy self-heal in
        # _buildStatsEmbed). An admin manually setting someone to
        # Diamond+ should credit that tier's reward exactly like earning
        # it normally would.
        if member is not None:
            user_id = member.id
            game = self._currentGame(guild_id)
            self.ensureGameStatsRow(guild_id, user_id, member.name, game)
            self.cursor.execute(
                "UPDATE game_stats SET elo=? WHERE guildId=? AND userId=? AND game=?",
                (elo, guild_id, user_id, game)
            )
            self.db.commit()
            self._checkTierRewardUnlocks(guild_id, user_id, elo)
            applied.append(f"{member.mention}'s **{game}** elo to **{elo}**")

        # What a brand new player's elo starts at in this guild, see
        # _defaultEloForGuild. Doesn't touch anyone's existing rating. Use
        # /clear (clear_elo) to reset current players to the new default.
        if default_elo is not None:
            self.update(guild_id, "default_elo", default_elo)
            applied.append(f"the default starting elo to **{default_elo}**")

        if len(applied) == 1:
            summary = applied[0]
        elif len(applied) == 2:
            summary = f"{applied[0]} and {applied[1]}"
        else:
            summary = f"{', '.join(applied[:-1])}, and {applied[-1]}"

        message = f"Updated {summary}."
        if betting_timer is not None:
            message += (
                " For a tournament round with several matches happening at once, that's "
                "multiplied by the number of matches in the round."
            )
        await ctx.response.send_message(message)

    # /set roster-permissions: whether Start/Start (no move)/Random Roles/
    # Balanced Roles stay open to anyone who can see the roster message
    # (the default) or get gated to a rostered player/Manage Server admin,
    # the same _isAdminOrInCurrentGame check the winner-report buttons
    # already use. See _handleRosterStartClick/_handleRosterRerollClick/
    # _handleRosterBalanceRolesClick for where this actually gets enforced.
    async def setRosterPermissionsHelper(self, ctx, strict):
        self.update(ctx.guild.id, "roster_permissions_strict", 1 if strict else 0)
        if strict:
            message = (
                "Roster buttons (Start, Start (no move), Random Roles, Balanced Roles) now require "
                "being a rostered player or having the Manage Server permission."
            )
        else:
            message = "Roster buttons are open to anyone who can see the roster message again."
        await ctx.response.send_message(message)

    # /set max-wager: caps a single /wager team or /wager against bet.
    # Omitting `amount` clears the cap back to unlimited, the same
    # "presence of the param is the signal" shape a single-purpose
    # command can get away with, since there's no separate way to check
    # the current value first.
    async def setMaxWagerHelper(self, ctx, amount):
        if amount is not None and amount <= 0:
            await ctx.response.send_message("amount must be greater than 0.", ephemeral=True)
            return
        self.update(ctx.guild.id, "max_wager", amount)
        if amount is not None:
            message = f"A single wager can now be at most **{amount} gold**."
        else:
            message = "The wager cap has been removed; bets are limited only by balance again."
        await ctx.response.send_message(message)

    # /set betting: a hard on/off switch for the whole wagering layer
    # (/wager team, /wager against), for a server that doesn't want
    # anything gambling-adjacent even with fictional gold. Games, elo, and
    # the winner-report flow all work exactly the same either way; see
    # wagerHelper/challengeDuelHelper for where this actually gets
    # enforced, and _openBetting/reconcileStaleBettingWindows for how the
    # betting-open message and its timer adapt when this is off.
    async def setBettingHelper(self, ctx, enabled):
        self.update(ctx.guild.id, "betting_enabled", 1 if enabled else 0)
        if enabled:
            message = "Betting is enabled. /wager team and /wager against accept bets again."
        else:
            message = "Betting is disabled. /wager team and /wager against will no longer accept bets."
        await ctx.response.send_message(message)

    # Points every future betting posting (open/closed, see _openBetting)
    # at a specific text channel instead of wherever a game or a
    # tournament match happens to run. Independent of /set matchup-channel,
    # which only redirects the matchup graphic/winner-report message; the
    # two can point at different channels.
    async def setWagerChannelHelper(self, ctx, channel_name=None):
        guild = ctx.guild
        if channel_name is None:
            # No channel given: point it at wherever this command was run,
            # same as omitting it entirely being "use right here".
            channel = ctx.channel
        else:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel is None:
                channel = await guild.create_text_channel(channel_name)
        self.update(guild.id, "wager_channel", channel.name)
        await ctx.response.send_message(f"The wager channel is now {channel.mention}.")

    # Points every future matchup graphic and winner-report message
    # (_sendMatchupImage, _openBetting's report half, _postReadyCheck,
    # _postMatchReport) at one specific text channel instead of wherever
    # the roster or tournament match happens to run. Independent of /set
    # wager-channel, which only redirects the betting-open/closed
    # notices; the two can point at different channels.
    async def setMatchupChannelHelper(self, ctx, channel_name=None):
        guild = ctx.guild
        if channel_name is None:
            # No channel given: point it at wherever this command was run,
            # same as omitting it entirely being "use right here".
            channel = ctx.channel
        else:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel is None:
                channel = await guild.create_text_channel(channel_name)
        self.update(guild.id, "matchup_channel", channel.name)
        await ctx.response.send_message(
            f"Matchup graphics and winner-report messages will now go to {channel.mention}."
        )

    # Turns a just-posted, actually-final roster (not a captains draft
    # still mid-pick) into a live control: Random Roles/Balanced Roles to
    # assign or reassign roles (only if the roster is exactly 5v5, see
    # below), Start to move everyone and open betting, and Start (no
    # move) to open betting without moving anyone. Start and Random Roles
    # replace the old standalone /randomize-roles and /start commands
    # respectively. The RosterActionView lives on `team2_message` only
    # (team1's own message stays a plain embed), see RosterActionView's
    # own callbacks for why one message is enough to drive both teams'
    # state.
    # `roster_team1_message_id`/`roster_team2_message_id` on `servers` is
    # what makes a click on an OLD roster message inert once a newer one
    # has been posted: each new call here overwrites them, so a stale
    # message's buttons simply fail the id check and no-op.
    #
    # size_eligible (exactly 5 a side) gates the role BUTTONS themselves,
    # so any 5v5 roster can assign roles after the fact regardless of how
    # it was formed (ranked or not). `use_roles` only controls whether
    # the embeds are already SHOWING role labels the moment this posts
    # (true for /make-teams ranked use_roles:true, false otherwise until
    # Random Roles/Balanced Roles is actually clicked).
    # `intro_messages`, when given, is every "team formation" text message
    # (the "Teams created!"-style reply, plus anything else like it) the
    # caller already posted for this roster. Stored onto
    # make_teams_message_ids so recordResult can delete them all once the
    # game they announced is actually scored, the same cleanup
    # roster_team1_message_id/roster_team2_message_id already get here.
    # Left untouched (None, the default) for the draft flow, which stores
    # its own intro/picker/pool messages onto that column separately
    # (captainsHelper starts it, _applyDraftPick appends to it once the
    # draft actually finishes and calls in here).
    async def _finalizeRoster(self, guild_id, team1_message, team2_message, team1, team2, use_roles, intro_messages=None):
        size_eligible = len(team1.get_players()) == 5 and len(team2.get_players()) == 5
        # Random Roles/Balanced Roles (and so role-labelled embeds/icons
        # entirely) are League-only - see /set game. roles_eligible, not
        # size_eligible alone, is what actually gates both the buttons
        # and whether use_roles is honored at all here.
        roles_eligible = size_eligible and self._gameSupportsRoles(self._activeGame(guild_id))
        roles_shown = use_roles and roles_eligible

        self.update(guild_id, "roster_team1_message_id", team1_message.id)
        self.update(guild_id, "roster_team2_message_id", team2_message.id)
        self.update(guild_id, "roster_channel_id", team2_message.channel.id)
        self.update(guild_id, "roster_use_roles", 1 if roles_shown else 0)
        # A fresh roster is always startable, even if the previous one
        # (now superseded) was mid-Start when this posted.
        self.update(guild_id, "roster_starting", 0)
        if intro_messages is not None:
            self.update(guild_id, "make_teams_message_ids", ",".join(str(m.id) for m in intro_messages))

        await team2_message.edit(view=RosterActionView(self, include_role_buttons=roles_eligible))

    # The voice channel to send everyone back to once the game ends (see
    # moveMembersToOriginalChannel). The old /start command took this from
    # ctx.user.voice.channel, but the ▶️ reaction can be clicked by anyone
    # (not necessarily someone in voice, see the design discussion this
    # feature shipped with). So this scans the roster itself for the first
    # rostered player who's actually sitting in a voice channel right now.
    def _findRosterVoiceChannel(self, guild, team1, team2):
        for player in team1.get_players() + team2.get_players():
            member = discord.utils.get(guild.members, id=player.get_id())
            if member is not None and member.voice is not None and member.voice.channel is not None:
                return member.voice.channel
        return None

    # Shared by _rerollRoster and _applyBalancedRolesToRoster: both mutate
    # team1/team2's role-labelled order and then need the exact same
    # "fetch the roster's two live messages by their stored id, rebuild
    # the embeds with roles showing, edit in place" finish. A no-op if
    # either message id isn't tracked (nothing posted yet, or a
    # stale/cleared roster). Each edit is independent, so one message's
    # transient HTTPException doesn't stop the other from updating.
    async def _editRosterTeamEmbeds(self, guild_id, channel, team1, team2):
        team1_msg_id = self.get(guild_id, "roster_team1_message_id")
        team2_msg_id = self.get(guild_id, "roster_team2_message_id")
        if team1_msg_id is None or team2_msg_id is None:
            return

        team1_embed, team2_embed = self._buildTeamEmbeds(team1, team2, useRoles=True)
        try:
            team1_message = await channel.fetch_message(int(team1_msg_id))
            await team1_message.edit(embed=team1_embed)
        except discord.HTTPException:
            logger.exception("_editRosterTeamEmbeds: team1 embed edit failed (guild %s)", guild_id)
        try:
            team2_message = await channel.fetch_message(int(team2_msg_id))
            await team2_message.edit(embed=team2_embed)
        except discord.HTTPException:
            logger.exception("_editRosterTeamEmbeds: team2 embed edit failed (guild %s)", guild_id)

    # Random Roles' whole implementation. Genuinely shuffles both teams'
    # player order, unlike the old randomRoleHelper this replaces, which
    # computed a shuffled result1/result2 text pair that nothing displayed
    # and never wrote the shuffle back to team1/team2 at all. /make-teams'
    # own embeds silently kept showing the un-shuffled split order no
    # matter how many times /randomize-roles ran. This one persists the
    # shuffle to team1/team2 and edits both live embeds in place, so
    # what's on screen is always what a /start-equivalent click would
    # actually use. Also what turns roles on in the first place for a 5v5
    # roster that was never posted with use_roles to begin with.
    async def _rerollRoster(self, guild_id, channel):
        team1_msg_id = self.get(guild_id, "roster_team1_message_id")
        team2_msg_id = self.get(guild_id, "roster_team2_message_id")
        if team1_msg_id is None or team2_msg_id is None:
            return

        team1 = Team()
        team1.deserializeTeam(self.get(guild_id, "team1"))
        team2 = Team()
        team2.deserializeTeam(self.get(guild_id, "team2"))

        random.shuffle(team1.get_players())
        random.shuffle(team2.get_players())

        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "roster_use_roles", 1)
        # A pure shuffle makes no preference claim about who landed where.
        # So any earlier disliked-role win-elo bonus flag (see
        # ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER) no longer means
        # anything once positions have been reshuffled at random.
        self.update(guild_id, "disliked_role_user_ids", "")

        await self._editRosterTeamEmbeds(guild_id, channel, team1, team2)

    # Balanced Roles' whole implementation: elo+preference role assignment
    # (see _assignRolesForFixedTeams) applied to whichever two teams are
    # already posted, without moving any player between them. Callers
    # (_handleRosterBalanceRolesClick) already checked both teams have
    # exactly 5 players.
    async def _applyBalancedRolesToRoster(self, guild_id, channel, team1, team2):
        default_elo = self._defaultEloForGuild(guild_id)
        game = self._activeGame(guild_id)

        def _withElo(players):
            entries = []
            for player in players:
                self.ensureGameStatsRow(guild_id, player.get_id(), player.get_name(), game)
                elo = self.getGameStat(guild_id, player.get_id(), game, "elo")
                entries.append((player, elo if elo is not None else default_elo))
            return entries

        team1_assigned, team2_assigned = self._assignRolesForFixedTeams(
            guild_id, _withElo(team1.get_players()), _withElo(team2.get_players())
        )

        new_team1 = Team()
        new_team1.name = team1.get_name()
        new_team2 = Team()
        new_team2.name = team2.get_name()
        disliked_role_ids = []
        for target, assigned in ((new_team1, team1_assigned), (new_team2, team2_assigned)):
            for player, _elo, _role, tier, _eff in assigned:
                target.add_player(Player(player.get_id(), player.get_name()))
                if tier == "disliked":
                    disliked_role_ids.append(player.get_id())

        self.update(guild_id, "team1", new_team1.serializeTeam())
        self.update(guild_id, "team2", new_team2.serializeTeam())
        self.update(guild_id, "disliked_role_user_ids", ",".join(str(i) for i in disliked_role_ids))
        self.update(guild_id, "roster_use_roles", 1)

        await self._editRosterTeamEmbeds(guild_id, channel, new_team1, new_team2)

    # Finds (or creates) DEFAULT_TEAM_CHANNEL_NAMES and points this
    # guild's channel1/channel2 at them. This is the self-heal ▶️ falls
    # back to instead of refusing to start a game just because /set
    # channels' team1/team2 were never given.
    async def _ensureDefaultTeamChannels(self, guild):
        name1, name2 = DEFAULT_TEAM_CHANNEL_NAMES

        channel1 = discord.utils.get(guild.channels, name=name1)
        if channel1 is None:
            channel1 = await guild.create_voice_channel(name=name1)

        channel2 = discord.utils.get(guild.channels, name=name2)
        if channel2 is None:
            channel2 = await guild.create_voice_channel(name=name2)

        self.update(guild.id, "channel1", name1)
        self.update(guild.id, "channel2", name2)

        return channel1, channel2

    # ▶️'s whole implementation, everything the old /start command did
    # (movefunc + sendCurrentMatchupImage + startBettingHelper), just
    # working from guild/channel directly instead of an Interaction, since
    # a reaction handler has neither. `move=False` is ⚡'s version of the
    # same thing: posts the matchup image and opens betting exactly the
    # same way, but skips the whole "find where to move everyone" dance.
    # Nobody has to be in a voice channel at all to click it, and there's
    # no "original channel" to send anyone back to once the game ends
    # (moveMembersToOriginalChannel simply no-ops for a game started this
    # way).
    # RosterActionView's Random Roles button callback.
    # Shared by _handleRosterRerollClick/_handleRosterBalanceRolesClick/
    # _handleRosterStartClick. A no-op (returns True) unless the guild has
    # opted into /set roster-permissions strict mode; once it has, only a
    # rostered player or a Manage Server admin can actually use these
    # buttons, the same _isAdminOrInCurrentGame gate the winner-report
    # buttons already enforce unconditionally.
    async def _checkRosterPermission(self, interaction, guild_id):
        if not self.get(guild_id, "roster_permissions_strict"):
            return True
        if self._isAdminOrInCurrentGame(interaction):
            return True
        await interaction.response.send_message(
            "Only a player in this game, or a member with the Manage Server permission, can use the "
            "roster buttons on this server.",
            ephemeral=True,
        )
        return False

    async def _handleRosterRerollClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        stored_message_id = self.get(guild_id, "roster_team2_message_id")
        already_starting = bool(self.get(guild_id, "roster_starting"))
        if stored_message_id is None or int(stored_message_id) != interaction.message.id or already_starting:
            await interaction.response.send_message("This roster is no longer live.", ephemeral=True)
            return
        if not await self._checkRosterPermission(interaction, guild_id):
            return
        if not self._gameSupportsRoles(self._activeGame(guild_id)):
            await interaction.response.send_message(
                "Role-based team balancing is League-only.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self._rerollRoster(guild_id, interaction.channel)

    # RosterActionView's Balanced Roles button callback.
    # include_role_buttons=False already keeps this button off a non-5v5
    # roster's own message (see _finalizeRoster). The explicit size check
    # here is just defense in depth against a mismatched or stale message,
    # the way Start's own voice-channel check works.
    async def _handleRosterBalanceRolesClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        stored_message_id = self.get(guild_id, "roster_team2_message_id")
        already_starting = bool(self.get(guild_id, "roster_starting"))
        if stored_message_id is None or int(stored_message_id) != interaction.message.id or already_starting:
            await interaction.response.send_message("This roster is no longer live.", ephemeral=True)
            return
        if not await self._checkRosterPermission(interaction, guild_id):
            return
        if not self._gameSupportsRoles(self._activeGame(guild_id)):
            await interaction.response.send_message(
                "Role-based team balancing is League-only.", ephemeral=True
            )
            return

        team1 = Team()
        team1.deserializeTeam(self.get(guild_id, "team1") or "")
        team2 = Team()
        team2.deserializeTeam(self.get(guild_id, "team2") or "")
        if len(team1.get_players()) != 5 or len(team2.get_players()) != 5:
            await interaction.response.send_message(
                "Balanced roles need exactly 5 players on each team.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self._applyBalancedRolesToRoster(guild_id, interaction.channel, team1, team2)

    # RosterActionView's Start/Start (no move) button callback, ▶️/⚡'s old
    # reaction-based whole implementation (everything the old /start
    # command did: movefunc + sendCurrentMatchupImage + startBettingHelper),
    # adapted for a persistent shared view. Re-derives which roster (and
    # whether it's still live) from the interaction itself. move=False is
    # the "start without moving anyone" version: same matchup image and
    # betting open, just skipping the whole "find where to move everyone"
    # dance, since nobody has to be in a voice channel at all to click it.
    async def _handleRosterStartClick(self, interaction, move):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        stored_message_id = self.get(guild_id, "roster_team2_message_id")
        already_starting = bool(self.get(guild_id, "roster_starting"))
        if stored_message_id is None or int(stored_message_id) != interaction.message.id or already_starting:
            await interaction.response.send_message("This roster is no longer live.", ephemeral=True)
            return
        if not await self._checkRosterPermission(interaction, guild_id):
            return

        guild = interaction.guild
        channel = interaction.channel

        team1 = Team()
        team1.deserializeTeam(self.get(guild_id, "team1"))
        team2 = Team()
        team2.deserializeTeam(self.get(guild_id, "team2"))

        channel1 = channel2 = original_channel = None
        if move:
            original_channel = self._findRosterVoiceChannel(guild, team1, team2)
            if original_channel is None:
                await interaction.response.send_message(
                    "Nobody from the roster is currently in a voice channel, so there's nowhere to move "
                    "them. Join a voice channel with the group and press Start again, or Start (no "
                    "move) to start without moving anyone."
                )
                return

            channel1name = self.get(guild_id, "channel1")
            channel2name = self.get(guild_id, "channel2")
            channel1 = discord.utils.get(guild.channels, name=channel1name)
            channel2 = discord.utils.get(guild.channels, name=channel2name)
            if channel1 is None or channel2 is None:
                # /set channels' team1/team2 were never given (or the
                # named channels got deleted). Rather than refuse to start
                # the game, fall back to DEFAULT_TEAM_CHANNEL_NAMES,
                # creating them if they don't already exist, and remember
                # them as this guild's own from here on so this only
                # happens once.
                channel1, channel2 = await self._ensureDefaultTeamChannels(guild)

        # BUG-PRONE PATTERN AVOIDED: flip this synchronously, with no
        # `await` between it and the checks above, so a second
        # near-simultaneous Start/Start (no move) click can't also pass
        # those checks and start the game twice. Same reasoning
        # _handleWinnerReportPick's own betting_message_id clear
        # documents. roster_team2_message_id itself stays intact (not
        # cleared) so recordResult's own cleanup can still find team2's
        # roster message by it once the game actually ends.
        # roster_starting is the actual mutex here instead, reset back to
        # 0 the next time _finalizeRoster posts a fresh roster.
        # _handleRosterRerollClick/_handleRosterBalanceRolesClick check it
        # too (not just this handler), since the message id alone staying
        # valid post-Start would otherwise leave those two fully clickable
        # on an already-started game - and recordResult reads team1/team2/
        # disliked_role_user_ids live at result time, not from a Start-time
        # snapshot, so a late reroll/rebalance would silently change who
        # actually gets credited for the game already in progress.
        self.update(guild_id, "roster_starting", 1)

        await interaction.response.defer()

        if move:
            self.update(guild_id, "original_channel", str(original_channel))

            for player in team1.get_players():
                member = discord.utils.get(guild.members, id=player.get_id())
                if member is not None:
                    await member.move_to(channel1)
            for player in team2.get_players():
                member = discord.utils.get(guild.members, id=player.get_id())
                if member is not None:
                    await member.move_to(channel2)

            await channel.send("Moved!")
        else:
            # Overwrite whatever original_channel might already be on
            # record. (captainsHelper captures the drafting caller's voice
            # channel the moment a draft starts, in case everyone's since
            # left voice by the time a click finally comes in. A leftover
            # value from an earlier game is possible too.) The no-move
            # start deliberately moved nobody, so
            # moveMembersToOriginalChannel must no-op for this game once
            # it resolves, the same way it already does for a guild
            # that's never started a game at all.
            self.update(guild_id, "original_channel", "")

        label = self._matchupLabelForMode(self.get(guild_id, "mode"), self._activeGame(guild_id))
        use_roles = bool(self.get(guild_id, "roster_use_roles"))
        await self._sendMatchupImage(channel, team1, team2, label, use_roles=use_roles, guild_id=guild_id)
        # The graphic above already shows both full rosters, so the
        # original "Teams created!"-style text reply (and, for a draft,
        # its picker/pool messages) has nothing left to say. Gone now
        # rather than waiting for the whole game to finish.
        # Deletes both roster embeds, interaction.message (team2's, the
        # one Start lives on) included, so there's no separate need to
        # strip its view afterward the way editing it would.
        await self._deleteMakeTeamsIntroMessages(guild_id)
        await self._openBetting(guild_id, channel)

    async def captainsHelper(self, ctx, captain_1, captain_2, ranked=False, snake=False):
        # Checked before clearTeamsHelper and before building
        # Player(captain_1.id, ...) from either captain, since either
        # would crash with AttributeError on a None captain instead of
        # showing the message below. bot.py's /make-teams draft command
        # happens to reject None captains before calling in here today,
        # which is the only reason this guard isn't hit in practice.
        # Checking first here keeps it meaningful if that ever changes.
        if captain_1 is None or captain_2 is None:
            await ctx.response.send_message("Mention two team captains!", ephemeral=True)
            return
        elif captain_1 == captain_2:
            await ctx.response.send_message("Mention two different people!", ephemeral=True)
            return

        await self.clearTeamsHelper(ctx)  # also resets is_ranked to 0

        captain1 = Player(captain_1.id, captain_1.name)
        captain2 = Player(captain_2.id, captain_2.name)

        self.update(ctx.guild.id, "captain1", captain1.serializePlayer())
        self.update(ctx.guild.id, "captain2", captain2.serializePlayer())
        self.update(ctx.guild.id, "mode", "Ranked Captains" if ranked else "Captains")
        if ranked:
            self.update(ctx.guild.id, "is_ranked", 1)
        self.update(ctx.guild.id, "draft_snake", 1 if snake else 0)
        self.update(ctx.guild.id, "game", self._currentGame(ctx.guild.id))

        original_channel = ctx.user.voice.channel
        self.update(ctx.guild.id, "original_channel", str(original_channel))

        team1 = Team()
        team2 = Team()

        team1.add_player(captain1)
        team2.add_player(captain2)

        team1.name, team2.name = self._rosterTeamNames(ctx.guild.id)

        self.update(ctx.guild.id, "team1", team1.serializeTeam())
        self.update(ctx.guild.id, "team2", team2.serializeTeam())

        players = Team()
        for player in ctx.user.voice.channel.members:
            if player != captain_1 and player != captain_2:
                players.add_player(Player(player.id, player.name))

        self.update(ctx.guild.id, "players", players.serializeTeam())

        message = (
            "Ranked captains selected! Elo will be updated when the winner is reported."
            if ranked else "Captains selected!"
        )
        if snake:
            message += (
                " Snake draft: pick order reverses every 2 picks (1,2,2,1,1,2,...) instead of "
                "alternating every pick."
            )
        message += f"\n{self._gameNote(ctx.guild.id)}"
        await ctx.response.send_message(message)
        intro_message = await ctx.original_response()
        # _applyDraftPick appends the picker/pool messages onto this once
        # the draft actually finishes. Started here rather than left for
        # _finalizeRoster (which only runs at that later point) so this
        # intro reply isn't lost track of in the meantime.
        self.update(ctx.guild.id, "make_teams_message_ids", str(intro_message.id))
        team1_message, team2_message, players_message = await self.printEmbed(ctx, team1, team2, players)
        self.update(ctx.guild.id, "roster_team1_message_id", team1_message.id)
        self.update(ctx.guild.id, "roster_team2_message_id", team2_message.id)
        self.update(ctx.guild.id, "roster_channel_id", team2_message.channel.id)
        self.update(ctx.guild.id, "draft_players_message_id", players_message.id if players_message else None)

        content, view = self._renderDraftPickView(ctx.guild.id)
        picker_message = await ctx.channel.send(content, view=view)
        self.update(ctx.guild.id, "draft_picker_message_id", picker_message.id)

    async def getRandomMember(self, ctx):
        playersSer = self.get(ctx.guild.id, "players")

        # deserializeTeam() mutates the object in place and returns None.
        # Instantiate first, then call deserializeTeam on the instance.
        players = Team()
        players.deserializeTeam(playersSer)

        player_members = players.get_players()
        if not player_members:
            return None

        m = np.array(player_members, dtype=object)
        np.random.shuffle(m)

        member = discord.utils.get(ctx.guild.members, id=m[0].get_id())
        return member

    # Builds the draft picker's (content, view) pair for whatever the
    # guild's draft state currently is: one _DraftPickSlotButton per pool
    # member on the current page, colored for whichever captain's turn it
    # is, Random always present, First/Prev/Next/Last only once the pool
    # is too big for one page. Called fresh on every post or re-render
    # (initial captainsHelper send, every pick, every page click) rather
    # than mutating an existing view in place, the same "always rebuild"
    # discipline SetupRoleSelectionView already uses.
    def _renderDraftPickView(self, guild_id):
        players = Team()
        players.deserializeTeam(self.get(guild_id, "players") or "")
        pool = players.get_players()
        turn = int(self.get(guild_id, "turn") or 1)

        paginated = len(pool) > DRAFT_PICK_MAX_UNPAGINATED
        if paginated:
            total_pages = max(1, -(-len(pool) // DRAFT_PICK_PAGE_SIZE))
            page = min(int(self.get(guild_id, "draft_pick_page") or 0), total_pages - 1)
            pool_page = pool[page * DRAFT_PICK_PAGE_SIZE:(page + 1) * DRAFT_PICK_PAGE_SIZE]
        else:
            pool_page = pool[:DRAFT_PICK_MAX_UNPAGINATED]

        view = CaptainsDraftPickView(self, pool_page, turn, paginated)

        captain = Player()
        captain.deserializePlayer(self.get(guild_id, f"captain{turn}"))

        team1 = Team()
        team1.deserializeTeam(self.get(guild_id, "team1") or "")
        team2 = Team()
        team2.deserializeTeam(self.get(guild_id, "team2") or "")
        label = self._draftPickLabel(guild_id, team1, team2)

        content = f"<@{captain.get_id()}>, pick a player for your team!"
        if label is not None:
            content += f" ({label})"
        return content, view

    # Shared by CaptainsDraftPickView's interaction_check and
    # _DraftPickSlotButton's own. (A DynamicItem reconstructed after a
    # restart isn't necessarily attached to a live View instance, so it
    # needs this check itself rather than relying on the containing
    # View's.) Re-derives whose turn it is from `servers` fresh every
    # time, since there's no per-instance state to trust on a persistent
    # view. Checked against draft_picker_message_id first, the same
    # "an older message's buttons stop working once a newer one takes
    # over" role roster_team2_message_id plays for the roster buttons -
    # without it, a draft abandoned via /clear teams (or superseded by a
    # fresh /make-teams draft/random) left its old picker message fully
    # clickable, and if the same person happened to be captain1 again in
    # the new draft, a click on it would resolve against the NEW draft's
    # pool at the OLD message's stale button position instead of being
    # rejected outright.
    async def _isDraftPickTurn(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return False
        stored_message_id = self.get(guild_id, "draft_picker_message_id")
        if stored_message_id is None or int(stored_message_id) != interaction.message.id:
            await interaction.response.send_message("This draft is no longer active.", ephemeral=True)
            return False
        turn = int(self.get(guild_id, "turn") or 1)
        captain = Player()
        captain.deserializePlayer(self.get(guild_id, f"captain{turn}"))
        if interaction.user.id != captain.get_id():
            await interaction.response.send_message("Not your turn to pick.", ephemeral=True)
            return False
        return True

    # A slot button click: re-resolves `index` (a position on the
    # currently-shown page, never a player id) against the guild's actual
    # current pool/page, then applies the pick exactly like a random pick
    # or (previously) a manual /choose would.
    async def _handleDraftPickSlotClick(self, interaction, index):
        guild_id = interaction.guild_id
        players = Team()
        players.deserializeTeam(self.get(guild_id, "players") or "")
        pool = players.get_players()

        paginated = len(pool) > DRAFT_PICK_MAX_UNPAGINATED
        page = int(self.get(guild_id, "draft_pick_page") or 0) if paginated else 0
        page_size = DRAFT_PICK_PAGE_SIZE if paginated else DRAFT_PICK_MAX_UNPAGINATED
        absolute_index = page * page_size + index

        if absolute_index >= len(pool):
            await interaction.response.send_message("That player is no longer available.", ephemeral=True)
            return

        member = discord.utils.get(interaction.guild.members, id=pool[absolute_index].get_id())
        if member is None:
            await interaction.response.send_message("That player is no longer in the server.", ephemeral=True)
            return

        turn = int(self.get(guild_id, "turn") or 1)
        await self._applyDraftPick(interaction, member, turn)

    async def _handleDraftPickRandomClick(self, interaction):
        member = await self.getRandomMember(interaction)
        if member is None:
            await interaction.response.send_message("There are no players left to choose from!", ephemeral=True)
            return
        turn = int(self.get(interaction.guild_id, "turn") or 1)
        await self._applyDraftPick(interaction, member, turn)

    async def _handleDraftPickPageClick(self, interaction, direction):
        guild_id = interaction.guild_id
        players = Team()
        players.deserializeTeam(self.get(guild_id, "players") or "")
        pool = players.get_players()

        total_pages = max(1, -(-len(pool) // DRAFT_PICK_PAGE_SIZE))
        page = min(int(self.get(guild_id, "draft_pick_page") or 0), total_pages - 1)
        new_page = self._computeNewPage(direction, page, total_pages)

        if new_page == page:
            await interaction.response.defer()
            return

        self.update(guild_id, "draft_pick_page", new_page)
        content, view = self._renderDraftPickView(guild_id)
        await interaction.response.edit_message(content=content, view=view)

    # Whoever picks next: straight alternation (1,2,1,2,...) normally, or
    # the classic 2-side snake pattern (1,2,2,1,1,2,2,1,...) when this
    # draft's draft_snake flag is set, so neither captain always drafts
    # immediately after seeing the other's pick. team1/team2 are the
    # POST-pick rosters (this pick has already been added to one of
    # them), so their combined size minus the two starting captains is
    # the 0-based index of the pick about to happen next.
    # Which captain a given 0-based pick index belongs to, under the
    # (1,2,2,1,1,2,2,1,...) snake pattern: pairs of picks alternate
    # captains, and which captain leads a pair alternates too.
    def _snakeTurnAtIndex(self, pick_index):
        round_index = pick_index // 2
        order = (1, 2) if round_index % 2 == 0 else (2, 1)
        return order[pick_index % 2]

    def _nextDraftTurn(self, guild_id, turn, team1, team2):
        if not self.get(guild_id, "draft_snake"):
            return 2 if turn == 1 else 1

        next_pick_index = len(team1.get_players()) + len(team2.get_players()) - 2
        return self._snakeTurnAtIndex(next_pick_index)

    # "Pick 1 of 2"/"Pick 2 of 2" for the draft-pick prompt: whether the
    # CURRENT pick (team1/team2 already reflect every pick so far, same
    # index math _nextDraftTurn uses) is the first or second half of a
    # same-captain snake pair, or a standalone "Pick 1 of 1" when neither
    # neighboring pick belongs to the same captain. None outside snake
    # drafts, since straight alternation makes every pick trivially solo
    # there.
    def _draftPickLabel(self, guild_id, team1, team2):
        if not self.get(guild_id, "draft_snake"):
            return None

        pick_index = len(team1.get_players()) + len(team2.get_players()) - 2
        current = self._snakeTurnAtIndex(pick_index)
        if pick_index > 0 and self._snakeTurnAtIndex(pick_index - 1) == current:
            return "Pick 2 of 2"
        if self._snakeTurnAtIndex(pick_index + 1) == current:
            return "Pick 1 of 2"
        return "Pick 1 of 1"

    # The actual pick: adds `member` to whichever team `turn` is drafting
    # for, removes them from the pool, and either wraps up the draft
    # (pool empty or both teams hit team_size, see the same regression
    # note the old chooseHelper had about spectators) or flips the turn
    # and re-renders the picker for whoever's up next. Shared by every
    # pick path (slot click, Random), so there's exactly one place this
    # logic lives, now that /choose itself is gone.
    async def _applyDraftPick(self, interaction, member, turn):
        guild_id = interaction.guild_id
        players = Team()
        players.deserializeTeam(self.get(guild_id, "players") or "")
        team1 = Team()
        team1.deserializeTeam(self.get(guild_id, "team1") or "")
        team2 = Team()
        team2.deserializeTeam(self.get(guild_id, "team2") or "")

        player = Player(member.id, member.name)
        team1ids = [p.get_id() for p in team1.get_players()]
        team2ids = [p.get_id() for p in team2.get_players()]
        playersids = [p.get_id() for p in players.get_players()]

        if member.id in team1ids or member.id in team2ids or member.id not in playersids:
            await interaction.response.send_message(
                "Player has already been selected or does not exist in the player list.", ephemeral=True
            )
            return

        if turn == 1:
            team1.add_player(player)
            self.update(guild_id, "team1", team1.serializeTeam())
        else:
            team2.add_player(player)
            self.update(guild_id, "team2", team2.serializeTeam())

        # remove_player() relies on __eq__/identity match. Find the
        # equivalent player object already inside `players` by id rather
        # than trying to remove the freshly-constructed `player`.
        toRemove = next((p for p in players.get_players() if p.get_id() == member.id), None)
        if toRemove is not None:
            players.remove_player(toRemove)
        self.update(guild_id, "players", players.serializeTeam())
        self.update(guild_id, "draft_pick_page", 0)

        # Also wrap up once both teams reach team_size, even if the pool
        # still has people left in it; a voice channel with more people
        # than team_size * 2 is expected to leave spectators undrafted, so
        # waiting on the pool to fully empty would never fire at all.
        team_size = self.get(guild_id, "team_size") or 0
        teams_full = (
            team_size
            and len(team1.get_players()) >= team_size
            and len(team2.get_players()) >= team_size
        )

        if len(players.get_players()) == 0 or teams_full:
            await interaction.response.edit_message(content=f"{member.name} added! Draft complete.", view=None)
            team1_message, team2_message = await self._updateDraftEmbeds(
                guild_id, interaction.channel, team1, team2, players
            )
            await self._finalizeRoster(guild_id, team1_message, team2_message, team1, team2, use_roles=False)
            ready_message = await interaction.channel.send(
                "Both teams are set! Press Start on the roster above to move everyone to the "
                "channels, or Start (no move) to open betting without moving anyone!"
            )
            # Appended onto whatever captainsHelper already started this
            # column with (the "Captains selected!" reply). The picker
            # message (interaction.message, now showing "Draft complete"),
            # the pool embed, and this "Both teams are set!" notice are
            # all just as much "make teams" chatter as that intro reply
            # was: done saying anything useful the moment recordResult
            # scores the game they were for.
            extra_ids = [interaction.message.id, ready_message.id]
            pool_message_id = self.get(guild_id, "draft_players_message_id")
            if pool_message_id is not None:
                extra_ids.append(int(pool_message_id))
            existing = self.get(guild_id, "make_teams_message_ids")
            all_ids = (existing.split(",") if existing else []) + [str(i) for i in extra_ids]
            self.update(guild_id, "make_teams_message_ids", ",".join(all_ids))
            return

        new_turn = self._nextDraftTurn(guild_id, turn, team1, team2)
        self.update(guild_id, "turn", new_turn)
        content, view = self._renderDraftPickView(guild_id)
        await interaction.response.edit_message(
            content=f"{member.name} added to team {turn}!\n\n{content}", view=view
        )
        await self._updateDraftEmbeds(guild_id, interaction.channel, team1, team2, players)

    # Clears all current teams.
    #
    # Every team-formation command (/make-teams random, /make-teams draft,
    # /make-teams saved) and /clear itself all funnel through here. An
    # in-progress game (betting_state OPEN/CLOSED) is cancelled cleanly
    # first (the same refund + move-back + "Game cancelled" notice Cancel
    # Game triggers), so wiping team1/team2/original_channel below can't
    # silently orphan a game still being bet on or played out
    # (getRosterPlayers finding nothing once a winner is later reported,
    # moveMembersToOriginalChannel finding original_channel already
    # blank).
    async def clearTeamsHelper(self, ctx):
        guild_id = ctx.guild.id

        if self.get(guild_id, "betting_state") in ("OPEN", "CLOSED"):
            await self.cancelGameHelper(guild_id, ctx.channel, ctx.guild)

        self.update(guild_id, "original_channel", "")
        self.update(guild_id, "team1", "")
        self.update(guild_id, "team2", "")
        self.update(guild_id, "players", "")
        self.update(guild_id, "mode", "Normal")
        self.update(guild_id, "turn", 1)
        # Goes stale the moment team1/team2 do (see rankedTeamHelper). A
        # fresh roster's own players earned no disliked-role bonus yet.
        self.update(guild_id, "disliked_role_user_ids", "")
        # A prior draft's snake flag has nothing to do with whatever
        # team-formation command runs next. captainsHelper sets this back
        # to 1 itself when the new draft actually wants it.
        self.update(guild_id, "draft_snake", 0)
        # Every team-formation path (/make-teams random, /make-teams
        # draft, either with or without ranked:true) runs through here
        # first. Resetting is_ranked to 0 by default means only the
        # ranked-specific helpers, which explicitly set it back to 1
        # afterward, cause elo to be touched when the winner is
        # eventually reported.
        self.update(guild_id, "is_ranked", 0)
        # Goes stale the moment team1/team2 do, same as disliked_role_user_ids
        # above - whichever team-formation helper runs next re-stamps it
        # from current_game.
        self.update(guild_id, "game", None)

    # `message`, when given, replaces the default "You've been invited..."
    # line entirely rather than being appended alongside it. The invite
    # link and a "Sent by" attribution line (since a custom message might
    # not mention the sender at all) still always follow it. Returns
    # whether the DM actually went through, so /notify can tally
    # successes/failures across a whole role instead of one member's
    # closed DMs silently aborting the rest of the batch.
    async def notifyHelper(self, ctx, member: discord.Member, message: str = None):
        channel = await member.create_dm()
        invite_channel = ctx.user.voice.channel
        invite_link = await invite_channel.create_invite(max_uses=1, unique=True)

        body = message if message is not None else "You've been invited to play in a game!"
        content = (
            f"{body} Join the voice channel here: {invite_link}\n\n"
            f"Sent by {ctx.user.global_name}"
        )
        try:
            await channel.send(content)
        except discord.HTTPException:
            return False
        return True

    # Moves everyone currently in either team channel (+ spectators) back
    # to the channel they started in. Takes a discord.Guild rather than
    # an Interaction so it can run both from cancelGameHelper
    # (CANCEL_GAME_EMOJI) and automatically once a winner is reported
    # (recordResult), neither of which always has a command Interaction
    # to work with. Returns False (and moves nobody) if the server was
    # never started, since there's no "original channel" on record to
    # send anyone back to.
    async def moveMembersToOriginalChannel(self, guild):
        guild_id = guild.id
        og = self.get(guild_id, "original_channel")
        chan1 = self.get(guild_id, "channel1")
        chan2 = self.get(guild_id, "channel2")

        original_channel = discord.utils.get(guild.channels, name=og)
        channel1 = discord.utils.get(guild.channels, name=chan1)
        channel2 = discord.utils.get(guild.channels, name=chan2)

        if original_channel is None:
            return False

        aggregate = []
        if channel1 is not None:
            aggregate.extend(channel1.members)
        if channel2 is not None:
            aggregate.extend(channel2.members)

        for member in aggregate:
            await member.move_to(original_channel)

        return True

    # ---------------- Economy ----------------

    def ensureEconomyRow(self, guild_id, user_id, username):
        self.cursor.execute(
            "INSERT OR IGNORE INTO economy"
            "(guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, gold_lost, "
            "last_daily) "
            "VALUES(?, ?, ?, 0, 0, 0, 0, 0, 0, NULL)",
            (guild_id, user_id, username)
        )
        self.cursor.execute(
            "UPDATE economy SET username=? WHERE guildId=? AND userId=?",
            (username, guild_id, user_id)
        )
        self.db.commit()

    # /set game: which game a server's next-formed roster tracks elo/
    # stats for. NULL (a server that's never run /set game) reads as
    # "League", the game this bot always tracked before per-game stats
    # existed.
    def _currentGame(self, guild_id):
        return self.get(guild_id, "current_game") or "League"

    # The game the CURRENT team1/team2 roster was actually stamped with
    # (see every team-forming helper), for recordResult and friends to
    # know which game_stats row a just-resolved game affects. Falls back
    # to _currentGame for a guild that somehow has a live roster from
    # before this column existed.
    def _activeGame(self, guild_id):
        return self.get(guild_id, "game") or self._currentGame(guild_id)

    # Appended to every team-forming command's own confirmation message
    # (randomizeTeamHelper/rankedTeamHelper/captainsHelper/
    # useTeamsHelper/reuseTeamsHelper), so it's always visible which game
    # a just-formed roster's elo/stats will actually count toward, and how
    # to change it, without needing to check /set game separately. Reads
    # _activeGame rather than _currentGame: every caller but
    # reuseTeamsHelper has already stamped servers.game by the time this
    # runs, and reuseTeamsHelper deliberately never re-stamps it (a reused
    # roster keeps whatever game it originally formed under).
    def _gameNote(self, guild_id):
        return f"\U0001f3ae Playing **{self._activeGame(guild_id)}**. Use `/set game` to switch."

    # Only "League" gets role-based team balancing/role icons - simpler to
    # link this to the game itself than maintain a separate per-game flag.
    # Takes the game string directly (not guild_id) since callers need
    # this at two different points with two different notions of "the
    # game": team-forming code checks it against _currentGame (the roster
    # being formed right now hasn't stamped servers.game yet), while
    # anything about an already-formed roster (the matchup image, the
    # roster buttons) checks it against _activeGame instead.
    def _gameSupportsRoles(self, game):
        return game == "League"

    def ensureGameStatsRow(self, guild_id, user_id, username, game):
        self.cursor.execute(
            "INSERT OR IGNORE INTO game_stats"
            "(guildId, userId, game, username, elo, game_wins, game_losses, ranked_wins, "
            "ranked_losses, current_win_streak) VALUES(?, ?, ?, ?, ?, 0, 0, 0, 0, 0)",
            (guild_id, user_id, game, username, self._defaultEloForGuild(guild_id))
        )
        self.cursor.execute(
            "UPDATE game_stats SET username=? WHERE guildId=? AND userId=? AND game=?",
            (username, guild_id, user_id, game)
        )
        self.db.commit()

    def getGameStat(self, guild_id, user_id, game, column):
        self.cursor.execute(
            f"SELECT {column} FROM game_stats WHERE guildId=? AND userId=? AND game=?",
            (guild_id, user_id, game)
        )
        row = self.cursor.fetchone()
        return row[0] if row is not None else None

    # /set game itself: registers `game` as known for this guild (so it
    # shows up in gameAutocomplete from now on, same as any other saved
    # game) and points current_game at it. Only affects the NEXT roster
    # formed (see servers.game's own comment) - whatever's currently in
    # progress keeps resolving against whichever game it actually started
    # under.
    async def setGameHelper(self, ctx, game):
        game = game.strip()
        if not game:
            await ctx.response.send_message("Give a game name.", ephemeral=True)
            return
        guild_id = ctx.guild.id
        self.cursor.execute(
            "INSERT OR IGNORE INTO guild_games(guildId, game) VALUES(?, ?)", (guild_id, game)
        )
        self.update(guild_id, "current_game", game)
        self.db.commit()
        await ctx.response.send_message(
            f"This server's current game is now **{game}**. New rosters will track its own elo and "
            f"stats from here on."
            + ("" if game == "League" else " Role-based team balancing is League-only, so it's off for this game.")
        )

    # /current-game: a plain, read-only way to check what /set game last
    # configured, without needing to run an admin-only command or dig a
    # roster's own _gameNote out of chat history. Also flags it when an
    # in-progress roster (_activeGame) is still tracking a different game
    # than current_game - possible since /set game only ever applies to
    # the NEXT roster formed, so switching mid-game doesn't retroactively
    # change which game the one already running affects (see
    # _activeGame's own comment).
    async def currentGameHelper(self, ctx):
        guild_id = ctx.guild.id
        game = self._currentGame(guild_id)
        active = self._activeGame(guild_id)

        message = f"This server's current game is **{game}**."
        if active != game:
            message += (
                f" The roster currently in progress is still tracking **{active}** though - "
                "`/set game` only affects the next roster formed."
            )
        await ctx.response.send_message(message)

    # Every game this guild has ever set itself to via /set game (always
    # includes "League", seeded per-guild - see guild_games), for
    # gameAutocomplete in bot.py. Typing something not in this list is
    # still accepted outright; this is a convenience list, not a
    # restriction.
    def listKnownGames(self, guild_id):
        self.cursor.execute("SELECT game FROM guild_games WHERE guildId=? ORDER BY game", (guild_id,))
        return [row[0] for row in self.cursor.fetchall()]

    # Wipes every player's currency stats (balance, wins/losses,
    # wagered/won/lost gold, daily-claim cooldown) for a guild, or just one
    # player if `user_id` is given (the same optional narrowing
    # resetAchievementsHelper/resetCardUnlocksHelper below already offer
    # for /clear achievements/card-unlocks' own `user` param). Rows get
    # recreated with fresh defaults the next time each player touches the
    # economy (daily, wager, balance) via ensureEconomyRow.
    def resetEconomyHelper(self, guild_id, user_id=None):
        if user_id is None:
            self.cursor.execute("DELETE FROM economy WHERE guildId=?", (guild_id,))
            # Every game's elo/record, not just the current one - a full
            # economy wipe means everything, unlike resetEloHelper's own
            # current-game-only reset.
            self.cursor.execute("DELETE FROM game_stats WHERE guildId=?", (guild_id,))
        else:
            self.cursor.execute("DELETE FROM economy WHERE guildId=? AND userId=?", (guild_id, user_id))
            self.cursor.execute("DELETE FROM game_stats WHERE guildId=? AND userId=?", (guild_id, user_id))
        self.db.commit()

    # Resets every existing player's elo back to this guild's configured
    # default (see _defaultEloForGuild), leaving balance/wins/losses/gold
    # untouched, unlike resetEconomyHelper, which wipes the whole row. Or
    # just one player's elo if `user_id` is given, same narrowing as
    # resetEconomyHelper above. Resets the CURRENT game's elo only (see
    # /set game) - a server running several games shouldn't have resetting
    # League's ladder also wipe Valorant's.
    def resetEloHelper(self, guild_id, user_id=None):
        if user_id is None:
            self.cursor.execute(
                "UPDATE game_stats SET elo=? WHERE guildId=? AND game=?",
                (self._defaultEloForGuild(guild_id), guild_id, self._currentGame(guild_id))
            )
        else:
            self.cursor.execute(
                "UPDATE game_stats SET elo=? WHERE guildId=? AND userId=? AND game=?",
                (self._defaultEloForGuild(guild_id), guild_id, user_id, self._currentGame(guild_id))
            )
        self.db.commit()

    # Resets EARNED ACHIEVEMENTS for a guild. Deletes only the
    # card_unlocks rows whose itemKey is a CARD_ACHIEVEMENT_TITLES key,
    # leaving every other unlock (tier rewards, special grants, shop
    # purchases) and the underlying economy stats those achievements were
    # computed from (game_wins, current_win_streak, etc.) untouched.
    # `user_id=None` (the default) resets every player in the guild, the
    # /clear counterpart to resetEconomyHelper/resetEloHelper above. A
    # real `user_id` narrows it to just that one player instead, for
    # /clear achievements' and /clear card-unlocks' own optional `user`
    # parameter. Unlike resetCardUnlocksHelper below (which wipes
    # EVERYTHING a player's unlocked), both modes here are
    # achievements-only.
    def resetAchievementsHelper(self, guild_id, user_id=None):
        achievement_keys = list(CARD_ACHIEVEMENT_TITLES.keys())
        placeholders = ",".join("?" for _ in achievement_keys)
        if user_id is None:
            self.cursor.execute(
                f"DELETE FROM card_unlocks WHERE guildId=? AND itemType='title' AND itemKey IN ({placeholders})",
                (guild_id, *achievement_keys)
            )
        else:
            self.cursor.execute(
                "DELETE FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='title' "
                f"AND itemKey IN ({placeholders})",
                (guild_id, user_id, *achievement_keys)
            )
        self.db.commit()

    # Resets EVERY trading-card unlock (title, color scheme, font,
    # however earned: tier reward, special grant, or shop purchase) for a
    # guild, and resets the equipped `trading_cards` row back to
    # Shockwave's own defaults so it isn't left pointed at something no
    # longer unlocked. The /clear counterpart to resetAchievementsHelper
    # above, just for the whole unlock table instead of achievements
    # alone. `user_id=None` (the default) resets every player in the
    # guild. A real `user_id` narrows it to just that one player,
    # replacing the old standalone /card-clear-unlocks admin command this
    # folded into /clear.
    def resetCardUnlocksHelper(self, guild_id, user_id=None):
        if user_id is None:
            self.cursor.execute("DELETE FROM card_unlocks WHERE guildId=?", (guild_id,))
            self.cursor.execute(
                "UPDATE trading_cards SET title=?, accent_color=?, background_color=?, text_color=?, "
                "font_style=?, customized=0, color_scheme_name=NULL WHERE guildId=?",
                (
                    CARD_DEFAULT_TITLE, CARD_DEFAULT_ACCENT_COLOR, CARD_DEFAULT_BACKGROUND_COLOR,
                    CARD_DEFAULT_TEXT_COLOR, CARD_DEFAULT_FONT_STYLE, guild_id,
                )
            )
        else:
            self.cursor.execute(
                "DELETE FROM card_unlocks WHERE guildId=? AND userId=?", (guild_id, user_id)
            )
            self.cursor.execute(
                "UPDATE trading_cards SET title=?, accent_color=?, background_color=?, text_color=?, "
                "font_style=?, customized=0, color_scheme_name=NULL WHERE guildId=? AND userId=?",
                (
                    CARD_DEFAULT_TITLE, CARD_DEFAULT_ACCENT_COLOR, CARD_DEFAULT_BACKGROUND_COLOR,
                    CARD_DEFAULT_TEXT_COLOR, CARD_DEFAULT_FONT_STYLE, guild_id, user_id,
                )
            )
        self.db.commit()

    # Posts the confirm/cancel view for /clear elo, /clear economy,
    # /clear achievements, and /clear card-unlocks. None of them actually
    # touch player data until the invoker clicks "Confirm reset" on the
    # message this sends. clear_economy takes priority over clear_elo
    # when both are set (the whole-row wipe already resets elo too, so
    # there's nothing left for clear_elo to do). clear_achievements and
    # clear_card_unlocks are independent of both and of each other, and
    # can combine with any of the others. `target` (None, or a
    # discord.Member) narrows clear_achievements/clear_card_unlocks to
    # just that one player. clear_elo/clear_economy always stay
    # whole-server regardless, so a combined run mixes "for every player"
    # and "for @member" sentences rather than trying to force everything
    # to one shared scope.
    # /clear teams/channels/tournament's own confirmation prompt
    # (ConfirmClearActionView). `action` picks which of the three it is,
    # see that view's own Confirm handler for what each one actually
    # does.
    async def confirmClearActionHelper(self, ctx, action, warning):
        view = ConfirmClearActionView(self, ctx.guild.id, ctx.user.id, action)
        await ctx.response.send_message(warning, view=view)
        view.message = await ctx.original_response()

    async def confirmDestructiveClearHelper(
        self, ctx, clear_economy, clear_elo, clear_achievements, clear_card_unlocks=False, target=None
    ):
        # Acks within Discord's 3-second window. The actual warning+view
        # below is what the user sees, sent as a followup instead.
        await ctx.response.defer()
        warnings = []
        if clear_economy:
            if target is not None:
                warnings.append(
                    f"This will **wipe the entire economy** (balance, elo, game record, "
                    f"betting record, gold wagered/won/lost) for {target.mention}."
                )
            else:
                warnings.append(
                    "This will **wipe the entire economy** (balance, elo, game record, "
                    "betting record, gold wagered/won/lost) for **every player** in "
                    f"**{ctx.guild.name}**."
                )
        elif clear_elo:
            reset_elo = self._defaultEloForGuild(ctx.guild.id)
            if target is not None:
                warnings.append(f"This will **reset elo back to {reset_elo}** for {target.mention}.")
            else:
                warnings.append(
                    f"This will **reset elo back to {reset_elo}** for **every player** "
                    f"in **{ctx.guild.name}**."
                )
        if clear_achievements:
            if target is not None:
                warnings.append(
                    f"This will **reset every earned achievement** for {target.mention}."
                )
            else:
                warnings.append(
                    f"This will **reset every earned achievement** for **every player** "
                    f"in **{ctx.guild.name}**."
                )
        if clear_card_unlocks:
            if target is not None:
                warnings.append(
                    f"This will **wipe every trading-card unlock** for {target.mention} and reset "
                    "their card to Shockwave's defaults."
                )
            else:
                warnings.append(
                    f"This will **wipe every trading-card unlock** for **every player** in "
                    f"**{ctx.guild.name}** and reset their cards to Shockwave's defaults."
                )
        warnings.append(
            "This will also clear the current teams/draft; any in-progress game will be cancelled "
            "(refunded) first."
        )
        warning = " ".join(warnings) + " This can't be undone."
        view = ConfirmResetView(
            self, ctx.guild.id, ctx.guild.name, ctx.user.id,
            clear_economy, clear_elo, clear_achievements, clear_card_unlocks, target,
        )
        view.message = await ctx.followup.send(warning, view=view)

    # ---------------- Tournaments ----------------

    # Writes `tournament` to the guild's row in `tournaments`, replacing
    # whatever tournament (if any) was there before. A server only ever
    # has one. `teams`/`bracket` are JSON since they're variable-length
    # nested data. Everything else is a direct column, one per Tournament
    # attribute.
    def saveTournament(self, guild_id, tournament):
        losers_nodes = tournament.get_losers_bracket_nodes()
        losers_payload = {
            "nodes": serialize_bracket(losers_nodes),
            "rounds": serialize_losers_rounds(tournament.get_losers_rounds(), losers_nodes),
            "wb_dependency": tournament.get_losers_bracket_wb_dependency(),
            "timing": tournament.get_losers_bracket_timing(),
        }
        self.cursor.execute(
            "INSERT OR REPLACE INTO tournaments"
            "(guildId, name, team_size, num_teams, double_elimination, teams, bracket, losers_bracket) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                tournament.get_name(),
                tournament.get_team_size(),
                tournament.get_num_teams(),
                int(tournament.is_double_elimination()),
                json.dumps([team.serializeTeam() for team in tournament.get_teams()]),
                # drop_targets=losers_nodes resolves each winners-bracket
                # result node's `drop_to` (a losers-bracket leaf) against
                # the losers bracket's own index space.
                json.dumps(serialize_bracket(tournament.get_bracket(), drop_targets=losers_nodes)),
                json.dumps(losers_payload),
            )
        )
        self.db.commit()

    # Returns the guild's Tournament, or None if it's never created one.
    def getTournament(self, guild_id):
        self.cursor.execute(
            "SELECT name, team_size, num_teams, double_elimination, teams, bracket, losers_bracket "
            "FROM tournaments WHERE guildId=?",
            (guild_id,)
        )
        row = self.cursor.fetchone()
        if row is None:
            return None

        name, team_size, num_teams, double_elimination, teams_json, bracket_json, losers_json = row
        tournament = Tournament(name, team_size, num_teams, bool(double_elimination))
        for serialized in json.loads(teams_json):
            team = Team()
            team.deserializeTeam(serialized)
            tournament.get_teams().append(team)

        # Losers bracket has to be deserialized BEFORE the winners
        # bracket, since a winners-bracket node's `drop_to` resolves
        # against the losers bracket's node list.
        losers_nodes, losers_rounds, wb_dependency = [], [], []
        # "after_winners" here covers both a single-elimination tournament
        # (the setting is meaningless without a losers bracket at all) and
        # a double-elimination one saved before this feature existed.
        # Both should keep the original "losers bracket waits for the
        # whole winners bracket" behavior.
        timing = "after_winners"
        if losers_json:
            losers_data = json.loads(losers_json)
            losers_nodes = deserialize_bracket(losers_data.get("nodes", []))
            losers_rounds = deserialize_losers_rounds(losers_data.get("rounds", []), losers_nodes)
            wb_dependency = losers_data.get("wb_dependency", [])
            timing = losers_data.get("timing", "after_winners")
        tournament.set_losers_bracket(losers_nodes, losers_rounds, wb_dependency)
        tournament.set_losers_bracket_timing(timing)

        tournament.set_bracket(deserialize_bracket(json.loads(bracket_json), drop_targets=losers_nodes))
        return tournament

    # Creates a new (empty, no teams registered yet) tournament for the
    # guild. Only one tournament can exist per server at a time, so if one
    # is already there this doesn't overwrite it immediately. It posts a
    # confirm/cancel view and waits for the invoker to confirm the
    # replacement instead.
    async def createTournamentHelper(self, ctx, name, team_size, num_teams, double_elimination):
        guild_id = ctx.guild.id

        if team_size <= 0:
            await ctx.response.send_message("Team size must be greater than 0.", ephemeral=True)
            return

        if num_teams <= 1:
            await ctx.response.send_message("A tournament needs at least 2 teams.", ephemeral=True)
            return

        existing = self.getTournament(guild_id)
        if existing is not None:
            if not ctx.user.guild_permissions.manage_guild:
                await ctx.response.send_message(
                    "Only a member with the Manage Server permission can overwrite an existing tournament.",
                    ephemeral=True,
                )
                return

            view = ConfirmTournamentOverwriteView(
                self, guild_id, ctx.user.id, name, team_size, num_teams, double_elimination
            )
            await ctx.response.send_message(
                f"Tournament **{existing.get_name()}** is already set up for this server. "
                f"Creating **{name}** will overwrite it. Are you sure?",
                view=view
            )
            view.message = await ctx.original_response()
            return

        tournament = Tournament(name, team_size, num_teams, double_elimination)
        self.saveTournament(guild_id, tournament)

        elim_style = "double" if double_elimination else "single"
        await ctx.response.send_message(
            f"Tournament **{name}** created! {num_teams} teams of {team_size}, {elim_style} elimination."
        )

    # Registers `team_name`'s team for this guild's tournament. The
    # captain-only, correct-size, not-already-registered, bracket-not-full
    # checks all happen here. register_team on Tournament itself only
    # enforces the "no shared players across registered teams" rule.
    async def registerTeamHelper(self, ctx, team_name):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server. Use /tournament create first.", ephemeral=True
            )
            return

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        _, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "register it for the tournament.",
                ephemeral=True,
            )
            return

        if team.get_size() != tournament.get_team_size():
            await ctx.response.send_message(
                f"**{team_name}** has {team.get_size()} player(s), but this tournament needs teams of "
                f"exactly {tournament.get_team_size()}.",
                ephemeral=True,
            )
            return

        if any(existing.get_id() == team.get_id() for existing in tournament.get_teams()):
            await ctx.response.send_message(
                f"**{team_name}** is already registered for this tournament.", ephemeral=True
            )
            return

        if len(tournament.get_teams()) >= tournament.get_num_teams():
            await ctx.response.send_message("This tournament's bracket is already full.", ephemeral=True)
            return

        try:
            tournament.register_team(team)
        except ValueError as error:
            await ctx.response.send_message(str(error), ephemeral=True)
            return

        self.saveTournament(guild_id, tournament)
        await ctx.response.send_message(
            f"**{team_name}** registered for **{tournament.get_name()}**! "
            f"({len(tournament.get_teams())}/{tournament.get_num_teams()} teams)"
        )

    # The reverse of registerTeamHelper: same captain-or-admin gate, but
    # refuses once a bracket exists (get_bracket() is non-empty), since a
    # registered team's roster is what buildBracket actually seeded into
    # the tree - unregistering after that would leave the bracket
    # referencing a team the tournament no longer considers entered.
    # /tournament create-bracket (its own Confirm-gated reroll once real
    # match history exists) is the way to change the lineup past that
    # point, not this. No confirmation needed here: a registration entry
    # before a bracket exists is trivially reversible by registering
    # again, the same "only gate what's actually destructive" reasoning
    # createBracketHelper/createTournamentHelper already apply elsewhere.
    async def unregisterTeamHelper(self, ctx, team_name):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server. Use /tournament create first.", ephemeral=True
            )
            return

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        _, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "unregister it from the tournament.",
                ephemeral=True,
            )
            return

        if tournament.get_bracket():
            await ctx.response.send_message(
                f"**{tournament.get_name()}**'s bracket has already been built; teams can't be "
                "unregistered anymore. Use /tournament create-bracket to reroll it if the lineup "
                "really needs to change.",
                ephemeral=True,
            )
            return

        if not tournament.unregister_team(team.get_id()):
            await ctx.response.send_message(
                f"**{team_name}** isn't registered for this tournament.", ephemeral=True
            )
            return

        self.saveTournament(guild_id, tournament)
        await ctx.response.send_message(
            f"**{team_name}** unregistered from **{tournament.get_name()}**. "
            f"({len(tournament.get_teams())}/{tournament.get_num_teams()} teams)"
        )

    def _nextPowerOfTwo(self, n):
        power = 1
        while power < n:
            power *= 2
        return power

    # Builds a fresh single-elimination bracket tree from `teams`
    # (shuffled for random seeding). Paired nodes share a `next` (the
    # empty node their winner advances into, same as a real bracket), and
    # that node's `previous` is one of the pair (`previous.opponent`
    # gives the other). Slots beyond len(teams), if the count isn't a
    # power of two, are byes (team=None). Returns the flat list of every
    # node across every round. Doesn't touch the database.
    def buildBracket(self, teams):
        shuffled = list(teams)
        random.shuffle(shuffled)

        size = self._nextPowerOfTwo(len(shuffled))
        num_byes = size - len(shuffled)

        # Spreads one bye per pair rather than placing every real team
        # first and every bye at the tail (team[i] if i < len(team) else
        # None). That naive layout could seat two byes in the same
        # first-round pair whenever num_byes was even: a "BYE vs BYE"
        # match that never has a winner to report, silently orphaning
        # that slot for the rest of the bracket. num_byes is always <
        # size // 2 (size is the smallest power of two >= len(teams)), so
        # there are always at least as many pairs as byes, guaranteeing
        # every bye is paired against a real team.
        team_iter = iter(shuffled)
        slots = []
        for pair_index in range(size // 2):
            slots.append(next(team_iter))
            slots.append(None if pair_index < num_byes else next(team_iter))

        current_round = [BracketNode(team) for team in slots]
        all_nodes = list(current_round)

        while len(current_round) > 1:
            next_round = []
            for i in range(0, len(current_round), 2):
                node_a = current_round[i]
                node_b = current_round[i + 1]
                parent = BracketNode()
                node_a.opponent = node_b
                node_b.opponent = node_a
                node_a.next = parent
                node_b.next = parent
                parent.previous = node_a
                next_round.append(parent)
            all_nodes.extend(next_round)
            current_round = next_round

        return all_nodes

    # Builds the losers bracket that pairs with a winners bracket built by
    # buildBracket(wb_nodes) above, for a double-elimination tournament.
    # Wires each winners-bracket result node's `drop_to` to the
    # losers-bracket leaf that should receive its loser once that match
    # resolves (see _resolveTournamentMatch). This is the only thing that
    # links the two trees together. Everything else about a
    # losers-bracket match plays out through the exact same round
    # machinery as the winners bracket. Returns (flat node list, rounds).
    # rounds groups each round's RESULT nodes explicitly, since (unlike
    # the winners bracket) losers-bracket round sizes don't follow a
    # simple halving pattern and can't be recovered from the graph alone.
    #
    # Losers-bracket rounds alternate in a fixed, well-known pattern for a
    # k-round winners bracket (k = log2(bracket size)):
    #   round 1            : winners round 1's losers, paired against each other
    #   round r, r odd > 1  : last round's survivors, paired against each other
    #   round r, r even     : last round's survivors, each paired against a
    #                         fresh loser dropping in from winners round (r//2 + 1)
    # ending after round (2k - 2) with exactly one survivor: the
    # losers-bracket champion. (k <= 1 is a degenerate case: with only one
    # winners-bracket match total, its loser has nobody left to play, so
    # they become the losers-bracket "champion" with no match at all.)
    # Returns (all_nodes, rounds, wb_dependency). `wb_dependency[i]` is
    # the WINNERS-bracket round_index whose losers this losers round
    # NEEDS to have dropped in before it can start (i.e. that winners
    # round must be fully RESOLVED first), or None if this round only
    # depends on the previous losers round finishing (no NEW
    # winners-bracket input). Derived from exactly which wb_rounds index
    # gets `drop_to` wired to it below: `drop_to` set on wb_rounds[Y]
    # means "the match at winners round_index Y-1 feeds this", since Y is
    # the round the LOSING match's winner (not loser) populates. The
    # loser goes to drop_to instead. See "Interleaved losers bracket
    # scheduling" in readme.md for how this gets used
    # (_readyUnstartedLosersRoundIndex, _advanceInterleavedTournament).
    def buildLosersBracket(self, wb_nodes):
        wb_rounds = self._bracketRounds(wb_nodes)
        k = len(wb_rounds) - 1
        if k < 1:
            return [], [], []

        if k == 1:
            champion = BracketNode()
            wb_rounds[1][0].drop_to = champion
            return [champion], [[champion]], [0]

        all_nodes = []
        rounds = []
        wb_dependency = []
        survivors = None

        for r in range(1, 2 * k - 1):
            if r == 1:
                sources = wb_rounds[1]
                round_results = []
                for i in range(0, len(sources), 2):
                    leaf_a, leaf_b = BracketNode(), BracketNode()
                    leaf_a.opponent = leaf_b
                    leaf_b.opponent = leaf_a
                    parent = BracketNode()
                    leaf_a.next = parent
                    leaf_b.next = parent
                    parent.previous = leaf_a
                    sources[i].drop_to = leaf_a
                    sources[i + 1].drop_to = leaf_b
                    all_nodes.extend([leaf_a, leaf_b, parent])
                    round_results.append(parent)
                survivors = round_results
                wb_dep = 0
            elif r % 2 == 1:
                round_results = []
                for i in range(0, len(survivors), 2):
                    a, b = survivors[i], survivors[i + 1]
                    a.opponent = b
                    b.opponent = a
                    parent = BracketNode()
                    a.next = parent
                    b.next = parent
                    parent.previous = a
                    all_nodes.append(parent)
                    round_results.append(parent)
                survivors = round_results
                wb_dep = None
            else:
                wb_sources = wb_rounds[r // 2 + 1]
                round_results = []
                for i, survivor in enumerate(survivors):
                    fresh_leaf = BracketNode()
                    survivor.opponent = fresh_leaf
                    fresh_leaf.opponent = survivor
                    parent = BracketNode()
                    survivor.next = parent
                    fresh_leaf.next = parent
                    parent.previous = survivor
                    wb_sources[i].drop_to = fresh_leaf
                    all_nodes.extend([fresh_leaf, parent])
                    round_results.append(parent)
                survivors = round_results
                wb_dep = r // 2

            rounds.append(round_results)
            wb_dependency.append(wb_dep)

        return all_nodes, rounds, wb_dependency

    # Builds (or rebuilds, calling this again is an explicit reroll) the
    # tournament's bracket from whichever teams are currently registered.
    # Double elimination also builds a real losers bracket
    # (buildLosersBracket) wired to this winners bracket via each result
    # node's `drop_to`. Wipes this guild's match history before a fresh
    # bracket replaces it (used by both createBracketHelper and /test).
    # `tournament_matches.id` is AUTOINCREMENT specifically so a match id
    # is never reused while ANY guild might still reference it.
    # _settleMatchWagers and the concurrent-betting-close timer both key
    # off matchId alone, with no guildId in the WHERE clause, so a reused
    # id could settle or close out a completely different guild's
    # still-live match. That guarantee is only worth breaking when it's
    # free: if this delete just left the table completely empty (no other
    # guild has a live match either), the id sequence can restart at 1
    # with no collision risk at all. That's also the one case a human
    # actually notices and wants, since a single test server watching
    # /test expects a fresh bracket to start back at "Match #1" instead
    # of climbing forever.
    def _clearTournamentMatchesForGuild(self, guild_id):
        self.cursor.execute("DELETE FROM tournament_matches WHERE guildId=?", (guild_id,))
        self.cursor.execute("SELECT COUNT(*) FROM tournament_matches")
        if self.cursor.fetchone()[0] == 0:
            # sqlite_sequence only exists once some AUTOINCREMENT table
            # somewhere in the DB has had its first insert, nothing to
            # reset yet on a brand new database.
            self.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            )
            if self.cursor.fetchone() is not None:
                self.cursor.execute("DELETE FROM sqlite_sequence WHERE name='tournament_matches'")
        self.db.commit()

    # Deletes this guild's tournament entirely (see /tournament create),
    # /clear tournament's own action. Leaves the persistent `teams` rows
    # themselves untouched, since those exist independently of any one
    # tournament and can just be registered into a new one. This only
    # clears the tournament shell/bracket/registration state and its
    # match history (_clearTournamentMatchesForGuild), same as starting
    # over with a fresh /tournament create.
    def deleteTournamentHelper(self, guild_id):
        self.cursor.execute("DELETE FROM tournaments WHERE guildId=?", (guild_id,))
        self.db.commit()
        self._clearTournamentMatchesForGuild(guild_id)

    def _bracketTimingNote(self, double_elimination, losers_bracket_timing):
        if not double_elimination:
            return ""
        return (
            " Losers bracket starts once the winners bracket finishes."
            if losers_bracket_timing == "after_winners" else
            " Losers bracket rounds are interleaved with the winners bracket as they're unlocked."
        )

    # The actual bracket build/save behind /tournament create-bracket, with
    # no messaging of its own - shared by createBracketHelper's direct path
    # (nothing to lose yet) and ConfirmBracketOverwriteView's Confirm button
    # (rebuilding over real match history), which announce the result two
    # different ways (a fresh response vs. editing the confirmation prompt
    # in place) and so can't share that part.
    def _rebuildBracket(self, guild_id, tournament, teams, double_elimination, losers_bracket_timing):
        tournament.set_double_elimination(double_elimination)
        wb_nodes = self.buildBracket(teams)
        tournament.set_bracket(wb_nodes)
        if double_elimination:
            lb_nodes, lb_rounds, lb_wb_dependency = self.buildLosersBracket(wb_nodes)
            tournament.set_losers_bracket(lb_nodes, lb_rounds, lb_wb_dependency)
            tournament.set_losers_bracket_timing(losers_bracket_timing)
        else:
            tournament.set_losers_bracket([], [])

        # Building a bracket (even a reroll of an existing one) starts a
        # completely fresh tournament run. Clear out any match rows left
        # over from a previous run. Without this, a finished
        # double-elimination tournament's resolved grand-finals row would
        # still be sitting there under this guildId, and the next
        # tournament's own completion check (which looks up the most
        # recent resolved finals match) could mistake it for having
        # already finished too.
        self._clearTournamentMatchesForGuild(guild_id)
        self.saveTournament(guild_id, tournament)

    async def createBracketHelper(self, ctx, double_elimination, losers_bracket_timing="after_winners"):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server. Use /tournament create first.", ephemeral=True
            )
            return

        teams = tournament.get_teams()
        if len(teams) < 2:
            await ctx.response.send_message(
                "Need at least 2 registered teams to build a bracket.", ephemeral=True
            )
            return

        # Only actually destructive once there's real match history to
        # lose (see ConfirmBracketOverwriteView's own comment) - a bracket
        # built or rerolled before /tournament start has ever run needs
        # neither Manage Server nor a confirmation.
        self.cursor.execute("SELECT COUNT(*) FROM tournament_matches WHERE guildId=?", (guild_id,))
        has_match_history = self.cursor.fetchone()[0] > 0
        if has_match_history:
            if not ctx.user.guild_permissions.manage_guild:
                await ctx.response.send_message(
                    "This tournament already has match history from a previous bracket. Only a member "
                    "with the Manage Server permission can rebuild it.",
                    ephemeral=True,
                )
                return

            view = ConfirmBracketOverwriteView(
                self, guild_id, ctx.user.id, double_elimination, losers_bracket_timing
            )
            await ctx.response.send_message(
                f"**{tournament.get_name()}** already has match history from a previous bracket. "
                "Rebuilding will erase it (results, and any bets that were never settled). Are you sure?",
                view=view
            )
            view.message = await ctx.original_response()
            return

        self._rebuildBracket(guild_id, tournament, teams, double_elimination, losers_bracket_timing)
        elim_style = "double" if double_elimination else "single"
        timing_note = self._bracketTimingNote(double_elimination, losers_bracket_timing)
        await ctx.response.send_message(
            f"Bracket created for **{tournament.get_name()}** - {len(teams)} teams, "
            f"{elim_style} elimination.{timing_note}"
        )
        await self._sendBracketText(ctx.channel, tournament, guild_id)

    # ---------------- Tournament matches (/tournament start) ----------------

    # Splits a flat bracket node list (leaves first, as buildBracket
    # returns it) back into per-round lists. Round sizes are always size,
    # size/2, ..., 1 for a size-leaf bracket, and len(nodes) == 2*size -
    # 1, so the leaf count is recoverable from the total without storing
    # it separately.
    def _bracketRounds(self, nodes):
        if not nodes:
            return []
        round_size = (len(nodes) + 1) // 2
        rounds = []
        start = 0
        while round_size >= 1:
            rounds.append(nodes[start:start + round_size])
            start += round_size
            round_size //= 2
        return rounds

    # Shockwave's own logo mark, resized once to BRACKET_LOGO_HEIGHT tall
    # and cached at module scope, see _bracket_logo_cache. None if the
    # asset couldn't be loaded (e.g. a self-hosted deploy that dropped
    # assets/logo-mark.png). Every caller treats that as "skip the logo"
    # rather than letting a missing file take bracket rendering down
    # with it.
    def _loadBracketLogo(self):
        global _bracket_logo_cache
        if _bracket_logo_cache is None:
            try:
                logo = Image.open(BRACKET_LOGO_PATH).convert("RGBA")
                target_width = round(BRACKET_LOGO_HEIGHT * logo.width / logo.height)
                _bracket_logo_cache = logo.resize((target_width, BRACKET_LOGO_HEIGHT), Image.LANCZOS)
            except (OSError, ValueError):
                _bracket_logo_cache = False
        return _bracket_logo_cache or None

    # A cached TTF font at a given size. `variation` selects a named
    # instance out of a variable font (IBM_PLEX_SANS ships every weight
    # in one file, see its own comment) and is ignored for the static
    # Chakra Petch files, which don't have any. Falls back to PIL's
    # built-in default font if the TTF itself is missing (e.g. a
    # self-hosted deploy that didn't pull assets/fonts) rather than
    # crashing rendering outright, the same "degrade gracefully instead
    # of taking the feature down" approach _loadBracketLogo takes for a
    # missing logo file.
    def _loadFont(self, path, size, variation=None):
        key = (path, size, variation)
        if key not in _font_cache:
            try:
                font = ImageFont.truetype(path, size)
                if variation is not None:
                    font.set_variation_by_name(variation)
            except (OSError, ValueError):
                font = ImageFont.load_default(size=size)
            _font_cache[key] = font
        return _font_cache[key]

    # How tall the logo/title/subtitle/rule block is, in total, computed
    # independently of any actual drawing so callers can reserve the right
    # amount of vertical space for it during their own (measurement-first)
    # layout pass, the same two-pass approach the rest of this file uses.
    def _bracketHeaderHeight(self, subtitle):
        height = BRACKET_MARGIN + BRACKET_LOGO_HEIGHT
        if subtitle:
            height += BRACKET_SUBTITLE_GAP + BRACKET_SUBTITLE_FONT_SIZE
        return height + BRACKET_HEADER_RULE_GAP + BRACKET_HEADER_RULE_MARGIN

    # Draws the logo (if available), the title next to it in
    # `accent_color`, an optional muted subtitle line below (the guild's
    # name, see renderBracketImages), and a full-width accent rule under
    # the whole block: the visual "masthead" every bracket/Grand Finals
    # image opens with. `width` is the FINAL canvas width, known by the
    # time this runs (unlike _bracketHeaderHeight, called during layout
    # before it exists).
    def _drawBracketHeader(self, image, draw, title, subtitle, accent_color, width, bold_title=False):
        title_font = self._loadFont(
            CHAKRA_PETCH_BOLD if bold_title else CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE
        )
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")

        logo = self._loadBracketLogo()
        title_x = BRACKET_MARGIN
        if logo is not None:
            logo_y = BRACKET_MARGIN + (BRACKET_LOGO_HEIGHT - logo.height) // 2
            image.paste(logo, (BRACKET_MARGIN, logo_y), logo)
            title_x = BRACKET_MARGIN + logo.width + BRACKET_PADDING

        # bold_title picks CHAKRA_PETCH_BOLD vs _SEMIBOLD above, a real
        # heavier font weight, not a faux-bold trick. So nothing extra is
        # needed here beyond just drawing with that font.
        draw.text(
            (title_x, BRACKET_MARGIN + BRACKET_LOGO_HEIGHT / 2), title, font=title_font, fill=accent_color,
            anchor="lm"
        )

        bottom = BRACKET_MARGIN + BRACKET_LOGO_HEIGHT
        if subtitle:
            subtitle_y = bottom + BRACKET_SUBTITLE_GAP
            draw.text((BRACKET_MARGIN, subtitle_y), subtitle, font=subtitle_font, fill=BRACKET_LINE_COLOR, anchor="la")
            bottom = subtitle_y + BRACKET_SUBTITLE_FONT_SIZE

        rule_y = bottom + BRACKET_HEADER_RULE_GAP
        draw.line(
            [(BRACKET_MARGIN, rule_y), (width - BRACKET_MARGIN, rule_y)], fill=accent_color,
            width=BRACKET_RULE_WIDTH
        )

    # The extra canvas width the header block itself needs: the logo (if
    # any) plus its gap before the title, compared against the title's
    # own text and the subtitle's. So a short bracket with a long guild
    # name (or vice versa) still sizes the canvas to whichever is
    # actually widest.
    def _bracketHeaderWidth(self, measurer, title, subtitle, title_font, subtitle_font):
        logo = self._loadBracketLogo()
        logo_width = (logo.width + BRACKET_PADDING) if logo is not None else 0
        title_width = logo_width + measurer.textlength(title, font=title_font)
        subtitle_width = measurer.textlength(subtitle, font=subtitle_font) if subtitle else 0
        return max(title_width, subtitle_width)

    # A fresh bracket-image canvas: a soft radial vignette
    # (`background_center` fading out to `background` at the corners,
    # computed with numpy since a plain flat fill at these pixel counts
    # would otherwise mean a per-pixel Python loop) inside a thin rounded
    # frame in `accent_color`. Returns (image, draw). Every caller needs
    # both anyway, and creating them together keeps that pairing from
    # ever drifting apart. `background`/`background_center` default to
    # Shockwave's own site palette so every existing caller (bracket,
    # matchup, Grand Finals images) is unaffected. Only the trading card
    # (see _renderTradingCardImage) actually overrides them, for a
    # player's customized background color.
    def _createBracketCanvas(
        self, width, height, accent_color=BRACKET_LINE_COLOR,
        background=BRACKET_BACKGROUND, background_center=BRACKET_BACKGROUND_CENTER,
    ):
        yy, xx = np.mgrid[0:height, 0:width]
        cx, cy = width / 2, height / 2
        max_dist = math.hypot(max(cx, width - cx), max(cy, height - cy))
        t = np.clip(np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max_dist, 0, 1)[..., None]
        center = np.array(background_center, dtype=np.float64)
        edge = np.array(background, dtype=np.float64)
        image = Image.fromarray((center * (1 - t) + edge * t).astype(np.uint8), "RGB")

        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            [BRACKET_LINE_WIDTH, BRACKET_LINE_WIDTH, width - BRACKET_LINE_WIDTH - 1, height - BRACKET_LINE_WIDTH - 1],
            radius=BRACKET_BORDER_RADIUS, outline=accent_color, width=BRACKET_LINE_WIDTH
        )
        return image, draw

    # The text a node prints as itself: its real team name once decided,
    # otherwise "BYE" for a permanently-empty round-0 slot or "TBD" for a
    # later round still waiting on an earlier match.
    def _bracketNodeLabel(self, node, round_index):
        if node.team is not None:
            return node.team.get_name()
        return "BYE" if round_index == 0 else "TBD"

    # Whether `node`'s own label is still live (still in it, or the match
    # it fed hasn't been decided yet) or eliminated, dimmed in the second
    # case, so a glance at the tree shows who's still alive. A node is
    # eliminated exactly when the match it feeds into (node.next) has
    # resolved to someone ELSE'S name. A node whose own team won and
    # advanced should stand out in full brightness, not fade as a "stale
    # waypoint".
    def _bracketNodeTextColor(self, node):
        if node.team is None:
            return BRACKET_TEXT_COLOR
        if (
            node.next is not None and node.next.team is not None
            and node.next.team.get_name() != node.team.get_name()
        ):
            return BRACKET_LINE_COLOR
        return BRACKET_TEXT_COLOR

    # WB-style round names, keyed off how many rounds remain until the
    # champion, "Round of 64" for the leaves of a 64-team bracket, on down
    # to "Finals" for the last real match and "Champion" for the result
    # itself (round_index == top_round_index).
    def _roundName(self, round_index, top_round_index):
        rounds_from_final = top_round_index - round_index
        if rounds_from_final <= 0:
            return "Champion"
        if rounds_from_final == 1:
            return "Finals"
        if rounds_from_final == 2:
            return "Semifinals"
        if rounds_from_final == 3:
            return "Quarterfinals"
        return f"Round of {2 ** rounds_from_final}"

    # The losers bracket doesn't cleanly halve every round the way the
    # winners bracket does (drop-ins keep the count uneven, see
    # buildLosersBracket), so "Quarterfinals"-style names don't reliably
    # apply. Plain numbering instead, 1-indexed from the leaves.
    def _losersRoundName(self, round_index):
        return f"Losers Round {round_index + 1}"

    # One text label per round_index, positioned directly above that
    # column's own nodes (same x formula _assignBracketPositions/
    # _drawBracketNode use to place them, just one row higher and with no
    # y-offset of its own). `offsets`/`x0`/`mirror` need to exactly match
    # whatever positions were already built from them. `max_round_index`
    # is the deepest round_index actually present in THIS `offsets`
    # table. The two-sided renderers pass half_round_index here, not the
    # whole bracket's top_round_index, since each half only goes that
    # deep.
    def _drawRoundHeaders(self, draw, offsets, x0, max_round_index, header_y, header_font, name_fn, mirror=False):
        for round_index in range(max_round_index + 1):
            x = x0 + (-offsets[round_index] if mirror else offsets[round_index])
            draw.text(
                (x, header_y), name_fn(round_index), font=header_font, fill=BRACKET_LINE_COLOR,
                anchor=("ra" if mirror else "la")
            )

    # A small 5-pointed star, standing in for a trophy icon. PIL's
    # bundled default font doesn't reliably have emoji glyphs (see the
    # losers-bracket-merge note above about box-drawing characters, the
    # same issue), so this is drawn as plain geometry instead of relying
    # on one.
    def _drawStar(self, draw, cx, cy, radius, color):
        points = []
        for i in range(10):
            angle = -math.pi / 2 + i * math.pi / 5
            r = radius if i % 2 == 0 else radius * 0.42
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=color)

    # A champion/final-result label with its own star badge in the gap
    # BRACKET_CHAMPION_BADGE_GAP already reserves immediately to its
    # left. Every caller that positions a champion-style label needs to
    # have added that gap to its own x math first (see _renderTreeImage
    # and friends), same as champion_width already accounts for the text
    # itself.
    def _drawChampionLabel(self, draw, x, y, label, font, color):
        star_cx = x - BRACKET_CHAMPION_BADGE_GAP + BRACKET_PADDING + BRACKET_CHAMPION_STAR_RADIUS
        self._drawStar(draw, star_cx, y, BRACKET_CHAMPION_STAR_RADIUS, color)
        draw.text((x, y), label, font=font, fill=color, anchor="lm")

    # Draws an axis-aligned two-segment path from `from_point` through
    # `corner` to `to_point` with the corner rounded to `radius`, instead
    # of a sharp draw.line. Purely cosmetic, softens every elbow in the
    # tree. Falls back to a sharp corner if either segment is too short
    # to fit the requested radius, so short connectors near the leaves
    # never overshoot.
    def _drawRoundedElbow(self, draw, from_point, corner, to_point, color, width, radius):
        fx, fy = from_point
        cx, cy = corner
        tx, ty = to_point

        if fx == cx:
            vdir = 1 if fy > cy else -1
            seg1_len = abs(fy - cy)
            hdir = 1 if tx > cx else -1
            seg2_len = abs(tx - cx)
        else:
            hdir = 1 if fx > cx else -1
            seg1_len = abs(fx - cx)
            vdir = 1 if ty > cy else -1
            seg2_len = abs(ty - cy)

        r = min(radius, seg1_len, seg2_len)
        if r <= 0:
            draw.line([from_point, corner, to_point], fill=color, width=width)
            return

        if fx == cx:
            trimmed_from = (cx, cy + vdir * r)
            trimmed_to = (cx + hdir * r, cy)
        else:
            trimmed_from = (cx + hdir * r, cy)
            trimmed_to = (cx, cy + vdir * r)

        draw.line([from_point, trimmed_from], fill=color, width=width)
        draw.line([trimmed_to, to_point], fill=color, width=width)

        # The arc's center sits `r` away from the corner along BOTH
        # segments' own directions: the only point equidistant (by r)
        # from both trimmed endpoints that keeps the curve tangent to
        # each line.
        center_x, center_y = cx + hdir * r, cy + vdir * r
        start_angle, end_angle = {
            (1, -1): (90, 180), (1, 1): (180, 270), (-1, -1): (0, 90), (-1, 1): (270, 360),
        }[(hdir, vdir)]
        draw.arc(
            [center_x - r, center_y - r, center_x + r, center_y + r], start_angle, end_angle,
            fill=color, width=width
        )

    # Walks the tree rooted at `node` (a champion node, `round_index`
    # rounds up from its own leaves) via `previous`/`previous.opponent`,
    # recording each node's (label, round_index) (leaves included) into
    # `labels`. A losers-bracket "fresh drop-in" leaf (see
    # buildLosersBracket) renders at the SAME round_index as whatever
    # sibling it's paired against, not at 0 the way every winners-bracket
    # leaf does. That's exactly what makes it land in the right column
    # below (see _assignBracketPositions) without needing any
    # special-casing here.
    def _collectBracketLabels(self, node, round_index, labels):
        labels[id(node)] = (self._bracketNodeLabel(node, round_index), round_index)
        if node.previous is not None:
            self._collectBracketLabels(node.previous, round_index - 1, labels)
            self._collectBracketLabels(node.previous.opponent, round_index - 1, labels)

    # One pixel column per round_index (the X coordinate every node at
    # that round_index gets drawn at), sized to the widest label anywhere
    # in that round_index (plus padding for the connector line into it),
    # so every round lines up in a straight column across the whole
    # image. The same idea the old ASCII renderer used column_widths for.
    # `header_font`/`round_name_fn`, if given, also count that round's
    # own header text (see _drawRoundHeaders) toward its column's width.
    # A small bracket with short team names but a longer header like
    # "Losers Round 1" would otherwise size the column to the names alone
    # and run that header straight into the next one.
    def _bracketColumnOffsets(self, labels, draw, font, top_round_index, header_font=None, round_name_fn=None):
        widths = [0] * (top_round_index + 1)
        for label, round_index in labels.values():
            widths[round_index] = max(widths[round_index], draw.textlength(label, font=font))
        if header_font is not None:
            for round_index in range(top_round_index + 1):
                header_width = draw.textlength(round_name_fn(round_index), font=header_font)
                widths[round_index] = max(widths[round_index], header_width)

        offsets = [0] * (top_round_index + 2)
        for round_index in range(top_round_index + 1):
            offsets[round_index + 1] = offsets[round_index] + widths[round_index] + BRACKET_PADDING * 4
        return offsets

    # Recursively assigns every node an (x, y) pixel position: x straight
    # from `offsets[round_index]`, y for a leaf from a shared counter (so
    # leaves stack top-to-bottom in traversal order) and for anything
    # else the midpoint of its two children's y. Writes into `positions`
    # and returns this node's own y so its caller can average it with its
    # sibling's. Unlike the ASCII renderer, a losers-bracket "fresh
    # drop-in" leaf needs no special handling at all here: it's still
    # just a leaf, its x already comes out right since it's called with
    # the SAME round_index as its sibling, and pixel space doesn't need
    # the leading-blank padding tightly-packed character columns did.
    def _assignBracketPositions(self, node, round_index, positions, leaf_counter, offsets):
        x = offsets[round_index]
        if node.previous is None:
            y = next(leaf_counter) * BRACKET_ROW_HEIGHT + BRACKET_ROW_HEIGHT / 2
            positions[id(node)] = (x, y)
            return y

        left_y = self._assignBracketPositions(node.previous, round_index - 1, positions, leaf_counter, offsets)
        right_y = self._assignBracketPositions(
            node.previous.opponent, round_index - 1, positions, leaf_counter, offsets
        )
        y = (left_y + right_y) / 2
        positions[id(node)] = (x, y)
        return y

    # Draws `node`'s own label at its position, then (if it's not a leaf)
    # the ┐/┘ elbow connecting its two children into it, and recurses.
    # Line drawing rather than box-drawing text characters specifically
    # so this never depends on a font actually having those glyphs.
    # Plain straight lines render identically on every platform.
    #
    # `mirror` draws the exact horizontal mirror image: labels grow
    # leftward from their anchor (`anchor="rm"` instead of `"lm"`) and
    # every connector offset flips direction, for the right-hand half of
    # a two-sided bracket (see _renderTwoSidedTreeImage), where
    # round_index still increases moving INTO the page (toward the
    # center) but that now means decreasing x instead of increasing it.
    # `skip_own_label`, when True, leaves THIS node's own text undrawn
    # (its connectors and children still get drawn/recursed normally),
    # for a caller that wants to draw the root itself separately, as a
    # champion label with its own star badge (see _drawChampionLabel)
    # instead of a plain dimmable node.
    def _drawBracketNode(self, draw, node, positions, labels, font, mirror=False, skip_own_label=False):
        sign = -1 if mirror else 1
        x, y = positions[id(node)]
        if not skip_own_label:
            label, _ = labels[id(node)]
            draw.text(
                (x, y), label, font=font, fill=self._bracketNodeTextColor(node),
                anchor=("rm" if mirror else "lm")
            )

        if node.previous is None:
            return

        left, right = node.previous, node.previous.opponent
        lx, ly = positions[id(left)]
        rx, ry = positions[id(right)]
        left_width = draw.textlength(labels[id(left)][0], font=font)
        right_width = draw.textlength(labels[id(right)][0], font=font)
        connector_x = x - sign * BRACKET_PADDING * 2
        mid = (connector_x, y)

        self._drawRoundedElbow(
            draw, (lx + sign * (left_width + BRACKET_PADDING), ly), (connector_x, ly), mid,
            BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawRoundedElbow(
            draw, (rx + sign * (right_width + BRACKET_PADDING), ry), (connector_x, ry), mid,
            BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        draw.line([mid, (x - sign * BRACKET_PADDING, y)], fill=BRACKET_LINE_COLOR, width=BRACKET_LINE_WIDTH)

        self._drawBracketNode(draw, left, positions, labels, font, mirror)
        self._drawBracketNode(draw, right, positions, labels, font, mirror)

    # Renders one bracket tree (winners or losers), everything from
    # `champion_node` down to its leaves, as a standalone image sized to
    # exactly fit its content, titled `title`. Positions are computed in
    # a first pass so the canvas can be sized from their actual bounds
    # before anything is drawn, rather than guessing a size up front and
    # risking clipping the bottom/right edge.
    # `accent_color` colors the title and the champion's own label/badge:
    # gold for the winners bracket, BRACKET_LOSERS_ACCENT_COLOR for the
    # losers bracket (see _renderLosersBracketImage), so the two images
    # read as "which one is this" without depending on remembering the
    # caption. `round_name_fn` similarly defaults to the
    # winners-bracket-style Round-of-N/Quarterfinals/... naming. The
    # losers bracket passes _losersRoundName instead (see that method for
    # why).
    def _renderTreeImage(
        self, champion_node, top_round_index, title, accent_color=BRACKET_TITLE_COLOR, round_name_fn=None,
        subtitle=None
    ):
        font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        title_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        header_font = self._loadFont(IBM_PLEX_SANS, BRACKET_ROUND_LABEL_FONT_SIZE, "Medium")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        if round_name_fn is None:
            round_name_fn = lambda round_index: self._roundName(round_index, top_round_index)

        labels = {}
        self._collectBracketLabels(champion_node, top_round_index, labels)
        offsets = self._bracketColumnOffsets(
            labels, measurer, font, top_round_index, header_font=header_font, round_name_fn=round_name_fn
        )

        positions = {}
        self._assignBracketPositions(champion_node, top_round_index, positions, itertools.count(), offsets)

        header_y = self._bracketHeaderHeight(subtitle)
        tree_top = header_y + BRACKET_ROUND_LABEL_HEIGHT
        positions = {key: (x + BRACKET_MARGIN, y + tree_top) for key, (x, y) in positions.items()}

        # The champion's own position is nudged further right to make room
        # for its star badge (see _drawChampionLabel), done here, before
        # anything is drawn, so the connector leading into it (drawn as
        # part of _drawBracketNode's normal recursion) naturally reaches
        # the shifted spot instead of needing special-casing later.
        champion_label = labels[id(champion_node)][0]
        champion_width = measurer.textlength(champion_label, font=font)
        champion_x, champion_y = positions[id(champion_node)]
        champion_x += BRACKET_CHAMPION_BADGE_GAP
        positions[id(champion_node)] = (champion_x, champion_y)

        header_width = self._bracketHeaderWidth(measurer, title, subtitle, title_font, subtitle_font)
        width = int(max(champion_x + champion_width, header_width) + BRACKET_MARGIN * 2)
        height = int(max(y for x, y in positions.values()) + BRACKET_ROW_HEIGHT / 2 + BRACKET_MARGIN)

        image, draw = self._createBracketCanvas(width, height, accent_color)
        self._drawBracketHeader(image, draw, title, subtitle, accent_color, width)
        self._drawRoundHeaders(draw, offsets, BRACKET_MARGIN, top_round_index, header_y, header_font, round_name_fn)
        self._drawBracketNode(draw, champion_node, positions, labels, font, skip_own_label=True)
        self._drawChampionLabel(draw, champion_x, champion_y, champion_label, font, accent_color)
        return image

    # Same idea as _renderTreeImage, but for a bracket deep enough
    # (BRACKET_TWO_SIDED_MIN_ROUNDS+) that it's worth splitting into two
    # halves growing toward the center. See that constant's comment for
    # why this only ever gets called for the winners bracket.
    # `champion_node`'s two children (always exactly even halves, see
    # buildBracket) are laid out independently, the right one mirrored
    # (_drawBracketNode's `mirror` flag), and joined by one final
    # connector into the champion in the middle.
    def _renderTwoSidedTreeImage(self, champion_node, top_round_index, title, subtitle=None):
        left_half = champion_node.previous
        right_half = champion_node.previous.opponent
        half_round_index = top_round_index - 1

        font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        title_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        header_font = self._loadFont(IBM_PLEX_SANS, BRACKET_ROUND_LABEL_FONT_SIZE, "Medium")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        round_name_fn = lambda round_index: self._roundName(round_index, top_round_index)

        left_labels, right_labels = {}, {}
        self._collectBracketLabels(left_half, half_round_index, left_labels)
        self._collectBracketLabels(right_half, half_round_index, right_labels)

        left_offsets = self._bracketColumnOffsets(
            left_labels, measurer, font, half_round_index, header_font=header_font, round_name_fn=round_name_fn
        )
        right_offsets = self._bracketColumnOffsets(
            right_labels, measurer, font, half_round_index, header_font=header_font, round_name_fn=round_name_fn
        )

        left_positions, right_positions = {}, {}
        self._assignBracketPositions(left_half, half_round_index, left_positions, itertools.count(), left_offsets)
        self._assignBracketPositions(right_half, half_round_index, right_positions, itertools.count(), right_offsets)

        # Each half's own top node (closest to center) sits at its
        # deepest local x - offsets_*[half_round_index], same as max()
        # over its positions. Since _drawBracketNode anchors a label at
        # its position and extends it AWAY from center, it needs its own
        # rendered width added on top of that to know where it actually
        # ends.
        champion_label = self._bracketNodeLabel(champion_node, top_round_index)
        champion_width = measurer.textlength(champion_label, font=font)
        left_top_x = max(x for x, y in left_positions.values())
        right_top_x = max(x for x, y in right_positions.values())
        left_half_width = measurer.textlength(left_labels[id(left_half)][0], font=font)
        right_half_width = measurer.textlength(right_labels[id(right_half)][0], font=font)

        header_y = self._bracketHeaderHeight(subtitle)
        tree_top = header_y + BRACKET_ROUND_LABEL_HEIGHT
        left_x0 = BRACKET_MARGIN
        connector_x = left_x0 + left_top_x + left_half_width + BRACKET_PADDING * 3
        champion_x = connector_x + BRACKET_PADDING + BRACKET_CHAMPION_BADGE_GAP
        # Right_half sits past the champion, not immediately across
        # connector_x from left_half. Otherwise its own top node reads as
        # a second, unrelated match crowded right next to the actual
        # result instead of a clearly separate half of the bracket.
        right_x0 = champion_x + champion_width + BRACKET_PADDING * 3 + right_top_x + right_half_width

        # The right half is also nudged down a couple of rows from where
        # a plain mirror of the left half would otherwise land.
        # Right_half's own connector line necessarily crosses the
        # champion's x-range now (it sits past it). Without this nudge,
        # any bracket without byes makes both halves the exact same
        # shape, so that line would land on the champion's exact row and
        # run straight through its label.
        row_nudge = BRACKET_ROW_HEIGHT * 2
        left_positions = {key: (x + left_x0, y + tree_top) for key, (x, y) in left_positions.items()}
        right_positions = {
            key: (right_x0 - x, y + tree_top + row_nudge) for key, (x, y) in right_positions.items()
        }

        left_half_x, left_half_y = left_positions[id(left_half)]
        right_half_x, right_half_y = right_positions[id(right_half)]
        champion_y = (left_half_y + right_half_y) / 2

        header_width = self._bracketHeaderWidth(measurer, title, subtitle, title_font, subtitle_font)
        width = int(max(right_x0, champion_x + champion_width, header_width) + BRACKET_MARGIN * 2)
        height = int(
            max(y for x, y in list(left_positions.values()) + list(right_positions.values()))
            + BRACKET_ROW_HEIGHT / 2 + BRACKET_MARGIN
        )

        image, draw = self._createBracketCanvas(width, height, BRACKET_TITLE_COLOR)
        self._drawBracketHeader(image, draw, title, subtitle, BRACKET_TITLE_COLOR, width)
        self._drawRoundHeaders(draw, left_offsets, left_x0, half_round_index, header_y, header_font, round_name_fn)
        self._drawRoundHeaders(
            draw, right_offsets, right_x0, half_round_index, header_y, header_font, round_name_fn, mirror=True
        )

        self._drawBracketNode(draw, left_half, left_positions, left_labels, font, mirror=False)
        self._drawBracketNode(draw, right_half, right_positions, right_labels, font, mirror=True)

        merge_point = (connector_x, champion_y)
        self._drawRoundedElbow(
            draw, (left_half_x + left_half_width + BRACKET_PADDING, left_half_y), (connector_x, left_half_y),
            merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawRoundedElbow(
            draw, (right_half_x - right_half_width - BRACKET_PADDING, right_half_y), (connector_x, right_half_y),
            merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawChampionLabel(draw, champion_x, champion_y, champion_label, font, BRACKET_TITLE_COLOR)

        return image

    # The winners bracket as an image, always present once a bracket
    # exists at all. A plain hyphen, not an em dash, in the title: PIL's
    # bundled default font doesn't have a glyph for it, which renders as
    # a visible tofu box. Plain ASCII punctuation is guaranteed to exist
    # in any font this ends up running with.
    def _renderWinnersBracketImage(self, tournament, guild_name=None):
        rounds = self._bracketRounds(tournament.get_bracket())
        top_round_index = len(rounds) - 1
        champion_node = rounds[-1][0]
        title = f"{tournament.get_name()} - Winners Bracket"
        subtitle = f"for {guild_name}" if guild_name else None
        if top_round_index >= BRACKET_TWO_SIDED_MIN_ROUNDS:
            return self._renderTwoSidedTreeImage(champion_node, top_round_index, title, subtitle)
        return self._renderTreeImage(champion_node, top_round_index, title, subtitle=subtitle)

    # The losers bracket as an image, for a double-elimination tournament,
    # None for the degenerate 2-team case (a single winners-bracket match
    # has no one left for its loser to play, so there's no losers-bracket
    # tree to draw at all).
    def _renderLosersBracketImage(self, tournament, guild_name=None):
        lb_rounds = tournament.get_losers_rounds()
        if not lb_rounds or (len(lb_rounds) == 1 and lb_rounds[0][0].previous is None):
            return None
        title = f"{tournament.get_name()} - Losers Bracket"
        subtitle = f"for {guild_name}" if guild_name else None
        if len(lb_rounds) >= BRACKET_TWO_SIDED_MIN_ROUNDS:
            return self._renderLosersTwoSidedTreeImage(lb_rounds, title, subtitle)
        return self._renderTreeImage(
            lb_rounds[-1][0], len(lb_rounds), title,
            accent_color=BRACKET_LOSERS_ACCENT_COLOR, round_name_fn=self._losersRoundName, subtitle=subtitle
        )

    # _renderTwoSidedTreeImage's counterpart for the losers bracket, which
    # can't just split at the champion's own two children the way the
    # winners bracket does. The losers bracket's last round is ALWAYS a
    # lopsided drop-in (see buildLosersBracket): one side is the deep
    # surviving lineage, the other is a single bare leaf (whichever team
    # lost the winners-bracket final outright). So that split would put
    # an entire tree on one side and one bare name on the other.
    #
    # One round earlier is where the two winners-bracket-side lineages
    # actually meet. Every losers-bracket round after that keeps
    # winners-left and winners-right losers strictly separate (each
    # drop-in pairs a survivor against a fresh loser from the SAME
    # winners-bracket side, see buildLosersBracket's round-alternation
    # pattern), right up until the second-to-last round, which is always
    # exactly one node: the first, and only, point where they merge. THAT
    # merge is the genuine even split. Drawing it two-sided and then
    # extending one more (normal, single-sided) hop past it to reach the
    # true champion keeps things honest about where the real asymmetry
    # is, instead of pushing it into a lopsided top-level split.
    def _renderLosersTwoSidedTreeImage(self, lb_rounds, title, subtitle=None):
        champion_node = lb_rounds[-1][0]
        merge_node = lb_rounds[-2][0]
        top_round_index = len(lb_rounds)
        merge_round_index = top_round_index - 1
        half_round_index = merge_round_index - 1

        left_half = merge_node.previous
        right_half = merge_node.previous.opponent
        other_child = (
            champion_node.previous.opponent if champion_node.previous is merge_node else champion_node.previous
        )

        font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        title_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        header_font = self._loadFont(IBM_PLEX_SANS, BRACKET_ROUND_LABEL_FONT_SIZE, "Medium")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        left_labels, right_labels = {}, {}
        self._collectBracketLabels(left_half, half_round_index, left_labels)
        self._collectBracketLabels(right_half, half_round_index, right_labels)
        left_offsets = self._bracketColumnOffsets(
            left_labels, measurer, font, half_round_index,
            header_font=header_font, round_name_fn=self._losersRoundName
        )
        right_offsets = self._bracketColumnOffsets(
            right_labels, measurer, font, half_round_index,
            header_font=header_font, round_name_fn=self._losersRoundName
        )

        left_positions, right_positions = {}, {}
        self._assignBracketPositions(left_half, half_round_index, left_positions, itertools.count(), left_offsets)
        self._assignBracketPositions(right_half, half_round_index, right_positions, itertools.count(), right_offsets)

        merge_label = self._bracketNodeLabel(merge_node, merge_round_index)
        merge_width = measurer.textlength(merge_label, font=font)
        other_label = self._bracketNodeLabel(other_child, merge_round_index)
        other_width = measurer.textlength(other_label, font=font)
        champion_label = self._bracketNodeLabel(champion_node, top_round_index)
        champion_width = measurer.textlength(champion_label, font=font)

        left_top_x = max(x for x, y in left_positions.values())
        right_top_x = max(x for x, y in right_positions.values())
        left_half_width = measurer.textlength(left_labels[id(left_half)][0], font=font)
        right_half_width = measurer.textlength(right_labels[id(right_half)][0], font=font)

        header_y = self._bracketHeaderHeight(subtitle)
        tree_top = header_y + BRACKET_ROUND_LABEL_HEIGHT
        left_x0 = BRACKET_MARGIN
        connector_x = left_x0 + left_top_x + left_half_width + BRACKET_PADDING * 3
        merge_x = connector_x + BRACKET_PADDING
        final_connector_x = merge_x + max(merge_width, other_width) + BRACKET_PADDING * 3
        champion_x = final_connector_x + BRACKET_PADDING + BRACKET_CHAMPION_BADGE_GAP
        # Right_half sits past the champion, not immediately across
        # connector_x from left_half; otherwise its own top node reads as
        # a second, unrelated match crowded right next to the merge_node/
        # other_child/champion hop instead of a clearly separate half of
        # the bracket.
        right_x0 = champion_x + champion_width + BRACKET_PADDING * 3 + right_top_x + right_half_width

        # The right half is also nudged well down from where a plain
        # mirror of the left half would otherwise land, more than the
        # minimum needed to keep right_half's own connector line off the
        # hop's rows. (That alone only bought ~1.25 rows of clearance,
        # which technically doesn't cross anything but still reads as
        # "another game" sitting right under the champion at a glance.)
        # Without any nudge at all, any bracket without byes makes both
        # halves the exact same shape, putting right_half's own top on
        # the hop's exact row.
        row_nudge = BRACKET_ROW_HEIGHT * 6
        left_positions = {key: (x + left_x0, y + tree_top) for key, (x, y) in left_positions.items()}
        right_positions = {
            key: (right_x0 - x, y + tree_top + row_nudge) for key, (x, y) in right_positions.items()
        }

        left_half_x, left_half_y = left_positions[id(left_half)]
        right_half_x, right_half_y = right_positions[id(right_half)]
        bar_mid_y = (left_half_y + right_half_y) / 2

        merge_y = bar_mid_y - BRACKET_ROW_HEIGHT / 2
        other_x, other_y = merge_x, merge_y + BRACKET_ROW_HEIGHT
        champion_y = (merge_y + other_y) / 2

        header_width = self._bracketHeaderWidth(measurer, title, subtitle, title_font, subtitle_font)
        width = int(max(right_x0, champion_x + champion_width, header_width) + BRACKET_MARGIN * 2)
        height = int(
            max(other_y, max(y for x, y in list(left_positions.values()) + list(right_positions.values())))
            + BRACKET_ROW_HEIGHT / 2 + BRACKET_MARGIN
        )

        image, draw = self._createBracketCanvas(width, height, BRACKET_LOSERS_ACCENT_COLOR)
        self._drawBracketHeader(image, draw, title, subtitle, BRACKET_LOSERS_ACCENT_COLOR, width)
        self._drawRoundHeaders(
            draw, left_offsets, left_x0, half_round_index, header_y, header_font, self._losersRoundName
        )
        self._drawRoundHeaders(
            draw, right_offsets, right_x0, half_round_index, header_y, header_font, self._losersRoundName,
            mirror=True
        )

        self._drawBracketNode(draw, left_half, left_positions, left_labels, font, mirror=False)
        self._drawBracketNode(draw, right_half, right_positions, right_labels, font, mirror=True)

        merge_point = (connector_x, champion_y)
        self._drawRoundedElbow(
            draw, (left_half_x + left_half_width + BRACKET_PADDING, left_half_y), (connector_x, left_half_y),
            merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawRoundedElbow(
            draw, (right_half_x - right_half_width - BRACKET_PADDING, right_half_y), (connector_x, right_half_y),
            merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        draw.text((merge_x, merge_y), merge_label, font=font, fill=self._bracketNodeTextColor(merge_node), anchor="lm")
        draw.text(
            (other_x, other_y), other_label, font=font, fill=self._bracketNodeTextColor(other_child), anchor="lm"
        )

        final_point = (final_connector_x, champion_y)
        self._drawRoundedElbow(
            draw, (merge_x + merge_width + BRACKET_PADDING, merge_y), (final_connector_x, merge_y), final_point,
            BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawRoundedElbow(
            draw, (other_x + other_width + BRACKET_PADDING, other_y), (final_connector_x, other_y), final_point,
            BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
        )
        self._drawChampionLabel(draw, champion_x, champion_y, champion_label, font, BRACKET_LOSERS_ACCENT_COLOR)

        return image

    # The DB-dependent half of _renderGrandFinalsImage: everything needed
    # to actually draw the stage except the drawing itself, or None if
    # there's nothing worth drawing yet (either bracket champion still
    # missing, or game 1 hasn't been played). Kept separate from
    # _buildGrandFinalsImage (the pure, DB-free drawing) for two reasons.
    # That code can be exercised without a real guildId or any
    # tournament_matches rows (see /test in bot.py, which simulates a
    # full run entirely in memory), and _buildGrandFinalsImage alone is
    # safe to run via asyncio.to_thread (see
    # _sendBracketText/printBracketHelper). self.cursor was opened with
    # sqlite3's default check_same_thread=True, so a DB read from a
    # to_thread-offloaded worker thread would raise outright.
    def _grandFinalsRenderInputs(self, guild_id, tournament):
        wb_rounds = self._bracketRounds(tournament.get_bracket())
        wb_champion = wb_rounds[-1][0].team if wb_rounds else None
        lb_rounds = tournament.get_losers_rounds()
        lb_champion = lb_rounds[-1][0].team if lb_rounds else None
        if wb_champion is None or lb_champion is None:
            return None

        self.cursor.execute(
            "SELECT roundIndex, team1, team2, winner, state FROM tournament_matches "
            "WHERE guildId=? AND bracketType='finals' ORDER BY roundIndex",
            (guild_id,)
        )
        game1_winner_name, reset_winner_name = None, None
        for round_index, team1_ser, team2_ser, winner, state in self.cursor.fetchall():
            if state != "RESOLVED":
                continue
            team1, team2 = Team(), Team()
            team1.deserializeTeam(team1_ser)
            team2.deserializeTeam(team2_ser)
            winning_name = (team1 if winner == 1 else team2).get_name()
            if round_index == 0:
                game1_winner_name = winning_name
            else:
                reset_winner_name = winning_name

        if game1_winner_name is None:
            return None

        return wb_champion, lb_champion, game1_winner_name, reset_winner_name

    # A dedicated third image for just the Grand Finals stage:
    # winners-bracket champion vs losers-bracket champion, and the
    # decider "bracket reset" match if the losers-bracket side forced one
    # by winning game 1. None until game 1 has actually been played, not
    # just once both bracket champions exist, since "vs, nothing decided
    # yet" isn't worth its own message (see _sendBracketText, which
    # sends this separately from the winners/losers bracket images, and
    # only when this isn't None). Synchronous convenience wrapper around
    # _grandFinalsRenderInputs + _buildGrandFinalsImage for callers (and
    # tests) that don't care about the thread-safety split those two
    # exist for. See _sendBracketText/printBracketHelper for the caller
    # that does.
    def _renderGrandFinalsImage(self, guild_id, tournament, guild_name=None):
        inputs = self._grandFinalsRenderInputs(guild_id, tournament)
        if inputs is None:
            return None
        return self._buildGrandFinalsImage(tournament, *inputs, guild_name)

    # The pure rendering half of _renderGrandFinalsImage. No DB access,
    # just the two bracket champions and however far Grand Finals has
    # resolved (both winner names None if it hasn't started,
    # reset_winner_name None until a reset was both needed and played).
    def _buildGrandFinalsImage(
        self, tournament, wb_champion, lb_champion, game1_winner_name, reset_winner_name, guild_name=None
    ):
        # A reset match only ever happens if the losers-bracket champion
        # won game 1 (both sides then sit at one loss apiece). See
        # _resolveFinalsMatch.
        needs_reset = game1_winner_name == lb_champion.get_name()

        font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        title_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        title = f"{tournament.get_name()} - Grand Finals"
        subtitle = f"for {guild_name}" if guild_name else None

        game1_label = game1_winner_name if game1_winner_name is not None else "TBD"
        # Dims whichever side lost once a stage resolves, matching the
        # main bracket images (see _bracketNodeTextColor). Stays False
        # (no dimming) for whichever side hasn't lost yet: either the
        # stage isn't decided, or that side is the one who won.
        stages = [{
            "top": f"{wb_champion.get_name()} (winners bracket)",
            "bottom": f"{lb_champion.get_name()} (losers bracket)",
            "result": game1_label,
            "gold": game1_winner_name is not None and not needs_reset,
            "top_dim": game1_winner_name is not None and game1_winner_name != wb_champion.get_name(),
            "bottom_dim": game1_winner_name is not None and game1_winner_name != lb_champion.get_name(),
        }]
        if needs_reset:
            stages.append({
                "top": f"{lb_champion.get_name()} (won Game 1)",
                "bottom": f"{wb_champion.get_name()} (elimination game)",
                "result": reset_winner_name if reset_winner_name is not None else "TBD",
                "gold": reset_winner_name is not None,
                "top_dim": reset_winner_name is not None and reset_winner_name != lb_champion.get_name(),
                "bottom_dim": reset_winner_name is not None and reset_winner_name != wb_champion.get_name(),
            })

        # Two-pass layout, same approach as the rest of this file: measure
        # everything against a throwaway Draw first so the canvas can be
        # sized from actual content, then draw for real once it exists.
        # Stages chain left to right (each one's result feeds the next
        # stage's inputs), all centered on the same horizontal mid-line.
        tree_top = self._bracketHeaderHeight(subtitle)
        x = BRACKET_MARGIN
        top_y = tree_top
        layout = []
        for stage in stages:
            bottom_y = top_y + BRACKET_ROW_HEIGHT
            mid_y = (top_y + bottom_y) / 2
            top_width = measurer.textlength(stage["top"], font=font)
            bottom_width = measurer.textlength(stage["bottom"], font=font)
            connector_x = x + max(top_width, bottom_width) + BRACKET_PADDING * 3
            result_x = connector_x + BRACKET_PADDING
            if stage["gold"]:
                result_x += BRACKET_CHAMPION_BADGE_GAP
            result_width = measurer.textlength(stage["result"], font=font)
            layout.append({
                **stage, "x": x, "top_y": top_y, "bottom_y": bottom_y, "mid_y": mid_y,
                "top_width": top_width, "bottom_width": bottom_width,
                "connector_x": connector_x, "result_x": result_x, "result_width": result_width,
            })
            x = result_x + result_width + BRACKET_PADDING * 3
            top_y = mid_y - BRACKET_ROW_HEIGHT / 2

        header_width = self._bracketHeaderWidth(measurer, title, subtitle, title_font, subtitle_font)
        last = layout[-1]
        width = int(max(last["result_x"] + last["result_width"], header_width) + BRACKET_MARGIN * 2)
        height = int(max(s["bottom_y"] for s in layout) + BRACKET_ROW_HEIGHT / 2 + BRACKET_MARGIN)

        image, draw = self._createBracketCanvas(width, height, BRACKET_TITLE_COLOR)
        self._drawBracketHeader(image, draw, title, subtitle, BRACKET_TITLE_COLOR, width)

        for stage in layout:
            top_color = BRACKET_LINE_COLOR if stage["top_dim"] else BRACKET_TEXT_COLOR
            bottom_color = BRACKET_LINE_COLOR if stage["bottom_dim"] else BRACKET_TEXT_COLOR
            draw.text((stage["x"], stage["top_y"]), stage["top"], font=font, fill=top_color, anchor="lm")
            draw.text(
                (stage["x"], stage["bottom_y"]), stage["bottom"], font=font, fill=bottom_color, anchor="lm"
            )
            merge_point = (stage["connector_x"], stage["mid_y"])
            self._drawRoundedElbow(
                draw, (stage["x"] + stage["top_width"] + BRACKET_PADDING, stage["top_y"]),
                (stage["connector_x"], stage["top_y"]), merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
            )
            self._drawRoundedElbow(
                draw, (stage["x"] + stage["bottom_width"] + BRACKET_PADDING, stage["bottom_y"]),
                (stage["connector_x"], stage["bottom_y"]), merge_point, BRACKET_LINE_COLOR, BRACKET_LINE_WIDTH, BRACKET_CORNER_RADIUS
            )
            if stage["gold"]:
                self._drawChampionLabel(
                    draw, stage["result_x"], stage["mid_y"], stage["result"], font, BRACKET_TITLE_COLOR
                )
            else:
                draw.text(
                    (stage["result_x"], stage["mid_y"]), stage["result"], font=font, fill=BRACKET_TEXT_COLOR,
                    anchor="lm"
                )

        return image

    # Every bracket image for `tournament`, as ready-to-attach
    # discord.Files: just the winners bracket for single elimination,
    # plus the losers bracket too (skipped only in the 2-team degenerate
    # case) for double. The Grand Finals image is deliberately NOT
    # included here. It's sent as its own separate message, and only
    # once Grand Finals has actually been played, instead of tagging
    # along on every bracket update, see _sendBracketText.
    def renderBracketImages(self, tournament, guild_name=None):
        files = [
            self._imageToFile(self._renderWinnersBracketImage(tournament, guild_name), "winners_bracket.png")
        ]
        if tournament.is_double_elimination():
            losers_image = self._renderLosersBracketImage(tournament, guild_name)
            if losers_image is not None:
                files.append(self._imageToFile(losers_image, "losers_bracket.png"))
        return files

    # Every bracket/matchup image is drawn BRACKET_SUPERSAMPLE times
    # bigger than it's meant to end up (see that constant's own
    # comment). This is the one place that scale gets undone, since
    # every renderer's output passes through here on its way to becoming
    # a discord.File. The LANCZOS downsize is what actually smooths out
    # jagged text/line edges. Drawing at 1x directly never had any
    # antialiasing to begin with.
    def _imageToFile(self, image, filename):
        if BRACKET_SUPERSAMPLE != 1:
            target_size = (image.width // BRACKET_SUPERSAMPLE, image.height // BRACKET_SUPERSAMPLE)
            image = image.resize(target_size, Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=filename)

    # `team`'s roster with its captain floated to the front (if it has one
    # and they're actually still on the roster), what _renderMatchupImage
    # prints, so "captain at the top" is just "print the list in order"
    # rather than something each caller has to special-case.
    def _orderedRoster(self, team):
        captain = team.get_captain()
        players = team.get_players()
        if isinstance(captain, Player) and any(p.get_id() == captain.get_id() for p in players):
            rest = [p for p in players if p.get_id() != captain.get_id()]
            return [captain] + rest
        return list(players)

    # SETUP_ROLE_NAMES entries are positional (team.players[i] is
    # whichever role useRoles put them in, see makeEmbedString's own
    # roles.get(i) lookup). Tried under a few candidate filenames in
    # ROLE_ICON_DIR (the plain lowercase name we ask for, plus the
    # "{Role}_icon.png"/"Middle" naming whoever populates that folder
    # actually used) and cached the same load-once-per-(role, size) shape
    # _eloBadgeImage already uses. Returns None (draw nothing, reserve no
    # extra width) if none of them exist. The assets aren't bundled with
    # the repo, so this has to degrade cleanly rather than crash every
    # roled matchup image until they're added.
    def _roleIconImage(self, role_name, size):
        cache_key = (role_name, size)
        if cache_key not in _role_icon_cache:
            icon = None
            for stem in ROLE_ICON_FILENAME_CANDIDATES.get(role_name, [role_name.lower()]):
                path = os.path.join(ROLE_ICON_DIR, f"{stem}.png")
                if os.path.isfile(path):
                    icon = Image.open(path).convert("RGBA")
                    icon.thumbnail((size, size), Image.LANCZOS)
                    break
            _role_icon_cache[cache_key] = icon
        return _role_icon_cache[cache_key]

    # {player_id: role_name} for a team formed with useRoles, positional
    # exactly like makeEmbedString's own roles.get(i) lookup
    # (team.players[i] is whichever role useRoles assigned them). Empty
    # for a team not formed with roles, or one that isn't exactly 5
    # players, the only size roles are ever assigned for.
    def _roleAssignments(self, team, use_roles):
        if not use_roles or len(team.players) != 5:
            return {}
        return {p.get_id(): SETUP_ROLE_NAMES[i] for i, p in enumerate(team.players)}

    # One team's half of the matchup image: its logo, its name, then its
    # roster with the captain marked with a star (on top of
    # _orderedRoster already having put them first). A persistent team
    # always has a logo of its own by now (see _ensureLogo, called on
    # every load). A team with none here is really one of the ad-hoc
    # rosters /make-teams random, /make-teams draft, etc. build on the
    # fly, which never go through that. Rather than draw a bare ring for
    # those, pick a random built-in logo just for this image. Not
    # persisted anywhere (there's no stable row to persist it against),
    # so a rerender can land on a different one, but that's fine for a
    # team with no identity to keep consistent in the first place. Only
    # falls back to the ring if the built-in set itself is unavailable
    # (assets folder missing/empty).
    def _drawMatchupColumn(
        self, image, draw, team, roster, cx, logo_top, name_y, roster_top, name_font, team_font, accent_color,
        column_width_px, use_roles=False,
    ):
        logo_path = team.get_logo_path()
        if logo_path is None or not os.path.isfile(logo_path):
            names = self.listAvailableLogos()
            logo_path = self._resolveLogoPath(random.choice(names)) if names else None

        if logo_path is not None and os.path.isfile(logo_path):
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((MATCHUP_LOGO_SIZE, MATCHUP_LOGO_SIZE), Image.LANCZOS)
            paste_x = int(cx - logo.width / 2)
            paste_y = int(logo_top + (MATCHUP_LOGO_SIZE - logo.height) / 2)
            image.paste(logo, (paste_x, paste_y), logo)
        else:
            draw.ellipse(
                [cx - MATCHUP_LOGO_SIZE / 2, logo_top, cx + MATCHUP_LOGO_SIZE / 2, logo_top + MATCHUP_LOGO_SIZE],
                outline=accent_color, width=BRACKET_LINE_WIDTH
            )

        draw.text((cx, name_y), team.get_name(), font=team_font, fill=accent_color, anchor="ma")

        if not roster:
            draw.text((cx, roster_top), "No players yet", font=name_font, fill=BRACKET_LINE_COLOR, anchor="ma")
            return

        captain = team.get_captain()
        captain_id = captain.get_id() if isinstance(captain, Player) else None
        role_by_id = self._roleAssignments(team, use_roles)
        star_radius = BRACKET_FONT_SIZE / 3

        if role_by_id:
            # Roles showing: left-justify the whole row (icon, then
            # captain star, then name) from the column's own left edge,
            # rather than centering the name and hanging the icon/star
            # off one side of it. This keeps every row's icon at the
            # same x regardless of how long the name is.
            row_left = cx - column_width_px / 2
            for i, player in enumerate(roster):
                is_captain = captain_id is not None and player.get_id() == captain_id
                y = roster_top + i * BRACKET_ROW_HEIGHT
                color = BRACKET_TITLE_COLOR if is_captain else BRACKET_TEXT_COLOR
                content_x = row_left

                role_name = role_by_id.get(player.get_id())
                if role_name is not None:
                    icon = self._roleIconImage(role_name, MATCHUP_ROLE_ICON_SIZE)
                    if icon is not None:
                        # Centered on the text's own rendered bounding
                        # box (not a hardcoded font-size guess), so it
                        # lines up with the glyphs regardless of the
                        # font's own ascent/descent metrics.
                        bbox = draw.textbbox((content_x, y), player.get_name(), font=name_font, anchor="la")
                        text_center_y = (bbox[1] + bbox[3]) / 2
                        icon_y = int(text_center_y - icon.height / 2)
                        image.paste(icon, (int(content_x), icon_y), icon)
                        content_x += icon.width + MATCHUP_ROLE_ICON_GAP

                if is_captain:
                    # A drawn star (same shape _drawChampionLabel uses
                    # for the champion badge), not a "★" text glyph. PIL's
                    # default font doesn't actually have that character,
                    # so it was rendering as a tofu box instead of a
                    # star.
                    star_cx = content_x + star_radius
                    self._drawStar(draw, star_cx, y + BRACKET_FONT_SIZE / 2, star_radius, color)
                    content_x += 2 * star_radius + BRACKET_PADDING / 2

                draw.text((content_x, y), player.get_name(), font=name_font, fill=color, anchor="la")
        else:
            for i, player in enumerate(roster):
                is_captain = captain_id is not None and player.get_id() == captain_id
                y = roster_top + i * BRACKET_ROW_HEIGHT
                color = BRACKET_TITLE_COLOR if is_captain else BRACKET_TEXT_COLOR
                if is_captain:
                    name_width = draw.textlength(player.get_name(), font=name_font)
                    star_cx = cx - name_width / 2 - BRACKET_PADDING / 2 - star_radius
                    self._drawStar(draw, star_cx, y + BRACKET_FONT_SIZE / 2, star_radius, color)
                draw.text((cx, y), player.get_name(), font=name_font, fill=color, anchor="ma")

    # The "vs" matchup graphic posted alongside the existing text
    # announcement whenever a tournament match is created
    # (_postMatchReport, _postReadyCheck): team 1 and team 2's logos and
    # rosters facing off, captain on top for each side (see
    # _orderedRoster). Reuses the exact same canvas/header treatment the
    # bracket images use (_createBracketCanvas, _drawBracketHeader) so
    # this reads as the same product instead of a bolted-on second visual
    # style. `round_label` is this match's place in the tournament (see
    # _matchRoundLabel), the headline, since "what round is this" is the
    # thing someone glancing at the graphic wants first. The
    # tournament/server name and match id are supporting context
    # underneath.
    def _renderMatchupImage(self, match_id, team1, team2, round_label, tournament_name, guild_name, use_roles=False):
        # match_id is None for a casual/ranked (non-tournament) matchup,
        # see _sendMatchupImage, which just omits the "Match #N" part of
        # the subtitle. use_roles is always False for a tournament match
        # (registered teams aren't formed with /make-teams' role balancing
        # at all); only _sendMatchupImage's own caller ever passes True.
        name_font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        team_font = self._loadFont(CHAKRA_PETCH_BOLD, BRACKET_TITLE_FONT_SIZE)
        vs_font = self._loadFont(CHAKRA_PETCH_BOLD, MATCHUP_VS_FONT_SIZE)
        title_font = self._loadFont(CHAKRA_PETCH_BOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        roster1 = self._orderedRoster(team1)
        roster2 = self._orderedRoster(team2)
        rows = max(len(roster1), len(roster2), 1)

        # The captain's star is drawn to the LEFT of their name, outside
        # the name's own text box (see _drawMatchupColumn's star_cx
        # formula). A name centered on cx that's already as wide as the
        # column leaves no room for it, clipping the star (and sometimes
        # the name itself) off the left edge. The name always stays
        # centered on cx regardless of the star, so keeping the column
        # symmetric around cx means matching that one-sided overhang
        # (BRACKET_PADDING/2 + 2x radius, offset to the star's center,
        # plus its own radius again to reach its far edge) on the
        # *other* side too, i.e. doubled.
        captain_star_radius = BRACKET_FONT_SIZE / 3
        captain_star_overhang = BRACKET_PADDING / 2 + 2 * captain_star_radius
        captain_star_allowance = 2 * captain_star_overhang
        # A role icon's own left-to-right footprint (icon + gap). Unlike
        # the captain star above, this is never doubled, since role rows
        # are left-justified from the column's own left edge (see
        # _drawMatchupColumn) rather than centered with the icon hanging
        # off one side of a centered name.
        role_icon_footprint = MATCHUP_ROLE_ICON_SIZE + MATCHUP_ROLE_ICON_GAP
        captain_star_footprint = 2 * captain_star_radius + BRACKET_PADDING / 2

        def column_width(team, roster):
            name_width = measurer.textlength(team.get_name(), font=team_font)
            captain = team.get_captain()
            captain_id = captain.get_id() if isinstance(captain, Player) else None
            role_by_id = self._roleAssignments(team, use_roles)
            roster_width = 0
            for p in roster:
                player_width = measurer.textlength(p.get_name(), font=name_font)
                is_captain_row = captain_id is not None and p.get_id() == captain_id
                role_name = role_by_id.get(p.get_id())
                # Only reserved if there's actually an icon to draw
                # there. A role assigned but its icon file missing must
                # render identically to use_roles=False, not leave dead
                # space.
                has_icon = role_name is not None and self._roleIconImage(role_name, MATCHUP_ROLE_ICON_SIZE) is not None
                if role_by_id:
                    row_width = player_width
                    if has_icon:
                        row_width += role_icon_footprint
                    if is_captain_row:
                        row_width += captain_star_footprint
                else:
                    row_width = player_width
                    if is_captain_row:
                        row_width += captain_star_allowance
                roster_width = max(roster_width, row_width)
            return max(name_width, roster_width, MATCHUP_LOGO_SIZE)

        column_width_px = max(column_width(team1, roster1), column_width(team2, roster2))
        body_width = BRACKET_MARGIN * 2 + column_width_px * 2 + MATCHUP_COLUMN_GAP

        subtitle_parts = [part for part in (tournament_name, guild_name) if part]
        if match_id is not None:
            subtitle_parts.append(f"Match #{match_id}")
        # Plain hyphen, not "•". PIL's default font doesn't have that
        # glyph either (the same issue the roster's captain star just
        # had).
        subtitle = " - ".join(subtitle_parts)

        header_width = self._bracketHeaderWidth(measurer, round_label, subtitle, title_font, subtitle_font)
        width = int(max(body_width, header_width + BRACKET_MARGIN * 2))

        header_y = self._bracketHeaderHeight(subtitle)
        logo_top = header_y + BRACKET_PADDING
        name_y = logo_top + MATCHUP_LOGO_SIZE + BRACKET_PADDING
        roster_top = name_y + BRACKET_TITLE_FONT_SIZE + BRACKET_PADDING
        height = int(roster_top + rows * BRACKET_ROW_HEIGHT + BRACKET_MARGIN)

        image, draw = self._createBracketCanvas(width, height, BRACKET_TITLE_COLOR)
        self._drawBracketHeader(image, draw, round_label, subtitle, BRACKET_TITLE_COLOR, width, bold_title=True)

        left_cx = BRACKET_MARGIN + column_width_px / 2
        right_cx = width - BRACKET_MARGIN - column_width_px / 2
        self._drawMatchupColumn(
            image, draw, team1, roster1, left_cx, logo_top, name_y, roster_top,
            name_font, team_font, TEAM1_ACCENT_COLOR, column_width_px, use_roles=use_roles
        )
        self._drawMatchupColumn(
            image, draw, team2, roster2, right_cx, logo_top, name_y, roster_top,
            name_font, team_font, TEAM2_ACCENT_COLOR, column_width_px, use_roles=use_roles
        )

        divider_x = width / 2
        draw.line(
            [(divider_x, logo_top), (divider_x, height - BRACKET_MARGIN)], fill=BRACKET_LINE_COLOR,
            width=BRACKET_RULE_WIDTH
        )
        draw.text(
            (divider_x, logo_top + MATCHUP_LOGO_SIZE / 2), "VS", font=vs_font, fill=BRACKET_TITLE_COLOR,
            anchor="mm"
        )
        return image

    # Posts the matchup graphic for a casual/ranked game outside a
    # tournament (/start, right as the match actually begins, see
    # sendCurrentMatchupImage), same renderer tournament matches use
    # (_renderMatchupImage), just with no match id or tournament name to
    # put in the subtitle.
    async def _sendMatchupImage(self, channel, team1, team2, label, use_roles=False, guild_id=None):
        # /set matchup-channel redirects the graphic there instead of
        # wherever the roster's Start button was clicked.
        if guild_id is not None:
            channel = self._resolveConfiguredChannel(guild_id, "matchup_channel", channel)
        guild_name = channel.guild.name if channel.guild is not None else None
        image = await asyncio.to_thread(
            self._renderMatchupImage, None, team1, team2, label, None, guild_name, use_roles
        )
        msg = await channel.send(file=self._imageToFile(image, "matchup.png"))
        # Read back by recordResult once this game's result is scored,
        # so it can reply to this same message instead of just posting
        # the result on its own further down the channel.
        if guild_id is not None:
            self.update(guild_id, "matchup_message_id", msg.id)

    # Maps the "mode" stored per-guild (set by /make-teams random,
    # /make-teams draft, /make-teams saved) to the matchup image's
    # headline. Used by /start, which posts the image from whatever's
    # currently loaded rather than knowing for itself how those teams
    # were formed.
    def _matchupLabelForMode(self, mode, game):
        base = {
            "Normal": "Casual Match",
            "Ranked": "Ranked Match",
            "Captains": "Captains Match",
            "Ranked Captains": "Ranked Captains Match",
        }.get(mode, "Match")
        return f"{game} - {base}"

    # The plain-text status that accompanies the bracket images: which
    # team's the (winners-bracket, for double elimination) champion, the
    # losers-bracket champion once it has one, and Grand Finals results
    # once that's started. `guild_id` is only needed for double
    # elimination (Grand Finals state lives in `tournament_matches`, not
    # on `tournament` itself). Omit it and that part is skipped.
    def renderBracketText(self, tournament, guild_id=None):
        rounds = self._bracketRounds(tournament.get_bracket())
        if not rounds:
            return "No bracket has been created yet."

        champion_node = rounds[-1][0]
        champion = self._bracketNodeLabel(champion_node, len(rounds) - 1)
        is_double = tournament.is_double_elimination()

        if not is_double:
            return f"**{tournament.get_name()}**\n\U0001f3c6 **Champion:** {champion}"

        lines = [f"**{tournament.get_name()}**", f"\U0001f3c6 **Winners Bracket Champion:** {champion}"]

        lb_rounds = tournament.get_losers_rounds()
        if lb_rounds:
            if len(lb_rounds) == 1 and lb_rounds[0][0].previous is None:
                lb_champion_node = lb_rounds[0][0]
                lb_name = lb_champion_node.team.get_name() if lb_champion_node.team is not None else "TBD"
                lines.append(f"{lb_name} advances directly to Grand Finals (no losers-bracket match needed).")
            else:
                lb_champion = self._bracketNodeLabel(lb_rounds[-1][0], len(lb_rounds))
                lines.append(f"**Losers Bracket Champion:** {lb_champion}")

        if guild_id is not None:
            finals_text = self._renderGrandFinalsText(guild_id, tournament)
            if finals_text:
                lines.append(finals_text)

        return "\n".join(lines)

    # Grand Finals status, once both brackets have produced a champion to
    # play it. Empty string before then (nothing to show yet). Needs
    # `guild_id` because, unlike everything else this renders, Grand
    # Finals state lives only in `tournament_matches`, not on
    # `tournament` itself (see _startGrandFinals).
    def _renderGrandFinalsText(self, guild_id, tournament):
        wb_rounds = self._bracketRounds(tournament.get_bracket())
        wb_champion = wb_rounds[-1][0].team
        lb_rounds = tournament.get_losers_rounds()
        lb_champion = lb_rounds[-1][0].team if lb_rounds else None

        if wb_champion is None or lb_champion is None:
            return ""

        self.cursor.execute(
            "SELECT roundIndex, team1, team2, winner, state FROM tournament_matches "
            "WHERE guildId=? AND bracketType='finals' ORDER BY roundIndex",
            (guild_id,)
        )
        rows = self.cursor.fetchall()

        lines = [
            "**Grand Finals**",
            f"{wb_champion.get_name()} (winners bracket) vs {lb_champion.get_name()} (losers bracket)",
        ]
        for round_index, team1_ser, team2_ser, winner, state in rows:
            if state != "RESOLVED":
                continue
            team1, team2 = Team(), Team()
            team1.deserializeTeam(team1_ser)
            team2.deserializeTeam(team2_ser)
            winning_team = team1 if winner == 1 else team2
            label = "Game 1" if round_index == 0 else "Bracket Reset"
            lines.append(f"{label}: **{winning_team.get_name()}** won")

        champion_name = self._tournamentChampionName(guild_id, tournament)
        if champion_name is not None:
            lines.append(f"\n\U0001f3c6 **Tournament Champion:** {champion_name}")

        return "\n".join(lines)

    # Posts a tournament's status: the text from renderBracketText plus
    # one image attachment per bracket (renderBracketImages), a single
    # message, single API call, no matter the bracket's size. Images
    # render fully inline in Discord with no truncation the way an
    # oversized text message or a big file-attachment preview would
    # have. The Grand Finals image, if Grand Finals has actually been
    # played, follows as its own separate message right after. It's a
    # distinct enough stage that bundling it into the same message as
    # the two full brackets buried it instead of standing out.
    async def _sendBracketText(self, channel, tournament, guild_id=None):
        guild_name = channel.guild.name if channel.guild is not None else None
        bracket_files = await asyncio.to_thread(self.renderBracketImages, tournament, guild_name)
        await channel.send(self.renderBracketText(tournament, guild_id), files=bracket_files)
        if guild_id is not None:
            # The DB read half runs here, not inside the offloaded thread
            # (self.cursor is thread-affined); only the pure drawing that
            # depends on it is actually offloaded.
            finals_inputs = self._grandFinalsRenderInputs(guild_id, tournament)
            if finals_inputs is not None:
                finals_image = await asyncio.to_thread(
                    self._buildGrandFinalsImage, tournament, *finals_inputs, guild_name
                )
                await channel.send(files=[self._imageToFile(finals_image, "grand_finals.png")])

    async def printBracketHelper(self, ctx):
        tournament = self.getTournament(ctx.guild.id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server. Use /tournament create first.", ephemeral=True
            )
            return

        # Rendering the whole bracket (Pillow, possibly several rounds
        # and teams) can take longer than Discord's ~3 second ack
        # window. Post a placeholder immediately and edit it in place
        # with the real text/images once they're ready, rather than
        # risking "This interaction failed" while the render is still
        # running.
        await ctx.response.send_message("Creating bracket, please wait...")
        bracket_files = await asyncio.to_thread(self.renderBracketImages, tournament, ctx.guild.name)
        await ctx.edit_original_response(
            content=self.renderBracketText(tournament, ctx.guild.id), attachments=bracket_files
        )
        # The DB read half runs here, not inside the offloaded thread
        # (self.cursor is thread-affined). Only the pure drawing that
        # depends on it is actually offloaded.
        finals_inputs = self._grandFinalsRenderInputs(ctx.guild.id, tournament)
        if finals_inputs is not None:
            finals_image = await asyncio.to_thread(
                self._buildGrandFinalsImage, tournament, *finals_inputs, ctx.guild.name
            )
            await ctx.channel.send(files=[self._imageToFile(finals_image, "grand_finals.png")])

    # "Where in the tournament is this match", the matchup graphic's
    # headline. Winners-bracket rounds get the same
    # "Quarterfinals"/"Round of 8"-style names the bracket image uses
    # (_roundName, which needs the bracket's own top_round_index to know
    # how far from the final this round is). The losers bracket has no
    # such clean naming (see _losersRoundName's own comment), so it's
    # just numbered. Grand Finals is its own two-state thing (the first
    # game, or the bracket-reset decider).
    def _matchRoundLabel(self, tournament, round_index, bracket_type):
        if bracket_type == "losers":
            return self._losersRoundName(round_index)
        if bracket_type == "finals":
            return "Grand Finals" if round_index == 0 else "Grand Finals - Bracket Reset"
        top_round_index = len(self._bracketRounds(tournament.get_bracket())) - 1
        return self._roundName(round_index, top_round_index)

    # Posts the "react when ready" prompt for a QUEUED sequential match.
    async def _postReadyCheck(self, guild_id, match_id, channel):
        self.cursor.execute(
            "SELECT team1, team2, roundIndex, bracketType FROM tournament_matches WHERE id=?", (match_id,)
        )
        team1_ser, team2_ser, round_index, bracket_type = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        # /set matchup-channel redirects this graphic (and, since every
        # later step in this match's own life - the ready click, the
        # betting/report cycle, the bracket update - is threaded through
        # wherever the ready-check interaction itself came from) the rest
        # of the match's own postings too, there instead of wherever the
        # round happened to start.
        channel = self._resolveConfiguredChannel(guild_id, "matchup_channel", channel)
        tournament = self.getTournament(guild_id)
        round_label = f"{self._currentGame(guild_id)} - {self._matchRoundLabel(tournament, round_index, bracket_type)}"
        guild_name = channel.guild.name if channel.guild is not None else None
        matchup_image = await asyncio.to_thread(
            self._renderMatchupImage, match_id, team1, team2, round_label, tournament.get_name(), guild_name
        )
        matchup_file = self._imageToFile(matchup_image, f"match_{match_id}_vs.png")
        msg = await channel.send(
            f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} - press Ready below "
            "when ready to play (either captain)!",
            file=matchup_file,
            view=TournamentReadyView(self),
        )

        self.cursor.execute(
            "UPDATE tournament_matches SET state='PENDING_READY', messageId=?, channelId=? WHERE id=?",
            (msg.id, channel.id, match_id)
        )
        self.db.commit()

    # Posts the "who won" prompt for a simultaneous-mode match, no ready
    # check, just a direct report same as a normal game. Betting on it
    # (alongside every other match in the same round) is opened separately,
    # see _openConcurrentTournamentBetting, called once after every match
    # in the round has its own report prompt posted.
    async def _postMatchReport(self, guild_id, match_id, channel):
        self.cursor.execute(
            "SELECT team1, team2, roundIndex, bracketType FROM tournament_matches WHERE id=?", (match_id,)
        )
        team1_ser, team2_ser, round_index, bracket_type = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        # /set matchup-channel redirects this graphic there instead of
        # wherever the round happened to start; _resolveTournamentMatch
        # (reached from the report click on this same message) then just
        # follows the same channel through for the match's own result.
        channel = self._resolveConfiguredChannel(guild_id, "matchup_channel", channel)
        tournament = self.getTournament(guild_id)
        round_label = f"{self._currentGame(guild_id)} - {self._matchRoundLabel(tournament, round_index, bracket_type)}"
        guild_name = channel.guild.name if channel.guild is not None else None
        matchup_image = await asyncio.to_thread(
            self._renderMatchupImage, match_id, team1, team2, round_label, tournament.get_name(), guild_name
        )
        matchup_file = self._imageToFile(matchup_image, f"match_{match_id}_vs.png")
        msg = await channel.send(
            f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} - press the winning "
            "team's own button below.",
            file=matchup_file,
            view=TournamentMatchReportView(self, team1.get_name(), team2.get_name()),
        )

        self.cursor.execute(
            "UPDATE tournament_matches SET state='AWAITING_RESULT', messageId=?, channelId=? WHERE id=?",
            (msg.id, channel.id, match_id)
        )
        self.db.commit()

    # Opens one shared betting window covering every match in a
    # just-posted simultaneous-mode round. Unlike _openBetting's
    # single-game singleton (one bet per user per GUILD, tracked on the
    # `servers` row), this is keyed by matchId in `tournament_wagers`.
    # Several matches can be open at once, and a user can bet on more
    # than one of them, something the old wagers table's PRIMARY
    # KEY(guildId, userId) couldn't represent at all. Duration is the
    # guild's configured per-match base (_getBettingTimerSeconds) times
    # how many matches are in the round, capped so a generous base times
    # a big bracket's first round can't leave betting open for an
    # unreasonable stretch.
    async def _openConcurrentTournamentBetting(self, guild_id, match_ids, channel):
        # /set wager-channel redirects this round-wide notice (and the
        # "now closed" one below) there instead of wherever the round
        # happened to start - independent of /set matchup-channel, which
        # only affects each match's own graphic/report message
        # (_postMatchReport). Resolved once here and threaded through to
        # _concurrentBettingTimer so the open/closed pair always lands in
        # the same channel as each other.
        channel = self._resolveConfiguredChannel(guild_id, "wager_channel", channel)
        base = self._getBettingTimerSeconds(guild_id)
        duration = min(base * len(match_ids), MAX_CONCURRENT_BETTING_SECONDS)
        match_list = ", ".join(f"#{match_id}" for match_id in match_ids)
        plural = "es" if len(match_ids) != 1 else ""

        msg = await channel.send(
            f"\U0001f3b2 Betting is open on {len(match_ids)} match{plural} ({match_list})! Use "
            f"`/wager team <amount> <team> match_id:<id>` to bet on one. Betting closes in {duration} seconds."
        )
        # Shared across every match in this round (see the column's own
        # comment in bot.py); read back and deleted once the round's last
        # match resolves, whichever of _resolveTournamentMatch/
        # _resolveLosersMatch/_resolveFinalsMatch that ends up being.
        placeholders = ",".join("?" * len(match_ids))
        self.cursor.execute(
            f"UPDATE tournament_matches SET roundBettingMessageId=? WHERE id IN ({placeholders})",
            [msg.id] + list(match_ids)
        )
        self.db.commit()
        asyncio.create_task(self._concurrentBettingTimer(match_ids, channel, duration))

    # No cancellation path (unlike cancelBettingHelper for the singleton
    # flow). Tournament rounds have no CANCEL_GAME_EMOJI-equivalent to
    # cancel one mid-flight. If every match in the round has already
    # resolved by the time this fires, the UPDATE below just touches
    # already-RESOLVED rows harmlessly. Each match's own wagers were
    # already settled and cleared at resolution time regardless of what
    # this timer does.
    async def _concurrentBettingTimer(self, match_ids, channel, duration):
        await asyncio.sleep(duration)
        placeholders = ",".join("?" * len(match_ids))
        self.cursor.execute(
            f"UPDATE tournament_matches SET bettingClosed=1 WHERE id IN ({placeholders})", match_ids
        )
        self.db.commit()
        msg = await channel.send("\U0001f512 Betting is now closed for this round's matches!")
        self.cursor.execute(
            f"UPDATE tournament_matches SET roundBettingClosedMessageId=? WHERE id IN ({placeholders})",
            [msg.id] + list(match_ids)
        )
        self.db.commit()

    # Deletes one simultaneous-mode round's shared "Betting is open on N
    # matches"/"Betting is now closed" messages, once every match in it
    # has resolved. Called from _resolveTournamentMatch/
    # _resolveLosersMatch/_resolveFinalsMatch, right where each already
    # knows the round is done. A no-op for sequential mode (which never
    # sets these columns at all) or a round that never had betting on it
    # in the first place.
    async def _deleteRoundBettingMessages(self, guild_id, round_index, bracket_type, channel):
        self.cursor.execute(
            "SELECT roundBettingMessageId, roundBettingClosedMessageId FROM tournament_matches "
            "WHERE guildId=? AND roundIndex=? AND bracketType=? "
            "AND (roundBettingMessageId IS NOT NULL OR roundBettingClosedMessageId IS NOT NULL) LIMIT 1",
            (guild_id, round_index, bracket_type)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        open_id, closed_id = row
        # These messages live wherever _openConcurrentTournamentBetting
        # actually posted them (the wager-channel-resolved channel, which
        # /set matchup-channel can now leave pointed somewhere other than
        # `channel`, the match/round's own thread), not necessarily
        # `channel` itself.
        betting_channel = self._resolveConfiguredChannel(guild_id, "wager_channel", channel)
        await self._deleteMessageIdSafely(betting_channel, open_id)
        await self._deleteMessageIdSafely(betting_channel, closed_id)
        self.cursor.execute(
            "UPDATE tournament_matches SET roundBettingMessageId=NULL, roundBettingClosedMessageId=NULL "
            "WHERE guildId=? AND roundIndex=? AND bracketType=?",
            (guild_id, round_index, bracket_type)
        )
        self.db.commit()

    # What fraction of the losing pool gets raked off before it's split
    # among winners, see MAX_IMBALANCE_RAKE. 0 at an even 50/50 split
    # (winning_pool == losing_pool), scaling linearly up to
    # MAX_IMBALANCE_RAKE at a maximally lopsided one (losing_pool -> 0
    # relative to winning_pool). Never negative, since a pool where the
    # eventual WINNERS were actually the minority (a real upset)
    # shouldn't be taxed at all. Shared by computeGameDeltas and
    # _matchWagerDeltas, the two places that otherwise duplicate this
    # exact pari-mutuel split.
    def _imbalanceRakeFraction(self, winning_pool, losing_pool):
        total_pool = winning_pool + losing_pool
        if total_pool <= 0:
            return 0.0
        favorite_share = winning_pool / total_pool
        imbalance = max(0.0, (favorite_share - 0.5) / 0.5)
        return MAX_IMBALANCE_RAKE * imbalance

    # Pure computation of one match's pari-mutuel payouts (winners split
    # the losing pool, minus an imbalance rake (_imbalanceRakeFraction),
    # proportional to their own wager, on top of getting it back) as a
    # deltas dict in the exact shape applyGameDeltas expects. Shared by
    # _settleMatchWagers (the normal path) and
    # _correctTournamentMatchHelper, which reverses this against the
    # original winner and reapplies it against the corrected one.
    def _matchWagerDeltas(self, wagers, winning_team):
        deltas = {}

        def bump(user_id, username, **kwargs):
            entry = deltas.setdefault(user_id, {
                "username": username, "balance": 0, "wins": 0, "losses": 0,
                "gold_wagered": 0, "gold_won": 0, "gold_lost": 0,
                "game_wins": 0, "game_losses": 0, "ranked_wins": 0, "ranked_losses": 0, "elo": 0,
            })
            for key, value in kwargs.items():
                entry[key] += value

        winning_bets = [w for w in wagers if w[2] == winning_team]
        losing_bets = [w for w in wagers if w[2] != winning_team]
        winning_pool = sum(w[3] for w in winning_bets)
        losing_pool = sum(w[3] for w in losing_bets)
        raked_losing_pool = losing_pool * (1 - self._imbalanceRakeFraction(winning_pool, losing_pool))

        for user_id, username, _team, amount in winning_bets:
            payout = round(amount + (amount / winning_pool) * raked_losing_pool) if winning_pool > 0 else amount
            bump(user_id, username, balance=payout, wins=1, gold_wagered=amount, gold_won=payout - amount)
        for user_id, username, _team, amount in losing_bets:
            bump(user_id, username, losses=1, gold_wagered=amount, gold_lost=amount)

        return deltas

    # Real-money settlement for one tournament match's wagers, via the
    # same deltas/applyGameDeltas machinery /set correct-winner's
    # casual-game path uses. Scoped to exactly this match_id's rows, so
    # settling one concurrent match never touches another's still-open
    # bets.
    async def _settleMatchWagers(self, guild_id, match_id, winning_team, channel):
        self.cursor.execute(
            "SELECT userId, username, team, amount FROM tournament_wagers WHERE matchId=?", (match_id,)
        )
        wagers = self.cursor.fetchall()
        if not wagers:
            return

        deltas = self._matchWagerDeltas(wagers, winning_team)
        newly_unlocked = self.applyGameDeltas(guild_id, deltas)

        lines = [f"\U0001f4b0 **Match #{match_id} payouts:**"]
        for user_id, username, team, amount in wagers:
            if team == winning_team:
                lines.append(f"{username} won {deltas[user_id]['balance']} gold (bet {amount})")

        # Snapshotted before the rows disappear, see
        # _correctTournamentMatchHelper, which needs to know exactly who
        # bet what on THIS match if it's ever corrected after the fact,
        # once tournament_wagers itself is gone.
        self.cursor.execute(
            "UPDATE tournament_matches SET settledWagers=? WHERE id=?",
            (json.dumps(wagers), match_id)
        )
        self.cursor.execute("DELETE FROM tournament_wagers WHERE matchId=?", (match_id,))
        self.db.commit()

        if len(lines) > 1:
            await channel.send("\n".join(lines))
        await self._announceAchievements(channel, newly_unlocked)

    # Queues every real pairing in `round_index` of the WINNERS bracket as
    # a tournament_matches row (byes, a pairing where only one side has a
    # team, auto-advance immediately with no match at all, and produce no
    # loser to drop into the losers bracket) and kicks the round off: the
    # first match's ready-check for sequential, or every match's report
    # prompt at once for simultaneous. Recurses forward through bye-only
    # rounds. Once the winners bracket itself is done, a
    # double-elimination tournament moves on to the losers bracket
    # instead of ending outright.
    # --- Interleaved losers-bracket scheduling ------------------------------
    # Only consulted when tournament.get_losers_bracket_timing() ==
    # "interleaved" (see /tournament create-bracket). The default
    # "after_winners" timing never calls any of this, and _startRound/
    # _startLosersRound just walk their own round list start to finish
    # exactly as they always have.

    # The smallest winners round_index with no tournament_matches row at
    # all yet, "hasn't been started". A round that was skipped entirely
    # (every pairing a bye, only possible for round 0, but handled
    # generally) never gets a row, so this jumps straight past it once a
    # LATER round has actually started.
    def _nextUnstartedWinnersRoundIndex(self, guild_id):
        self.cursor.execute(
            "SELECT MAX(roundIndex) FROM tournament_matches WHERE guildId=? AND bracketType='winners'",
            (guild_id,)
        )
        max_started = self.cursor.fetchone()[0]
        return 0 if max_started is None else max_started + 1

    # Whether winners round_index is fully done: it has real matches and
    # every one is RESOLVED, or it never had any (an all-bye round) and
    # progression has already moved past it.
    def _winnersRoundFullyResolved(self, guild_id, round_index):
        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? AND bracketType='winners'",
            (guild_id, round_index)
        )
        if self.cursor.fetchone()[0] == 0:
            return self._nextUnstartedWinnersRoundIndex(guild_id) > round_index
        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? "
            "AND bracketType='winners' AND state != 'RESOLVED'",
            (guild_id, round_index)
        )
        return self.cursor.fetchone()[0] == 0

    # Same pair of checks, for the losers bracket.
    def _nextUnstartedLosersRoundIndex(self, guild_id, lb_rounds):
        if not lb_rounds:
            return None
        self.cursor.execute(
            "SELECT MAX(roundIndex) FROM tournament_matches WHERE guildId=? AND bracketType='losers'",
            (guild_id,)
        )
        max_started = self.cursor.fetchone()[0]
        next_ri = 0 if max_started is None else max_started + 1
        return next_ri if next_ri < len(lb_rounds) else None

    def _losersRoundFullyResolved(self, guild_id, round_index, lb_rounds):
        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? AND bracketType='losers'",
            (guild_id, round_index)
        )
        if self.cursor.fetchone()[0] == 0:
            next_ri = self._nextUnstartedLosersRoundIndex(guild_id, lb_rounds)
            return next_ri is None or next_ri > round_index
        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? "
            "AND bracketType='losers' AND state != 'RESOLVED'",
            (guild_id, round_index)
        )
        return self.cursor.fetchone()[0] == 0

    # The next losers round that's both unstarted AND actually unlocked:
    # its own predecessor losers round (if any) is fully resolved, and
    # (per tournament.get_losers_bracket_wb_dependency()) the winners
    # round it depends on (if any) is fully resolved too. None if nothing
    # is ready to start right now.
    def _readyUnstartedLosersRoundIndex(self, guild_id, tournament):
        lb_rounds = tournament.get_losers_rounds()
        next_ri = self._nextUnstartedLosersRoundIndex(guild_id, lb_rounds)
        if next_ri is None:
            return None
        if next_ri > 0 and not self._losersRoundFullyResolved(guild_id, next_ri - 1, lb_rounds):
            return None
        wb_dependency = tournament.get_losers_bracket_wb_dependency()
        dep = wb_dependency[next_ri] if next_ri < len(wb_dependency) else None
        if dep is not None and not self._winnersRoundFullyResolved(guild_id, dep):
            return None
        return next_ri

    # The shared "what should play next" decision for interleaved timing,
    # called any time a round (winners or losers) finishes. A losers
    # round that's now unlocked always takes priority (this is what
    # "winners await the previous round's losers" means). Otherwise the
    # winners bracket advances if it still has a round to play.
    # Otherwise both brackets have nothing left to START (something may
    # still be mid-play, which will call back in here once it resolves)
    # and Grand Finals gets a shot. Safe to call unconditionally: it
    # silently no-ops without both champions decided.
    async def _advanceInterleavedTournament(self, guild_id, tournament, mode, channel):
        ready_ri = self._readyUnstartedLosersRoundIndex(guild_id, tournament)
        if ready_ri is not None:
            await self._startLosersRound(guild_id, tournament, ready_ri, mode, channel)
            return

        wb_rounds = self._bracketRounds(tournament.get_bracket())
        champion_decided = wb_rounds[-1][0].team is not None
        if not champion_decided:
            next_wb_ri = self._nextUnstartedWinnersRoundIndex(guild_id)
            await self._startRound(guild_id, tournament, next_wb_ri, mode, channel)
            return

        await self._startGrandFinals(guild_id, tournament, mode, channel)

    async def _startRound(self, guild_id, tournament, round_index, mode, channel):
        rounds = self._bracketRounds(tournament.get_bracket())
        interleaved = tournament.is_double_elimination() and tournament.get_losers_bracket_timing() == "interleaved"

        # Interleaved timing: a losers round that's now unlocked plays
        # before winners moves on to round_index, "winners await the
        # previous round's losers." Not checked for the terminal
        # round_index itself; see the branch below for what interleaved
        # mode does once winners is actually done.
        if interleaved and round_index < len(rounds) - 1:
            ready_ri = self._readyUnstartedLosersRoundIndex(guild_id, tournament)
            if ready_ri is not None:
                await self._startLosersRound(guild_id, tournament, ready_ri, mode, channel)
                return

        if round_index >= len(rounds) - 1:
            champion = rounds[-1][0]
            name = champion.team.get_name() if champion.team is not None else "Unknown"
            if tournament.is_double_elimination():
                await channel.send(
                    f"\U0001f3c6 **{tournament.get_name()}** winners bracket complete! "
                    f"**{name}** advances to Grand Finals undefeated."
                )
                if interleaved:
                    # Some losers rounds may already be underway (or even
                    # finished). Let the shared scheduler pick up
                    # wherever that's at, rather than blindly restarting
                    # from round 0.
                    await self._advanceInterleavedTournament(guild_id, tournament, mode, channel)
                else:
                    await self._startLosersRound(guild_id, tournament, 0, mode, channel)
            else:
                await channel.send(f"\U0001f3c6 **{tournament.get_name()}** is complete! Champion: **{name}**")
                await self._postTournamentLeaderboard(channel, guild_id, tournament)
                if champion.team is not None:
                    await self._announceAchievements(
                        channel, self._grantTournamentChampionAchievement(guild_id, champion.team)
                    )
            return

        round_nodes = rounds[round_index]
        real_pairs = []
        for i in range(0, len(round_nodes), 2):
            a, b = round_nodes[i], round_nodes[i + 1]
            if a.team is not None and b.team is not None:
                real_pairs.append((a, b))
            elif a.team is not None or b.team is not None:
                winner_node = a if a.team is not None else b
                if winner_node.next is not None:
                    winner_node.next.team = winner_node.team

        self.saveTournament(guild_id, tournament)

        if not real_pairs:
            await self._startRound(guild_id, tournament, round_index + 1, mode, channel)
            return

        bracket = tournament.get_bracket()
        match_ids = []
        for a, b in real_pairs:
            node_index = bracket.index(a)
            self.cursor.execute(
                "INSERT INTO tournament_matches"
                "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, "
                "bracketType, game) "
                "VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'winners', ?)",
                (guild_id, round_index, node_index, a.team.serializeTeam(), b.team.serializeTeam(),
                 mode, channel.id, self._currentGame(guild_id))
            )
            self.db.commit()
            match_ids.append(self.cursor.lastrowid)

        plural = "es" if len(match_ids) != 1 else ""
        await channel.send(f"__Round {round_index + 1}__ - {len(match_ids)} match{plural} to play.")

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_ids[0], channel)
        else:
            for match_id in match_ids:
                await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, match_ids, channel)

    # Mirrors _startRound above, but for the LOSERS bracket: `round_nodes`
    # here are the round's RESULT nodes (see buildLosersBracket), so each
    # match's two participants are reached via `result_node.previous` /
    # `.previous.opponent` instead of iterating a flat pairs list
    # directly. A losers-bracket "bye" happens when one feeder never got
    # a team at all (a winners-bracket bye pairing produces no loser to
    # drop down), same auto-advance treatment as a real bye. If BOTH
    # feeders are empty (two winners-bracket byes landed in the same
    # losers-bracket pairing), that slot just never fills, same as the
    # equivalent winners-bracket edge case. Once every losers round has
    # been played, moves on to Grand Finals.
    async def _startLosersRound(self, guild_id, tournament, round_index, mode, channel):
        lb_rounds = tournament.get_losers_rounds()

        if not lb_rounds or round_index >= len(lb_rounds):
            await self._startGrandFinals(guild_id, tournament, mode, channel)
            return

        if tournament.get_losers_bracket_timing() == "interleaved":
            wb_dependency = tournament.get_losers_bracket_wb_dependency()
            dep = wb_dependency[round_index] if round_index < len(wb_dependency) else None
            if dep is not None and not self._winnersRoundFullyResolved(guild_id, dep):
                # Not unlocked yet. Pause the losers bracket here and let
                # the winners bracket continue instead. This exact round
                # gets retried (via _advanceInterleavedTournament) once
                # its dependency resolves.
                await self._advanceInterleavedTournament(guild_id, tournament, mode, channel)
                return

        round_nodes = lb_rounds[round_index]
        real_pairs = []
        for result_node in round_nodes:
            a = result_node.previous
            b = a.opponent if a is not None else None
            if a is not None and a.team is not None and b is not None and b.team is not None:
                real_pairs.append((a, b))
            elif a is not None and a.team is not None:
                result_node.team = a.team
            elif b is not None and b.team is not None:
                result_node.team = b.team

        self.saveTournament(guild_id, tournament)

        if not real_pairs:
            await self._startLosersRound(guild_id, tournament, round_index + 1, mode, channel)
            return

        losers_nodes = tournament.get_losers_bracket_nodes()
        match_ids = []
        for a, b in real_pairs:
            node_index = losers_nodes.index(a)
            self.cursor.execute(
                "INSERT INTO tournament_matches"
                "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, "
                "bracketType, game) "
                "VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'losers', ?)",
                (guild_id, round_index, node_index, a.team.serializeTeam(), b.team.serializeTeam(),
                 mode, channel.id, self._currentGame(guild_id))
            )
            self.db.commit()
            match_ids.append(self.cursor.lastrowid)

        plural = "es" if len(match_ids) != 1 else ""
        await channel.send(
            f"__Losers Bracket Round {round_index + 1}__ - {len(match_ids)} match{plural} to play."
        )

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_ids[0], channel)
        else:
            for match_id in match_ids:
                await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, match_ids, channel)

    # Posts the winners-bracket champion vs losers-bracket champion match.
    # `reset` is True for the second, decider match that's only played
    # if the losers-bracket champion wins game 1. At that point both
    # sides have exactly one loss, so a single game settles it either
    # way.
    async def _startGrandFinals(self, guild_id, tournament, mode, channel, reset=False):
        wb_rounds = self._bracketRounds(tournament.get_bracket())
        wb_champion = wb_rounds[-1][0].team
        lb_rounds = tournament.get_losers_rounds()
        lb_champion = lb_rounds[-1][0].team if lb_rounds else None

        if wb_champion is None or lb_champion is None:
            return

        if not reset:
            await channel.send(
                f"\U0001f3c6 **Grand Finals:** {wb_champion.get_name()} (undefeated) vs "
                f"{lb_champion.get_name()} (one loss) - {lb_champion.get_name()} must win twice "
                f"to take the title."
            )

        self.cursor.execute(
            "INSERT INTO tournament_matches"
            "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, "
            "bracketType, game) "
            "VALUES(?, ?, -1, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'finals', ?)",
            (guild_id, 1 if reset else 0, wb_champion.serializeTeam(), lb_champion.serializeTeam(),
             mode, channel.id, self._currentGame(guild_id))
        )
        self.db.commit()
        match_id = self.cursor.lastrowid

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_id, channel)
        else:
            await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, [match_id], channel)

    # The overall tournament champion's name once EVERYTHING (including
    # Grand Finals, and a bracket reset if one was needed) has resolved.
    # None if there's still something left to play. Single elimination
    # has no Grand Finals stage, so its own bracket is the whole story.
    def _tournamentChampionName(self, guild_id, tournament):
        if not tournament.is_double_elimination():
            rounds = self._bracketRounds(tournament.get_bracket())
            champion = rounds[-1][0]
            return champion.team.get_name() if champion.team is not None else None

        self.cursor.execute(
            "SELECT roundIndex, team1, team2, winner FROM tournament_matches "
            "WHERE guildId=? AND bracketType='finals' AND state='RESOLVED' "
            "ORDER BY roundIndex DESC LIMIT 1",
            (guild_id,)
        )
        row = self.cursor.fetchone()
        if row is None:
            return None
        round_index, team1_ser, team2_ser, winner = row

        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        winning_team = team1 if winner == 1 else team2

        if round_index == 1:
            # The decider match, whoever wins it is champion outright.
            return winning_team.get_name()

        # round_index == 0: only actually over if the winners-bracket
        # champion won game 1 outright. If the losers-bracket champion
        # won instead, a reset match is needed, and if one had already
        # been played, the query above would have returned that row
        # (roundIndex 1) instead of this one.
        # Compared by name rather than id: team names are guaranteed
        # unique per guild (enforced by /team create), whereas
        # .get_id() is only ever set once a team's been persisted
        # through _saveNewTeam, a guarantee this comparison shouldn't
        # have to lean on.
        wb_rounds = self._bracketRounds(tournament.get_bracket())
        wb_champion = wb_rounds[-1][0].team
        if wb_champion is not None and winning_team.get_name() == wb_champion.get_name():
            return winning_team.get_name()
        return None

    # Starts (or restarts, if the whole tournament is idle) the current
    # round. Refuses to run while a round is already in progress, or once
    # a champion has already been decided. Only ever kicks off winners
    # bracket round 0; a double-elimination tournament's losers bracket
    # and Grand Finals play out on their own from there, driven entirely
    # by match resolution (_resolveTournamentMatch), no repeat command
    # needed.
    async def startTournamentHelper(self, ctx, mode):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server. Use /tournament create first.", ephemeral=True
            )
            return

        bracket = tournament.get_bracket()
        if not bracket:
            await ctx.response.send_message(
                "No bracket has been created yet. Use /tournament create-bracket first.", ephemeral=True
            )
            return

        champion_name = self._tournamentChampionName(guild_id, tournament)
        if champion_name is not None:
            await ctx.response.send_message(
                f"**{tournament.get_name()}** is already finished; **{champion_name}** is the champion!",
                ephemeral=True,
            )
            return

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND state != 'RESOLVED'", (guild_id,)
        )
        if self.cursor.fetchone()[0] > 0:
            await ctx.response.send_message(
                "This tournament's current round is already in progress.", ephemeral=True
            )
            return

        await ctx.response.send_message(
            f"Starting **{tournament.get_name()}**: {mode} mode."
        )
        await self._startRound(guild_id, tournament, 0, mode, ctx.channel)

    # TournamentReadyView's Ready button callback, re-derives which match
    # (and whether the clicker is actually one of its captains) from the
    # interaction itself, since the view is a single shared persistent
    # instance with nothing match-specific stored on it.
    async def _handleReadyClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, team1, team2 FROM tournament_matches "
            "WHERE guildId=? AND messageId=? AND state='PENDING_READY'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This match isn't waiting on a ready check.", ephemeral=True
            )
            return
        match_id, team1_ser, team2_ser = row
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        if not (self.isTeamCaptain(team1, interaction.user.id) or self.isTeamCaptain(team2, interaction.user.id)):
            await interaction.response.send_message(
                "Only one of this match's captains can mark it ready.", ephemeral=True
            )
            return

        # BUG-PRONE PATTERN AVOIDED: flip state before anything async below,
        # so a double-click can't begin the same match twice.
        self.cursor.execute("UPDATE tournament_matches SET state='AWAITING_RESULT' WHERE id=?", (match_id,))
        self.db.commit()

        channel = interaction.channel

        # Route through the exact same team-game cycle a casual/ranked game
        # uses: team1/team2 + betting + the winner-report message.
        # active_tournament_match_id is what tells recordResult (once that
        # cycle resolves) to come back here and advance the bracket.
        team1.set_id(1)
        team2.set_id(2)
        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "original_channel", "")
        self.update(guild_id, "is_ranked", 0)
        self.update(guild_id, "game", self._currentGame(guild_id))
        self.update(guild_id, "active_tournament_match_id", match_id)

        await interaction.response.defer()
        await self._clearMessageButtons(interaction.message)
        await channel.send(f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} is starting!")
        await self._openBetting(guild_id, channel)

    # Only flips a match's state back to AWAITING_RESULT if it's still
    # CONFIRMING (i.e. nothing else already resolved it a different way in
    # the meantime), a conditional UPDATE rather than a
    # select-then-update, so this is atomic and just no-ops instead of
    # stomping a state that's moved on.
    def _restoreTournamentMatchAwaitingResult(self, match_id):
        self.cursor.execute(
            "UPDATE tournament_matches SET state='AWAITING_RESULT' WHERE id=? AND state='CONFIRMING'",
            (match_id,)
        )
        self.db.commit()

    # TournamentMatchReportView's Team 1/Team 2 button callback. A pick no
    # longer resolves the match immediately; it posts a
    # ConfirmTournamentMatchReportView instead (Confirm actually calls
    # _resolveTournamentMatch; Cancel/timeout restores the match via
    # _restoreTournamentMatchAwaitingResult so its buttons work again),
    # matching WinnerReportView/ConfirmWinnerReportView's two-step shape.
    async def _handleTournamentMatchReportClick(self, interaction, winning_team):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, team1, team2 FROM tournament_matches WHERE guildId=? AND messageId=? "
            "AND state='AWAITING_RESULT' AND mode='simultaneous'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This match has already been reported or is no longer pending.", ephemeral=True
            )
            return
        match_id, team1_ser, team2_ser = row

        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        rostered_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        if not (interaction.user.guild_permissions.manage_guild or interaction.user.id in rostered_ids):
            await interaction.response.send_message(
                "Only a player in this match, or a member with the Manage Server permission, can report a winner.",
                ephemeral=True,
            )
            return

        # BUG-PRONE PATTERN AVOIDED: flip out of AWAITING_RESULT before
        # anything async below, so a second near-simultaneous click on
        # this same match message can't also pass the check above and
        # post a second confirmation for it. Also closes betting on this
        # match right away (same reasoning _handleWinnerReportPick's own
        # betting_state close has). Otherwise /wager team match_id: stays
        # open for the whole confirmation window, letting someone bet on
        # whichever side just got reported before it's even confirmed.
        # Left closed even if the report is later cancelled.
        self.cursor.execute(
            "UPDATE tournament_matches SET state='CONFIRMING', bettingClosed=1 WHERE id=?", (match_id,)
        )
        self.db.commit()

        name = team1.get_name() if winning_team == 1 else team2.get_name()

        view = ConfirmTournamentMatchReportView(
            self, guild_id, match_id, winning_team, interaction.channel_id, report_message=interaction.message
        )
        await interaction.response.send_message(
            f"**{name}** reported as the winner of Match #{match_id}. Confirm to finalize it, or "
            "Cancel to report again.",
            view=view,
        )
        view.message = await interaction.original_response()

    # Records the winner, advances the bracket (propagating the winning
    # team into the shared "next" node), prints the updated bracket, and
    # either starts the next queued match (sequential, round not done),
    # or moves on to the next round once every match in this one has
    # resolved. Shared by both modes, reached via recordResult's hook for
    # sequential, or directly from a result reaction for simultaneous.
    # Dispatches to the losers-bracket / Grand Finals equivalents below
    # for anything that isn't a winners-bracket match.
    async def _resolveTournamentMatch(self, guild_id, match_id, winning_team, channel_id):
        self.cursor.execute(
            "SELECT roundIndex, nodeIndex, mode, state, bracketType, game FROM tournament_matches WHERE id=?",
            (match_id,)
        )
        row = self.cursor.fetchone()
        if row is None or row[3] == "RESOLVED":
            return
        round_index, node_index, mode, _, bracket_type, game = row

        # BUG-PRONE PATTERN AVOIDED: flip to RESOLVED before anything async
        # below, so a concurrent/duplicate call can't process this twice.
        self.cursor.execute(
            "UPDATE tournament_matches SET state='RESOLVED', winner=? WHERE id=?", (winning_team, match_id)
        )
        self.db.commit()

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        tournament = self.getTournament(guild_id)
        if tournament is None:
            return

        if bracket_type == "finals":
            await self._resolveFinalsMatch(
                guild_id, tournament, match_id, round_index, winning_team, mode, channel, game
            )
            return

        if bracket_type == "losers":
            await self._resolveLosersMatch(
                guild_id, tournament, match_id, round_index, node_index, winning_team, mode, channel, game
            )
            return

        bracket = tournament.get_bracket()
        node_a = bracket[node_index]
        node_b = node_a.opponent
        winner_node = node_a if winning_team == 1 else node_b
        loser_node = node_b if winning_team == 1 else node_a
        if node_a.next is not None:
            node_a.next.team = winner_node.team
            # Only a REAL match (both sides had a team) has an actual
            # loser to drop into the losers bracket; a bye pairing's
            # "winner" never played anyone, so node_a.next.drop_to (if
            # this is a double-elimination tournament) is simply left
            # unfilled, same as the equivalent losers-bracket slot.
            if loser_node.team is not None:
                node_a.next.loser = loser_node.team
                if node_a.next.drop_to is not None:
                    node_a.next.drop_to.team = loser_node.team
        self.saveTournament(guild_id, tournament)
        self._recordMatchResult(guild_id, winner_node.team, loser_node.team, game)

        matchup_message = await self._fetchMatchupMessage(guild_id, channel, match_id=match_id)
        await channel.send(
            f"**Match #{match_id} result:** {winner_node.team.get_name()} wins!", reference=matchup_message
        )
        await self._settleMatchWagers(guild_id, match_id, winning_team, channel)
        await self._sendBracketText(channel, tournament, guild_id)

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? "
            "AND bracketType='winners' AND state != 'RESOLVED'",
            (guild_id, round_index)
        )
        if self.cursor.fetchone()[0] > 0:
            if mode == "sequential":
                self.cursor.execute(
                    "SELECT id FROM tournament_matches WHERE guildId=? AND roundIndex=? "
                    "AND bracketType='winners' AND state='QUEUED' ORDER BY id LIMIT 1",
                    (guild_id, round_index)
                )
                next_row = self.cursor.fetchone()
                if next_row is not None:
                    await self._postReadyCheck(guild_id, next_row[0], channel)
            return

        # Every match in this round is in. Announce the round ending and
        # show the freshly-updated bracket before moving on. _startRound
        # (below) is what actually announces the champion once there's
        # no round left to start. This is purely the "round N is over"
        # transition message, distinct from the per-match update above.
        # No sleeps or blocking waits anywhere in this chain: reactions
        # are handled by discord.py as their own tasks, so a round
        # transition (even one that recurses through several bye rounds)
        # never blocks other users from placing bets or running other
        # commands meanwhile.
        await self._deleteRoundBettingMessages(guild_id, round_index, "winners", channel)
        await channel.send(f"\U0001f3c1 **Round {round_index + 1} has ended!**")
        await self._sendBracketText(channel, tournament, guild_id)

        await self._startRound(guild_id, tournament, round_index + 1, mode, channel)

    # Mirrors the winners-bracket tail of _resolveTournamentMatch above,
    # for a losers-bracket match: propagate the winner into `.next`,
    # announce, and either advance to the round's next queued match or
    # (once the round's fully resolved) move on to the next losers round
    # (or Grand Finals, once there isn't one). A losers-bracket loser is
    # simply eliminated, nothing further to propagate for them.
    async def _resolveLosersMatch(
        self, guild_id, tournament, match_id, round_index, node_index, winning_team, mode, channel, game
    ):
        losers_nodes = tournament.get_losers_bracket_nodes()
        node_a = losers_nodes[node_index]
        node_b = node_a.opponent
        winner_node = node_a if winning_team == 1 else node_b
        loser_node = node_b if winning_team == 1 else node_a
        if node_a.next is not None:
            node_a.next.team = winner_node.team
        self.saveTournament(guild_id, tournament)
        self._recordMatchResult(guild_id, winner_node.team, loser_node.team, game)

        matchup_message = await self._fetchMatchupMessage(guild_id, channel, match_id=match_id)
        await channel.send(
            f"**Match #{match_id} result (losers bracket):** {winner_node.team.get_name()} wins!",
            reference=matchup_message,
        )
        await self._settleMatchWagers(guild_id, match_id, winning_team, channel)
        await self._sendBracketText(channel, tournament, guild_id)

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? "
            "AND bracketType='losers' AND state != 'RESOLVED'",
            (guild_id, round_index)
        )
        if self.cursor.fetchone()[0] > 0:
            if mode == "sequential":
                self.cursor.execute(
                    "SELECT id FROM tournament_matches WHERE guildId=? AND roundIndex=? "
                    "AND bracketType='losers' AND state='QUEUED' ORDER BY id LIMIT 1",
                    (guild_id, round_index)
                )
                next_row = self.cursor.fetchone()
                if next_row is not None:
                    await self._postReadyCheck(guild_id, next_row[0], channel)
            return

        await self._deleteRoundBettingMessages(guild_id, round_index, "losers", channel)
        await channel.send(f"\U0001f3c1 **Losers Bracket Round {round_index + 1} has ended!**")
        await self._sendBracketText(channel, tournament, guild_id)

        await self._startLosersRound(guild_id, tournament, round_index + 1, mode, channel)

    # Resolves a Grand Finals match. roundIndex 0 is the first game
    # (winners-bracket champion vs losers-bracket champion). If the
    # losers-bracket champion wins that one, both sides now have exactly
    # one loss, so a second, decider match (roundIndex 1) is posted
    # instead of ending the tournament. Whoever wins THAT one is champion
    # no matter what.
    async def _resolveFinalsMatch(
        self, guild_id, tournament, match_id, round_index, winning_team, mode, channel, game
    ):
        self.cursor.execute("SELECT team1, team2 FROM tournament_matches WHERE id=?", (match_id,))
        team1_ser, team2_ser = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        winner = team1 if winning_team == 1 else team2
        loser = team2 if winning_team == 1 else team1
        # Recorded here regardless of whether this is game 1 or the
        # decider. Both are real, played matches, even when game 1's
        # result just leads into a reset rather than ending the
        # tournament.
        self._recordMatchResult(guild_id, winner, loser, game)
        await self._settleMatchWagers(guild_id, match_id, winning_team, channel)
        # Unlike winners/losers rounds (several matches, only "done" once
        # every one resolves), a finals round is always exactly this one
        # match, so it's fully resolved the moment it is.
        await self._deleteRoundBettingMessages(guild_id, round_index, "finals", channel)

        matchup_message = await self._fetchMatchupMessage(guild_id, channel, match_id=match_id)

        if round_index == 0:
            # Compared by name, not id, see _tournamentChampionName.
            wb_rounds = self._bracketRounds(tournament.get_bracket())
            wb_champion = wb_rounds[-1][0].team
            if wb_champion is not None and winner.get_name() != wb_champion.get_name():
                await channel.send(
                    f"**Grand Finals result:** {winner.get_name()} wins! Since the winners-bracket "
                    f"champion has now lost once too, one final decider match settles the tournament.",
                    reference=matchup_message,
                )
                await self._startGrandFinals(guild_id, tournament, mode, channel, reset=True)
                return

        await channel.send(
            f"\U0001f3c6 **{tournament.get_name()}** is complete! Champion: **{winner.get_name()}**",
            reference=matchup_message,
        )
        # Every other match-resolution path (_resolveTournamentMatch,
        # _resolveLosersMatch) reprints the bracket after it updates.
        # Grand Finals resolving is exactly the same kind of update, and
        # skipping it here meant the last bracket image anyone saw was
        # whatever the losers bracket looked like before Grand Finals
        # even started, never showing the actual finals result.
        # _sendBracketText already knows how to post the Grand Finals
        # image too, once _renderGrandFinalsImage finds a resolved
        # finals match.
        await self._sendBracketText(channel, tournament, guild_id)
        await self._postTournamentLeaderboard(channel, guild_id, tournament)
        await self._announceAchievements(channel, self._grantTournamentChampionAchievement(guild_id, winner))

    # Whether ANY winners-bracket match in the round after `round_index`
    # has already been queued, the signal _correctTournamentMatchHelper (and
    # ConfirmTournamentMatchCorrectionView's own re-check at Confirm time)
    # both refuse to correct past, since the bracket's already moved on by
    # then and a correction could no longer be safely re-propagated.
    def _nextTournamentRoundStarted(self, guild_id, round_index):
        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? AND bracketType='winners'",
            (guild_id, round_index + 1)
        )
        return self.cursor.fetchone()[0] > 0

    # The actual bracket-propagation/wager-reversal work behind /set
    # correct-winner's match_id path, with no messaging of its own -
    # shared by nothing else, but kept separate from
    # _correctTournamentMatchHelper so ConfirmTournamentMatchCorrectionView's
    # Confirm button can re-verify the match is still in the exact state
    # the warning was built from (see its own comment) before calling this.
    # None if the tournament itself is somehow gone by the time Confirm is
    # pressed. Otherwise (result_text, tournament, newly_unlocked).
    def _applyTournamentMatchCorrection(self, guild_id, match_id, node_index, correct_team):
        self.cursor.execute(
            "SELECT winner, settledWagers FROM tournament_matches WHERE guildId=? AND id=?",
            (guild_id, match_id)
        )
        winner, settled_wagers_json = self.cursor.fetchone()

        tournament = self.getTournament(guild_id)
        if tournament is None:
            return None

        bracket = tournament.get_bracket()
        node_a = bracket[node_index]
        node_b = node_a.opponent
        correct_winner_node = node_a if correct_team == 1 else node_b
        if node_a.next is not None:
            node_a.next.team = correct_winner_node.team
        self.saveTournament(guild_id, tournament)

        self.cursor.execute("UPDATE tournament_matches SET winner=? WHERE id=?", (correct_team, match_id))

        wager_note = ""
        newly_unlocked = []
        if settled_wagers_json:
            wagers = json.loads(settled_wagers_json)
            self.applyGameDeltas(guild_id, self._matchWagerDeltas(wagers, winner), sign=-1)
            newly_unlocked = self.applyGameDeltas(guild_id, self._matchWagerDeltas(wagers, correct_team))
            wager_note = " Bet payouts on this match have been reversed and reapplied."

        self.db.commit()

        result_text = f"Match #{match_id} corrected: **{correct_winner_node.team.get_name()}** actually won.{wager_note}"
        return result_text, tournament, newly_unlocked

    # /set correct-winner's match_id path: posts a confirmation for fixing
    # a specific tournament match's recorded winner rather than
    # re-propagating the bracket (and, if anyone had money on it, reversing
    # the payouts _settleMatchWagers already made against the wrong winner
    # and reapplying them against the right one, using the settledWagers
    # snapshot _settleMatchWagers leaves behind, since tournament_wagers'
    # own rows are long gone by the time a match is old enough to need
    # correcting) immediately - the same "a real payout/bracket change
    # shouldn't hinge on one click" reasoning every other winner-report
    # flow in this file already follows. Independent of the guild-wide
    # last_result correction (which only ever covers the single
    # most-recently-resolved team game). Refuses once the next round has
    # already started, rather than risk silently corrupting a bracket
    # that's already moved on; ConfirmTournamentMatchCorrectionView
    # re-checks that (and the match's own state) again at Confirm time,
    # in case either changed while the prompt was sitting there.
    async def _correctTournamentMatchHelper(self, ctx, match_id, correct_team):
        guild_id = ctx.guild.id

        self.cursor.execute(
            "SELECT roundIndex, nodeIndex, state, winner, bracketType, settledWagers "
            "FROM tournament_matches WHERE guildId=? AND id=?",
            (guild_id, match_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await ctx.response.send_message(
                f"No tournament match with id {match_id} in this server.", ephemeral=True
            )
            return
        round_index, node_index, state, winner, bracket_type, _settled_wagers_json = row

        if bracket_type != "winners":
            await ctx.response.send_message(
                f"Match #{match_id} is a {'losers bracket' if bracket_type == 'losers' else 'Grand Finals'} "
                f"match. Correcting those isn't supported yet.",
                ephemeral=True,
            )
            return

        if state != "RESOLVED":
            await ctx.response.send_message(f"Match #{match_id} hasn't been resolved yet.", ephemeral=True)
            return

        if winner == correct_team:
            await ctx.response.send_message(
                f"Match #{match_id} is already recorded as Team {correct_team}.", ephemeral=True
            )
            return

        if self._nextTournamentRoundStarted(guild_id, round_index):
            await ctx.response.send_message(
                f"Can't correct Match #{match_id}; the next round has already started.", ephemeral=True
            )
            return

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message("This server's tournament no longer exists.", ephemeral=True)
            return

        bracket = tournament.get_bracket()
        node_a = bracket[node_index]
        node_b = node_a.opponent
        correct_winner_node = node_a if correct_team == 1 else node_b

        view = ConfirmTournamentMatchCorrectionView(
            self, guild_id, ctx.user.id, match_id, round_index, node_index, winner, correct_team
        )
        await ctx.response.send_message(
            f"This will correct Match #{match_id}: **{correct_winner_node.team.get_name()}** actually won "
            f"(previously recorded as Team {winner}). Any bet payouts on this match will be reversed and "
            "reapplied. This can't be undone.",
            view=view,
        )
        view.message = await ctx.original_response()

    # ---------------- Persistent teams ----------------

    # (team_id, Team) for the named team in this guild, or None. Team
    # names are unique per guild case-insensitively (enforced by
    # createTeamHelper/teamRenameHelper), so this is always at most one
    # row. COLLATE NOCASE means "red" finds "Red" here too, not just an
    # exact-case match.
    def getTeamRow(self, guild_id, name):
        self.cursor.execute(
            "SELECT id, data FROM teams WHERE guildId=? AND name = ? COLLATE NOCASE", (guild_id, name)
        )
        row = self.cursor.fetchone()
        if row is None:
            return None
        team_id, data = row
        team = Team()
        team.deserializeTeam(data)
        self._ensureLogo(team_id, team)
        self._hydrateTeamGameRecord(guild_id, team_id, team)
        return team_id, team

    def getTeamById(self, guild_id, team_id):
        self.cursor.execute(
            "SELECT data FROM teams WHERE guildId=? AND id=?", (guild_id, team_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return None
        team = Team()
        team.deserializeTeam(row[0])
        self._ensureLogo(team_id, team)
        self._hydrateTeamGameRecord(guild_id, team_id, team)
        return team

    def getTeamsForGuild(self, guild_id):
        self.cursor.execute("SELECT id, data FROM teams WHERE guildId=?", (guild_id,))
        teams = []
        for team_id, data in self.cursor.fetchall():
            team = Team()
            team.deserializeTeam(data)
            self._ensureLogo(team_id, team)
            self._hydrateTeamGameRecord(guild_id, team_id, team)
            teams.append((team_id, team))
        return teams

    # Every team in the guild `user_id` is a rostered player on (captain
    # or not), what /team lookup pages through. Sorted by team_id so
    # paging stays stable across clicks even though this is recomputed
    # fresh from the DB on every page flip (see
    # _handleMyTeamsPageClick), the same way getLeaderboardEntries is
    # recomputed fresh rather than snapshotted.
    def getTeamsForPlayer(self, guild_id, user_id):
        teams = self.getTeamsForGuild(guild_id)
        mine = [
            (team_id, team) for team_id, team in teams
            if any(player.get_id() == user_id for player in team.get_players())
        ]
        return sorted(mine, key=lambda entry: entry[0])

    # Narrower than getTeamsForPlayer above: only teams `user_id`
    # actually captains, not just any team they're rostered on. Backs
    # the autocomplete on team-name params for commands that require
    # being that team's captain (/team set, /team rename, /team delete,
    # /team invite, /tournament register), same "only suggest what's
    # actually usable" idea cardTitleAutocomplete's own comment
    # describes, just scoped to captaincy instead of unlocks. Sorted by
    # team_id for the same stability reason getTeamsForPlayer is.
    def getTeamsCaptainedBy(self, guild_id, user_id):
        teams = self.getTeamsForGuild(guild_id)
        captained = [
            (team_id, team) for team_id, team in teams if self.isTeamCaptain(team, user_id)
        ]
        return sorted(captained, key=lambda entry: entry[0])

    # ---------------- /team list ----------------

    # Every team in the guild, filtered/sorted for /team list. `search`
    # is a case-insensitive substring match on the team's name.
    # `recruiting_only` keeps only teams that HAVE a target size (set
    # via /team create) and haven't reached it yet. A team with no
    # target size is an ephemeral game-formation roster, never
    # "recruiting" in the sense this filter means. `member_ids` (a set,
    # possibly empty/None) keeps only teams whose roster is a superset
    # of it. Every given member has to be on the SAME team, not just any
    # of them, so this is how to find "the team with both Alice and Bob"
    # rather than a broad "any team either of them happens to be on"
    # search. `sort`/`order` are always applied, even filtered down to
    # nothing, so a page-flip on an empty result still has a stable (if
    # empty) list to re-render instead of erroring.
    def _filterAndSortTeams(self, guild_id, search, recruiting_only, sort, order, member_ids=None):
        teams = self.getTeamsForGuild(guild_id)

        if search:
            needle = search.lower()
            teams = [(team_id, team) for team_id, team in teams if needle in team.get_name().lower()]

        if recruiting_only:
            teams = [
                (team_id, team) for team_id, team in teams
                if team.get_team_size() is not None and team.get_size() < team.get_team_size()
            ]

        if member_ids:
            teams = [
                (team_id, team) for team_id, team in teams
                if member_ids <= {p.get_id() for p in team.get_players()}
            ]

        def sort_key(entry):
            _, team = entry
            if sort == "wins":
                return team.wins
            if sort == "losses":
                return team.losses
            if sort == "roster_size":
                return team.get_size()
            if sort == "win_rate":
                games = team.wins + team.losses
                return (team.wins / games) if games > 0 else -1
            return team.get_name().lower()

        return sorted(teams, key=sort_key, reverse=(order == "desc"))

    def _teamListPageCount(self, teams):
        return max(1, -(-len(teams) // LEADERBOARD_PAGE_SIZE))

    def _renderTeamListEmbed(self, guild_name, teams_sorted, search, recruiting_only, sort, order, page, member_names=None):
        total_pages = self._teamListPageCount(teams_sorted)
        start = page * LEADERBOARD_PAGE_SIZE
        page_teams = teams_sorted[start:start + LEADERBOARD_PAGE_SIZE]

        lines = []
        for i, (_, team) in enumerate(page_teams):
            rank = start + i + 1
            target_size = team.get_team_size()
            roster_size = f"{team.get_size()}/{target_size}" if target_size is not None else str(team.get_size())
            games = team.wins + team.losses
            win_rate = f"{(team.wins / games) * 100:.1f}%" if games > 0 else "N/A"
            lines.append(
                f"**#{rank}.** {team.get_name()} - {roster_size} players | "
                f"{team.wins}W-{team.losses}L ({win_rate})"
            )

        embed = discord.Embed(
            title=f"\U0001f4cb {guild_name} Teams",
            description="\n".join(lines) if lines else "No teams match those filters.",
            color=discord.Color.gold(),
        )
        active_filters = []
        if search:
            active_filters.append(f'search "{search}"')
        if recruiting_only:
            active_filters.append("recruiting only")
        if member_names:
            active_filters.append(f"with {', '.join(member_names)}")
        filter_text = f" · {', '.join(active_filters)}" if active_filters else ""
        order_label = "Ascending" if order == "asc" else "Descending"
        embed.set_footer(
            text=f"Page {page + 1}/{total_pages} · Sorted by {TEAM_LIST_SORT_LABELS[sort]} "
                 f"({order_label}){filter_text}"
        )
        return embed

    # Posts the first page with its own TeamListPagingView, same pattern
    # as leaderboardHelper/myTeamsHelper. Clicking a button
    # (_handleTeamListPageClick) edits this same message. `cards`
    # switches to the exact same one-team-full-stats-card-per-page
    # rendering /team lookup uses (_renderMyTeamsEmbed/_myTeamsPageCount
    # take a plain list of (team_id, team) tuples and don't care where
    # it came from), just sourced from every team matching
    # search/recruiting_only/sort/order/members instead of one player's
    # own teams. /team lookup for the whole server, in effect. `members`
    # (a list of up to 5 discord.Member, possibly empty) is stored as
    # two parallel CSV columns rather than re-derived on every page
    # flip: memberIds is what _filterAndSortTeams actually filters on,
    # memberNames is purely the footer's display text. Resolving live
    # Discord members back from bare stored ids on every click would be
    # needless API calls for something that never changes for the life
    # of this message.
    async def teamListHelper(self, ctx, search, recruiting_only, sort, order, cards=False, members=None):
        guild_id = ctx.guild.id
        members = members or []
        member_ids = {m.id for m in members}
        member_names = [m.display_name for m in members]

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order, member_ids)
        if not teams_sorted:
            message = "No teams have been created in this server yet!" \
                if not (search or recruiting_only or member_ids) else "No teams match those filters."
            await ctx.response.send_message(message, ephemeral=True)
            return

        if cards:
            view = TeamListPagingView(self, cards=True, card_shown=False)
            embed, file = self._renderMyTeamsEmbed(teams_sorted, page=0)
            if file is not None:
                await ctx.response.send_message(embed=embed, file=file, view=view)
            else:
                await ctx.response.send_message(embed=embed, view=view)
        else:
            view = TeamListPagingView(self)
            embed = self._renderTeamListEmbed(
                ctx.guild.name, teams_sorted, search, recruiting_only, sort, order, page=0,
                member_names=member_names,
            )
            await ctx.response.send_message(embed=embed, view=view)
        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT OR REPLACE INTO team_list_views"
            "(messageId, guildId, channelId, search, recruitingOnly, sort, sort_order, page, cards, cardShown, "
            "memberIds, memberNames) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)",
            (
                msg.id, guild_id, ctx.channel.id, search, int(recruiting_only), sort, order, int(cards),
                ",".join(str(i) for i in member_ids), ",".join(member_names),
            )
        )
        self.db.commit()

    # The trading-card counterpart to _renderMyTeamsEmbed: same
    # (teams_sorted, page) -> (embed, file) shape, but the team's actual
    # trading-card image (_renderTeamCardImage) instead of its plain
    # stats embed, with the same "Team X/N" footer so paging still has
    # something to orient by while looking at cards instead of stats.
    # Offloaded to a thread the same way every other Pillow render is.
    # Shared by _handleTeamListShowCardClick and _handleTeamListPageClick's
    # own cardShown branch, rather than each rebuilding this
    # independently.
    async def _renderTeamListCardEmbed(self, guild_name, teams_sorted, page):
        _team_id, team = teams_sorted[page]
        card_image = await asyncio.to_thread(self._renderTeamCardImage, guild_name, team)
        file = self._imageToFile(card_image, "team_card.png")
        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        embed.set_footer(text=f"Team {page + 1}/{len(teams_sorted)}")
        return embed, file

    # TeamListPagingView's button callback, no-ops (with a plain
    # ephemeral note) unless the interaction's message still matches an
    # active /team list page view. cardShown (only meaningful in cards
    # mode) carries across the flip, so paging while looking at a team's
    # trading card keeps showing trading cards, and paging on the plain
    # stats card keeps showing stats.
    async def _handleTeamListPageClick(self, interaction, direction=None, target_page=None):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT search, recruitingOnly, sort, sort_order, page, cards, cardShown, memberIds, memberNames "
            "FROM team_list_views WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This list is no longer live.", ephemeral=True)
            return
        search, recruiting_only, sort, order, page, cards, card_shown, member_ids_raw, member_names_raw = row
        recruiting_only = bool(recruiting_only)
        cards = bool(cards)
        card_shown = bool(card_shown)
        member_ids = {int(x) for x in member_ids_raw.split(",") if x} if member_ids_raw else set()
        member_names = member_names_raw.split(",") if member_names_raw else []

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order, member_ids)
        if cards and not teams_sorted:
            # _renderTeamListEmbed tolerates an empty list gracefully (its
            # own "No teams match those filters" text), but
            # _renderMyTeamsEmbed/_renderTeamListCardEmbed both index
            # straight into teams[page] and would raise on an empty list,
            # same guard _handleMyTeamsPageClick already needs for the
            # same reason.
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(teams_sorted) if cards else self._teamListPageCount(teams_sorted)
        page = min(page, total_pages - 1)
        new_page = self._computeNewPage(direction, page, total_pages, target_page)

        if new_page == page:
            await interaction.response.defer()
            return

        if cards:
            guild_name = interaction.guild.name if interaction.guild is not None else ""
            if card_shown:
                embed, file = await self._renderTeamListCardEmbed(guild_name, teams_sorted, new_page)
            else:
                embed, file = self._renderMyTeamsEmbed(teams_sorted, new_page)
            if file is not None:
                await interaction.response.edit_message(embed=embed, attachments=[file])
            else:
                await interaction.response.edit_message(embed=embed, attachments=[])
        else:
            guild_name = interaction.guild.name if interaction.guild is not None else ""
            embed = self._renderTeamListEmbed(
                guild_name, teams_sorted, search, recruiting_only, sort, order, new_page,
                member_names=member_names,
            )
            await interaction.response.edit_message(embed=embed)

        self.cursor.execute(
            "UPDATE team_list_views SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, interaction.message.id)
        )
        self.db.commit()

    # TeamListPagingView's Page # button: see _handleLeaderboardJumpClick,
    # same "no longer live"/empty-cards guards _handleTeamListPageClick
    # needs.
    async def _handleTeamListJumpClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT search, recruitingOnly, sort, sort_order, cards, memberIds "
            "FROM team_list_views WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This list is no longer live.", ephemeral=True)
            return
        search, recruiting_only, sort, order, cards, member_ids_raw = row
        recruiting_only = bool(recruiting_only)
        cards = bool(cards)
        member_ids = {int(x) for x in member_ids_raw.split(",") if x} if member_ids_raw else set()

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order, member_ids)
        if cards and not teams_sorted:
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(teams_sorted) if cards else self._teamListPageCount(teams_sorted)
        await interaction.response.send_modal(
            _PageJumpModal(self, "_handleTeamListPageClick", total_pages)
        )

    # TeamListPagingView's Card button callback (cards mode only), swaps
    # the currently-paged team's plain stats card for its actual trading
    # card. Re-derives which team is "current" from the view's own stored
    # filter/sort/page rather than trusting a fixed team_id, since /team-
    # list cards:true pages through many teams (unlike /team stats' own
    # Card button, which only ever has the one team it was posted for).
    async def _handleTeamListShowCardClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT search, recruitingOnly, sort, sort_order, page, memberIds FROM team_list_views "
            "WHERE guildId=? AND messageId=? AND cards=1 AND cardShown=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This team list view is no longer live.", ephemeral=True)
            return
        search, recruiting_only, sort, order, page, member_ids_raw = row
        recruiting_only = bool(recruiting_only)
        member_ids = {int(x) for x in member_ids_raw.split(",") if x} if member_ids_raw else set()

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order, member_ids)
        if not teams_sorted:
            await interaction.response.send_message("This team list view is no longer live.", ephemeral=True)
            return
        page = min(page, len(teams_sorted) - 1)

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        embed, file = await self._renderTeamListCardEmbed(guild_name, teams_sorted, page)
        await message.edit(
            embed=embed, attachments=[file], view=TeamListPagingView(self, cards=True, card_shown=True)
        )
        self.cursor.execute(
            "UPDATE team_list_views SET cardShown=1 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # TeamListPagingView's Back button callback, the reverse swap.
    async def _handleTeamListReturnClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT search, recruitingOnly, sort, sort_order, page, memberIds FROM team_list_views "
            "WHERE guildId=? AND messageId=? AND cards=1 AND cardShown=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This team list view is no longer live.", ephemeral=True)
            return
        search, recruiting_only, sort, order, page, member_ids_raw = row
        recruiting_only = bool(recruiting_only)
        member_ids = {int(x) for x in member_ids_raw.split(",") if x} if member_ids_raw else set()

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order, member_ids)
        if not teams_sorted:
            await interaction.response.send_message("This team list view is no longer live.", ephemeral=True)
            return
        page = min(page, len(teams_sorted) - 1)

        await interaction.response.defer()
        embed, file = self._renderMyTeamsEmbed(teams_sorted, page)
        edit_kwargs = {"embed": embed, "attachments": [file] if file is not None else []}
        edit_kwargs["view"] = TeamListPagingView(self, cards=True, card_shown=False)
        await message.edit(**edit_kwargs)
        self.cursor.execute(
            "UPDATE team_list_views SET cardShown=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # Every built-in logo's name (filename minus extension), e.g.
    # "Demacia" for assets/clash-logos/Demacia.png. What /team set's
    # logo autocomplete offers and validates against. Empty if the
    # folder isn't there at all (e.g. a dev checkout that never fetched
    # it) rather than raising.
    def listAvailableLogos(self):
        if not os.path.isdir(TEAM_LOGO_DIR):
            return []
        names = [
            os.path.splitext(f)[0] for f in os.listdir(TEAM_LOGO_DIR)
            if os.path.isfile(os.path.join(TEAM_LOGO_DIR, f))
        ]
        return sorted(names)

    # Case-insensitive lookup from a logo's name back to its file path,
    # None if `name` isn't one of listAvailableLogos()'s names.
    def _resolveLogoPath(self, name):
        if not os.path.isdir(TEAM_LOGO_DIR):
            return None
        target = name.strip().lower()
        for filename in os.listdir(TEAM_LOGO_DIR):
            stem, _ext = os.path.splitext(filename)
            if stem.lower() == target:
                return os.path.join(TEAM_LOGO_DIR, filename)
        return None

    # A team with no logo yet gets a random built-in one, persisted
    # right away. Called everywhere a team is loaded (not just created),
    # so a team that predates this feature self-heals into having a
    # logo the next time it's touched instead of needing a one-off
    # migration. No-op if the assets folder is missing/empty: a team
    # just stays logo-less rather than this raising.
    def _ensureLogo(self, team_id, team):
        if team.get_logo_path() is not None:
            return
        names = self.listAvailableLogos()
        if not names:
            return
        team.set_logo_path(self._resolveLogoPath(random.choice(names)))
        self.updateTeamData(team_id, team)

    def updateTeamData(self, team_id, team):
        self.cursor.execute("UPDATE teams SET data=? WHERE id=?", (team.serializeTeam(), team_id))
        self.db.commit()

    # Per-game team win/loss record - see /set game. Split out of
    # Team.wins/Team.losses (now frozen/unused, see bot.py's
    # team_game_stats comment) the same way game_stats split off of
    # economy.elo, so a team's record only reflects matches played under
    # the same game.
    def ensureTeamGameStatsRow(self, guild_id, team_id, game):
        self.cursor.execute(
            "INSERT OR IGNORE INTO team_game_stats(guildId, teamId, game, wins, losses) "
            "VALUES(?, ?, ?, 0, 0)",
            (guild_id, team_id, game)
        )
        self.db.commit()

    def getTeamGameStat(self, guild_id, team_id, game, column):
        self.cursor.execute(
            f"SELECT {column} FROM team_game_stats WHERE guildId=? AND teamId=? AND game=?",
            (guild_id, team_id, game)
        )
        row = self.cursor.fetchone()
        return row[0] if row is not None else None

    def _recordTeamGameResult(self, guild_id, team_id, game, won):
        self.ensureTeamGameStatsRow(guild_id, team_id, game)
        column = "wins" if won else "losses"
        self.cursor.execute(
            f"UPDATE team_game_stats SET {column} = {column} + 1 WHERE guildId=? AND teamId=? AND game=?",
            (guild_id, team_id, game)
        )
        self.db.commit()

    # Overwrites a freshly-deserialized Team's in-memory wins/losses with
    # its team_game_stats record for the guild's current game, so every
    # display site that already reads team.wins/team.losses (the team
    # list, /team stats, the trading card) shows the per-game record
    # without needing its own game-scoping logic. Only ever touches the
    # in-memory object - never persisted back through updateTeamData - so
    # Team.wins/Team.losses stays whatever was embedded in `teams`.data
    # (frozen, unused going forward) on disk. Called from every loader
    # that deserializes a Team (getTeamRow/getTeamById/getTeamsForGuild)
    # right after _ensureLogo, the other per-load enrichment step.
    def _hydrateTeamGameRecord(self, guild_id, team_id, team):
        game = self._currentGame(guild_id)
        team.wins = self.getTeamGameStat(guild_id, team_id, game, "wins") or 0
        team.losses = self.getTeamGameStat(guild_id, team_id, game, "losses") or 0

    # Records one played tournament match against each side's PERSISTENT
    # team record (the one /team list, /team lookup, and /team stats
    # actually read), called from every match-resolution path (winners
    # bracket, losers bracket, Grand Finals). Looked up by name rather
    # than trusting the bracket node's own embedded Team object: that's
    # just a snapshot from whenever the bracket was last serialized, not
    # the live, incrementally-updated row, so writing straight back
    # through it would silently lose whatever wins/losses had already
    # accumulated since. Either side can be None (a bracket node with no
    # team, shouldn't happen for a match that was ever actually queued,
    # but this is cheap insurance) or simply not a persisted team at
    # all, in which case there's nothing to record and this is a no-op.
    # `game` is whichever game the match was actually played under
    # (tournament_matches.game, stamped at match-creation time), not
    # necessarily the server's current game if it's been switched since.
    def _recordMatchResult(self, guild_id, winner_team, loser_team, game):
        for team, won in ((winner_team, True), (loser_team, False)):
            if team is None:
                continue
            result = self.getTeamRow(guild_id, team.get_name())
            if result is None:
                continue
            team_id, _persisted_team = result
            self._recordTeamGameResult(guild_id, team_id, game, won)

    def isTeamCaptain(self, team, user_id):
        captain = team.get_captain()
        return isinstance(captain, Player) and captain.get_id() == user_id

    # Inserts `team`, then stamps the row's own autoincrement id back
    # onto the Team object and re-saves it. The DB row IS the team's id,
    # so it can't be known until after the INSERT.
    def _saveNewTeam(self, guild_id, team):
        self.cursor.execute(
            "INSERT INTO teams(guildId, name, data) VALUES(?, ?, ?)",
            (guild_id, team.get_name(), team.serializeTeam())
        )
        self.db.commit()
        team_id = self.cursor.lastrowid
        team.set_id(team_id)
        self.updateTeamData(team_id, team)
        self._ensureLogo(team_id, team)
        return team_id

    # Creates a new persistent team with the caller as its captain. Unlike
    # the ephemeral team1/team2 a game gets, this one sticks around across
    # sessions and is what /tournament create's roster registration and
    # /team invite work against.
    async def createTeamHelper(self, ctx, name, team_size, captain_member=None):
        guild_id = ctx.guild.id

        if team_size <= 0:
            await ctx.response.send_message("Team size must be greater than 0.", ephemeral=True)
            return

        if self.getTeamRow(guild_id, name) is not None:
            await ctx.response.send_message(
                f"A team named **{name}** already exists in this server.", ephemeral=True
            )
            return

        captain_user = captain_member if captain_member is not None else ctx.user

        team = Team()
        team.set_name(name)
        team.set_team_size(team_size)
        captain = Player(captain_user.id, captain_user.name)
        team.add_player(captain)
        team.set_captain(captain)

        self._saveNewTeam(guild_id, team)

        await ctx.response.send_message(
            f"Team **{name}** created! {captain_user.mention} is the captain, looking for {team_size} player"
            f"{'s' if team_size != 1 else ''} total."
        )

    # Snapshots whichever side of the LAST completed/loaded game
    # (whatever is currently sitting in servers.team1/team2, the same
    # "last game" roster /make-teams repeat re-posts) the caller
    # actually played on, as a brand new persistent team with the
    # caller as captain. Only reads team1/team2, never touches them, so
    # the ephemeral roster is untouched and can still be reused/reported
    # on normally afterward.
    async def saveTeamHelper(self, ctx, team, name):
        guild_id = ctx.guild.id
        column = "team1" if team == 1 else "team2"

        serialized = self.get(guild_id, column)
        if not serialized:
            await ctx.response.send_message(
                f"There's no Team {team} from the last game to save.", ephemeral=True
            )
            return

        roster = Team()
        roster.deserializeTeam(serialized)

        if not any(p.get_id() == ctx.user.id for p in roster.get_players()):
            await ctx.response.send_message(
                f"You weren't part of Team {team} in the last game, so you can't save it.", ephemeral=True
            )
            return

        if self.getTeamRow(guild_id, name) is not None:
            await ctx.response.send_message(
                f"A team named **{name}** already exists in this server.", ephemeral=True
            )
            return

        saved = Team()
        saved.set_name(name)
        captain = None
        for player in roster.get_players():
            newPlayer = Player(player.get_id(), player.get_name())
            saved.add_player(newPlayer)
            if newPlayer.get_id() == ctx.user.id:
                captain = newPlayer
        saved.set_captain(captain)
        # Snapshotted as already full, not "recruiting" (see
        # _filterAndSortTeams' recruiting_only), since this is a copy of
        # a roster that was already complete, not a fresh call for
        # players.
        saved.set_team_size(saved.get_size())

        self._saveNewTeam(guild_id, saved)

        await ctx.response.send_message(
            f"Team **{name}** saved from Team {team}'s last game roster! {ctx.user.mention} is the captain, "
            f"{saved.get_size()} player{'s' if saved.get_size() != 1 else ''} total."
        )

    # Finds whichever OTHER team (if any) already has `channel_name` set as
    # its voice channel.
    def _findTeamUsingChannel(self, guild_id, channel_name, exclude_team_id):
        for team_id, team in self.getTeamsForGuild(guild_id):
            if team_id != exclude_team_id and team.get_voice_channel() == channel_name:
                return team
        return None

    # /team set: sets any combination of a persistent team's voice
    # channel and/or logo in one call, captain-only. `new_voice_channel`
    # creates a fresh channel named after the team (mutually exclusive
    # with passing an existing `voice_channel`). Passing an existing
    # channel that's already assigned to a different team asks for
    # confirmation before reusing it (see
    # ConfirmVoiceChannelOverwriteView), rather than silently doing it.
    # `logo` is resolved and validated against listAvailableLogos()
    # before anything is applied, the same "gate it, don't trust free
    # text" reasoning cardSetHelper's own comment gives, since a client
    # can send an arbitrary string for a slash command option even when
    # it's autocomplete-backed.
    async def teamSetHelper(self, ctx, team_name, voice_channel, new_voice_channel, logo):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "change its settings.",
                ephemeral=True,
            )
            return

        if voice_channel is None and not new_voice_channel and logo is None:
            await ctx.response.send_message(
                "Give at least one of voice_channel, new_voice_channel, or logo to set.", ephemeral=True
            )
            return

        if voice_channel is not None and new_voice_channel:
            await ctx.response.send_message(
                "Pick either voice_channel or new_voice_channel, not both.", ephemeral=True
            )
            return

        logo_path = None
        if logo is not None:
            logo_path = self._resolveLogoPath(logo)
            if logo_path is None:
                await ctx.response.send_message(
                    f"No logo named **{logo}**; pick one from the autocomplete list.", ephemeral=True
                )
                return

        conflicting = None
        if voice_channel is not None:
            conflicting = self._findTeamUsingChannel(guild_id, str(voice_channel), team_id)

        applied = []
        if logo_path is not None:
            team.set_logo_path(logo_path)
            self.updateTeamData(team_id, team)
            applied.append(f'logo **{os.path.splitext(os.path.basename(logo_path))[0]}**')

        if new_voice_channel:
            new_channel = await ctx.guild.create_voice_channel(team.get_name())
            team.set_voice_channel(new_channel)
            self.updateTeamData(team_id, team)
            applied.append(f'voice channel {new_channel.mention}')
        elif voice_channel is not None and conflicting is None:
            team.set_voice_channel(voice_channel)
            self.updateTeamData(team_id, team)
            applied.append(f'voice channel {voice_channel.mention}')

        if len(applied) == 2:
            summary = f"{applied[0]} and {applied[1]}"
        else:
            summary = applied[0] if applied else ""

        logo_file = discord.File(logo_path) if logo_path is not None else None

        if conflicting is not None:
            prefix = f"Set {summary}. " if applied else ""
            view = ConfirmVoiceChannelOverwriteView(self, guild_id, ctx.user.id, team_id, team_name, voice_channel)
            await ctx.response.send_message(
                f"{prefix}**{voice_channel.name}** is already **{conflicting.get_name()}**'s voice channel. "
                f"Set it as **{team_name}**'s too?",
                view=view, file=logo_file
            )
            view.message = await ctx.original_response()
            return

        await ctx.response.send_message(content=f"**{team_name}**: set {summary}.", file=logo_file)

    # Invites `members` (one or more) to a team the caller captains,
    # posts a single message mentioning everyone valid with one shared
    # Accept button (TeamInviteAcceptView). Each invited member only
    # actually joins once THEY press it themselves
    # (_handleTeamInviteAcceptClick), independently of whether anyone
    # else invited alongside them has. Bots, duplicates (the same member
    # passed more than once), and players already on the team are
    # filtered out rather than failing the whole command. With exactly
    # one member given, the old single-invite error messages are
    # preserved verbatim rather than folded into the multi-invite
    # phrasing. `force` (Manage Server only, checked separately from,
    # and on top of, the ordinary captain-or-admin gate above, so a
    # captain who isn't also an admin still can't skip anyone's
    # consent) adds every valid member straight to the roster instead:
    # no posted invite, no Accept button, no team_invites row for
    # anyone to accept later. Same add_player + updateTeamData pair
    # _handleTeamInviteAcceptClick itself commits once a real invite is
    # actually accepted, just run immediately instead of waiting on a
    # click that force is specifically here to skip.
    async def teamInviteHelper(self, ctx, team_name, members, force=False):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "invite players.",
                ephemeral=True,
            )
            return

        if force and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                "Only a member with the Manage Server permission can force-add players; "
                "everyone else still needs the invitee's own confirmation.",
                ephemeral=True,
            )
            return

        seen_ids = set()
        unique_members = []
        for member in members:
            if member.id in seen_ids:
                continue
            seen_ids.add(member.id)
            unique_members.append(member)

        rostered_ids = {player.get_id() for player in team.get_players()}
        # force skips the whole invite mechanism entirely (no team_invites
        # row ever gets written for it), so an existing pending invite is
        # irrelevant to it - re-checking here would wrongly block an admin
        # from force-adding someone who happens to already have one
        # outstanding.
        if force:
            already_invited_ids = set()
        else:
            self.cursor.execute(
                "SELECT targetId FROM team_invites WHERE guildId=? AND teamId=?", (guild_id, team_id)
            )
            already_invited_ids = {row[0] for row in self.cursor.fetchall()}

        valid, skipped = [], []
        for member in unique_members:
            if member.bot:
                skipped.append((member, "bot"))
            elif member.id in rostered_ids:
                skipped.append((member, "already on the team"))
            elif member.id in already_invited_ids:
                skipped.append((member, "already has a pending invite"))
            else:
                valid.append(member)

        if not valid:
            if len(unique_members) == 1:
                member, reason = skipped[0]
                if reason == "bot":
                    await ctx.response.send_message("You can't invite a bot to a team.", ephemeral=True)
                elif reason == "already has a pending invite":
                    await ctx.response.send_message(
                        f"{member.display_name} already has a pending invite to **{team_name}**.",
                        ephemeral=True,
                    )
                else:
                    await ctx.response.send_message(
                        f"{member.display_name} is already on **{team_name}**.", ephemeral=True
                    )
                return
            reasons = "; ".join(f"{member.display_name} ({reason})" for member, reason in skipped)
            await ctx.response.send_message(f"Nobody to invite: {reasons}.", ephemeral=True)
            return

        if force:
            for member in valid:
                team.add_player(Player(member.id, member.name))
            self.updateTeamData(team_id, team)

            mentions = ", ".join(member.mention for member in valid)
            message = f"{mentions} added to **{team_name}** by {ctx.user.mention}; no confirmation needed."
            if skipped:
                reasons = "; ".join(f"{member.display_name} ({reason})" for member, reason in skipped)
                message += f"\n(Not added: {reasons}.)"
            await ctx.response.send_message(message)
            return

        mentions = ", ".join(member.mention for member in valid)
        message = (
            f"{mentions}, {ctx.user.mention} invited you to join **{team_name}**! "
            "Press Accept below to join."
        )
        if skipped:
            reasons = "; ".join(f"{member.display_name} ({reason})" for member, reason in skipped)
            message += f"\n(Not invited: {reasons}.)"

        await ctx.response.send_message(message, view=TeamInviteAcceptView(self))
        msg = await ctx.original_response()

        created_at = int(time.time())
        for member in valid:
            self.cursor.execute(
                "INSERT INTO team_invites"
                "(guildId, channelId, messageId, teamId, teamName, inviterId, targetId, targetName, createdAt) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id, ctx.channel.id, msg.id, team_id, team_name, ctx.user.id, member.id, member.name,
                    created_at,
                )
            )
        self.db.commit()

    # The captain/admin counterpart to /team leave, symmetric with how
    # /team invite force is the captain/admin counterpart to a normal
    # (self-accepted) invite: a captain or a Manage Server admin can
    # remove someone from the roster who won't (or can't) run /team leave
    # themselves. No confirmation, same as force-inviting - easily undone
    # either way (re-invite, or leave). The captain themselves can't be
    # removed this way, same "no captain-less non-empty team" reasoning
    # teamLeaveHelper's own captain guard has below; /team transfer or
    # /team delete are what that situation actually needs.
    async def teamRemoveHelper(self, ctx, team_name, member):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "remove a player from the roster.",
                ephemeral=True,
            )
            return

        if self.isTeamCaptain(team, member.id):
            await ctx.response.send_message(
                f"{member.display_name} is **{team_name}**'s captain; use /team transfer to hand off "
                "the captaincy first, or /team delete if you want the team gone entirely.",
                ephemeral=True,
            )
            return

        player = next((p for p in team.get_players() if p.get_id() == member.id), None)
        if player is None:
            await ctx.response.send_message(f"{member.display_name} isn't on **{team_name}**.", ephemeral=True)
            return

        team.remove_player(player)
        self.updateTeamData(team_id, team)

        await ctx.response.send_message(f"{member.mention} has been removed from **{team_name}**.")

    # /team leave: the self-service counterpart to /team invite/
    # /team remove, no captain/admin gate at all, since removing
    # *yourself* needs nobody else's permission. The captain can't use
    # this one directly, though. Unlike every other team command, there's
    # no "who's in charge now" answer to fall back to, so leaving a
    # non-empty team captain-less would break isTeamCaptain everywhere
    # else it's checked (rename/set/invite/delete) down to just "whoever
    # has Manage Server." A captain who wants out has to answer that
    # question explicitly first: either /team transfer to someone else
    # already on the roster, or /team delete if the team shouldn't
    # exist at all anymore.
    async def teamLeaveHelper(self, ctx, team_name):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if self.isTeamCaptain(team, ctx.user.id):
            await ctx.response.send_message(
                f"You're **{team_name}**'s captain; use /team transfer to hand off the captaincy first, "
                "or /team delete if you want the team gone entirely.",
                ephemeral=True,
            )
            return

        # remove_player() relies on __eq__/identity match, same "find the
        # actual roster object by id first" pattern _applyDraftPick's own
        # players.remove_player call already has to use.
        player = next((p for p in team.get_players() if p.get_id() == ctx.user.id), None)
        if player is None:
            await ctx.response.send_message(f"You're not on **{team_name}**.", ephemeral=True)
            return

        team.remove_player(player)
        self.updateTeamData(team_id, team)

        await ctx.response.send_message(f"You've left **{team_name}**.")

    # Every role `user_id` has an explicit preference on, split into
    # (liked, disliked) lists of SETUP_ROLE_NAMES entries.
    def getRolePreferences(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT role, preference FROM player_role_preferences WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        liked, disliked = [], []
        for role, preference in self.cursor.fetchall():
            (liked if preference == "like" else disliked).append(role)
        return liked, disliked

    # Whether `user_id` has ever run /setup in this guild. Gates
    # /make-teams' use_roles (see bot.py's makeTeams) against everyone
    # currently in the caller's voice channel. Reuses the "onboarded"
    # achievement's own card_unlocks row as the signal rather than a
    # separate completion table, since unlocking it already only ever
    # happens once, the first time /setup runs.
    def hasCompletedSetup(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT 1 FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='title' AND itemKey='onboarded'",
            (guild_id, user_id)
        )
        return self.cursor.fetchone() is not None

    # /setup: a one-stop first command for a new player: a short
    # explanation of what Shockwave actually does (pointing at /help for
    # the rest), a personal "solo team" (a persistent, team_size=1 team
    # with just them on it), and their liked/disliked roles for
    # role-aware matchmaking, picked by reacting on a posted message and
    # confirming with a button rather than typing role names. Safe to
    # re-run any time afterward to update either.
    #
    # solo_team_name is always optional. If the caller already captains
    # a size-1 team (found structurally, same lookup /team invite's
    # solo-team logic never needed until now: captained by this player,
    # team_size exactly 1, no separate soloTeamId column to keep in
    # sync), omitting it just keeps that team as-is, and giving a new
    # name renames it through the same case-insensitive collision check
    # /team rename uses. Omitting it with no solo team yet names the new
    # one after the caller's current server display name instead. A
    # collision there (someone else's persistent team already has that
    # name) is the one case that still asks for an explicit name, same
    # as any other naming collision below.
    async def setupHelper(self, ctx, solo_team_name=None):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        solo_team = next(
            (team for _team_id, team in self.getTeamsCaptainedBy(guild_id, user_id) if team.get_team_size() == 1),
            None
        )

        if solo_team is None:
            auto_named = solo_team_name is None
            if auto_named:
                solo_team_name = ctx.user.display_name
            if self.getTeamRow(guild_id, solo_team_name) is not None:
                if auto_named:
                    await ctx.response.send_message(
                        f"Your display name, **{solo_team_name}**, is already taken by another team in "
                        "this server. Run /setup again with solo_team_name set to something else.",
                        ephemeral=True,
                    )
                else:
                    await ctx.response.send_message(
                        f"A team named **{solo_team_name}** already exists in this server; pick another "
                        "name for your solo team.",
                        ephemeral=True,
                    )
                return
            solo_team = Team()
            solo_team.set_name(solo_team_name)
            solo_team.set_team_size(1)
            captain = Player(ctx.user.id, ctx.user.name)
            solo_team.add_player(captain)
            solo_team.set_captain(captain)
            self._saveNewTeam(guild_id, solo_team)
            team_note = f"Created your solo team **{solo_team_name}**."
        elif solo_team_name is None:
            team_note = f"Your solo team is still **{solo_team.get_name()}**."
        elif solo_team_name.lower() != solo_team.get_name().lower():
            if self.getTeamRow(guild_id, solo_team_name) is not None:
                await ctx.response.send_message(
                    f"A team named **{solo_team_name}** already exists in this server; pick another "
                    "name for your solo team.",
                    ephemeral=True,
                )
                return
            old_name = solo_team.get_name()
            solo_team.set_name(solo_team_name)
            self._renameTeam(solo_team.get_id(), solo_team)
            team_note = f"Renamed your solo team from **{old_name}** to **{solo_team_name}**."
        elif solo_team_name != solo_team.get_name():
            old_name = solo_team.get_name()
            solo_team.set_name(solo_team_name)
            self._renameTeam(solo_team.get_id(), solo_team)
            team_note = f"Renamed your solo team from **{old_name}** to **{solo_team_name}**."
        else:
            team_note = f"Your solo team is still **{solo_team_name}**."

        blurb = (
            "**Shockwave** splits your voice channel into two teams (randomly, by live captain "
            "draft, or roughly elo-balanced for ranked play) and moves everyone into the right "
            "channel automatically. It also runs a gold economy (betting, a daily allowance, a "
            "leaderboard) and tournaments (persistent teams, a real bracket, sequential or "
            "simultaneous matches). Run **/daily** now to claim your first gold, and **/help** "
            "any time for the full command list."
        )
        view = SetupRoleSelectionView(self, guild_id, user_id)
        await ctx.response.send_message(
            f"{blurb}\n\n{team_note}\n\n"
            f"Press the roles you **like** playing ({', '.join(SETUP_ROLE_NAMES)}) to toggle them on, "
            "then press Confirm.",
            view=view,
        )
        message = await ctx.original_response()
        view.message = message

        self.cursor.execute(
            "INSERT INTO setup_role_sessions(messageId, guildId, userId, step, selectedRoles, likedRoles) "
            "VALUES(?, ?, ?, 'liked', '', '')",
            (message.id, guild_id, user_id)
        )
        self.db.commit()

    # Overwrites `player_role_preferences` for `user_id` with exactly
    # `liked`/`disliked`. The reaction-based flow always walks both
    # steps in full each run (no way to leave a side untouched the way
    # the old string-param version allowed), so a plain replace is
    # correct: a role that's in neither list just ends up with no row,
    # i.e. neutral.
    def _applySetupRolePreferences(self, guild_id, user_id, liked, disliked):
        self.cursor.execute(
            "DELETE FROM player_role_preferences WHERE guildId=? AND userId=?", (guild_id, user_id)
        )
        for role in liked:
            self.cursor.execute(
                "INSERT INTO player_role_preferences(guildId, userId, role, preference) VALUES(?, ?, ?, 'like')",
                (guild_id, user_id, role)
            )
        for role in disliked:
            self.cursor.execute(
                "INSERT INTO player_role_preferences(guildId, userId, role, preference) "
                "VALUES(?, ?, ?, 'dislike')",
                (guild_id, user_id, role)
            )
        self.db.commit()

    def _expireSetupRoleSession(self, guild_id, message_id):
        self.cursor.execute(
            "DELETE FROM setup_role_sessions WHERE guildId=? AND messageId=?", (guild_id, message_id)
        )
        self.db.commit()

    # SetupRoleSelectionView's Confirm button, for both the liked-roles
    # step and the disliked-roles step that follows it. Which one is
    # live is read fresh from setup_role_sessions rather than the view
    # tracking it itself, so the exact same view instance can be reused
    # for both (see that class's own comment).
    #
    # First confirm: snapshots the currently-toggled roles as the liked
    # set, flips the session to the disliked step, and re-poses the same
    # message/reactions for the second round. Second confirm: any role
    # toggled in BOTH rounds is a contradiction. "Can't like and dislike
    # the same role" is enforced by simply leaving that role out of
    # both final sets (neutral, no player_role_preferences row at all)
    # rather than rejecting the whole thing, with the summary telling
    # the caller which role(s) that happened to and pointing them at
    # /setup again to fix it.
    async def _confirmSetupRoleStep(self, interaction, view):
        guild_id, user_id = view.guild_id, view.user_id
        message = interaction.message

        self.cursor.execute(
            "SELECT step, selectedRoles, likedRoles FROM setup_role_sessions WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This role selection has expired. Run /setup again.", ephemeral=True
            )
            return
        step, selected_csv, liked_csv = row
        selected = sorted(r for r in selected_csv.split(",") if r)

        if step == "liked":
            self.cursor.execute(
                "UPDATE setup_role_sessions SET step='disliked', likedRoles=?, selectedRoles='' "
                "WHERE guildId=? AND messageId=?",
                (",".join(selected), guild_id, message.id)
            )
            self.db.commit()
            fresh_view = SetupRoleSelectionView(self, guild_id, user_id)
            fresh_view.message = message
            await interaction.response.edit_message(
                content=(
                    f"Liked roles set: {', '.join(selected) if selected else 'none'}.\n\n"
                    f"Now press the roles you **dislike** playing "
                    f"({', '.join(SETUP_ROLE_NAMES)}) to toggle them on, then press Confirm."
                ),
                view=fresh_view,
            )
            return

        liked = [r for r in liked_csv.split(",") if r]
        disliked = selected
        overlap = sorted(set(liked) & set(disliked))
        final_liked = [r for r in liked if r not in overlap]
        final_disliked = [r for r in disliked if r not in overlap]

        self._applySetupRolePreferences(guild_id, user_id, final_liked, final_disliked)
        self._expireSetupRoleSession(guild_id, message.id)

        newly_unlocked = []
        if self._unlockAchievement(guild_id, user_id, "onboarded"):
            newly_unlocked.append((user_id, "onboarded"))

        liked_now, disliked_now = self.getRolePreferences(guild_id, user_id)
        summary = (
            f"Liked roles: {', '.join(liked_now) if liked_now else 'none set'}.\n"
            f"Disliked roles: {', '.join(disliked_now) if disliked_now else 'none set'}."
        )
        if overlap:
            was = "was" if len(overlap) == 1 else "were"
            it = "it was" if len(overlap) == 1 else "they were"
            summary += (
                f"\n\n{', '.join(overlap)} {was} marked as both liked and disliked, so {it} left "
                "neutral instead. Run /setup again if you'd like to fix that."
            )

        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(content=summary, view=view)

        if newly_unlocked:
            await self._announceAchievements(interaction.channel, newly_unlocked)

    # SetupRoleToggleButton's callback, toggles one role on/off in the
    # current setup_role_sessions step, then rebuilds and re-renders a
    # fresh SetupRoleSelectionView so the clicked button's own style
    # reflects its new state. No-ops (with a plain ephemeral note) for a
    # stale/expired session. interaction_check already keeps this to
    # the player who ran /setup, so there's nothing else to guard here.
    async def _handleSetupRoleToggleClick(self, interaction, role_name):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT selectedRoles FROM setup_role_sessions WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This role selection has expired. Run /setup again.", ephemeral=True
            )
            return

        selected = {r for r in row[0].split(",") if r}
        selected.symmetric_difference_update({role_name})
        self.cursor.execute(
            "UPDATE setup_role_sessions SET selectedRoles=? WHERE guildId=? AND messageId=?",
            (",".join(sorted(selected)), guild_id, message.id)
        )
        self.db.commit()

        new_view = SetupRoleSelectionView(self, guild_id, interaction.user.id, selected_roles=selected)
        new_view.message = message
        await interaction.response.edit_message(view=new_view)

    # Renames a persistent team both in the `name` column (what
    # getTeamRow looks it up by) and inside its own serialized `data`
    # blob. Those two have to move together, or a later
    # getTeamRow(guild_id, new_name) call would miss the row while the
    # Team object it eventually does find under the old name claims a
    # different name than the one it's stored under. updateTeamData
    # alone only ever touches `data`, which is why this is its own
    # method rather than a set_name() + updateTeamData() call site
    # would naively reach for.
    def _renameTeam(self, team_id, team):
        self.cursor.execute(
            "UPDATE teams SET name=?, data=? WHERE id=?",
            (team.get_name(), team.serializeTeam(), team_id)
        )
        self.db.commit()

    # /team rename: captain-only, same "must not collide with an
    # existing team name in this guild" rule createTeamHelper enforces
    # on creation, case-insensitively, same as getTeamRow's own lookup,
    # so "red" is rejected as taken if "Red" already exists. The one
    # exception is a pure capitalization change of THIS team's own name
    # ("Red" -> "RED"). That's still allowed, since without excluding it
    # the collision check below would just find this same team
    # (case-insensitively) and wrongly call it already taken. Doesn't
    # touch anything else about the team (voice channel, logo, roster,
    # record). Those are independent of the name once set, same as
    # /team set already treats them.
    async def teamRenameHelper(self, ctx, team_name, new_name):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result
        current_name = team.get_name()

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{current_name}**'s captain or a member with the Manage Server permission can "
                "rename it.",
                ephemeral=True,
            )
            return

        if new_name == current_name:
            await ctx.response.send_message(f"**{current_name}** is already named that.", ephemeral=True)
            return

        if new_name.lower() != current_name.lower():
            if self.getTeamRow(guild_id, new_name) is not None:
                await ctx.response.send_message(
                    f"A team named **{new_name}** already exists in this server.", ephemeral=True
                )
                return

        team.set_name(new_name)
        self._renameTeam(team_id, team)

        await ctx.response.send_message(f"**{current_name}** has been renamed to **{new_name}**.")

    # /team transfer: offers a persistent team's captaincy to another
    # player already on its roster, same "check manage_guild by hand"
    # gate /team rename and /team delete both use for who can even start
    # this. Doesn't move captaincy immediately: the new captain gets a
    # press-to-accept prompt (TeamTransferAcceptView), the exact same
    # shape /team invite already uses, since taking on a team's admin
    # responsibilities (voice channel, roster, renaming, deleting it)
    # isn't something that should just happen to someone. force (Manage
    # Server only, same gate /team invite's own force uses) skips all of
    # that and transfers immediately, for an admin who needs it done now
    # rather than waiting on someone's response. set_captain() itself
    # still enforces "captain must be a roster player" (see
    # TourneyClasses.Team) either way, so the new captain has to already
    # be rostered - inviting them first is on the caller, not something
    # this quietly does for them. This is what /team leave's own "you're
    # the captain, there's nobody to hand it to" block needed to exist
    # before a captain could ever use it.
    async def teamTransferHelper(self, ctx, team_name, new_captain, force=False):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "transfer it.",
                ephemeral=True,
            )
            return

        if force and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                "Only a member with the Manage Server permission can force-transfer captaincy; "
                "everyone else still needs the new captain's own confirmation.",
                ephemeral=True,
            )
            return

        if self.isTeamCaptain(team, new_captain.id):
            await ctx.response.send_message(
                f"{new_captain.mention} is already **{team_name}**'s captain.", ephemeral=True
            )
            return

        player = next((p for p in team.get_players() if p.get_id() == new_captain.id), None)
        if player is None:
            await ctx.response.send_message(
                f"{new_captain.mention} isn't on **{team_name}**'s roster; invite them with "
                "/team invite first.",
                ephemeral=True,
            )
            return

        if force:
            team.set_captain(player)
            self.updateTeamData(team_id, team)
            await ctx.response.send_message(
                f"**{team_name}**'s captaincy has been transferred to {new_captain.mention}; no "
                "confirmation needed."
            )
            return

        self.cursor.execute(
            "SELECT 1 FROM team_transfers WHERE guildId=? AND teamId=?", (guild_id, team_id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message(
                f"**{team_name}** already has a pending captaincy transfer. Cancel it first if you "
                "want to send a different one.",
                ephemeral=True,
            )
            return

        await ctx.response.send_message(
            f"{new_captain.mention}, {ctx.user.mention} wants to hand you the captaincy of "
            f"**{team_name}**! Press Accept below to take it.",
            view=TeamTransferAcceptView(self),
        )
        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT INTO team_transfers"
            "(guildId, channelId, messageId, teamId, teamName, fromCaptainId, fromCaptainName, "
            "toCaptainId, toCaptainName, createdAt) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, ctx.channel.id, msg.id, team_id, team_name, ctx.user.id, ctx.user.name,
                new_captain.id, new_captain.name, int(time.time()),
            )
        )
        self.db.commit()

    # TeamTransferAcceptView's Accept button callback. Deletes the pending
    # row before anything async below, the same "nothing async between
    # finding a row and deleting it" discipline _handleTeamInviteAcceptClick
    # documents, so a rapid double-click can't transfer captaincy twice.
    async def _handleTeamTransferAcceptClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, teamId, teamName, toCaptainId FROM team_transfers WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This transfer is no longer pending.", ephemeral=True)
            return
        transfer_id, team_id, team_name, to_captain_id = row

        if interaction.user.id != to_captain_id:
            await interaction.response.send_message(
                "Only the player being offered the captaincy can accept it.", ephemeral=True
            )
            return

        self.cursor.execute("DELETE FROM team_transfers WHERE id=?", (transfer_id,))
        self.db.commit()

        team = self.getTeamById(guild_id, team_id)
        if team is None:
            return
        player = next((p for p in team.get_players() if p.get_id() == to_captain_id), None)
        if player is None:
            # Left the roster (or was removed) between the offer going out
            # and this accept - nothing left to actually transfer.
            await interaction.response.send_message(
                f"You're no longer on **{team_name}**'s roster, so there's nothing to accept.",
                ephemeral=True,
            )
            return

        team.set_captain(player)
        self.updateTeamData(team_id, team)

        await interaction.response.send_message(
            f"**{team_name}**'s captaincy has been transferred to {interaction.user.mention}!"
        )

    # TeamTransferAcceptView's Decline button callback. Only the offered
    # player can press it, same as _handleTeamInviteDeclineClick; nothing
    # was ever moved, so declining is just deleting the row.
    async def _handleTeamTransferDeclineClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, teamName, fromCaptainName, toCaptainId "
            "FROM team_transfers WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This transfer is no longer pending.", ephemeral=True)
            return
        transfer_id, team_name, from_captain_name, to_captain_id = row

        if interaction.user.id != to_captain_id:
            await interaction.response.send_message(
                "Only the player being offered the captaincy can decline it.", ephemeral=True
            )
            return

        self.cursor.execute("DELETE FROM team_transfers WHERE id=?", (transfer_id,))
        self.db.commit()

        await interaction.response.send_message(
            f"{interaction.user.mention} declined the captaincy of **{team_name}**. "
            f"**{from_captain_name}** is still captain."
        )

    # TeamTransferAcceptView's Cancel transfer button callback - the
    # current captain's (or a Manage Server admin's) own side, mirroring
    # _handleTeamInviteCancelClick: retracting an offer before the other
    # side has answered, rather than leaving them to accept or decline
    # something no longer wanted. Checked against whoever's captain right
    # now (isTeamCaptain), not just whichever captain originally sent this
    # particular offer, so it still works correctly even if captaincy
    # somehow changed hands some other way while this was pending.
    async def _handleTeamTransferCancelClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, teamId, teamName FROM team_transfers WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This transfer is no longer pending.", ephemeral=True)
            return
        transfer_id, team_id, team_name = row

        team = self.getTeamById(guild_id, team_id)
        is_captain = team is not None and self.isTeamCaptain(team, interaction.user.id)
        if not is_captain and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "cancel this transfer.",
                ephemeral=True,
            )
            return

        self.cursor.execute("DELETE FROM team_transfers WHERE id=?", (transfer_id,))
        self.db.commit()

        await interaction.response.edit_message(
            content=f"The captaincy transfer for **{team_name}** was cancelled by {interaction.user.mention}.",
            view=None,
        )

    # Deletes a team's row and any pending /team invite for it (a stale
    # invite would otherwise just silently no-op the moment someone
    # accepted it, see _handleTeamInviteAcceptClick's own team-lookup
    # guard, rather than telling them it's gone). Doesn't touch a
    # tournament this team's already registered in. See
    # ConfirmTeamDeleteView for why that's safe to leave alone.
    def _deleteTeam(self, guild_id, team_id):
        self.cursor.execute("DELETE FROM teams WHERE guildId=? AND id=?", (guild_id, team_id))
        self.cursor.execute("DELETE FROM team_invites WHERE guildId=? AND teamId=?", (guild_id, team_id))
        self.db.commit()

    # /team delete: the team's own captain, or any member with the
    # Manage Server permission (so a team whose captain has left, gone
    # inactive, or is being abusive isn't stuck undeletable). Either
    # way, confirmation-gated (see ConfirmTeamDeleteView) since it can't
    # be undone. Checked inline rather than with an
    # @app_commands.checks.has_permissions decorator (which would make
    # Manage Server required outright), since a plain captain has to be
    # allowed through too, same "check manage_guild by hand" shape
    # createTournamentHelper's overwrite-confirmation uses.
    async def teamDeleteHelper(self, ctx, team_name):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id) and not ctx.user.guild_permissions.manage_guild:
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can delete it.",
                ephemeral=True,
            )
            return

        view = ConfirmTeamDeleteView(self, guild_id, ctx.user.id, team_id, team_name)
        await ctx.response.send_message(
            f"Delete **{team_name}**? This can't be undone: its roster, record, and any pending "
            "invites will all be gone.",
            view=view,
        )
        view.message = await ctx.original_response()

    # TeamInviteAcceptView's Accept button callback. Several different
    # invited members can share the exact same message (one team_invites
    # row per invitee), so unlike every other button in this file the
    # dispatch here doesn't need a special multi-user interaction_check.
    # Scoping the lookup to `targetId=interaction.user.id` already
    # answers "is this invite actually for whoever clicked" on its own.
    async def _handleTeamInviteAcceptClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, teamId, teamName, targetId, targetName "
            "FROM team_invites WHERE guildId=? AND messageId=? AND targetId=?",
            (guild_id, interaction.message.id, interaction.user.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This isn't an invite for you, or it's already been used.", ephemeral=True
            )
            return
        invite_id, team_id, team_name, target_id, target_name = row

        # BUG-PRONE PATTERN AVOIDED: delete the invite before anything
        # async below, so a double-click can't add the player twice.
        self.cursor.execute("DELETE FROM team_invites WHERE id=?", (invite_id,))
        self.db.commit()

        team = self.getTeamById(guild_id, team_id)
        if team is None:
            return

        team.add_player(Player(target_id, target_name))
        self.updateTeamData(team_id, team)

        await interaction.response.send_message(f"**{target_name}** has joined **{team_name}**!")

    # TeamInviteAcceptView's Decline button callback. Same per-invitee
    # row lookup as _handleTeamInviteAcceptClick, just without the
    # add_player/updateTeamData side effect: only this invitee's own
    # invite row is deleted, so anyone else still invited on the same
    # message keeps their own Accept/Decline choice.
    async def _handleTeamInviteDeclineClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, teamName, targetName "
            "FROM team_invites WHERE guildId=? AND messageId=? AND targetId=?",
            (guild_id, interaction.message.id, interaction.user.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This isn't an invite for you, or it's already been used.", ephemeral=True
            )
            return
        invite_id, team_name, target_name = row

        self.cursor.execute("DELETE FROM team_invites WHERE id=?", (invite_id,))
        self.db.commit()

        await interaction.response.send_message(f"**{target_name}** declined the invite to **{team_name}**.")

    # TeamInviteAcceptView's Cancel invite button callback. Unlike Accept/
    # Decline (each scoped to targetId=interaction.user.id, one invitee's
    # own row), this retracts every remaining invitee's row for this
    # message at once - the captain/admin side undoing the whole /team
    # invite call, not any one invitee's individual response to it.
    async def _handleTeamInviteCancelClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT teamId, teamName, targetName FROM team_invites WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        rows = self.cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                "This invite's already been used up - everyone on it has accepted, declined, or it "
                "expired.",
                ephemeral=True,
            )
            return

        team_id, team_name = rows[0][0], rows[0][1]
        team = self.getTeamById(guild_id, team_id)
        is_captain = team is not None and self.isTeamCaptain(team, interaction.user.id)
        if not is_captain and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                f"Only **{team_name}**'s captain or a member with the Manage Server permission can "
                "cancel this invite.",
                ephemeral=True,
            )
            return

        self.cursor.execute(
            "DELETE FROM team_invites WHERE guildId=? AND messageId=?", (guild_id, interaction.message.id)
        )
        self.db.commit()

        target_names = ", ".join(target_name for _team_id, _team_name, target_name in rows)
        await interaction.response.edit_message(
            content=f"Invite to **{team_name}** for {target_names} was cancelled by {interaction.user.mention}.",
            view=None,
        )

    # Builds a team's stats embed, shared by /team stats and /team
    # lookup's paging, so both stay in sync automatically. Returns
    # (embed, file). file is None whenever there's no logo to attach
    # (the built-in set was unavailable when _ensureLogo ran, or the
    # file's since been removed from disk). Send the embed without a
    # thumbnail rather than erroring on a discord.File() open that
    # can't succeed.
    def _renderTeamStatsEmbed(self, team):
        games = team.wins + team.losses
        win_rate = f"{(team.wins / games) * 100:.1f}%" if games > 0 else "N/A"
        captain = team.get_captain()
        captain_name = captain.get_name() if isinstance(captain, Player) else "None"
        roster = ", ".join(player.get_name() for player in team.get_players()) or "No players yet"
        target_size = team.get_team_size()
        roster_size = f"{team.get_size()}/{target_size}" if target_size is not None else str(team.get_size())

        embed = discord.Embed(title=f"{team.get_name()} Stats", color=discord.Color.gold())
        embed.add_field(name="Captain", value=captain_name, inline=True)
        embed.add_field(name="Roster Size", value=roster_size, inline=True)
        embed.add_field(name="Record", value=f"{team.wins}W - {team.losses}L", inline=True)
        embed.add_field(name="Win Rate", value=win_rate, inline=True)
        embed.add_field(name="Voice Channel", value=team.get_voice_channel() or "Not set", inline=True)
        embed.add_field(name="Roster", value=roster, inline=False)

        logo_path = team.get_logo_path()
        file = None
        if logo_path is not None and os.path.isfile(logo_path):
            filename = os.path.basename(logo_path)
            file = discord.File(logo_path, filename=filename)
            embed.set_thumbnail(url=f"attachment://{filename}")

        return embed, file

    async def teamStatsHelper(self, ctx, team_name):
        result = self.getTeamRow(ctx.guild.id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.", ephemeral=True)
            return
        team_id, team = result

        embed, file = self._renderTeamStatsEmbed(team)
        view = TeamStatsView(self, card_shown=False)
        if file is not None:
            await ctx.response.send_message(embed=embed, file=file, view=view)
        else:
            await ctx.response.send_message(embed=embed, view=view)

        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT OR REPLACE INTO team_stats_views(messageId, guildId, teamId, cardShown) "
            "VALUES(?, ?, ?, 0)",
            (msg.id, ctx.guild.id, team_id)
        )
        self.db.commit()

    # A representative color sampled straight from `logo_path`'s own
    # artwork. The team card's accent color "matches the logo" (see
    # _renderTeamCardImage) without needing a stored per-team setting
    # the way the player card's customizable accent_color does.
    # Near-transparent pixels (background) and near-white/near-black
    # ones (padding, outlines) are excluded, so a logo's actual
    # identifying color wins out over whatever surrounds it. Colors are
    # bucketed to the nearest 16 to collapse anti-aliasing noise into
    # one dominant group instead of splitting votes across dozens of
    # near-identical shades.
    def _dominantLogoColor(self, logo_path, fallback):
        try:
            image = Image.open(logo_path).convert("RGBA")
        except Exception:
            return fallback
        image.thumbnail((64, 64))
        counts = {}
        for r, g, b, a in image.getdata():
            if a < 128:
                continue
            brightness = (r + g + b) / 3
            if brightness < 20 or brightness > 235:
                continue
            key = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return fallback
        return max(counts, key=counts.get)

    # `color` lightened toward white (see _lightenColor) just enough that
    # it's at least `min_contrast` brighter, on average, than
    # `background`. Closed-form rather than searching, since
    # _lightenColor's blend is linear in `amount`:
    # brightness(lighten(color, amount)) is color_brightness + (255 -
    # color_brightness) * amount, so the amount that hits the target
    # brightness exactly is a straight division. A `color` already
    # bright enough comes back unchanged. This only ever pulls a color
    # TOWARD readable, never away from it, so a well-lit logo color
    # renders exactly as sampled.
    def _ensureReadableAccent(self, color, background, min_contrast=CARD_MIN_ACCENT_CONTRAST):
        color_brightness = sum(color) / 3
        background_brightness = sum(background) / 3
        deficit = min_contrast - (color_brightness - background_brightness)
        if deficit <= 0:
            return color
        headroom = 255 - color_brightness
        if headroom <= 0:
            return color
        amount = min(deficit / headroom, 1.0)
        return self._lightenColor(color, amount)

    # Pure rendering: a portrait "team card", the team's own counterpart
    # to _renderTradingCardImage, with its logo as the focal point (same
    # big-and-centered treatment _drawMatchupColumn gives it) instead of
    # a player's avatar, and its accent/background colors sampled
    # straight off that logo (_dominantLogoColor) rather than being a
    # stored per-player customization. A team has no settings row of its
    # own, so "match the logo" is derived fresh on every render instead.
    def _renderTeamCardImage(self, guild_name, team):
        logo_path = team.get_logo_path()
        has_logo = logo_path is not None and os.path.isfile(logo_path)
        accent_color = (
            self._dominantLogoColor(logo_path, TEAM_CARD_FALLBACK_ACCENT_COLOR) if has_logo
            else TEAM_CARD_FALLBACK_ACCENT_COLOR
        )
        # Same "darken the one distinguishing color into a background,
        # then lighten it back up for the vignette center" relationship
        # the player card's background_color has to its own center.
        # Here it's derived from the sampled accent instead of a stored
        # setting.
        background_color = tuple(round(c * CARD_BACKGROUND_DARKEN_RATIO) for c in accent_color)
        background_center = self._lightenColor(background_color, 0.3)
        text_color = self._hexToRgb(CARD_DEFAULT_TEXT_COLOR, BRACKET_TEXT_COLOR)
        # The background itself is always derived from (and so stays
        # true to) the logo's own sampled accent_color above. But a dark
        # logo color (a deep navy, forest green, etc.) drawn as TEXT
        # against the background's own lightened vignette center can end
        # up with poor contrast, especially toward the middle of the
        # card. Every drawn element below uses this readability-boosted
        # version instead of the raw sampled color, so the card still
        # visibly carries the logo's color scheme without any of its
        # text becoming hard to read. Only the background derivation
        # above uses the true, unboosted sample.
        accent_color = self._ensureReadableAccent(accent_color, background_center)

        name_font = self._loadFont(CHAKRA_PETCH_BOLD, CARD_NAME_FONT_SIZE)
        label_font = self._loadFont(IBM_PLEX_SANS, CARD_STAT_LABEL_FONT_SIZE, "Bold")
        value_font = self._loadFont(IBM_PLEX_SANS, CARD_STAT_VALUE_FONT_SIZE, "SemiBold")
        roster_font = self._loadFont(IBM_PLEX_SANS, CARD_STAT_LABEL_FONT_SIZE, "Medium")

        games = team.wins + team.losses
        win_rate = f"{(team.wins / games) * 100:.1f}%" if games > 0 else "N/A"
        captain = team.get_captain()
        captain_name = captain.get_name() if isinstance(captain, Player) else "None"
        captain_id = captain.get_id() if isinstance(captain, Player) else None
        stat_rows = [
            ("CAPTAIN", captain_name),
            ("RECORD", f"{team.wins}W - {team.losses}L"),
            ("WIN RATE", win_rate),
        ]

        # Captain-first (_orderedRoster), same as the matchup image's
        # own roster columns, capped the same "N rows then a +count
        # line" way the player card caps its team list.
        roster = self._orderedRoster(team)
        shown_roster = roster[:TEAM_CARD_MAX_ROSTER_ROWS]
        extra_roster_count = len(roster) - len(shown_roster)

        header_height = self._bracketHeaderHeight(None)
        logo_top = header_height + BRACKET_PADDING * 2
        logo_cx = CARD_WIDTH / 2
        name_y = logo_top + TEAM_CARD_LOGO_SIZE + BRACKET_PADDING * 2
        rule_y = name_y + CARD_NAME_FONT_SIZE + BRACKET_PADDING * 2
        stats_top = rule_y + BRACKET_PADDING * 2
        stats_bottom = stats_top + CARD_STAT_LINE_HEIGHT * len(stat_rows)

        roster_top = stats_bottom + BRACKET_PADDING * 2
        roster_rows = len(shown_roster) + (1 if extra_roster_count > 0 else 0) if shown_roster else 1
        height = int(roster_top + roster_rows * TEAM_CARD_ROSTER_ROW_HEIGHT + BRACKET_MARGIN)

        image, draw = self._createBracketCanvas(
            CARD_WIDTH, height, accent_color, background=background_color, background_center=background_center
        )
        self._drawBracketHeader(image, draw, guild_name, None, accent_color, CARD_WIDTH, bold_title=True)

        logo_x = int(logo_cx - TEAM_CARD_LOGO_SIZE / 2)
        if has_logo:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((TEAM_CARD_LOGO_SIZE, TEAM_CARD_LOGO_SIZE), Image.LANCZOS)
            paste_x = int(logo_cx - logo.width / 2)
            paste_y = int(logo_top + (TEAM_CARD_LOGO_SIZE - logo.height) / 2)
            image.paste(logo, (paste_x, paste_y), logo)
        draw.rounded_rectangle(
            [logo_x, logo_top, logo_x + TEAM_CARD_LOGO_SIZE, logo_top + TEAM_CARD_LOGO_SIZE],
            radius=TEAM_CARD_LOGO_RADIUS, outline=accent_color, width=TEAM_CARD_LOGO_BORDER
        )

        draw.text((logo_cx, name_y), team.get_name(), font=name_font, fill=text_color, anchor="ma")

        draw.line(
            [(BRACKET_MARGIN, rule_y), (CARD_WIDTH - BRACKET_MARGIN, rule_y)],
            fill=accent_color, width=BRACKET_RULE_WIDTH
        )

        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        label_column_width = max(measurer.textlength(label, font=label_font) for label, _value in stat_rows)
        value_x = BRACKET_MARGIN + label_column_width + BRACKET_PADDING * 2
        for i, (label, value) in enumerate(stat_rows):
            row_y = stats_top + i * CARD_STAT_LINE_HEIGHT + CARD_STAT_LINE_HEIGHT / 2
            draw.text((BRACKET_MARGIN, row_y), label, font=label_font, fill=accent_color, anchor="lm")
            draw.text((value_x, row_y), value, font=value_font, fill=text_color, anchor="lm")

        if not shown_roster:
            draw.text(
                (BRACKET_MARGIN, roster_top + TEAM_CARD_ROSTER_ROW_HEIGHT / 2), "No players yet",
                font=roster_font, fill=BRACKET_LINE_COLOR, anchor="lm"
            )
        for i, player in enumerate(shown_roster):
            row_y = roster_top + i * TEAM_CARD_ROSTER_ROW_HEIGHT + TEAM_CARD_ROSTER_ROW_HEIGHT / 2
            text_x = BRACKET_MARGIN
            if captain_id is not None and player.get_id() == captain_id:
                star_cx = BRACKET_MARGIN + TEAM_CARD_STAR_RADIUS
                self._drawStar(draw, star_cx, row_y, TEAM_CARD_STAR_RADIUS, accent_color)
                text_x = BRACKET_MARGIN + TEAM_CARD_STAR_RADIUS * 2 + BRACKET_PADDING / 2
            draw.text((text_x, row_y), player.get_name(), font=roster_font, fill=text_color, anchor="lm")

        if extra_roster_count > 0:
            row_y = roster_top + len(shown_roster) * TEAM_CARD_ROSTER_ROW_HEIGHT + TEAM_CARD_ROSTER_ROW_HEIGHT / 2
            draw.text(
                (BRACKET_MARGIN, row_y), f"+{extra_roster_count} more player"
                f"{'s' if extra_roster_count != 1 else ''}", font=roster_font, fill=accent_color, anchor="lm"
            )

        return image

    # The card half of TeamStatsView's toggle (see
    # _handleTeamStatsShowCardClick). Re-fetches the team fresh
    # (getTeamById) rather than trusting whatever was true when
    # /team stats first posted, same "always current" approach
    # _swapStatsForTradingCard takes for a player's live stats. `view`,
    # when given, is included in the same edit call that swaps the
    # image in. Passing view=None here (the default) omits the kwarg
    # entirely rather than passing it through, since discord.py's
    # Message.edit treats an explicit view=None as "remove every
    # component," not "leave the current one alone."
    async def _swapTeamStatsForCard(self, message, guild_id, guild_name, team_id, view=None):
        team = self.getTeamById(guild_id, team_id)
        if team is None:
            return
        card_image = await asyncio.to_thread(self._renderTeamCardImage, guild_name, team)
        file = self._imageToFile(card_image, "team_card.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        edit_kwargs = {"embed": embed, "attachments": [file]}
        if view is not None:
            edit_kwargs["view"] = view
        await message.edit(**edit_kwargs)

    # The reverse: rebuilds the plain /team stats embed
    # (_renderTeamStatsEmbed, the same one teamStatsHelper itself posts) in
    # place of the card image. See _swapTeamStatsForCard on `view`.
    async def _swapTeamCardForStats(self, message, guild_id, team_id, view=None):
        team = self.getTeamById(guild_id, team_id)
        if team is None:
            return
        embed, file = self._renderTeamStatsEmbed(team)
        edit_kwargs = {"embed": embed, "attachments": [file] if file is not None else []}
        if view is not None:
            edit_kwargs["view"] = view
        await message.edit(**edit_kwargs)

    # TeamStatsView's Card button callback, swaps the plain embed for the
    # team's trading-card image and re-renders with a Back button in place
    # of Card (see TeamStatsView).
    async def _handleTeamStatsShowCardClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT teamId FROM team_stats_views WHERE guildId=? AND messageId=? AND cardShown=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This team stats view is no longer live.", ephemeral=True)
            return
        team_id = row[0]

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        await self._swapTeamStatsForCard(
            message, guild_id, guild_name, team_id, view=TeamStatsView(self, card_shown=True)
        )
        self.cursor.execute(
            "UPDATE team_stats_views SET cardShown=1 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # TeamStatsView's Back button callback, the reverse swap.
    async def _handleTeamStatsReturnClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT teamId FROM team_stats_views WHERE guildId=? AND messageId=? AND cardShown=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This team stats view is no longer live.", ephemeral=True)
            return
        team_id = row[0]

        await interaction.response.defer()
        await self._swapTeamCardForStats(message, guild_id, team_id, view=TeamStatsView(self, card_shown=False))
        self.cursor.execute(
            "UPDATE team_stats_views SET cardShown=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # ---------------- /team lookup ----------------

    # One team per "page" rather than a batch of rows like /leaderboard;
    # /team lookup is for flipping through each of a player's teams' full
    # stats cards one at a time, not scanning a ranked list.
    def _myTeamsPageCount(self, teams):
        return max(1, len(teams))

    # Same embed /team stats uses, plus a "Team X/N" footer so paging has
    # something to orient by (team-stats itself doesn't need one; there's
    # only ever the one team on screen there).
    def _renderMyTeamsEmbed(self, teams, page):
        team_id, team = teams[page]
        embed, file = self._renderTeamStatsEmbed(team)
        embed.set_footer(text=f"Team {page + 1}/{len(teams)}")
        return embed, file

    # Shared by every LeaderboardPagingView/MyTeamsPagingView/
    # TeamListPagingView button callback. First/Prev/Next/Last all
    # reduce to the same arithmetic regardless of which of the three
    # tables/render functions the caller actually pages through.
    # `target`, when given (a 0-based page from
    # _PageJumpModal.on_submit), skips direction entirely and just
    # clamps straight to it, the same "landing on the nearest valid
    # page instead of erroring" behavior Last already gives you when
    # total_pages shrank out from under a stale view.
    def _computeNewPage(self, direction, page, total_pages, target=None):
        if target is not None:
            return max(0, min(target, total_pages - 1))
        if direction == "first":
            return 0
        if direction == "prev":
            return max(0, page - 1)
        if direction == "next":
            return min(total_pages - 1, page + 1)
        return total_pages - 1

    async def myTeamsHelper(self, ctx, member=None):
        target = member if member is not None else ctx.user
        guild_id = ctx.guild.id
        user_id = target.id

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            if member is None:
                await ctx.response.send_message("You're not on any teams in this server.", ephemeral=True)
            else:
                await ctx.response.send_message(
                    f"{target.display_name} isn't on any teams in this server.", ephemeral=True
                )
            return

        embed, file = self._renderMyTeamsEmbed(teams, page=0)
        view = MyTeamsPagingView(self)
        if file is not None:
            await ctx.response.send_message(embed=embed, file=file, view=view)
        else:
            await ctx.response.send_message(embed=embed, view=view)
        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT OR REPLACE INTO my_team_views(messageId, guildId, channelId, userId, page) "
            "VALUES(?, ?, ?, ?, 0)",
            (msg.id, guild_id, ctx.channel.id, user_id)
        )
        self.db.commit()

    # MyTeamsPagingView's button callback, no-ops (with a plain ephemeral
    # note) unless the interaction's message still matches an active
    # /team lookup page view. The stored userId is whoever the list is
    # ABOUT (the looked-up member, /team lookup's own optional `member`
    # param, defaulting to whoever ran the command), not whoever clicks
    # the button. The button itself is clickable by anyone, and
    # re-derives the team list from that stored userId regardless of who
    # actually clicked, so anyone else's click still moves the same
    # shared view. That matches how /leaderboard's own paging already
    # behaves (any clicker can page a guild-wide view). A personal view
    # being paged by someone else just steps through the looked-up
    # player's teams, not the clicker's. cardShown carries across the
    # flip the same way team_list_views' own does, see
    # _handleTeamListPageClick.
    async def _handleMyTeamsPageClick(self, interaction, direction=None, target_page=None):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT userId, page, cardShown FROM my_team_views WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        user_id, page, card_shown = row

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(teams)
        page = min(page, total_pages - 1)
        new_page = self._computeNewPage(direction, page, total_pages, target_page)

        if new_page == page:
            await interaction.response.defer()
            return

        if card_shown:
            guild_name = interaction.guild.name if interaction.guild is not None else ""
            embed, file = await self._renderTeamListCardEmbed(guild_name, teams, new_page)
            await interaction.response.edit_message(embed=embed, attachments=[file])
        else:
            embed, file = self._renderMyTeamsEmbed(teams, new_page)
            if file is not None:
                await interaction.response.edit_message(embed=embed, attachments=[file])
            else:
                await interaction.response.edit_message(embed=embed, attachments=[])

        self.cursor.execute(
            "UPDATE my_team_views SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, interaction.message.id)
        )
        self.db.commit()

    # MyTeamsPagingView's Card button callback, swaps the currently-paged
    # team's plain stats card for its actual trading card - see
    # _handleTeamListShowCardClick, same idea, just re-deriving the team
    # list from my_team_views' own stored userId instead of a stored
    # search/sort/filter.
    async def _handleMyTeamsShowCardClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT userId, page FROM my_team_views WHERE guildId=? AND messageId=? AND cardShown=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        user_id, page = row

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        page = min(page, len(teams) - 1)

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        embed, file = await self._renderTeamListCardEmbed(guild_name, teams, page)
        await message.edit(embed=embed, attachments=[file], view=MyTeamsPagingView(self, card_shown=True))
        self.cursor.execute(
            "UPDATE my_team_views SET cardShown=1 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # MyTeamsPagingView's Back button callback, the reverse swap.
    async def _handleMyTeamsReturnClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT userId, page FROM my_team_views WHERE guildId=? AND messageId=? AND cardShown=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        user_id, page = row

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        page = min(page, len(teams) - 1)

        await interaction.response.defer()
        embed, file = self._renderMyTeamsEmbed(teams, page)
        edit_kwargs = {"embed": embed, "attachments": [file] if file is not None else []}
        edit_kwargs["view"] = MyTeamsPagingView(self, card_shown=False)
        await message.edit(**edit_kwargs)
        self.cursor.execute(
            "UPDATE my_team_views SET cardShown=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # MyTeamsPagingView's Page # button: see _handleLeaderboardJumpClick,
    # same "no longer live"/empty guards _handleMyTeamsPageClick needs.
    async def _handleMyTeamsJumpClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT userId FROM my_team_views WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This view is no longer live.", ephemeral=True)
            return
        user_id, = row

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(teams)
        await interaction.response.send_modal(
            _PageJumpModal(self, "_handleMyTeamsPageClick", total_pages)
        )

    # Per-team win/loss record scoped to just THIS tournament, computed
    # from resolved tournament_matches rows rather than each team's own
    # persisted (all-time, cross-tournament) wins/losses.
    # tournament_matches has no tournamentId column, but doesn't need
    # one here: a guild has exactly one tournament at a time, and
    # building a fresh bracket always clears out the previous
    # tournament's rows first (see _clearTournamentMatchesForGuild), so
    # every row still in the table for this guild belongs to THIS
    # tournament. Keyed by team NAME (matching how
    # _recordMatchResult/getTeamRow already resolve a bracket team back
    # to its persisted row), seeded at 0-0 for every registered team so
    # one that never won a game still shows up instead of being left
    # out.
    def _tournamentTeamRecords(self, guild_id, tournament):
        records = {team.get_name(): [0, 0] for team in tournament.get_teams()}
        self.cursor.execute(
            "SELECT team1, team2, winner FROM tournament_matches WHERE guildId=? AND state='RESOLVED'",
            (guild_id,)
        )
        for team1_ser, team2_ser, winner in self.cursor.fetchall():
            if winner is None:
                continue
            team1, team2 = Team(), Team()
            team1.deserializeTeam(team1_ser)
            team2.deserializeTeam(team2_ser)
            winner_name = team1.get_name() if winner == 1 else team2.get_name()
            loser_name = team2.get_name() if winner == 1 else team1.get_name()
            if winner_name in records:
                records[winner_name][0] += 1
            if loser_name in records:
                records[loser_name][1] += 1
        return records

    # The "tournament just finished" results embed: every team
    # REGISTERED FOR THIS TOURNAMENT, ranked by its record IN THIS
    # TOURNAMENT (see _tournamentTeamRecords). Deliberately distinct
    # from _renderTeamListEmbed (/team list), which is server-wide and
    # all-time on purpose. This one exists specifically so a team that
    # played in a dozen past tournaments doesn't show up here with its
    # entire history, and a team that wasn't even in this one doesn't
    # show up at all.
    def _renderTournamentResultsEmbed(self, guild_id, tournament, guild_name):
        teams = tournament.get_teams()
        if not teams:
            return None
        records = self._tournamentTeamRecords(guild_id, tournament)

        def sort_key(team):
            wins, losses = records[team.get_name()]
            games = wins + losses
            win_rate = (wins / games) if games > 0 else -1
            return (-win_rate, -wins)

        ranked = sorted(teams, key=sort_key)

        lines = []
        for i, team in enumerate(ranked, start=1):
            wins, losses = records[team.get_name()]
            games = wins + losses
            win_rate = f"{(wins / games) * 100:.1f}%" if games > 0 else "N/A"
            lines.append(f"**#{i}.** {team.get_name()} - {wins}W-{losses}L ({win_rate})")

        title = f"\U0001f3c6 {tournament.get_name()} Results"
        if guild_name:
            title += f" - {guild_name}"
        return discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())

    # Posts the tournament-scoped results embed right after a tournament
    # fully wraps up (both the single-elimination and double-elimination
    # "it's complete" messages call this). No-op if there are somehow no
    # registered teams. Shouldn't happen right after a tournament
    # finishes, but _renderTournamentResultsEmbed already handles it
    # cleanly either way.
    async def _postTournamentLeaderboard(self, channel, guild_id, tournament):
        guild_name = channel.guild.name if channel.guild is not None else None
        embed = self._renderTournamentResultsEmbed(guild_id, tournament, guild_name)
        if embed is not None:
            await channel.send(embed=embed)

    # Loads two persistent teams straight into team1/team2 for a casual
    # or ranked game, the "quickly reuse a tournament team" path,
    # skipping /make-teams'/`/ranked`'s random-split-or-draft entirely.
    # Same "build the roster, then click ▶️" contract as those commands:
    # nobody is moved and no elo/betting starts until the roster's ▶️
    # reaction (see _finalizeRoster) is clicked.
    async def useTeamsHelper(self, ctx, team1_name, team2_name, ranked):
        guild_id = ctx.guild.id

        # Case-insensitive, matching getTeamRow's own lookup; "Red" and
        # "red" resolve to the same team, so comparing the raw strings
        # byte-for-byte would let that pair slip through as "different"
        # right up until both getTeamRow calls below returned the exact
        # same row.
        if team1_name.lower() == team2_name.lower():
            await ctx.response.send_message("Pick two different teams.", ephemeral=True)
            return

        result1 = self.getTeamRow(guild_id, team1_name)
        if result1 is None:
            await ctx.response.send_message(
                f"No team named **{discord.utils.escape_markdown(team1_name)}** in this server.",
                ephemeral=True,
            )
            return

        result2 = self.getTeamRow(guild_id, team2_name)
        if result2 is None:
            await ctx.response.send_message(
                f"No team named **{discord.utils.escape_markdown(team2_name)}** in this server.",
                ephemeral=True,
            )
            return

        _, team1 = result1
        _, team2 = result2
        team1.set_id(1)
        team2.set_id(2)

        await self.clearTeamsHelper(ctx)

        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "mode", "Ranked" if ranked else "Normal")
        if ranked:
            self.update(guild_id, "is_ranked", 1)
        self.update(guild_id, "game", self._currentGame(guild_id))

        ranked_note = " (ranked, elo will update when the winner is reported)" if ranked else ""
        # escape_markdown on the actual resolved names (not the raw
        # args), so this reflects what /team create stored even if the
        # caller typed different casing, and so a stray
        # underscore/asterisk in either name can't bleed italics/bold
        # into the rest of the line.
        await ctx.response.send_message(
            f"**{discord.utils.escape_markdown(team1.get_name())}** vs "
            f"**{discord.utils.escape_markdown(team2.get_name())}** loaded{ranked_note}. "
            "Press Start on the roster below when you're ready to move everyone and open betting, or "
            "Start (no move) to open betting without moving anyone.\n"
            f"{self._gameNote(guild_id)}"
        )
        intro_message = await ctx.original_response()
        team1_message, team2_message, _ = await self.printEmbed(ctx, team1, team2)
        await self._finalizeRoster(
            guild_id, team1_message, team2_message, team1, team2, use_roles=False,
            intro_messages=[intro_message],
        )

    # /make-teams repeat: re-posts whichever two rosters /make-teams
    # random, /make-teams draft, or /make-teams saved most recently
    # produced, instead of drawing a fresh random split, elo-balanced
    # split, or captains draft. team1/team2 already hold that exact
    # roster until the next team-forming command overwrites them
    # (nothing clears them just because a game resolved, see
    # clearTeamsHelper's own bug-fix note), so this only has to read
    # them back rather than reconstruct anything. mode/is_ranked/
    # roster_use_roles are read but never written here, so a reused
    # ranked game stays ranked, a casual one stays casual, and a
    # roles-eligible roster keeps showing role labels. "Ranked behavior
    # stays" the same as whatever the original game was, not whatever
    # /make-teams repeat defaults to.
    async def reuseTeamsHelper(self, ctx):
        guild_id = ctx.guild.id

        team1_data = self.get(guild_id, "team1")
        team2_data = self.get(guild_id, "team2")
        if not team1_data or not team2_data:
            await ctx.response.send_message(
                "No previous teams to reuse. Make some first with /make-teams random, /make-teams draft, or "
                "/make-teams saved.",
                ephemeral=True,
            )
            return

        # A game still being bet on or played from these same teams gets
        # cancelled cleanly first (refund + move back), same as every
        # other team-forming command does via clearTeamsHelper, just
        # without clearTeamsHelper's own team1/team2 wipe, since reusing
        # them is the whole point here.
        if self.get(guild_id, "betting_state") in ("OPEN", "CLOSED"):
            await self.cancelGameHelper(guild_id, ctx.channel, ctx.guild)

        team1 = Team()
        team1.deserializeTeam(team1_data)
        team2 = Team()
        team2.deserializeTeam(team2_data)

        is_ranked = bool(self.get(guild_id, "is_ranked"))
        use_roles = bool(self.get(guild_id, "roster_use_roles"))

        self.update(guild_id, "original_channel", "")

        ranked_note = " (ranked, elo will update when the winner is reported)" if is_ranked else ""
        await ctx.response.send_message(
            f"Reusing **{discord.utils.escape_markdown(team1.get_name())}** vs "
            f"**{discord.utils.escape_markdown(team2.get_name())}**{ranked_note}. "
            "Press Start on the roster below when you're ready to move everyone and open betting, or "
            "Start (no move) to open betting without moving anyone.\n"
            f"{self._gameNote(guild_id)}"
        )
        intro_message = await ctx.original_response()
        team1_message, team2_message, _ = await self.printEmbed(ctx, team1, team2, useRoles=use_roles)
        await self._finalizeRoster(
            guild_id, team1_message, team2_message, team1, team2, use_roles=use_roles,
            intro_messages=[intro_message],
        )

    def getEconomy(self, guild_id, user_id, column):
        self.cursor.execute(
            f"SELECT {column} FROM economy WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        row = self.cursor.fetchone()
        return row[0] if row is not None else None

    async def dailyHelper(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)

        today = datetime.date.today().isoformat()
        last_daily = self.getEconomy(guild_id, user_id, "last_daily")

        if last_daily == today:
            # <t:...:R> is Discord's own relative-timestamp markdown, so
            # this reads correctly in whatever timezone the viewer's own
            # client is set to instead of guessing at one. The
            # underlying cutoff is still just local midnight on the host
            # machine (the same boundary `today`/`last_daily` already
            # compare against).
            tomorrow = datetime.date.today() + datetime.timedelta(days=1)
            reset_at = datetime.datetime.combine(tomorrow, datetime.time.min)
            await ctx.response.send_message(
                f"You've already claimed your daily gold today! Come back <t:{int(reset_at.timestamp())}:R>.",
                ephemeral=True,
            )
            return

        self.cursor.execute(
            "UPDATE economy SET balance = balance + ?, last_daily = ? WHERE guildId=? AND userId=?",
            (DAILY_GOLD_AMOUNT, today, guild_id, user_id)
        )
        self.db.commit()

        new_balance = self.getEconomy(guild_id, user_id, "balance")
        await ctx.response.send_message(
            f"You claimed your daily {DAILY_GOLD_AMOUNT} gold! Your balance is now {new_balance}."
        )

    # /give: a plain, immediate gold transfer between two players, no
    # accept step needed (unlike /wager against - the recipient never has
    # to consent to being given gold, the same way nobody has to consent
    # to /notify DMing them an invite). Deliberately NOT capped by /set
    # max-wager (that's a per-bet risk limit; a transfer isn't a wager,
    # there's no outcome to hedge against) and not gated by
    # betting_enabled either (moving gold between players isn't betting,
    # so turning betting off on a server shouldn't also turn this off).
    # Doesn't touch wins/losses/gold_wagered/gold_won/gold_lost - those
    # are bet-outcome-only columns, and a gift is even less bet-like than
    # a cancelled duel (see _finishDuelCancellation, which already
    # doesn't touch them for the same reason).
    async def giveGoldHelper(self, ctx, member, amount):
        guild_id = ctx.guild.id
        giver = ctx.user

        if member.id == giver.id:
            await ctx.response.send_message("You can't give gold to yourself!", ephemeral=True)
            return

        if member.bot:
            await ctx.response.send_message("You can't give gold to a bot!", ephemeral=True)
            return

        if amount <= 0:
            await ctx.response.send_message("Amount must be greater than 0.", ephemeral=True)
            return

        self.ensureEconomyRow(guild_id, giver.id, giver.name)
        balance = self.getEconomy(guild_id, giver.id, "balance")
        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}.", ephemeral=True
            )
            return

        self.ensureEconomyRow(guild_id, member.id, member.name)

        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (amount, guild_id, giver.id)
        )
        self.cursor.execute(
            "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?",
            (amount, guild_id, member.id)
        )
        self.db.commit()

        giver_balance = self.getEconomy(guild_id, giver.id, "balance")
        recipient_balance = self.getEconomy(guild_id, member.id, "balance")
        await ctx.response.send_message(
            f"{giver.mention} gave **{amount} gold** to {member.mention}! "
            f"{giver.name}'s balance: {giver_balance}. {member.name}'s balance: {recipient_balance}."
        )

    # ---------------- Betting ----------------

    # user_ids from the current roster's own disliked_role_user_ids
    # column (see rankedTeamHelper): whoever got stuck with a role they
    # marked disliked when this team1/team2 split was formed. For
    # computeGameDeltas to credit the
    # ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER bonus to if they
    # win. Empty for a roleless split, a casual game, or any roster
    # formed before this column existed.
    def _dislikedRoleUserIds(self, guild_id):
        raw = self.get(guild_id, "disliked_role_user_ids")
        if not raw:
            return frozenset()
        return frozenset(int(uid) for uid in raw.split(","))

    # Returns [(user_id, name), ...] for a team column
    # ("team1"/"team2"), or [] if that side hasn't been set up. Never
    # crashes on an unset column, unlike
    # Team().deserializeTeam(None/"").
    def getRosterPlayers(self, guild_id, column):
        serialized = self.get(guild_id, column)
        if not serialized:
            return []
        team = Team()
        team.deserializeTeam(serialized)
        return [(p.get_id(), p.get_name()) for p in team.get_players()]

    # The Team's own .name for a team column, same "team1"/"team2" a
    # roster was formed into, but its display name rather than its
    # player list (see getRosterPlayers). recordResult uses this so the
    # win/elo-change summary says the same name the roster embed and
    # matchup image already showed (see _rosterTeamNames). `fallback`
    # for a column that's unset or (defensively) came back with an
    # empty name.
    def getRosterName(self, guild_id, column, fallback, escape=True):
        serialized = self.get(guild_id, column)
        if not serialized:
            return fallback
        team = Team()
        team.deserializeTeam(serialized)
        name = team.get_name() or fallback
        if not escape:
            # Button labels render as plain text (Discord doesn't apply
            # markdown to them), so escaping here would show a stray
            # backslash instead of protecting anything.
            return name
        # Team names are free text (/team create, /team rename). Escaped
        # here rather than at creation so the stored name stays exact.
        # An unescaped underscore/asterisk from one team's name can pair
        # up across the whole message with a marker from unrelated text
        # later in the same string (e.g. the other team's name), putting
        # everything in between into unintended italics/bold.
        return discord.utils.escape_markdown(name)

    # True if `user_id` is a rostered player (either side) in the game the
    # roster's ▶️ reaction most recently moved into channels, used to stop
    # players from betting on their own game.
    def isPlayerInCurrentGame(self, guild_id, user_id):
        player_ids = {uid for uid, _name in self.getRosterPlayers(guild_id, "team1")}
        player_ids |= {uid for uid, _name in self.getRosterPlayers(guild_id, "team2")}
        return user_id in player_ids

    # Same idea as isPlayerInCurrentGame, scoped to one specific
    # simultaneous-mode tournament match instead of the guild-wide
    # singleton game, since several matches (each with their own rosters)
    # can be live at once. Used by the winner-report confirm gate below,
    # not betting (that's already scoped to a match itself, no roster
    # check needed beyond "not one of the two teams", see wagerHelper).
    def _isPlayerInTournamentMatch(self, match_id, user_id):
        self.cursor.execute("SELECT team1, team2 FROM tournament_matches WHERE id=?", (match_id,))
        row = self.cursor.fetchone()
        if row is None:
            return False
        team1, team2 = Team(), Team()
        team1.deserializeTeam(row[0])
        team2.deserializeTeam(row[1])
        rostered_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        return user_id in rostered_ids

    # Same idea, scoped to one specific heads-up duel (/wager against):
    # only its own two participants count as "in the game".
    def _isPlayerInDuel(self, duel_id, user_id):
        self.cursor.execute("SELECT challengerId, targetId FROM duels WHERE id=?", (duel_id,))
        row = self.cursor.fetchone()
        if row is None:
            return False
        challenger_id, target_id = row
        return user_id in (challenger_id, target_id)

    # Shared gate for confirming a game/match/duel RESULT specifically
    # (elo, payouts, records: real, hard-to-casually-undo consequences).
    # The two team-game confirm views (winner report, cancel game) share
    # this exact "admin or rostered in the CURRENT guild-wide game"
    # check. The tournament-match and duel confirm views use the
    # narrower per-match/per-duel checks above instead, since several of
    # either can be live in the same guild at once. Previously these
    # views were open to literally anyone in the server to click
    # (reporting a winner has always been something anyone at the table
    # could initiate). Now narrowed to just the people who actually have
    # something at stake, or an admin, so a bystander can't finalize (or
    # troll-cancel) a game they were never part of.
    def _isAdminOrInCurrentGame(self, interaction):
        guild_id = interaction.guild_id
        return (
            interaction.user.guild_permissions.manage_guild
            or self.isPlayerInCurrentGame(guild_id, interaction.user.id)
        )

    # Current elo for each (user_id, name) in `roster`, defaulting to this
    # guild's configured default (see _defaultEloForGuild) for anyone
    # without an economy row yet.
    def getEloLookup(self, guild_id, roster, game=None):
        if game is None:
            game = self._activeGame(guild_id)
        default_elo = self._defaultEloForGuild(guild_id)
        lookup = {}
        for user_id, _name in roster:
            elo = self.getGameStat(guild_id, user_id, game, "elo")
            lookup[user_id] = elo if elo is not None else default_elo
        return lookup

    # The two real team names a `team` param resolves against for
    # `match_id` (a specific tournament match) or the current guild-wide
    # game (no match_id). /wager team's own autocomplete, plus
    # resolveWagerTeamValue below, both go through this so a mistyped or
    # stale match_id falls back to the same generic "Team 1"/"Team 2"
    # labels the roster itself would show for an unnamed team.
    # escape=False since these only ever end up in a Choice label or
    # plain comparison, never markdown-rendered message text.
    def getWagerTeamNames(self, guild_id, match_id=None):
        if match_id is not None:
            self.cursor.execute(
                "SELECT team1, team2 FROM tournament_matches WHERE id=? AND guildId=?", (match_id, guild_id)
            )
            row = self.cursor.fetchone()
            if row is not None:
                team1, team2 = Team(), Team()
                team1.deserializeTeam(row[0])
                team2.deserializeTeam(row[1])
                return team1.get_name() or "Team 1", team2.get_name() or "Team 2"
            return "Team 1", "Team 2"
        return (
            self.getRosterName(guild_id, "team1", "Team 1", escape=False),
            self.getRosterName(guild_id, "team2", "Team 2", escape=False),
        )

    # Accepts "1"/"2" (autocomplete's own Choice values) or either
    # team's real name typed out directly (case-insensitive), so
    # someone who ignores the suggestion list and just types the team
    # they mean still works. None if it matches neither, letting the
    # caller reject it. Resolves the caller's free-text `team` input at
    # the command boundary (bot.py's own wagerTeam), not inside
    # wagerHelper itself, so wagerHelper's own signature/tests keep
    # taking the already-resolved 1/2 int exactly as before.
    def resolveWagerTeamValue(self, team_input, name1, name2):
        normalized = team_input.strip()
        if normalized in ("1", "2"):
            return int(normalized)
        lowered = normalized.lower()
        if lowered == name1.lower():
            return 1
        if lowered == name2.lower():
            return 2
        return None

    # `match_id`, when given, bets on that ONE specific tournament match
    # (see _openConcurrentTournamentBetting) instead of the single current
    # casual/ranked/sequential-tournament game, a separate path
    # (_placeTournamentWager) since it's scoped by matchId in
    # `tournament_wagers` rather than the guild-wide `wagers` singleton.
    async def wagerHelper(self, ctx, amount: int, team: int, match_id: int = None):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        # /set betting: a hard off-switch for the whole wagering layer,
        # checked before anything else here so it applies uniformly to a
        # singleton game's own bet and a tournament match's (match_id is
        # not None below), not just one of the two.
        if not self.get(guild_id, "betting_enabled"):
            await ctx.response.send_message("Betting is disabled on this server.", ephemeral=True)
            return

        if amount <= 0:
            await ctx.response.send_message("Wager amount must be greater than 0.", ephemeral=True)
            return

        # /set max-wager: NULL (the default) means no cap, same as before
        # this existed.
        max_wager = self.get(guild_id, "max_wager")
        if max_wager is not None and amount > max_wager:
            await ctx.response.send_message(
                f"A single wager can't be more than {max_wager} gold on this server.", ephemeral=True
            )
            return

        if match_id is not None:
            await self._placeTournamentWager(ctx, guild_id, user_id, amount, team, match_id)
            return

        state = self.get(guild_id, "betting_state")
        if state != "OPEN":
            await ctx.response.send_message(
                "Betting is not currently open. Press Start on the roster message to start a game "
                "and open betting.",
                ephemeral=True,
            )
            return

        if self.isPlayerInCurrentGame(guild_id, user_id):
            await ctx.response.send_message("You can't wager on a game you're playing in!", ephemeral=True)
            return

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        balance = self.getEconomy(guild_id, user_id, "balance")

        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}.", ephemeral=True
            )
            return

        self.cursor.execute(
            "SELECT team FROM wagers WHERE guildId=? AND userId=?", (guild_id, user_id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message("You've already placed a bet on this game.", ephemeral=True)
            return

        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (amount, guild_id, user_id)
        )
        self.cursor.execute(
            "INSERT INTO wagers(guildId, userId, username, team, amount) VALUES(?, ?, ?, ?, ?)",
            (guild_id, user_id, ctx.user.name, team, amount)
        )
        self.db.commit()

        team_name = self.getRosterName(guild_id, "team1" if team == 1 else "team2", f"Team {team}")
        await ctx.response.send_message(
            f"You wagered {amount} gold on **{team_name}**!",
            view=WagerCancelView(self, guild_id, user_id),
        )

    # wagerHelper's match_id path. Same shape as the block above it
    # (state check, self-bet guard, balance check, duplicate-bet guard,
    # escrow, insert) but scoped to one match instead of the whole
    # guild. Several of these can be running at once for a
    # simultaneous-mode round, each independently.
    async def _placeTournamentWager(self, ctx, guild_id, user_id, amount, team, match_id):
        self.cursor.execute(
            "SELECT team1, team2, state, bettingClosed FROM tournament_matches WHERE id=? AND guildId=?",
            (match_id, guild_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await ctx.response.send_message(
                f"No tournament match with id {match_id} in this server.", ephemeral=True
            )
            return
        team1_ser, team2_ser, state, betting_closed = row
        if state == "RESOLVED" or betting_closed:
            await ctx.response.send_message(f"Betting is closed for match #{match_id}.", ephemeral=True)
            return

        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        rostered_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        if user_id in rostered_ids:
            await ctx.response.send_message("You can't wager on a match you're playing in!", ephemeral=True)
            return

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        balance = self.getEconomy(guild_id, user_id, "balance")
        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}.", ephemeral=True
            )
            return

        self.cursor.execute(
            "SELECT team FROM tournament_wagers WHERE matchId=? AND userId=?", (match_id, user_id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message(f"You've already placed a bet on match #{match_id}.", ephemeral=True)
            return

        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (amount, guild_id, user_id)
        )
        self.cursor.execute(
            "INSERT INTO tournament_wagers(matchId, guildId, userId, username, team, amount) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (match_id, guild_id, user_id, ctx.user.name, team, amount)
        )
        self.db.commit()

        team_name = discord.utils.escape_markdown(
            (team1.get_name() if team == 1 else team2.get_name()) or f"Team {team}"
        )
        await ctx.response.send_message(
            f"You wagered {amount} gold on **{team_name}** for match #{match_id}!",
            view=WagerCancelView(self, guild_id, user_id, match_id=match_id),
        )

    # WagerCancelView's Cancel bet button callback, for both the current-
    # game path (match_id is None) and the tournament-match path. Only the
    # bettor themselves can cancel their own bet (interaction.user.id is
    # checked, not just whoever's looking at the message), and only while
    # betting's genuinely still open - the view's own timeout
    # (WAGER_CANCEL_VIEW_TIMEOUT_SECONDS) just bounds how long Discord
    # keeps the button clickable at all; this is what actually decides a
    # given click. The lookup+delete+refund below has no `await` in
    # between, so nothing else can interleave and double-refund a rapid
    # double-click the way BUG-PRONE PATTERN AVOIDED comments elsewhere in
    # this file warn about.
    async def _handleWagerCancelClick(self, interaction, guild_id, user_id, match_id):
        if interaction.user.id != user_id:
            await interaction.response.send_message(
                "Only the person who placed this bet can cancel it.", ephemeral=True
            )
            return

        if match_id is None:
            if self.get(guild_id, "betting_state") != "OPEN":
                await interaction.response.send_message(
                    "Betting's already closed, so this bet can't be cancelled anymore.", ephemeral=True
                )
                return
            self.cursor.execute(
                "SELECT amount FROM wagers WHERE guildId=? AND userId=?", (guild_id, user_id)
            )
            row = self.cursor.fetchone()
            if row is None:
                await interaction.response.send_message(
                    "This bet's already been cancelled or resolved.", ephemeral=True
                )
                return
            amount, = row
            self.cursor.execute("DELETE FROM wagers WHERE guildId=? AND userId=?", (guild_id, user_id))
        else:
            self.cursor.execute(
                "SELECT state, bettingClosed FROM tournament_matches WHERE id=? AND guildId=?",
                (match_id, guild_id)
            )
            match_row = self.cursor.fetchone()
            if match_row is None or match_row[0] == "RESOLVED" or match_row[1]:
                await interaction.response.send_message(
                    f"Betting's already closed for match #{match_id}, so this bet can't be cancelled anymore.",
                    ephemeral=True,
                )
                return
            self.cursor.execute(
                "SELECT amount FROM tournament_wagers WHERE matchId=? AND userId=?", (match_id, user_id)
            )
            row = self.cursor.fetchone()
            if row is None:
                await interaction.response.send_message(
                    "This bet's already been cancelled or resolved.", ephemeral=True
                )
                return
            amount, = row
            self.cursor.execute(
                "DELETE FROM tournament_wagers WHERE matchId=? AND userId=?", (match_id, user_id)
            )

        self.cursor.execute(
            "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?", (amount, guild_id, user_id)
        )
        self.db.commit()

        await interaction.response.edit_message(content=f"Bet cancelled - refunded {amount} gold.", view=None)

    # This guild's own configured betting-window length (/set
    # betting-timer's param), or BETTING_DURATION_SECONDS for a guild
    # that's never set one. Doesn't go through self.get(): that crashes
    # outright if there's no `servers` row for this guild at all, which
    # a real guild always has by the time any command can run (see
    # on_guild_join), but /test's simulated tournament has no reason to
    # require one just to open a betting window.
    def _getBettingTimerSeconds(self, guild_id):
        self.cursor.execute("SELECT betting_timer_seconds FROM servers WHERE guildId=?", (guild_id,))
        row = self.cursor.fetchone()
        if row is None or row[0] is None:
            return BETTING_DURATION_SECONDS
        return int(row[0])

    # Resolves an admin-configured channel-name column (wager_channel,
    # matchup_channel) to an actual channel, falling back to `fallback`
    # (wherever the game/match actually happened to run) if the column is
    # unset or no longer resolves to a real channel. Shared by every
    # "redirect this family of postings elsewhere" setting.
    def _resolveConfiguredChannel(self, guild_id, column, fallback):
        name = self.get(guild_id, column)
        if not name:
            return fallback
        guild = self.client.get_guild(guild_id) if self.client is not None else None
        if guild is None:
            return fallback
        resolved = discord.utils.get(guild.channels, name=name)
        return resolved if resolved is not None else fallback

    # Resolves a stored channel id (betting_channel_id, the wager-channel-
    # resolved channel a betting round's open/closed notices actually
    # live in) back to a channel object, best-effort. Needed wherever a
    # caller only has the id on hand rather than the channel the
    # interaction itself came from - e.g. recordResult, whose own
    # `channel` param now follows /set matchup-channel instead, a
    # possibly different channel than where the betting-closed notice
    # this deletes was actually posted.
    async def _resolveChannelId(self, channel_id):
        if channel_id is None or self.client is None:
            return None
        try:
            resolved = self.client.get_channel(int(channel_id))
            if resolved is None:
                resolved = await self.client.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
        return resolved

    # Where whichever matchup graphic this guild's CURRENT game, or a
    # specific tournament `match_id`, is actually showing lives:
    # (channel_id, message_id), channel_id None when the caller's own
    # fallback channel is the right one.
    #
    # An explicit match_id (a simultaneous-mode match, resolved directly
    # from a report click rather than through recordResult/
    # active_tournament_match_id) looks up that match's own row, whose
    # messageId/channelId is whichever of _postReadyCheck's ready-check
    # message or _postMatchReport's report message posted it.
    #
    # Without one, this falls back to guild_id's CURRENT game:
    # active_tournament_match_id (already set by _handleReadyClick before
    # it calls _openBetting, and still set when recordResult later
    # resolves that same game) means a sequential tournament match, same
    # per-match row lookup as above. Otherwise it's a casual/ranked game,
    # which has no channel of its own tracked for matchup_message_id -
    # it's always wherever /set matchup-channel most recently resolved
    # to, the same place a caller of this already has on hand.
    #
    # None from any of these paths if nothing's actually resolvable.
    def _matchupMessageLocation(self, guild_id, match_id=None):
        if match_id is None:
            match_id = self.get(guild_id, "active_tournament_match_id")

        if match_id is not None:
            self.cursor.execute(
                "SELECT messageId, channelId FROM tournament_matches WHERE id=?", (match_id,)
            )
            row = self.cursor.fetchone()
            if row is None or row[0] is None:
                return None
            message_id, channel_id = row
            return channel_id, message_id

        message_id = self.get(guild_id, "matchup_message_id")
        if message_id is None:
            return None
        return None, message_id

    # A jump-to-message link for _matchupMessageLocation's target, for
    # _openBetting's "betting is open" text to point at.
    def _matchupGraphicLink(self, guild_id, report_channel):
        location = self._matchupMessageLocation(guild_id)
        if location is None:
            return None
        channel_id, message_id = location
        if channel_id is None:
            channel_id = report_channel.id
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    # The matchup graphic message itself (not just a link), for a caller
    # that wants to reply to it directly (recordResult, and every
    # tournament match-resolution path's own result line). Best-effort:
    # None if nothing's tracked, the channel can't be resolved, or the
    # message was already deleted.
    async def _fetchMatchupMessage(self, guild_id, fallback_channel, match_id=None):
        location = self._matchupMessageLocation(guild_id, match_id)
        if location is None:
            return None
        channel_id, message_id = location
        channel = fallback_channel
        if channel_id is not None:
            channel = await self._resolveChannelId(channel_id) or fallback_channel
        try:
            return await channel.fetch_message(int(message_id))
        except discord.HTTPException:
            return None

    # Core of the above, taking guild_id/channel directly rather than a
    # full Interaction. /tournament start's sequential mode calls this
    # too, from a reaction handler that has no ctx to hand it. Cancels
    # and refunds any previous unresolved game first so re-opening
    # never leaves an orphaned timer or stranded bets behind.
    #
    # The winner-report message goes out immediately, right alongside
    # the "betting is open" one, rather than waiting for the timer to
    # close betting first. A real game doesn't wait for a 60-second
    # countdown to finish before anyone knows who won, so
    # WinnerReportView's buttons (Team 1/Team 2, and Cancel Game, the
    # button replacement for the old /return command) are live on this
    # same message from the moment the game starts.
    # _handleWinnerReportPick accepts either while betting_state is OPEN
    # or CLOSED, so a fast game can be reported before the window even
    # closes.
    async def _openBetting(self, guild_id, channel):
        # /set wager-channel redirects the betting-open/closed notices to
        # one channel. /set matchup-channel redirects the winner-report
        # message (with its Team 1/Team 2/Cancel Game buttons) to another,
        # independently - these can point at two different channels now.
        # Once betting_channel_id below points at the wager one,
        # everything downstream that's about the WAGER side (the timer,
        # the closed notice, reconcileStaleBettingWindows) just follows it
        # through naturally. The report message needs no such tracking:
        # every later step that touches it (_handleWinnerReportPick,
        # recordResult) already works off interaction.channel instead.
        wager_channel = self._resolveConfiguredChannel(guild_id, "wager_channel", channel)
        report_channel = self._resolveConfiguredChannel(guild_id, "matchup_channel", channel)

        await self.cancelBettingHelper(guild_id, wager_channel)

        self.update(guild_id, "betting_state", "OPEN")
        self.update(guild_id, "betting_channel_id", wager_channel.id)
        # Read back by reconcileStaleBettingWindows (called from on_ready)
        # to work out how much of the window was actually left if the bot
        # restarts mid-window; the in-memory timer task below doesn't
        # survive that, only a process reconnect.
        self.update(guild_id, "betting_opened_at", int(time.time()))

        team1_name = self.getRosterName(guild_id, "team1", "Team 1", escape=False)
        team2_name = self.getRosterName(guild_id, "team2", "Team 2", escape=False)

        # /set betting: with wagering off, there's nothing for a timer to
        # close, and inviting people to a /wager command that's just going
        # to reject them would be confusing, so there's no separate
        # "betting is open" message at all, just the report message below.
        betting_enabled = bool(self.get(guild_id, "betting_enabled"))
        duration = self._getBettingTimerSeconds(guild_id)
        if betting_enabled:
            open_content = (
                f"🎲 Betting is open on **{team1_name}** vs **{team2_name}**! Use "
                f"`/wager team <amount> <team>` to bet on this game (closes in {duration} seconds)."
            )
            graphic_link = self._matchupGraphicLink(guild_id, report_channel)
            if graphic_link is not None:
                open_content += f"\n{graphic_link}"
            await wager_channel.send(open_content)
            report_content = (
                "🎮 Once the game ends, press the winning team's button below to report it (you'll "
                "be asked to confirm before it's recorded and bets are paid out), or Cancel Game to "
                "cancel the game."
            )
        else:
            report_content = (
                "🎮 Game started! Once it ends, press the winning team's button below to report it "
                "(you'll be asked to confirm before it's recorded), or Cancel Game to cancel the game."
            )
        msg = await report_channel.send(report_content, view=WinnerReportView(self, team1_name, team2_name))

        self.update(guild_id, "betting_message_id", msg.id)

        if not betting_enabled:
            return

        # BUG-PRONE PATTERN AVOIDED: awaiting asyncio.sleep() directly
        # inside this command handler would still (technically) let
        # other interactions run, since asyncio.sleep() yields control.
        # But it would keep this command's own Interaction/task alive
        # and blocked for a full minute, and a cancelled game
        # (CANCEL_GAME_EMOJI) would have no way to stop it from firing
        # later. Running it as its own Task makes both of those explicit
        # and lets cancelBettingHelper cancel it.
        task = asyncio.create_task(self._bettingTimer(guild_id, wager_channel, duration))
        self.bettingTasks[guild_id] = task

    # The actual "close it" side effect a betting window's own expiry
    # has, split out of _bettingTimer so reconcileStaleBettingWindows
    # can reach it directly too, for a window whose remaining time had
    # already elapsed by the time the bot came back up. The
    # winner-report message (and its buttons) already went out with the
    # "betting is open" one in _openBetting, so there's nothing left for
    # this to post beyond the closed notice itself.
    async def _closeBettingWindow(self, guild_id, channel):
        self.update(guild_id, "betting_state", "CLOSED")
        msg = await channel.send("🔒 Betting is now closed! No more wagers will be accepted for this game.")
        # Read back by recordResult once the game this window was for is
        # actually scored, so this notice doesn't linger in the channel
        # after it's no longer relevant. NULL whenever a winner is
        # reported before the timer ever gets here in the first place.
        self.update(guild_id, "betting_closed_message_id", msg.id)

    # Just waits out the configured duration, then closes the window.
    async def _bettingTimer(self, guild_id, channel, duration):
        try:
            await asyncio.sleep(duration)
            await self._closeBettingWindow(guild_id, channel)
        except asyncio.CancelledError:
            # CANCEL_GAME_EMOJI (or a fresh ▶️ click) ended the game
            # before betting closed. cancelBettingHelper already handles
            # the refund, nothing more to do here.
            pass
        finally:
            self.bettingTasks.pop(guild_id, None)

    # Stops the running betting timer (if any) without touching wagers
    # or betting_state, the one piece cancelBettingHelper and
    # _handleWinnerReportPick both need before they go on to actually
    # resolve or cancel the round, since a winner can now be reported
    # (or the game cancelled) while the timer's still counting down.
    def _cancelBettingTimerTask(self, guild_id):
        task = self.bettingTasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    # Deletes any /team invite, /wager against challenge, or /team transfer
    # nobody ever answered within PENDING_INVITE_EXPIRY_SECONDS. Called
    # periodically (see bot.py's expireInvitesTask) rather than scheduling
    # a real timer per invite: unlike the betting timers below, whose
    # whole point is firing at a precise moment, a 24-hour window is long
    # enough that a coarse periodic sweep is plenty, and it means a
    # pending invite doesn't need any reconciliation on restart the way an
    # in-flight betting countdown does. Only ever touches PENDING_ACCEPT
    # duels - an accepted one has real gold escrowed and stays open
    # indefinitely regardless of age, same as today. A NULL createdAt (a
    # row from before that column existed) counts as already expired
    # rather than guessing how old it actually is. Returns
    # (invites_expired, duels_expired, transfers_expired) purely for the
    # caller's own logging.
    def expireStalePendingInvites(self):
        cutoff = int(time.time()) - PENDING_INVITE_EXPIRY_SECONDS
        self.cursor.execute(
            "DELETE FROM team_invites WHERE createdAt IS NULL OR createdAt < ?", (cutoff,)
        )
        invites_expired = self.cursor.rowcount
        self.cursor.execute(
            "DELETE FROM duels WHERE state='PENDING_ACCEPT' AND (createdAt IS NULL OR createdAt < ?)",
            (cutoff,)
        )
        duels_expired = self.cursor.rowcount
        self.cursor.execute(
            "DELETE FROM team_transfers WHERE createdAt IS NULL OR createdAt < ?", (cutoff,)
        )
        transfers_expired = self.cursor.rowcount
        self.db.commit()
        return invites_expired, duels_expired, transfers_expired

    # Called once from on_ready. _bettingTimer's own countdown is only
    # ever an in-memory asyncio.Task (self.bettingTasks), lost on a
    # genuine process restart, though not on a mere gateway reconnect,
    # where on_ready can also fire but self.bettingTasks is untouched
    # (guarded against below by skipping any guild that already has a
    # live task, so a reconnect can't stomp a window that was never
    # actually interrupted). Without this, a guild whose betting_state
    # was OPEN at the moment of a restart would stay OPEN forever,
    # nobody left to ever flip it to CLOSED, so /wager team would keep
    # accepting new bets indefinitely past when the timer should have
    # closed it. Resumes the remaining time via betting_opened_at rather
    # than just closing outright, so a window that had, say, 55 of its
    # 60 seconds left when the bot restarted still gets roughly that
    # long rather than being cut short.
    async def reconcileStaleBettingWindows(self, client):
        self.cursor.execute(
            "SELECT guildId, betting_channel_id, betting_opened_at FROM servers WHERE betting_state='OPEN'"
        )
        rows = self.cursor.fetchall()
        for guild_id, channel_id, opened_at in rows:
            if guild_id in self.bettingTasks:
                continue
            if not self.get(guild_id, "betting_enabled"):
                # _openBetting never started a timer for this guild in the
                # first place (see its own betting_enabled check), so
                # there's nothing to resume, and closing it now would post
                # a "Betting is now closed!" notice for wagering that was
                # never actually open. betting_state stays OPEN either way
                # (reporting a winner still works fine); it's just never
                # this function's job to touch a betting-disabled guild.
                continue
            channel = client.get_channel(channel_id) if channel_id is not None else None
            if channel is None:
                # Channel deleted, or not yet in cache, leave the window
                # open rather than guessing; it's still fully resolvable
                # by hand via the report/cancel buttons either way.
                continue

            duration = self._getBettingTimerSeconds(guild_id)
            # opened_at is only unset for a window that was already open
            # before this column existed, treated as already expired
            # rather than guessing how long ago it actually opened.
            elapsed = (int(time.time()) - opened_at) if opened_at is not None else duration
            remaining = duration - elapsed

            if remaining > 0:
                task = asyncio.create_task(self._bettingTimer(guild_id, channel, remaining))
                self.bettingTasks[guild_id] = task
            else:
                await self._closeBettingWindow(guild_id, channel)

    # Undoes _handleWinnerReportPick's own synchronous
    # betting_message_id clear once a pending winner confirmation is
    # cancelled or times out, so clicking a button on the original
    # report message works again instead of leaving the game stuck with
    # no way to report a winner at all. Only restores it if nothing else
    # has since resolved the game a different way (a fresh
    # betting_message_id already set, or betting_state no longer
    # OPEN/CLOSED, e.g. 🛑 or a new team-forming command's own
    # clearTeamsHelper cancelled it out from under the pending
    # confirmation). Otherwise this would stomp whatever that newer
    # state actually is.
    def _restoreWinnerReportMessage(self, guild_id, report_message_id):
        if (
            self.get(guild_id, "betting_state") in ("OPEN", "CLOSED")
            and self.get(guild_id, "betting_message_id") is None
        ):
            self.update(guild_id, "betting_message_id", report_message_id)

    # Strips a message's buttons once its flow has actually concluded (a
    # winner recorded, or the game cancelled), so a resolved
    # winner-report message doesn't keep showing Team 1/Team 2/Cancel
    # Game as if it were still live. `message` is optional only so
    # callers/tests that don't have a real message object (or don't
    # care) can skip it. A message that's already been deleted (or a
    # stale/mocked one in a test) just no-ops rather than raising.
    async def _clearMessageButtons(self, message):
        if message is None:
            return
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass

    # Same "optional/already-gone message just no-ops" shape as
    # _clearMessageButtons above, for callers that want the message gone
    # entirely rather than just stripped of its buttons. recordResult's
    # own betting/make-teams cleanup, and the winner-confirmed flows'
    # report/confirmation messages once the result they were for is
    # actually recorded.
    async def _deleteMessageSafely(self, message):
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    # Fetches and deletes a message by id in `channel`, same best-effort
    # shape as _deleteMessageSafely for callers that only have an id
    # (recordResult's own betting_closed_message_id/make_teams_message_ids
    # cleanup, and the tournament round-betting cleanup), not a live
    # message object already in hand.
    async def _deleteMessageIdSafely(self, channel, message_id):
        if channel is None or message_id is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.HTTPException:
            return
        await self._deleteMessageSafely(message)

    # Resolves roster_channel_id to an actual channel object, best-effort
    # (None if it's unset, or the client/guild can't resolve it right
    # now). Shared by _deleteMakeTeamsIntroMessages and
    # _deleteMakeTeamsMessages, which each need to reach the same
    # roster channel for a different subset of its "make teams"
    # messages.
    async def _resolveRosterChannel(self, guild_id):
        channel_id = self.get(guild_id, "roster_channel_id")
        if channel_id is None or self.client is None:
            return None
        try:
            channel = self.client.get_channel(int(channel_id))
            if channel is None:
                channel = await self.client.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
        return channel

    # The "make teams" INTRO text message(s) (the "Teams created!"-style
    # reply, plus a draft's own picker/pool/"Both teams are set!"
    # messages, see make_teams_message_ids) AND the roster embeds
    # themselves (roster_team1_message_id/roster_team2_message_id) for the
    # CURRENT roster. Called by _handleRosterStartClick right after
    # /start's own matchup graphic posts, since the graphic already shows
    # both full rosters and none of these have anything left to say by
    # then.
    async def _deleteMakeTeamsIntroMessages(self, guild_id):
        channel = await self._resolveRosterChannel(guild_id)
        if channel is None:
            return
        extra_ids = self.get(guild_id, "make_teams_message_ids")
        if extra_ids:
            for message_id in extra_ids.split(","):
                if message_id:
                    await self._deleteMessageIdSafely(channel, int(message_id))
        self.update(guild_id, "make_teams_message_ids", None)

        for column in ("roster_team1_message_id", "roster_team2_message_id"):
            message_id = self.get(guild_id, column)
            if message_id is not None:
                await self._deleteMessageIdSafely(channel, int(message_id))
            self.update(guild_id, column, None)

    # The roster embeds (team1/team2) for the JUST-RESOLVED game, now
    # that recordResult is done with them. Called once the game they
    # were for is actually scored, never for a tournament match (which
    # never goes through /make-teams at all, so these columns would
    # just be stale leftovers from an unrelated earlier game).
    # roster_team1_message_id/roster_team2_message_id and
    # make_teams_message_ids are normally already empty by this point
    # (see _deleteMakeTeamsIntroMessages), but are swept here too in case
    # a game somehow ended without ever going through a Start click.
    async def _deleteMakeTeamsMessages(self, guild_id):
        channel = await self._resolveRosterChannel(guild_id)
        if channel is None:
            return

        message_ids = []
        for column in ("roster_team1_message_id", "roster_team2_message_id"):
            value = self.get(guild_id, column)
            if value is not None:
                message_ids.append(int(value))
        extra_ids = self.get(guild_id, "make_teams_message_ids")
        if extra_ids:
            message_ids.extend(int(x) for x in extra_ids.split(",") if x)

        for message_id in message_ids:
            await self._deleteMessageIdSafely(channel, message_id)

        self.update(guild_id, "roster_team1_message_id", None)
        self.update(guild_id, "roster_team2_message_id", None)
        self.update(guild_id, "make_teams_message_ids", None)

    # WinnerReportView's Team 1/Team 2 button callback. A pick no longer
    # records the result immediately. It posts a ConfirmWinnerReportView
    # instead (Confirm actually calls recordResult, then strips this
    # message's own buttons via _clearMessageButtons; Cancel/timeout
    # restores the report message via _restoreWinnerReportMessage so its
    # buttons work again). A real elo/payout/game-record change
    # shouldn't hinge on a single accidental click. Valid while
    # betting_state is OPEN or CLOSED, not just after the timer's own
    # window has closed.
    async def _handleWinnerReportPick(self, interaction, winning_team):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        if not self._isAdminOrInCurrentGame(interaction):
            await interaction.response.send_message(
                "Only a player in this game, or a member with the Manage Server permission, can report a winner.",
                ephemeral=True,
            )
            return

        state = self.get(guild_id, "betting_state")
        if state not in ("OPEN", "CLOSED"):
            await interaction.response.send_message(
                "There's no open betting round to report a winner for.", ephemeral=True
            )
            return

        stored_message_id = self.get(guild_id, "betting_message_id")
        if stored_message_id is None or int(stored_message_id) != interaction.message.id:
            await interaction.response.send_message(
                "This game has already been reported or cancelled.", ephemeral=True
            )
            return

        # BUG-PRONE PATTERN AVOIDED: clear the stored message id before
        # doing anything async below, so a second click on this same
        # message (the other team, or Cancel Game) can't also pass the
        # check above and double-process the same game. Cleared here
        # rather than flipping betting_state itself, since the cancel
        # button's own handler still needs to read betting_state as it
        # actually is to decide whether there's anything to refund.
        self.update(guild_id, "betting_message_id", None)

        self._cancelBettingTimerTask(guild_id)

        # Close betting the moment a report click lands, win-confirmed
        # or not. Otherwise /wager team stays open for the entire
        # confirmation window (state is still OPEN/CLOSED either way,
        # and nothing else here touches it), letting someone place a
        # brand new bet on whichever side just got reported as the
        # winner before it's even confirmed. Left CLOSED even if the
        # report is later cancelled, matching how a report has always
        # cleared every wager for good the instant it landed, confirmed
        # or not.
        if state == "OPEN":
            self.update(guild_id, "betting_state", "CLOSED")

        name = self.getRosterName(guild_id, "team1" if winning_team == 1 else "team2", f"Team {winning_team}")
        view = ConfirmWinnerReportView(
            self, guild_id, winning_team, interaction.message.id, report_message=interaction.message
        )
        await interaction.response.send_message(
            f"**{name}** reported as the winner. Confirm to finalize it (records elo and pays out "
            "bets), or Cancel to report again.",
            view=view,
        )
        view.message = await interaction.original_response()

    # WinnerReportView's Cancel Game button callback. A click no longer
    # cancels immediately. It posts a ConfirmCancelGameView instead
    # (Confirm actually calls _finishGameCancel; Cancel/timeout restores
    # the report message via _restoreWinnerReportMessage so its buttons
    # work again), the same two-step shape _handleWinnerReportPick
    # uses, since this button sits on the exact same message and
    # message id.
    async def _handleWinnerReportCancelClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        if not self._isAdminOrInCurrentGame(interaction):
            await interaction.response.send_message(
                "Only a player in this game, or a member with the Manage Server permission, can cancel it.",
                ephemeral=True,
            )
            return

        state = self.get(guild_id, "betting_state")
        if state not in ("OPEN", "CLOSED"):
            await interaction.response.send_message(
                "There's no open betting round to cancel.", ephemeral=True
            )
            return

        stored_message_id = self.get(guild_id, "betting_message_id")
        if stored_message_id is None or int(stored_message_id) != interaction.message.id:
            await interaction.response.send_message(
                "This game has already been reported or cancelled.", ephemeral=True
            )
            return

        self.update(guild_id, "betting_message_id", None)
        self._cancelBettingTimerTask(guild_id)

        view = ConfirmCancelGameView(self, guild_id, interaction.message.id, report_message=interaction.message)
        await interaction.response.send_message(
            "Cancel this game? Any open bets get refunded and everyone moves back to the original "
            "channel. Confirm to cancel, or Cancel to keep playing.",
            view=view,
        )
        view.message = await interaction.original_response()

    # ConfirmCancelGameView's Confirm button callback, refunds any open
    # bets and moves everyone back to the original channel (the same two
    # things the old /return command did), then deletes the original
    # report message outright - it has nothing left to say once the game
    # it was reporting on is gone. (ConfirmCancelGameView.confirm itself
    # deletes the confirmation prompt.)
    async def _finishGameCancel(self, guild_id, channel, guild, report_message=None):
        await self.cancelGameHelper(guild_id, channel, guild)
        await self._deleteMessageSafely(report_message)

    # cancelGameHelper: refunds any open bets and moves everyone back to
    # the original channel. `cancelBettingHelper` handles the
    # refund/state-reset/tournament-hook-clear. This just adds the
    # voice-channel move on top, since cancelBettingHelper alone is also
    # used by _openBetting to silently clear a stale round before
    # opening a fresh one, where moving anyone would be wrong.
    async def cancelGameHelper(self, guild_id, channel, guild):
        # Same "stay visually anchored to the graphic" reasoning
        # recordResult's own result message follows (see
        # _matchupMessageLocation); best-effort, so a game that never
        # actually got a matchup graphic just falls back to a plain send.
        matchup_message = await self._fetchMatchupMessage(guild_id, channel)
        await channel.send(f"{CANCEL_GAME_EMOJI} Game cancelled.", reference=matchup_message)
        await self.cancelBettingHelper(guild_id, channel)

        if guild is not None and await self.moveMembersToOriginalChannel(guild):
            await channel.send("Moved everyone back to the original channel!")

    # Pari-mutuel payout: winners split the losing side's pool
    # proportional to their own wager, on top of getting their own
    # wager back, so a bet on the less-backed (riskier) side pays out
    # more than a bet on the heavily-favored side. Also moves everyone
    # back to the original channel once the result is settled.
    # Reporting a winner ends the game, no separate cancel/return
    # needed. `guild` is optional only so callers/tests that don't care
    # about the move can omit it.
    async def recordResult(self, guild_id, winning_team, channel, guild=None):
        # Captured before active_tournament_match_id gets cleared below,
        # so the make-teams cleanup near the end knows whether this game
        # ever went through /make-teams at all. A tournament match never
        # does (see _handleReadyClick), so roster_team1_message_id and
        # friends would just be stale leftovers from an unrelated
        # earlier game there, not this match's own messages.
        is_tournament_match = self.get(guild_id, "active_tournament_match_id") is not None

        self.cursor.execute(
            "SELECT userId, username, team, amount FROM wagers WHERE guildId=?", (guild_id,)
        )
        allWagers = self.cursor.fetchall()

        self.cursor.execute("DELETE FROM wagers WHERE guildId=?", (guild_id,))
        self.update(guild_id, "betting_state", "NONE")
        self.update(guild_id, "betting_message_id", None)
        self.db.commit()

        team1_roster = self.getRosterPlayers(guild_id, "team1")
        team2_roster = self.getRosterPlayers(guild_id, "team2")
        team1_name = self.getRosterName(guild_id, "team1", "Team 1")
        team2_name = self.getRosterName(guild_id, "team2", "Team 2")
        game = self._activeGame(guild_id)
        elo_lookup = self.getEloLookup(guild_id, team1_roster + team2_roster, game)
        is_ranked = bool(self.get(guild_id, "is_ranked"))
        disliked_role_user_ids = self._dislikedRoleUserIds(guild_id)

        deltas, summary = self.computeGameDeltas(
            allWagers, team1_roster, team2_roster, elo_lookup, winning_team, is_ranked,
            default_elo=self._defaultEloForGuild(guild_id),
            team1_name=team1_name, team2_name=team2_name,
            disliked_role_user_ids=disliked_role_user_ids,
        )
        newly_unlocked = self.applyGameDeltas(guild_id, deltas, game)
        self.saveLastResult(
            guild_id, winning_team, allWagers, team1_roster, team2_roster, deltas, is_ranked,
            team1_name=team1_name, team2_name=team2_name,
            disliked_role_user_ids=disliked_role_user_ids, game=game,
        )

        # Replies to the game/match's own matchup graphic (_sendMatchupImage
        # for a casual/ranked game, _postReadyCheck's ready-check message
        # for a sequential tournament match - see _matchupMessageLocation)
        # when there is one, so the result stays visually anchored to it
        # instead of just landing further down the channel. Best-effort:
        # an already-deleted message just falls back to a plain,
        # un-replied send.
        matchup_message = await self._fetchMatchupMessage(guild_id, channel)
        if not is_tournament_match:
            self.update(guild_id, "matchup_message_id", None)

        await channel.send(self.formatResultMessage(winning_team, summary), reference=matchup_message)
        await self._announceAchievements(channel, newly_unlocked)

        if guild is not None and await self.moveMembersToOriginalChannel(guild):
            await channel.send("Moved everyone back to the original channel!")

        # The winner-report message itself is deleted by whichever
        # Confirm view called into this (it's the one holding that
        # message, not this function). This is just the separate
        # "Betting is now closed!" notice _closeBettingWindow may have
        # posted after it, if the timer beat the report to it - in the
        # wager-channel-resolved channel (betting_channel_id), which
        # /set matchup-channel can now leave pointed somewhere other than
        # `channel` (the report message's own channel) above.
        betting_channel = await self._resolveChannelId(self.get(guild_id, "betting_channel_id"))
        await self._deleteMessageIdSafely(betting_channel or channel, self.get(guild_id, "betting_closed_message_id"))
        self.update(guild_id, "betting_closed_message_id", None)

        if not is_tournament_match:
            await self._deleteMakeTeamsMessages(guild_id)

        # /tournament start (sequential mode) routes its matches through
        # this exact same betting/report cycle by temporarily setting
        # team1/team2 to the match's two teams.
        # active_tournament_match_id is how this function knows the game
        # it just resolved was one of those, so it can also advance the
        # bracket once the normal payout/elo handling above is done.
        active_match_id = self.get(guild_id, "active_tournament_match_id")
        if active_match_id is not None:
            self.update(guild_id, "active_tournament_match_id", None)
            await self._resolveTournamentMatch(guild_id, active_match_id, winning_team, channel.id)

    # ---------------- Duels (/wager against) ----------------

    # Challenges `member` to a heads-up wager for `amount` gold,
    # independent of any team game. Posts a message mentioning them
    # with a DuelAcceptView button. The duel only actually escrows gold
    # once they press it (see _handleDuelAcceptClick), so nothing is
    # held here.
    async def challengeDuelHelper(self, ctx, member, amount):
        guild_id = ctx.guild.id
        challenger = ctx.user

        # /set betting: same hard off-switch wagerHelper's own checks
        # enforce, since a heads-up duel is just as much "betting" as a
        # team-game wager is.
        if not self.get(guild_id, "betting_enabled"):
            await ctx.response.send_message("Betting is disabled on this server.", ephemeral=True)
            return

        if member.id == challenger.id:
            await ctx.response.send_message("You can't wager against yourself!", ephemeral=True)
            return

        if member.bot:
            await ctx.response.send_message("You can't wager against a bot!", ephemeral=True)
            return

        if amount <= 0:
            await ctx.response.send_message("Wager amount must be greater than 0.", ephemeral=True)
            return

        max_wager = self.get(guild_id, "max_wager")
        if max_wager is not None and amount > max_wager:
            await ctx.response.send_message(
                f"A single wager can't be more than {max_wager} gold on this server.", ephemeral=True
            )
            return

        self.cursor.execute(
            "SELECT 1 FROM duels WHERE guildId=? AND challengerId=? AND targetId=? AND state='PENDING_ACCEPT'",
            (guild_id, challenger.id, member.id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message(
                f"You already have a pending challenge against {member.mention}. Cancel it first if you "
                "want to send a different one.",
                ephemeral=True,
            )
            return

        self.ensureEconomyRow(guild_id, challenger.id, challenger.name)
        balance = self.getEconomy(guild_id, challenger.id, "balance")
        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}.", ephemeral=True
            )
            return

        self.cursor.execute(
            "INSERT INTO duels(guildId, channelId, messageId, challengerId, challengerName, "
            "targetId, targetName, amount, state, createdAt) "
            "VALUES(?, ?, NULL, ?, ?, ?, ?, ?, 'PENDING_ACCEPT', ?)",
            (
                guild_id, ctx.channel.id, challenger.id, challenger.name, member.id, member.name, amount,
                int(time.time()),
            )
        )
        self.db.commit()
        duel_id = self.cursor.lastrowid

        await ctx.response.send_message(
            f"{member.mention}, {challenger.mention} has challenged you to a **{amount} gold** wager! "
            "Press Accept below to take the bet.",
            view=DuelAcceptView(self),
        )
        msg = await ctx.original_response()

        self.cursor.execute("UPDATE duels SET messageId=? WHERE id=?", (msg.id, duel_id))
        self.db.commit()

    # DuelAcceptView's Accept button callback, re-derives which duel (and
    # whether this clicker is actually the challenged player) from the
    # interaction itself, since the view is a single shared persistent
    # instance with nothing duel-specific stored on it.
    async def _handleDuelAcceptClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, channelId, challengerId, challengerName, targetId, targetName, amount "
            "FROM duels WHERE guildId=? AND messageId=? AND state='PENDING_ACCEPT'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This challenge is no longer pending.", ephemeral=True
            )
            return
        duel_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount = row

        # Only the challenged player can accept their own challenge.
        if interaction.user.id != target_id:
            await interaction.response.send_message(
                "Only the challenged player can accept this.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self._acceptDuel(
            guild_id, duel_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount
        )
        await self._clearMessageButtons(interaction.message)

    # DuelAcceptView's Decline button callback, the reverse of Accept
    # above: same "only the challenged player" gate, but just deletes the
    # pending duel row outright rather than moving it forward. No gold to
    # refund - _acceptDuel is the only place a duel's amount ever actually
    # leaves anyone's balance.
    async def _handleDuelDeclineClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, challengerName, targetId, targetName "
            "FROM duels WHERE guildId=? AND messageId=? AND state='PENDING_ACCEPT'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This challenge is no longer pending.", ephemeral=True
            )
            return
        duel_id, challenger_name, target_id, target_name = row

        # Only the challenged player can decline their own challenge.
        if interaction.user.id != target_id:
            await interaction.response.send_message(
                "Only the challenged player can decline this.", ephemeral=True
            )
            return

        self.cursor.execute("DELETE FROM duels WHERE id=?", (duel_id,))
        self.db.commit()

        await interaction.response.send_message(
            f"**{target_name}** declined **{challenger_name}**'s wager."
        )
        await self._clearMessageButtons(interaction.message)

    # DuelAcceptView's Cancel challenge button callback, the challenger's
    # own side of _handleDuelDeclineClick right above: retracting a
    # challenge they regret sending rather than leaving it for the target
    # to either accept or decline. Same "no gold escrowed yet" reasoning
    # as Decline - nothing to refund, just a row to delete.
    async def _handleDuelRetractClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, challengerId, challengerName, targetName "
            "FROM duels WHERE guildId=? AND messageId=? AND state='PENDING_ACCEPT'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This challenge is no longer pending.", ephemeral=True
            )
            return
        duel_id, challenger_id, challenger_name, target_name = row

        if interaction.user.id != challenger_id:
            await interaction.response.send_message(
                "Only the challenger can cancel this.", ephemeral=True
            )
            return

        self.cursor.execute("DELETE FROM duels WHERE id=?", (duel_id,))
        self.db.commit()

        await interaction.response.send_message(
            f"**{challenger_name}** cancelled the challenge against **{target_name}**."
        )
        await self._clearMessageButtons(interaction.message)

    # DuelResultView's Challenger Won/Target Won button callback. A
    # result no longer pays out immediately. It posts a
    # ConfirmDuelResultView instead (Confirm actually pays out via
    # _finishDuelResolution; Cancel/timeout restores the duel via
    # _restoreDuelAwaitingResult so its buttons work again), matching
    # WinnerReportView/ConfirmWinnerReportView's two-step shape for the
    # exact same reason: a real gold transfer shouldn't hinge on a
    # single accidental click.
    async def _handleDuelResultClick(self, interaction, winner_is_challenger):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, challengerId, challengerName, targetId, targetName FROM duels "
            "WHERE guildId=? AND messageId=? AND state='AWAITING_RESULT'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This duel has already been reported or is no longer pending.", ephemeral=True
            )
            return
        duel_id, challenger_id, challenger_name, target_id, target_name = row

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.id in (challenger_id, target_id)
        ):
            await interaction.response.send_message(
                "Only a participant in this duel, or a member with the Manage Server permission, can "
                "report a result.",
                ephemeral=True,
            )
            return

        # BUG-PRONE PATTERN AVOIDED: flip the state before doing anything
        # async below, so a second near-simultaneous click (the other
        # result button) can't also pass the check above and double-
        # process the same duel.
        self.cursor.execute("UPDATE duels SET state='CONFIRMING' WHERE id=?", (duel_id,))
        self.db.commit()

        winner_name = challenger_name if winner_is_challenger else target_name
        view = ConfirmDuelResultView(self, duel_id, winner_is_challenger, report_message=interaction.message)
        await interaction.response.send_message(
            f"**{winner_name}** reported as the winner. Confirm to pay out the wager, or Cancel to "
            "report again.",
            view=view,
        )
        view.message = await interaction.original_response()

    # DuelResultView's Cancel Duel button callback, same participant-or-
    # admin gate and CONFIRMING flip _handleDuelResultClick uses above,
    # just for the opposite outcome (a refund instead of a payout).
    async def _handleDuelCancelClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT id, challengerId, targetId FROM duels "
            "WHERE guildId=? AND messageId=? AND state='AWAITING_RESULT'",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message(
                "This duel has already been reported or is no longer pending.", ephemeral=True
            )
            return
        duel_id, challenger_id, target_id = row

        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.id in (challenger_id, target_id)
        ):
            await interaction.response.send_message(
                "Only a participant in this duel, or a member with the Manage Server permission, can "
                "cancel it.",
                ephemeral=True,
            )
            return

        # BUG-PRONE PATTERN AVOIDED: flip the state before doing anything
        # async below, so a second near-simultaneous click (a result
        # button, or another cancel click) can't also pass the check
        # above and double-process the same duel.
        self.cursor.execute("UPDATE duels SET state='CONFIRMING' WHERE id=?", (duel_id,))
        self.db.commit()

        view = ConfirmDuelCancelView(self, duel_id, report_message=interaction.message)
        await interaction.response.send_message(
            "Cancel this duel? Both players get their exact stake back. Confirm to cancel, or Cancel "
            "to keep it going.",
            view=view,
        )
        view.message = await interaction.original_response()

    # Undoes _handleDuelResultClick's/_handleDuelCancelClick's own
    # CONFIRMING flip once a pending confirmation is cancelled or times
    # out, so the duel's report/cancel buttons work again. An atomic
    # conditional UPDATE (not select-then-update) so it naturally no-ops
    # if the duel already resolved a different way in the meantime.
    def _restoreDuelAwaitingResult(self, duel_id):
        self.cursor.execute(
            "UPDATE duels SET state='AWAITING_RESULT' WHERE id=? AND state='CONFIRMING'", (duel_id,)
        )
        self.db.commit()

    # ConfirmDuelResultView's Confirm button callback, re-fetches the
    # duel's own row by id (rather than trusting anything stored on the
    # view besides the id/winner_is_challenger themselves) before handing
    # off to _resolveDuel for the actual payout.
    async def _finishDuelResolution(self, duel_id, winner_is_challenger):
        self.cursor.execute(
            "SELECT guildId, channelId, challengerId, challengerName, targetId, targetName, amount "
            "FROM duels WHERE id=?",
            (duel_id,)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        guild_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount = row
        await self._resolveDuel(
            guild_id, duel_id, channel_id, challenger_id, challenger_name,
            target_id, target_name, amount, winner_is_challenger
        )

    # ConfirmDuelCancelView's Confirm button callback, the refund
    # counterpart to _finishDuelResolution's payout. Re-fetches the duel's
    # own row by id the same way. Doesn't touch wins/losses/gold_won/
    # gold_lost - a cancelled duel never happened, unlike a resolved one.
    async def _finishDuelCancellation(self, duel_id):
        self.cursor.execute(
            "SELECT guildId, channelId, challengerId, challengerName, targetId, targetName, amount "
            "FROM duels WHERE id=?",
            (duel_id,)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        guild_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount = row

        # BUG-PRONE PATTERN AVOIDED: delete the duel row before anything
        # async below, so a double-click on Confirm can't refund twice.
        self.cursor.execute("DELETE FROM duels WHERE id=?", (duel_id,))
        self.db.commit()

        self.cursor.execute(
            "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?",
            (amount, guild_id, challenger_id)
        )
        self.cursor.execute(
            "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?",
            (amount, guild_id, target_id)
        )
        self.db.commit()

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        await channel.send(
            f"Duel cancelled: **{challenger_name}** and **{target_name}** have each been refunded "
            f"{amount} gold."
        )

    # Escrows `amount` from both players and posts the win/loss report
    # message with a DuelResultView.
    async def _acceptDuel(
        self, guild_id, duel_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount
    ):
        # BUG-PRONE PATTERN AVOIDED: flip the state before anything async
        # below, so a double-click on accept can't process twice.
        self.cursor.execute("UPDATE duels SET state='ACCEPTING' WHERE id=?", (duel_id,))
        self.db.commit()

        self.ensureEconomyRow(guild_id, target_id, target_name)
        challenger_balance = self.getEconomy(guild_id, challenger_id, "balance")
        target_balance = self.getEconomy(guild_id, target_id, "balance")

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        if challenger_balance < amount or target_balance < amount:
            self.cursor.execute("DELETE FROM duels WHERE id=?", (duel_id,))
            self.db.commit()
            await channel.send(
                f"Wager cancelled: one of you no longer has {amount} gold to cover it."
            )
            return

        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (amount, guild_id, challenger_id)
        )
        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (amount, guild_id, target_id)
        )
        self.db.commit()

        pot = amount * 2
        msg = await channel.send(
            f"\U0001f4b0 **{challenger_name}** vs **{target_name}** - {pot} gold on the line! "
            f"Press the button below for whoever actually won.",
            view=DuelResultView(self),
        )

        self.cursor.execute(
            "UPDATE duels SET state='AWAITING_RESULT', messageId=? WHERE id=?", (msg.id, duel_id)
        )
        self.db.commit()

    # Pays the pot to the winner and records both players' bet win/loss
    # stats, same economy columns the team-game bets use.
    async def _resolveDuel(
        self, guild_id, duel_id, channel_id, challenger_id, challenger_name,
        target_id, target_name, amount, winner_is_challenger
    ):
        # BUG-PRONE PATTERN AVOIDED: delete the duel row before anything
        # async below, so a double-click on Confirm (or any other caller)
        # can't find this row and pay out twice.
        self.cursor.execute("DELETE FROM duels WHERE id=?", (duel_id,))
        self.db.commit()

        if winner_is_challenger:
            winner_id, winner_name, loser_id, loser_name = (
                challenger_id, challenger_name, target_id, target_name
            )
        else:
            winner_id, winner_name, loser_id, loser_name = (
                target_id, target_name, challenger_id, challenger_name
            )

        pot = amount * 2
        self.cursor.execute(
            "UPDATE economy SET balance = balance + ?, wins = wins + 1, "
            "gold_wagered = gold_wagered + ?, gold_won = gold_won + ? WHERE guildId=? AND userId=?",
            (pot, amount, amount, guild_id, winner_id)
        )
        self.cursor.execute(
            "UPDATE economy SET losses = losses + 1, gold_wagered = gold_wagered + ?, "
            "gold_lost = gold_lost + ? WHERE guildId=? AND userId=?",
            (amount, amount, guild_id, loser_id)
        )
        self.db.commit()

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)

        await channel.send(
            f"\U0001f3c6 **{winner_name}** wins the wager against **{loser_name}** and takes home **{pot} gold**!"
        )

    # Pari-mutuel betting payouts (winners split the losing side's pool,
    # after an imbalance rake, see _imbalanceRakeFraction, proportional
    # to their own wager, on top of getting their own wager back),
    # GAME_WIN_GOLD/GAME_LOSS_GOLD for every rostered player depending
    # on which side of the result they landed on, and a simple
    # team-average elo update for whoever was actually rostered on
    # team1/team2. Pure computation (no DB writes), so the exact same
    # result can be applied once by recordResult and later
    # reversed/reapplied by reportCorrectWinnerHelper without
    # re-deriving the math (which would go wrong once elo ratings have
    # moved on).
    #
    # Returns (deltas, summary):
    #   deltas: user_id -> {username, balance, wins, losses, gold_wagered,
    #           gold_won, gold_lost, game_wins, game_losses, ranked_wins,
    #           ranked_losses, elo}; all values are deltas to ADD to that
    #           user's economy row. `balance` here is bet payouts/losses
    #           AND GAME_WIN_GOLD/GAME_LOSS_GOLD combined, not just one or
    #           the other; gold_wagered/gold_won/gold_lost stay wager-only.
    #           ranked_wins/ranked_losses are the
    #           RANKED subset of game_wins/game_losses (0 for a casual
    #           game); a casual win/loss count is just game_wins minus
    #           ranked_wins (see getLeaderboardEntries), so there's nothing
    #           separate to track for that side.
    #   summary: display-only info for formatResultMessage().
    def computeGameDeltas(
        self, wagers, team1_roster, team2_roster, elo_lookup, winning_team, is_ranked=False,
        default_elo=DEFAULT_ELO, team1_name="Team 1", team2_name="Team 2", disliked_role_user_ids=frozenset(),
    ):
        deltas = {}

        def bump(user_id, username, **kwargs):
            entry = deltas.setdefault(user_id, {
                "username": username, "balance": 0, "wins": 0, "losses": 0,
                "gold_wagered": 0, "gold_won": 0, "gold_lost": 0,
                "game_wins": 0, "game_losses": 0, "ranked_wins": 0, "ranked_losses": 0, "elo": 0,
            })
            for key, value in kwargs.items():
                entry[key] += value

        winningBets = [w for w in wagers if w[2] == winning_team]
        losingBets = [w for w in wagers if w[2] != winning_team]
        winningPool = sum(w[3] for w in winningBets)
        losingPool = sum(w[3] for w in losingBets)
        rakedLosingPool = losingPool * (1 - self._imbalanceRakeFraction(winningPool, losingPool))

        for user_id, username, _team, amount in losingBets:
            bump(user_id, username, losses=1, gold_wagered=amount, gold_lost=amount)

        winning_bettors = []
        for user_id, username, _team, amount in winningBets:
            payout = round(amount + (amount / winningPool) * rakedLosingPool) if winningPool > 0 else amount
            bump(user_id, username, balance=payout, wins=1, gold_wagered=amount, gold_won=payout - amount)
            winning_bettors.append((username, payout, amount))

        # Game record (game_wins/game_losses) is tracked for every
        # reported game regardless of ranked status. Elo is not: it's
        # exclusive to games started with ranked:true (is_ranked=True),
        # so a casual /make-teams random or /make-teams draft game
        # never moves anyone's rating. GAME_WIN_GOLD/GAME_LOSS_GOLD
        # follow game_wins/game_losses' lead here too. Every rostered
        # player gets one or the other, win or lose, ranked or casual,
        # entirely independent of anything they wagered.
        elo_changes = []
        winner_count = 0
        loser_count = 0
        disliked_role_bonus_players = []
        if team1_roster or team2_roster:
            elo_delta1 = elo_delta2 = 0
            if is_ranked:
                team1_elos = [elo_lookup.get(uid, default_elo) for uid, _name in team1_roster]
                team2_elos = [elo_lookup.get(uid, default_elo) for uid, _name in team2_roster]
                team1_avg = sum(team1_elos) / len(team1_elos) if team1_elos else default_elo
                team2_avg = sum(team2_elos) / len(team2_elos) if team2_elos else default_elo

                expected1 = 1 / (1 + 10 ** ((team2_avg - team1_avg) / 400))
                actual1 = 1 if winning_team == 1 else 0
                elo_delta1 = round(ELO_K_FACTOR * (actual1 - expected1))
                elo_delta2 = round(ELO_K_FACTOR * ((1 - actual1) - (1 - expected1)))

            # A win on a role marked disliked (see rankedTeamHelper's
            # own disliked_role_user_ids) earns more than the
            # team-average swing every teammate gets. A losing player
            # on a disliked role gets no such break, only a win counts.
            # Multiplies rather than adds a flat bonus, so it scales
            # with how much elo was actually on the line that game
            # rather than being a fixed number regardless of the
            # matchup.
            def _playerEloDelta(user_id, username, base_delta, is_winning_side):
                if not (is_ranked and is_winning_side and user_id in disliked_role_user_ids):
                    return base_delta
                boosted = round(base_delta * ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER)
                if boosted != base_delta:
                    disliked_role_bonus_players.append((username, boosted))
                return boosted

            for user_id, username in team1_roster:
                bump(
                    user_id, username,
                    elo=_playerEloDelta(user_id, username, elo_delta1, winning_team == 1),
                    balance=GAME_WIN_GOLD if winning_team == 1 else GAME_LOSS_GOLD,
                    game_wins=1 if winning_team == 1 else 0,
                    game_losses=0 if winning_team == 1 else 1,
                    ranked_wins=(1 if winning_team == 1 else 0) if is_ranked else 0,
                    ranked_losses=(0 if winning_team == 1 else 1) if is_ranked else 0,
                )
            for user_id, username in team2_roster:
                bump(
                    user_id, username,
                    elo=_playerEloDelta(user_id, username, elo_delta2, winning_team == 2),
                    balance=GAME_WIN_GOLD if winning_team == 2 else GAME_LOSS_GOLD,
                    game_wins=1 if winning_team == 2 else 0,
                    game_losses=0 if winning_team == 2 else 1,
                    ranked_wins=(1 if winning_team == 2 else 0) if is_ranked else 0,
                    ranked_losses=(0 if winning_team == 2 else 1) if is_ranked else 0,
                )
            winner_count = len(team1_roster) if winning_team == 1 else len(team2_roster)
            loser_count = len(team2_roster) if winning_team == 1 else len(team1_roster)

            if is_ranked:
                if team1_roster:
                    elo_changes.append((team1_name, elo_delta1))
                if team2_roster:
                    elo_changes.append((team2_name, elo_delta2))

        summary = {
            "no_bets": not wagers,
            "no_winning_bets": bool(wagers) and not winning_bettors,
            "winning_bettors": winning_bettors,
            "elo_changes": elo_changes,
            "disliked_role_bonus_players": disliked_role_bonus_players,
            "winner_gold_count": winner_count,
            "loser_gold_count": loser_count,
            "team1_name": team1_name,
            "team2_name": team2_name,
        }
        return deltas, summary

    # Applies (sign=1) or reverses (sign=-1) a deltas dict from
    # computeGameDeltas() against every affected player's economy row.
    # Returns the (user_id, achievement_key) pairs newly unlocked while
    # applying these deltas, always [] on a reversal (sign<0), same
    # reasoning as the elo-tier check below. Callers with a channel handy
    # pass this straight to _announceAchievements; callers that don't
    # (or a reversal, which never populates it) just ignore it.
    def applyGameDeltas(self, guild_id, deltas, game=None, sign=1):
        if game is None:
            game = self._currentGame(guild_id)
        newly_unlocked = []
        for user_id, d in deltas.items():
            self.ensureEconomyRow(guild_id, user_id, d["username"])
            self.cursor.execute(
                "UPDATE economy SET balance = balance + ?, wins = wins + ?, losses = losses + ?, "
                "gold_wagered = gold_wagered + ?, gold_won = gold_won + ?, gold_lost = gold_lost + ? "
                "WHERE guildId=? AND userId=?",
                (
                    sign * d["balance"], sign * d["wins"], sign * d["losses"],
                    sign * d["gold_wagered"], sign * d["gold_won"], sign * d["gold_lost"],
                    guild_id, user_id,
                )
            )

            # game_wins/game_losses/ranked_wins/ranked_losses/elo are the
            # per-game slice (see /set game): a pure bettor with no
            # rostered-player delta at all (every one of these still 0)
            # has nothing to touch here, and no reason to seed a
            # game_stats row for a game they didn't even play.
            has_game_delta = (
                d["game_wins"] or d["game_losses"] or d["ranked_wins"] or d["ranked_losses"] or d["elo"]
            )
            if has_game_delta:
                self.ensureGameStatsRow(guild_id, user_id, d["username"], game)
                self.cursor.execute(
                    "UPDATE game_stats SET game_wins = game_wins + ?, game_losses = game_losses + ?, "
                    "ranked_wins = ranked_wins + ?, ranked_losses = ranked_losses + ?, elo = elo + ? "
                    "WHERE guildId=? AND userId=? AND game=?",
                    (
                        sign * d["game_wins"], sign * d["game_losses"],
                        sign * d["ranked_wins"], sign * d["ranked_losses"], sign * d["elo"],
                        guild_id, user_id, game,
                    )
                )

            # Only on forward application, not a correction's reversal.
            # Unlocking a tier reward (or an achievement) while UNDOING
            # a wrongly-recorded result (see
            # _correctTournamentMatchHelper) would be checking against
            # elo/streak on their way back down, not up. The reapply
            # that follows a reversal calls back in here with sign=1
            # anyway, so a corrected winner still gets checked properly.
            if sign > 0:
                if d["elo"] != 0:
                    self.cursor.execute(
                        "SELECT elo FROM game_stats WHERE guildId=? AND userId=? AND game=?",
                        (guild_id, user_id, game)
                    )
                    self._checkTierRewardUnlocks(guild_id, user_id, self.cursor.fetchone()[0])

                # Streak tracking: a win extends it, a loss resets it to
                # zero. Not itself a pure additive delta the way every
                # other game_stats column is, so it needs the CURRENT
                # stored value rather than just adding a fixed amount,
                # hence its own UPDATE instead of joining the one above.
                if d["game_wins"] > 0:
                    self.cursor.execute(
                        "UPDATE game_stats SET current_win_streak = current_win_streak + 1 "
                        "WHERE guildId=? AND userId=? AND game=?", (guild_id, user_id, game)
                    )
                elif d["game_losses"] > 0:
                    self.cursor.execute(
                        "UPDATE game_stats SET current_win_streak = 0 "
                        "WHERE guildId=? AND userId=? AND game=?", (guild_id, user_id, game)
                    )

                # High Roller, Jackpot, and Giant Slayer all need this
                # specific event's own context (this game's wager
                # amount, this game's payout, this game's elo swing)
                # rather than a plain row snapshot, so they're checked
                # here directly instead of inside _checkAchievements.
                if d["wins"] > 0 and d["gold_wagered"] >= CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD:
                    if self._unlockAchievement(guild_id, user_id, "high_roller"):
                        newly_unlocked.append((user_id, "high_roller"))
                if (
                    d["wins"] > 0 and d["gold_wagered"] > 0
                    and d["gold_won"] >= d["gold_wagered"] * (CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER - 1)
                ):
                    if self._unlockAchievement(guild_id, user_id, "jackpot"):
                        newly_unlocked.append((user_id, "jackpot"))
                if d["elo"] >= CARD_ACHIEVEMENT_UNDERDOG_ELO_GAIN:
                    if self._unlockAchievement(guild_id, user_id, "underdog"):
                        newly_unlocked.append((user_id, "underdog"))

                for achievement_key in self._checkAchievements(guild_id, user_id, game):
                    newly_unlocked.append((user_id, achievement_key))
        self.db.commit()
        return newly_unlocked

    def formatResultMessage(self, winning_team, summary):
        # .get(...) with a fallback rather than summary[...] so a caller
        # that hasn't threaded real names through computeGameDeltas still
        # gets a sensible label instead of a KeyError.
        winner_name = summary.get(f"team{winning_team}_name") or f"Team {winning_team}"
        lines = [f"**{winner_name}** wins!"]

        if summary["no_bets"]:
            lines.append("No bets were placed on this game.")
        elif summary["no_winning_bets"]:
            lines.append("Nobody bet on the winning team; all bets were lost.")
        else:
            lines.append("Paying out bets...")
            for username, payout, amount in summary["winning_bettors"]:
                lines.append(f"{username} won {payout} gold (bet {amount})")

        if summary["elo_changes"]:
            lines.append(
                "Elo: " + ", ".join(f"{name} {delta:+d}" for name, delta in summary["elo_changes"])
            )

        if summary.get("disliked_role_bonus_players"):
            lines.append(
                "Disliked-role win bonus: "
                + ", ".join(f"{name} {delta:+d}" for name, delta in summary["disliked_role_bonus_players"])
            )

        winner_count = summary.get("winner_gold_count", 0)
        loser_count = summary.get("loser_gold_count", 0)
        if winner_count or loser_count:
            parts = []
            if winner_count:
                word = "player" if winner_count == 1 else "players"
                each = "" if winner_count == 1 else " each"
                parts.append(f"{winner_count} winning {word} earned {GAME_WIN_GOLD} gold{each}")
            if loser_count:
                word = "player" if loser_count == 1 else "players"
                each = "" if loser_count == 1 else " each"
                parts.append(f"{loser_count} losing {word} earned {GAME_LOSS_GOLD} gold{each}")
            lines.append(" and ".join(parts) + " just for playing.")

        return "\n".join(lines)

    # Snapshots exactly what was applied for a resolved game: the
    # wagers, both rosters, the deltas computeGameDeltas() produced, and
    # the team names shown at the time, so reportCorrectWinnerHelper can
    # reverse it precisely (and keep saying the same names) later. One
    # row per guild. A new result overwrites the previous snapshot.
    def saveLastResult(
        self, guild_id, winning_team, wagers, team1_roster, team2_roster, deltas, is_ranked=False,
        team1_name="Team 1", team2_name="Team 2", disliked_role_user_ids=frozenset(), game=None,
    ):
        if game is None:
            game = self._currentGame(guild_id)
        payload = {
            "winning_team": winning_team,
            "wagers": [list(w) for w in wagers],
            "team1_roster": [list(p) for p in team1_roster],
            "team2_roster": [list(p) for p in team2_roster],
            "deltas": {str(uid): d for uid, d in deltas.items()},
            "is_ranked": is_ranked,
            "team1_name": team1_name,
            "team2_name": team2_name,
            # So reportCorrectWinnerHelper's own recomputation still
            # credits whoever actually played a disliked role this
            # game, no matter how much later the correction happens or
            # what team1/team2 have moved on to since.
            "disliked_role_user_ids": list(disliked_role_user_ids),
            # Which game_stats row this snapshot's deltas actually
            # touched (see /set game), so a later correction/invalidation
            # reverses/recomputes against the SAME game even if the
            # server's current_game has since moved on to something else.
            "game": game,
        }
        self.cursor.execute(
            "INSERT OR REPLACE INTO last_result(guildId, data) VALUES(?, ?)",
            (guild_id, json.dumps(payload))
        )
        self.db.commit()

    def getLastResult(self, guild_id):
        self.cursor.execute("SELECT data FROM last_result WHERE guildId=?", (guild_id,))
        row = self.cursor.fetchone()
        if row is None:
            return None

        payload = json.loads(row[0])
        payload["wagers"] = [tuple(w) for w in payload["wagers"]]
        payload["team1_roster"] = [tuple(p) for p in payload["team1_roster"]]
        payload["team2_roster"] = [tuple(p) for p in payload["team2_roster"]]
        payload["deltas"] = {int(uid): d for uid, d in payload["deltas"].items()}
        payload.setdefault("is_ranked", False)
        payload["disliked_role_user_ids"] = frozenset(payload.get("disliked_role_user_ids", []))
        # An older snapshot saved before /set game existed was always for
        # "League", the only game this bot tracked at the time.
        payload.setdefault("game", "League")
        return payload

    # Fully undoes the last resolved game. Reverses last["deltas"] the
    # same way a correction's first step does (bet payouts, win/loss
    # records, elo, GAME_WIN_GOLD/GAME_LOSS_GOLD), but without
    # reapplying anything for either team, and refunds every wager's
    # exact original stake back to balance on top of that. The reversal
    # alone isn't "refund" for a bettor: a winner's stored delta
    # credited their whole *payout* (stake plus winnings), so reversing
    # it removes the payout entirely and leaves them down by exactly
    # their stake, indistinguishable from having lost. Adding the
    # stake back afterward is what actually returns everyone, winners
    # and losers alike, to their pre-bet balance. Clears last_result
    # entirely afterward: once invalidated, there's no "last game" left
    # for a further correction to flip between team1/team2.
    def _invalidateLastResult(self, guild_id, last):
        self.applyGameDeltas(guild_id, last["deltas"], last["game"], sign=-1)
        for user_id, _username, _team, amount in last["wagers"]:
            self.cursor.execute(
                "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?",
                (amount, guild_id, user_id)
            )
        self.cursor.execute("DELETE FROM last_result WHERE guildId=?", (guild_id,))
        self.db.commit()

    # Admin correction for a misreported /start winner: undoes exactly
    # what was applied for the last resolved game in this guild (bet
    # payouts, win/loss records, elo) and re-applies the same
    # wagers/rosters against the corrected winner. Elo is recomputed
    # rather than reused, since after undoing the wrong result each
    # player's rating is back to its pre-match value. Recomputing
    # against that gives the correct alternate-history rating, not a
    # stale or double-applied one.
    #
    # `invalidate=True` is the other path: instead of flipping the
    # winner, it undoes the game entirely (see _invalidateLastResult),
    # for a game that shouldn't have counted at all rather than one
    # that just recorded the wrong side. Mutually exclusive with
    # `correct_team`.
    #
    # match_id, when given, corrects a specific tournament match
    # instead, a separate, narrower path (see
    # _correctTournamentMatchHelper) that only touches that match's
    # bracket node, not the guild-wide economy snapshot below.
    # invalidate isn't supported there yet, since undoing a match would
    # also mean un-advancing whatever it fed into the bracket.
    # The actual reverse-and-reapply (or invalidate) work behind
    # ConfirmCorrectWinnerView's Confirm button, with no messaging of its
    # own. `snapshot` is the exact last_result dict the warning was built
    # from - the caller has already checked it's still current (see that
    # view's own comment) before calling this. Returns (result_text,
    # summary, newly_unlocked); `summary` is None for an invalidation
    # (nothing further to announce), a computeGameDeltas summary dict
    # otherwise, for the caller to post via formatResultMessage.
    def _applyCorrectWinner(self, guild_id, snapshot, correct_team, invalidate):
        # .get(...) with a fallback rather than snapshot[...]: an older
        # last_result snapshot saved before team1_name/team2_name existed
        # won't have these keys, and shouldn't crash a correction over it.
        team1_name = snapshot.get("team1_name", "Team 1")
        team2_name = snapshot.get("team2_name", "Team 2")

        if invalidate:
            self._invalidateLastResult(guild_id, snapshot)
            result_text = (
                f"**{team1_name}** vs **{team2_name}** has been invalidated; bets refunded and "
                "elo/records/gold undone, as if the game never happened."
            )
            return result_text, None, []

        correct_name = team1_name if correct_team == 1 else team2_name
        game = snapshot["game"]
        self.applyGameDeltas(guild_id, snapshot["deltas"], game, sign=-1)

        team1_roster = snapshot["team1_roster"]
        team2_roster = snapshot["team2_roster"]
        is_ranked = snapshot["is_ranked"]
        disliked_role_user_ids = snapshot["disliked_role_user_ids"]
        elo_lookup = self.getEloLookup(guild_id, team1_roster + team2_roster, game)
        new_deltas, summary = self.computeGameDeltas(
            snapshot["wagers"], team1_roster, team2_roster, elo_lookup, correct_team, is_ranked,
            default_elo=self._defaultEloForGuild(guild_id),
            team1_name=team1_name, team2_name=team2_name,
            disliked_role_user_ids=disliked_role_user_ids,
        )
        newly_unlocked = self.applyGameDeltas(guild_id, new_deltas, game)
        self.saveLastResult(
            guild_id, correct_team, snapshot["wagers"], team1_roster, team2_roster, new_deltas, is_ranked,
            team1_name=team1_name, team2_name=team2_name,
            disliked_role_user_ids=disliked_role_user_ids, game=game,
        )

        previous_name = team1_name if snapshot["winning_team"] == 1 else team2_name
        result_text = (
            f"Correction recorded: **{correct_name}** actually won (previously recorded as "
            f"{previous_name}). Balances, records, and elo have been adjusted."
        )
        return result_text, summary, newly_unlocked

    async def reportCorrectWinnerHelper(self, ctx, correct_team, match_id=None, invalidate=False):
        if match_id is not None:
            if invalidate:
                await ctx.response.send_message(
                    "Invalidating isn't supported for a specific tournament match yet; correct it "
                    "to the other team instead.",
                    ephemeral=True,
                )
                return
            await self._correctTournamentMatchHelper(ctx, match_id, correct_team)
            return

        if invalidate and correct_team is not None:
            await ctx.response.send_message("Give team or invalidate, not both.", ephemeral=True)
            return
        if not invalidate and correct_team is None:
            await ctx.response.send_message(
                "Give team (who actually won), or invalidate to undo the game entirely.", ephemeral=True
            )
            return

        guild_id = ctx.guild.id
        last = self.getLastResult(guild_id)

        if last is None:
            await ctx.response.send_message("There's no recent game result to correct.", ephemeral=True)
            return

        # .get(...) with a fallback rather than last[...]: an older
        # last_result snapshot saved before team1_name/team2_name
        # existed won't have these keys, and shouldn't crash a
        # correction over it.
        team1_name = last.get("team1_name", "Team 1")
        team2_name = last.get("team2_name", "Team 2")

        if invalidate:
            warning = (
                f"This will **invalidate** **{team1_name}** vs **{team2_name}**: bets refunded and "
                "elo/records/gold undone, as if the game never happened. This can't be undone."
            )
        else:
            correct_name = team1_name if correct_team == 1 else team2_name
            if last["winning_team"] == correct_team:
                await ctx.response.send_message(
                    f"**{correct_name}** is already the recorded winner; nothing to correct.", ephemeral=True
                )
                return
            previous_name = team1_name if last["winning_team"] == 1 else team2_name
            warning = (
                f"This will correct **{team1_name}** vs **{team2_name}**: **{correct_name}** actually "
                f"won (previously recorded as {previous_name}). Balances, records, and elo will be "
                "adjusted accordingly. This can't be undone."
            )

        view = ConfirmCorrectWinnerView(self, guild_id, ctx.user.id, last, correct_team, invalidate)
        await ctx.response.send_message(warning, view=view)
        view.message = await ctx.original_response()

    # Builds the plain /stats embed for `target` (a discord.Member or
    # discord.User, anything with
    # .id/.name/.display_name/.display_avatar) in `guild_id`. Factored
    # out of statsHelper so _handleStatsReturnClick can rebuild the
    # exact same embed when the Back button swaps a trading card back
    # to it, without duplicating the field layout.
    def _buildStatsEmbed(self, guild_id, target):
        user_id = target.id
        game = self._currentGame(guild_id)

        self.ensureEconomyRow(guild_id, user_id, target.name)
        self.ensureGameStatsRow(guild_id, user_id, target.name, game)
        # Keeps this player's trading_cards row in sync with the
        # current CARD_DEFAULT_* palette (see ensureCardSettings) every
        # time /stats runs for them, not just the first time their card
        # is ever rendered. So a later change to Shockwave's own
        # defaults reaches them the next time they check their stats,
        # with no manual DB fix needed.
        self.ensureCardSettings(guild_id, user_id)

        self.cursor.execute(
            "SELECT balance, wins, losses, gold_wagered, gold_won, gold_lost FROM economy "
            "WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        balance, bet_wins, bet_losses, gold_wagered, gold_won, gold_lost = self.cursor.fetchone()
        self.cursor.execute(
            "SELECT game_wins, game_losses, ranked_wins, ranked_losses, elo FROM game_stats "
            "WHERE guildId=? AND userId=? AND game=?",
            (guild_id, user_id, game)
        )
        game_wins, game_losses, ranked_wins, ranked_losses, elo = self.cursor.fetchone()
        # Lazy self-heal, same idea as
        # ensureCardSettings/ensureEconomyRow just above: a player
        # already sitting at Diamond+ before card_unlocks existed (or
        # one who reached a tier without their elo ever passing back
        # through applyGameDeltas, a tournament correction, a manual DB
        # edit) still gets credited the next time anything looks at
        # their stats, not never.
        self._checkTierRewardUnlocks(guild_id, user_id, elo)
        # Same self-heal for the snapshot-checkable achievements (return
        # value intentionally discarded, no _announceAchievements call
        # here, since a quiet backfill shouldn't suddenly announce
        # something that may have been true for a while).
        self._checkAchievements(guild_id, user_id, game)

        net_gold = gold_won - gold_lost

        bet_games = bet_wins + bet_losses
        bet_win_rate = f"{(bet_wins / bet_games) * 100:.1f}%" if bet_games > 0 else "N/A"

        games_played = game_wins + game_losses
        game_win_rate = f"{(game_wins / games_played) * 100:.1f}%" if games_played > 0 else "N/A"

        # casual = the non-ranked slice of game_wins/game_losses, see
        # getLeaderboardEntries's identical derivation.
        casual_wins = game_wins - ranked_wins
        casual_losses = game_losses - ranked_losses
        casual_games = casual_wins + casual_losses
        ranked_games = ranked_wins + ranked_losses
        casual_win_rate = f"{(casual_wins / casual_games) * 100:.1f}%" if casual_games > 0 else "N/A"
        ranked_win_rate = f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A"

        elo_rank = self.eloRankLabel(elo)

        embed = discord.Embed(
            title=f"{target.display_name}'s Stats - {game}", color=discord.Color.gold()
        )
        # display_avatar (not the possibly-None .avatar) always
        # resolves to something: the member's own custom avatar if they
        # have one, or Discord's default avatar for their account
        # otherwise. with_format("png") forces a static snapshot even
        # for an animated (GIF) avatar (a no-op for already-static
        # ones). Discord's embed thumbnail slot doesn't reliably unfurl
        # a .gif URL, so without this an animated avatar's thumbnail
        # can silently fail to attach at all.
        embed.set_thumbnail(url=target.display_avatar.with_format("png").url)
        # Exactly 3 inline fields per row (Discord wraps at 3), grouped
        # ranked / casual+bet / gold top to bottom, with nothing left
        # over to force a row break with. A blank spacer field looks
        # like a good way to end a short row early, but it still
        # renders its own (invisible) name+value line and shows up as a
        # big empty gap instead of a clean break. Elo joins the ranked
        # row (rather than being merged into a record field like the
        # others) specifically to round that row out to 3. Game/Casual/
        # Bet Record fold their win rate into the same field (see the
        # comment on those below), so 3 fields already covers all of
        # them without needing a filler.
        embed.add_field(name="Elo", value=f"{elo} ({elo_rank})", inline=True)
        embed.add_field(name="Ranked Wins", value=f"{ranked_wins}W - {ranked_losses}L", inline=True)
        embed.add_field(name="Ranked Win Rate", value=ranked_win_rate, inline=True)
        # Record and win rate folded into one field each here (rather
        # than two separate ones) so a pair can't straddle a row
        # boundary the way splitting them would risk.
        embed.add_field(name="Game Record", value=f"{game_wins}W - {game_losses}L ({game_win_rate})", inline=True)
        embed.add_field(
            name="Casual Record", value=f"{casual_wins}W - {casual_losses}L ({casual_win_rate})", inline=True
        )
        embed.add_field(name="Bet Record", value=f"{bet_wins}W - {bet_losses}L ({bet_win_rate})", inline=True)
        embed.add_field(name="Balance", value=f"{balance} gold", inline=True)
        embed.add_field(name="Net Gold Won/Lost", value=f"{net_gold:+d} gold", inline=True)
        embed.add_field(name="Gold Wagered", value=str(gold_wagered), inline=True)

        # Role preferences (see /setup), not part of the 3-wide inline
        # grid above, since it's two lines of names rather than one
        # short value, and gets its own full-width row rather than
        # being squeezed inline.
        liked, disliked = self.getRolePreferences(guild_id, user_id)
        embed.add_field(
            name="Role Preferences",
            value=(
                f"Liked: {', '.join(liked) if liked else 'none set'}\n"
                f"Disliked: {', '.join(disliked) if disliked else 'none set'}"
            ),
            inline=False,
        )

        return embed

    async def statsHelper(self, ctx, member=None):
        target = member if member is not None else ctx.user
        guild_id = ctx.guild.id
        user_id = target.id

        embed = self._buildStatsEmbed(guild_id, target)

        await ctx.response.send_message(embed=embed, view=StatsView(self, card_shown=False))
        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT OR REPLACE INTO stats_views(messageId, guildId, targetUserId, cardShown) "
            "VALUES(?, ?, ?, 0)",
            (msg.id, guild_id, user_id)
        )
        self.db.commit()

    # Resolves `user_id` to a live discord.Member of `guild_id`, cache
    # first, then a real API fetch if they're not cached, or None if
    # they can't be resolved at all (left the guild, or some other API
    # hiccup). Shared by the avatar toggle and the trading card, both
    # of which need to look someone back up well after /stats itself
    # first ran.
    async def _resolveGuildMember(self, guild_id, user_id):
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    # The per-server half of StatsView's avatar toggle. display_avatar
    # resolves this server's own profile picture if the player has set
    # one, falling back to their regular account-wide avatar otherwise
    # (the same avatar /stats itself shows by default). None if the
    # member can't be resolved at all. The caller just leaves whatever's
    # currently showing rather than erroring out over what's ultimately
    # a cosmetic toggle.
    async def _resolveMemberAvatarUrl(self, guild_id, user_id):
        member = await self._resolveGuildMember(guild_id, user_id)
        if member is None:
            return None
        return member.display_avatar.with_format("png").url

    # The regular/global half of the same toggle: the account-wide
    # avatar a discord.User carries, deliberately bypassing any
    # per-server override a discord.Member might have (that's the whole
    # point of this half). Cached users are used first. A real fetch
    # only happens for someone not already in the client's cache. None
    # if the user can't be resolved at all (e.g. their account no
    # longer exists).
    async def _resolveGlobalAvatarUrl(self, user_id):
        user = self.client.get_user(user_id) if self.client is not None else None
        if user is None:
            try:
                user = await self.client.fetch_user(user_id)
            except discord.HTTPException:
                return None
        return user.display_avatar.with_format("png").url

    # _resolveGuildMember first, falling back to a plain discord.User
    # (the same global-account resolution _resolveGlobalAvatarUrl's own
    # fallback uses) if they've left the guild. /leaderboard's
    # cards:true mode needs a real target for _buildStatsEmbed
    # regardless of current guild membership, unlike /stats itself
    # (only ever reachable by someone currently in the guild to run the
    # command at all, and never paged through a whole roster the way a
    # leaderboard is). None only if the Discord account itself no
    # longer resolves either way.
    async def _resolveGuildMemberOrUser(self, guild_id, user_id):
        member = await self._resolveGuildMember(guild_id, user_id)
        if member is not None:
            return member
        if self.client is None:
            return None
        user = self.client.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.client.fetch_user(user_id)
        except discord.HTTPException:
            return None

    # Converts a "#RRGGBB" hex string (trading_cards' own storage
    # format, portable and human-editable, unlike a raw RGB tuple) back
    # to the (r, g, b) tuple PIL wants. Falls back to `fallback` for
    # anything that doesn't parse. A hand-edited or otherwise corrupted
    # value shouldn't take card rendering down with it.
    def _hexToRgb(self, hex_color, fallback):
        if not isinstance(hex_color, str) or len(hex_color) != 7 or not hex_color.startswith("#"):
            return fallback
        try:
            return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return fallback

    # The reverse of _hexToRgb, an (r, g, b) tuple back to trading_cards'
    # own "#RRGGBB" storage format.
    def _rgbToHex(self, rgb):
        return "#{:02X}{:02X}{:02X}".format(*rgb)

    # A lighter shade of `color`, blended toward white by `amount` (0-1),
    # used to turn a single customizable background_color into the
    # matching (center, edge) pair _createBracketCanvas's vignette wants,
    # without needing a second color column just for that.
    def _lightenColor(self, color, amount):
        return tuple(round(c + (255 - c) * amount) for c in color)

    # Resolves a trading_cards.font_style key to the actual bundled
    # fonts to use: name_font/title_font (+ their own variation, for
    # the two variable ones) for the name and the title/epithet, plus
    # body_font and a weight for each of the smaller body-text elements
    # (stat labels, stat values, team/roster rows, username shares
    # team_weight, both being small secondary text).
    #
    # CARD_SHOP_FONT_STYLES backs each style with a genuinely different
    # bundled typeface (RUSSO_ONE/CINZEL/ORBITRON, then PRESS_START_2P/
    # CREEPSTER/BLACK_OPS_ONE in a second wave, all Google Fonts, SIL
    # Open Font License) for name/title, and shifts every body
    # element's weight together too. There's still only the one
    # bundled body typeface (IBM_PLEX_SANS, a variable font, see
    # _loadFont), so "a different font" for the smaller text still
    # means a different weight of it rather than a whole second body
    # typeface.
    #
    # Anything unrecognized (including CARD_DEFAULT_FONT_STYLE itself)
    # falls back to Shockwave's own pairing, the same "unknown preset
    # degrades to the default" approach _hexToRgb takes for a bad
    # color.
    def _cardFontPaths(self, font_style):
        if font_style == "Bold":
            return {
                "name_font": RUSSO_ONE, "name_variation": None,
                "title_font": RUSSO_ONE, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Bold", "value_weight": "Bold", "team_weight": "SemiBold",
            }
        if font_style == "Elegant":
            return {
                "name_font": CINZEL, "name_variation": "Bold",
                "title_font": CINZEL, "title_variation": "Regular",
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Medium", "value_weight": "Regular", "team_weight": "Light",
            }
        if font_style == "Cyber":
            return {
                "name_font": ORBITRON, "name_variation": "Bold",
                "title_font": ORBITRON, "title_variation": "SemiBold",
                "body_font": IBM_PLEX_SANS,
                "label_weight": "SemiBold", "value_weight": "Medium", "team_weight": "Regular",
            }
        if font_style == "Retro":
            return {
                "name_font": PRESS_START_2P, "name_variation": None,
                "title_font": PRESS_START_2P, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "SemiBold", "value_weight": "Medium", "team_weight": "Medium",
            }
        if font_style == "Villain":
            return {
                "name_font": CREEPSTER, "name_variation": None,
                "title_font": CREEPSTER, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Bold", "value_weight": "Regular", "team_weight": "Regular",
            }
        if font_style == "Military":
            return {
                "name_font": BLACK_OPS_ONE, "name_variation": None,
                "title_font": BLACK_OPS_ONE, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Bold", "value_weight": "SemiBold", "team_weight": "SemiBold",
            }
        if font_style == "Neon":
            return {
                "name_font": BUNGEE, "name_variation": None,
                "title_font": BUNGEE, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Bold", "value_weight": "SemiBold", "team_weight": "Medium",
            }
        if font_style == "Western":
            return {
                "name_font": RYE, "name_variation": None,
                "title_font": RYE, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "SemiBold", "value_weight": "Medium", "team_weight": "Regular",
            }
        if font_style == "Handwritten":
            return {
                "name_font": PERMANENT_MARKER, "name_variation": None,
                "title_font": PERMANENT_MARKER, "title_variation": None,
                "body_font": IBM_PLEX_SANS,
                "label_weight": "Medium", "value_weight": "Regular", "team_weight": "Light",
            }
        return {
            "name_font": CHAKRA_PETCH_BOLD, "name_variation": None,
            "title_font": CHAKRA_PETCH_SEMIBOLD, "title_variation": None,
            "body_font": IBM_PLEX_SANS,
            "label_weight": "Bold", "value_weight": "SemiBold", "team_weight": "Medium",
        }

    # A player's trading_cards row, created with Shockwave's own
    # defaults (CARD_DEFAULT_*) the first time it's needed, same
    # self-healing "insert if missing, read either way" shape
    # ensureEconomyRow uses. So a card can be customized (by hand in
    # the database today; a future /card-customize-style command could
    # write the same columns, and should set customized=1 when it
    # does) without ever needing a one-off migration for players who
    # predate that.
    #
    # INSERT OR IGNORE alone would leave an existing row frozen at
    # whatever CARD_DEFAULT_* was the day it was first created. Since
    # there's no customization command yet, every existing row is
    # really just a stale snapshot of the defaults, not a deliberate
    # choice, so an uncustomized row (customized=0) is re-synced to the
    # current CARD_DEFAULT_* values on every call here, keeping it
    # "following the defaults" instead of pinned to the past. Once
    # real customization exists, setting customized=1 opts a row out
    # of this and this UPDATE becomes a no-op for it.
    def ensureCardSettings(self, guild_id, user_id):
        self.cursor.execute(
            "INSERT OR IGNORE INTO trading_cards"
            "(guildId, userId, title, accent_color, background_color, text_color, font_style, customized) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
            (
                guild_id, user_id, CARD_DEFAULT_TITLE, CARD_DEFAULT_ACCENT_COLOR,
                CARD_DEFAULT_BACKGROUND_COLOR, CARD_DEFAULT_TEXT_COLOR, CARD_DEFAULT_FONT_STYLE,
            )
        )
        self.cursor.execute(
            "UPDATE trading_cards SET title=?, accent_color=?, background_color=?, text_color=?, "
            "font_style=? WHERE guildId=? AND userId=? AND customized=0",
            (
                CARD_DEFAULT_TITLE, CARD_DEFAULT_ACCENT_COLOR, CARD_DEFAULT_BACKGROUND_COLOR,
                CARD_DEFAULT_TEXT_COLOR, CARD_DEFAULT_FONT_STYLE, guild_id, user_id,
            )
        )
        self._resyncEquippedColorScheme(guild_id, user_id)
        self.db.commit()

    # A row picking a NAMED color scheme (color_scheme_name set, by
    # /card-set, see setCardColorScheme) tracks that scheme's current
    # colors on every call here, the same "follow the source of truth
    # instead of freezing at equip time" idea the customized=0 branch
    # above already applies to the whole default palette. Without this,
    # a later tweak to CARD_SHOP_COLOR_SCHEMES/ELO_TIER_BADGE_COLORS (or
    # to CARD_MIN_ACCENT_CONTRAST itself, exactly what motivated adding
    # this) would never reach a player who'd already equipped that
    # scheme, the same staleness bug the customized flag was built to
    # avoid in the first place. A hand-edited custom hex value (no
    # recorded scheme name) is untouched, there's nothing to track it
    # against.
    def _resyncEquippedColorScheme(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT color_scheme_name FROM trading_cards WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        row = self.cursor.fetchone()
        if row is None or row[0] is None:
            return
        scheme_name = row[0]
        schemes = {s["name"]: s for s in self.getAvailableCardColorSchemes(guild_id, user_id)}
        scheme = schemes.get(scheme_name)
        if scheme is None:
            return
        self.cursor.execute(
            "UPDATE trading_cards SET accent_color=?, background_color=? WHERE guildId=? AND userId=?",
            (scheme["accent_color"], scheme["background_color"], guild_id, user_id)
        )

    def getCardSettings(self, guild_id, user_id):
        self.ensureCardSettings(guild_id, user_id)
        self.cursor.execute(
            "SELECT title, accent_color, background_color, text_color, font_style "
            "FROM trading_cards WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        title, accent_color, background_color, text_color, font_style = self.cursor.fetchone()
        return {
            "title": title, "accent_color": accent_color, "background_color": background_color,
            "text_color": text_color, "font_style": font_style,
        }

    # Permanently records that `user_id` has unlocked `item_key` (a
    # CARD_TIER_REWARD_TITLES key, e.g. "Diamond") for their trading
    # card in this guild. A title and a matching color scheme unlock
    # together as one reward (see _checkTierRewardUnlocks), so both go
    # in under the same key with a different itemType. INSERT OR
    # IGNORE makes this idempotent (re-unlocking something already
    # unlocked is a no-op), and since nothing anywhere deletes from
    # card_unlocks, whatever's unlocked here stays unlocked even if the
    # player later deranks below the tier that earned it.
    def _unlockCardReward(self, guild_id, user_id, item_key):
        for item_type in ("title", "color_scheme"):
            self.cursor.execute(
                "INSERT OR IGNORE INTO card_unlocks(guildId, userId, itemType, itemKey) "
                "VALUES(?, ?, ?, ?)",
                (guild_id, user_id, item_type, item_key)
            )
        self.db.commit()

    # Unlocks every CARD_TIER_REWARD_TITLES tier `elo` currently
    # qualifies for, not just the single tier `elo` presently sits in,
    # so a big enough one-time elo swing (a huge upset, or a
    # manual/tournament correction) that jumps straight from, say,
    # Platinum to Grandmaster still credits Diamond and Master along
    # the way, not just the top one landed on. Called both right after
    # a ranked result actually changes someone's elo (applyGameDeltas)
    # and lazily whenever their card settings are read
    # (getCardSettings), the same "self-heal on the next read" idea
    # ensureEconomyRow/_ensureLogo/stats_views' own resync already use
    # elsewhere in this file, so a player who was already sitting at
    # Diamond+ before this feature existed gets credited the first time
    # anything touches their card, not never.
    def _checkTierRewardUnlocks(self, guild_id, user_id, elo):
        for tier_name in CARD_TIER_REWARD_TITLES:
            if elo >= ELO_TIER_THRESHOLDS[tier_name]:
                self._unlockCardReward(guild_id, user_id, tier_name)

    # Permanently records that `user_id` has earned `achievement_key`
    # (a CARD_ACHIEVEMENT_TITLES key), same INSERT OR IGNORE shape
    # _unlockCardReward uses, so an achievement
    # title shows up through the exact same
    # getUnlockedCardTitles/getAvailableCardTitles/_countShopPurchases
    # reads those use, with no separate "did they earn this" concept
    # anywhere else in the code. Returns whether this call actually
    # inserted a new row (rather than hitting the IGNORE branch on an
    # already-earned achievement), the one thing this path needs that
    # the other three don't, since achievements are the only unlock
    # type that also notifies (see _announceAchievements) and a
    # notification firing on every repeat check would be spam.
    def _unlockAchievement(self, guild_id, user_id, achievement_key):
        self.cursor.execute(
            "INSERT OR IGNORE INTO card_unlocks(guildId, userId, itemType, itemKey) VALUES(?, ?, 'title', ?)",
            (guild_id, user_id, achievement_key)
        )
        newly_unlocked = self.cursor.rowcount > 0
        self.db.commit()
        return newly_unlocked

    # How many CARD_SHOP_* items (any of the three catalogs) `user_id`
    # has actually purchased, used by the "big_spender" achievement.
    # card_unlocks doesn't distinguish "bought" from "earned by rank" or
    # "specially granted" on its own (they're all just rows). So this
    # cross-checks each row's itemKey against the shop catalogs
    # specifically rather than just counting every unlock they have.
    def _countShopPurchases(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT itemType, itemKey FROM card_unlocks WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        count = 0
        for item_type, item_key in self.cursor.fetchall():
            if item_type == "title" and item_key in CARD_SHOP_TITLES:
                count += 1
            elif item_type == "color_scheme" and item_key in CARD_SHOP_COLOR_SCHEMES:
                count += 1
            elif item_type == "font_style" and item_key in CARD_SHOP_FONT_STYLES:
                count += 1
        return count

    # (game_wins threshold, achievement_key) pairs, lowest to highest,
    # the Veteran ladder. Walked as a plain list rather than four
    # separate if-statements so a fifth tier is a one-line addition
    # later.
    CARD_ACHIEVEMENT_VETERAN_LADDER = [
        (CARD_ACHIEVEMENT_VETERAN_WINS, "veteran"),
        (CARD_ACHIEVEMENT_VETERAN_ELITE_WINS, "veteran_elite"),
        (CARD_ACHIEVEMENT_VETERAN_MASTER_WINS, "veteran_master"),
        (CARD_ACHIEVEMENT_VETERAN_IMMORTAL_WINS, "veteran_immortal"),
    ]
    # Same shape for the On Fire streak ladder.
    CARD_ACHIEVEMENT_ON_FIRE_LADDER = [
        (CARD_ACHIEVEMENT_ON_FIRE_STREAK, "on_fire"),
        (CARD_ACHIEVEMENT_ON_FIRE_UNSTOPPABLE_STREAK, "on_fire_unstoppable"),
        (CARD_ACHIEVEMENT_ON_FIRE_UNTOUCHABLE_STREAK, "on_fire_untouchable"),
    ]

    # Every achievement checkable from a plain snapshot of `user_id`'s
    # own state (their economy row plus a couple of cheap live queries)
    # rather than needing extra context from a specific event. Those
    # (High Roller, Jackpot, Giant Slayer, Tournament Champion) are
    # checked separately, inline where that event's own extra context
    # is already available (applyGameDeltas, the tournament-champion
    # announcement sites). Called both from applyGameDeltas (right
    # after a game result changes
    # game_wins/game_losses/current_win_streak) and lazily from
    # _buildStatsEmbed, the same "self-heal on the next read" idea
    # ensureEconomyRow/ensureCardSettings/_checkTierRewardUnlocks
    # already use elsewhere, so someone who already qualified before an
    # achievement existed (or whose team-count/shop-purchase count
    # changed some other way) still gets credited the next time
    # anything looks at their stats. Returns the keys newly unlocked
    # this call, for the caller to notify about (empty from the lazy
    # _buildStatsEmbed path, which intentionally discards it. The
    # self-heal shouldn't announce something that may have quietly been
    # true for a while).
    def _checkAchievements(self, guild_id, user_id, game=None):
        if game is None:
            game = self._currentGame(guild_id)
        self.cursor.execute(
            "SELECT wins, losses FROM economy WHERE guildId=? AND userId=?", (guild_id, user_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return []
        bet_wins, bet_losses = row

        self.cursor.execute(
            "SELECT game_wins, game_losses, current_win_streak FROM game_stats "
            "WHERE guildId=? AND userId=? AND game=?",
            (guild_id, user_id, game)
        )
        game_row = self.cursor.fetchone()
        game_wins, game_losses, current_win_streak = game_row if game_row is not None else (0, 0, 0)

        newly_unlocked = []
        if game_wins >= 1 and self._unlockAchievement(guild_id, user_id, "first_blood"):
            newly_unlocked.append("first_blood")
        for threshold, key in self.CARD_ACHIEVEMENT_VETERAN_LADDER:
            if game_wins >= threshold and self._unlockAchievement(guild_id, user_id, key):
                newly_unlocked.append(key)
        for threshold, key in self.CARD_ACHIEVEMENT_ON_FIRE_LADDER:
            if current_win_streak >= threshold and self._unlockAchievement(guild_id, user_id, key):
                newly_unlocked.append(key)
        if (
            game_losses >= CARD_ACHIEVEMENT_IRON_WILL_LOSSES
            and self._unlockAchievement(guild_id, user_id, "iron_will")
        ):
            newly_unlocked.append("iron_will")
        if (
            bet_wins + bet_losses >= CARD_ACHIEVEMENT_GAMBLER_BETS
            and self._unlockAchievement(guild_id, user_id, "gambler")
        ):
            newly_unlocked.append("gambler")

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if (
            len(teams) >= CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS
            and self._unlockAchievement(guild_id, user_id, "team_player")
        ):
            newly_unlocked.append("team_player")
        is_captain = any(self.isTeamCaptain(team, user_id) for _team_id, team in teams)
        if is_captain and self._unlockAchievement(guild_id, user_id, "captain"):
            newly_unlocked.append("captain")

        if (
            self._countShopPurchases(guild_id, user_id) >= CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS
            and self._unlockAchievement(guild_id, user_id, "big_spender")
        ):
            newly_unlocked.append("big_spender")
        return newly_unlocked

    # Posts one message per newly-unlocked achievement. `newly_unlocked`
    # is a list of (user_id, achievement_key) pairs, the shape
    # applyGameDeltas/_grantTournamentChampionAchievement both return. A
    # raw `<@id>` mention is used directly rather than resolving a real
    # Member first. It renders identically either way, and every caller
    # here already has a channel but not necessarily a fetched member.
    async def _announceAchievements(self, channel, newly_unlocked):
        for user_id, achievement_key in newly_unlocked:
            title = CARD_ACHIEVEMENT_TITLES.get(achievement_key)
            if title is None:
                continue
            await channel.send(f"\U0001f3c6 <@{user_id}> unlocked the **{title}** achievement!")

    # Grants every rostered player on `team` credit for winning a
    # tournament, called from both tournament-completion announcement
    # sites (single elimination, and the Grand Finals path for double
    # elimination). Returns the same (user_id, achievement_key) list
    # shape applyGameDeltas does, ready to pass straight to
    # _announceAchievements.
    def _grantTournamentChampionAchievement(self, guild_id, team):
        newly_unlocked = []
        for player in team.get_players():
            if self._unlockAchievement(guild_id, player.get_id(), "tournament_champion"):
                newly_unlocked.append((player.get_id(), "tournament_champion"))
        return newly_unlocked

    # Every trading-card title `user_id` has permanently unlocked in
    # this guild, as display-ready strings (CARD_TITLE_CATALOG's
    # values, not the raw itemKeys stored in card_unlocks: a tier name
    # for a rank reward, or a CARD_SPECIAL_TITLES key for a
    # manually-granted one), read by /card-set to offer as choices.
    # CARD_DEFAULT_TITLE isn't included here since it needs no
    # unlocking (see getAvailableCardTitles).
    def getUnlockedCardTitles(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT itemKey FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='title'",
            (guild_id, user_id)
        )
        titles = [
            CARD_TITLE_CATALOG[key] for (key,) in self.cursor.fetchall() if key in CARD_TITLE_CATALOG
        ]
        # SHOCKWAVE_DEVELOPER_ID always has Developer, in every guild,
        # regardless of whether a card_unlocks row exists for them here,
        # see that constant's own comment for why this isn't just a
        # per-guild grant instead.
        if user_id == SHOCKWAVE_DEVELOPER_ID and CARD_SPECIAL_TITLES["Developer"] not in titles:
            titles.append(CARD_SPECIAL_TITLES["Developer"])
        return titles

    # /card-set's own title choice list: CARD_DEFAULT_TITLE (always
    # available; it needs no unlocking, it's just the base title) plus
    # whatever this player has actually unlocked.
    def getAvailableCardTitles(self, guild_id, user_id):
        return [CARD_DEFAULT_TITLE] + self.getUnlockedCardTitles(guild_id, user_id)

    # Sets `user_id`'s equipped trading-card title. Trusts `title` is
    # already validated (see cardSetHelper, the command boundary that
    # checks it against getAvailableCardTitles). This is the internal
    # write half only. Also marks the row customized=1, the same flag
    # ensureCardSettings' own resync-to-defaults check respects.
    # Without it, the very next /stats call would silently reset this
    # right back to CARD_DEFAULT_TITLE.
    def setCardTitle(self, guild_id, user_id, title):
        self.ensureCardSettings(guild_id, user_id)
        self.cursor.execute(
            "UPDATE trading_cards SET title=?, customized=1 WHERE guildId=? AND userId=?",
            (title, guild_id, user_id)
        )
        self.db.commit()

    # Renders `member`'s current trading card as a ready-to-send
    # discord.File, shared by the three /card-set-* commands so each
    # one can actually show the result of the change it just made, not
    # just confirm it in text. Simpler than _swapStatsForTradingCard's
    # own version: the caller of a /card-set-* command is always a
    # real, currently-present member (they're the one running the
    # command right now), so there's no "member left the guild"
    # fallback to handle here.
    async def _renderMemberTradingCardFile(self, guild_id, guild_name, member):
        user_id = member.id
        display_name = member.display_name

        game = self._currentGame(guild_id)
        self.ensureEconomyRow(guild_id, user_id, display_name)
        self.ensureGameStatsRow(guild_id, user_id, display_name, game)
        self.cursor.execute(
            "SELECT elo, ranked_wins, ranked_losses FROM game_stats WHERE guildId=? AND userId=? AND game=?",
            (guild_id, user_id, game)
        )
        elo, ranked_wins, ranked_losses = self.cursor.fetchone()
        ranked_games = ranked_wins + ranked_losses
        stats = {
            "elo": elo, "elo_rank": self.eloRankLabelPlain(elo),
            "ranked_wins": ranked_wins, "ranked_losses": ranked_losses,
            "ranked_win_rate": f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A",
        }
        teams = [team for _, team in self.getTeamsForPlayer(guild_id, user_id)]
        settings = self.getCardSettings(guild_id, user_id)

        try:
            avatar_bytes = await member.display_avatar.with_format("png").read()
            avatar_image = Image.open(io.BytesIO(avatar_bytes))
        except Exception:
            avatar_image = Image.new("RGBA", (CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), BRACKET_BACKGROUND_CENTER)

        card_image = await asyncio.to_thread(
            self._renderTradingCardImage,
            guild_name, display_name, avatar_image, settings, stats, teams, username=member.name
        )
        return self._imageToFile(card_image, "trading_card.png")

    # An embed+file pair ready to pass straight to
    # ctx.response.send_message (embed=..., file=...) showing
    # `member`'s current trading card. Thin wrapper around
    # _renderMemberTradingCardFile so each /card-set-* helper doesn't
    # repeat the same two lines of embed setup.
    async def _cardPreviewEmbedAndFile(self, ctx, member):
        guild_name = ctx.guild.name if ctx.guild is not None else ""
        file = await self._renderMemberTradingCardFile(ctx.guild.id, guild_name, member)
        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        return embed, file

    # Every trading-card color scheme `user_id` has permanently unlocked
    # in this guild: {name, accent_color, background_color}, hex-encoded
    # the same way trading_cards itself stores colors, ready to write
    # straight into that table's own columns (see setCardColorScheme,
    # /card-set-color-scheme). Colors are derived from
    # ELO_TIER_BADGE_COLORS on every call rather than stored, so a
    # scheme always matches whatever that tier's badge color currently
    # is instead of freezing at whatever it was the day it was
    # unlocked.
    # `accent_rgb` boosted for readability against `background_rgb`'s
    # own lightened vignette center (see _ensureReadableAccent),
    # hex-encoded. The one place getUnlockedCardColorSchemes computes
    # this for either a tier-earned or a shop-bought scheme, so the two
    # branches below share one implementation instead of drifting
    # apart.
    def _readableAccentHex(self, accent_rgb, background_rgb):
        background_center = self._lightenColor(background_rgb, 0.3)
        return self._rgbToHex(self._ensureReadableAccent(accent_rgb, background_center))

    def getUnlockedCardColorSchemes(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT itemKey FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='color_scheme'",
            (guild_id, user_id)
        )
        schemes = []
        for (key,) in self.cursor.fetchall():
            if key in ELO_TIER_BADGE_COLORS:
                accent_rgb = ELO_TIER_BADGE_COLORS[key]
                background_rgb = tuple(round(c * CARD_BACKGROUND_DARKEN_RATIO) for c in accent_rgb)
                background_hex = self._rgbToHex(background_rgb)
            elif key in CARD_SHOP_COLOR_SCHEMES:
                entry = CARD_SHOP_COLOR_SCHEMES[key]
                accent_rgb = self._hexToRgb(entry["accent_color"], TEAM_CARD_FALLBACK_ACCENT_COLOR)
                background_rgb = self._hexToRgb(entry["background_color"], (0, 0, 0))
                background_hex = entry["background_color"]
            else:
                continue
            # _renderTradingCardImage trusts trading_cards' stored
            # colors exactly as given (a player who hand-edits a custom
            # hex value should see exactly that value, not something
            # silently adjusted). So the readability guarantee has to
            # live here instead, in the catalog itself, before a scheme
            # is ever offered or equipped. A tier badge color was
            # picked for how it reads as a small circle/diamond
            # standing in for an emoji (see ELO_TIERS), not for driving
            # a whole card's header/label text against its own
            # darkened background, and even a hand-picked shop color
            # gets the same safety net rather than trusting it was
            # chosen carefully enough.
            schemes.append({
                "name": key,
                "accent_color": self._readableAccentHex(accent_rgb, background_rgb),
                "background_color": background_hex,
            })
        return schemes

    # /card-set's own color scheme choice list: CARD_DEFAULT_SCHEME_NAME
    # (Shockwave's own palette, always available, needs no unlocking) plus
    # whatever this player has actually unlocked, same shape
    # getAvailableCardTitles has to getUnlockedCardTitles.
    def getAvailableCardColorSchemes(self, guild_id, user_id):
        default = {
            "name": CARD_DEFAULT_SCHEME_NAME,
            "accent_color": CARD_DEFAULT_ACCENT_COLOR,
            "background_color": CARD_DEFAULT_BACKGROUND_COLOR,
        }
        return [default] + self.getUnlockedCardColorSchemes(guild_id, user_id)

    # Sets `user_id`'s equipped trading-card accent/background colors.
    # Trusts `accent_color`/`background_color` are already validated
    # (see cardSetHelper, the command boundary that resolves a scheme
    # name against getAvailableCardColorSchemes). This is the internal
    # write half only. Also marks the row customized=1, same reasoning
    # setCardTitle's own comment gives: without it, the very next
    # /stats call would silently resync these back to CARD_DEFAULT_*.
    # `scheme_name`, when given, is remembered (color_scheme_name) so
    # _resyncEquippedColorScheme can keep tracking that scheme's
    # current colors. Omitting it (a hand-edited custom hex value some
    # other way) leaves nothing to track, same as before this
    # parameter existed.
    def setCardColorScheme(self, guild_id, user_id, accent_color, background_color, scheme_name=None):
        self.ensureCardSettings(guild_id, user_id)
        self.cursor.execute(
            "UPDATE trading_cards SET accent_color=?, background_color=?, color_scheme_name=?, customized=1 "
            "WHERE guildId=? AND userId=?",
            (accent_color, background_color, scheme_name, guild_id, user_id)
        )
        self.db.commit()

    # Every trading-card font style `user_id` has purchased in this
    # guild (see /shop buy). Unlike titles/color schemes there's no
    # elo-tier path to one of these at all, only the shop, so this is a
    # straight itemKey lookup against CARD_SHOP_FONT_STYLES rather than
    # needing a combining catalog the way getUnlockedCardTitles does.
    def getUnlockedCardFontStyles(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT itemKey FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='font_style'",
            (guild_id, user_id)
        )
        return [key for (key,) in self.cursor.fetchall() if key in CARD_SHOP_FONT_STYLES]

    # /card-set's own font choice list: CARD_DEFAULT_FONT_STYLE (always
    # available, needs no unlocking) plus whatever this player has
    # actually purchased, same shape getAvailableCardTitles/
    # getAvailableCardColorSchemes have to their own unlocked-items lookup.
    def getAvailableCardFontStyles(self, guild_id, user_id):
        return [CARD_DEFAULT_FONT_STYLE] + self.getUnlockedCardFontStyles(guild_id, user_id)

    # /shop preview: the four things worth seeing all at once before
    # spending gold or picking a name blind: every built-in logo, every
    # card title, every color scheme, and every font, regardless of
    # what any specific player has actually unlocked (unlike
    # getAvailableCard*, which are deliberately per-player). Cached to
    # PREVIEW_DIR (see _cachedPreviewFiles) since none of these change
    # without a code change, so there's no reason to re-render on every
    # call.
    PREVIEW_FILE_STEMS = {
        "Logos": "logos",
        "Card Titles": "card-titles",
        "Color Schemes": "color-schemes",
        "Fonts": "fonts",
    }

    # Sequential `<stem>-1.png`, `<stem>-2.png`, ... rather than a
    # glob/regex scan. Probes until the next page is simply missing, so
    # a partially-deleted cache (someone removed page 2 by hand) just
    # looks like "only 1 page exists" instead of needing any special
    # handling.
    def _cachedPreviewFiles(self, stem):
        paths = []
        page = 1
        while True:
            path = os.path.join(PREVIEW_DIR, f"{stem}-{page}.png")
            if not os.path.isfile(path):
                break
            paths.append(path)
            page += 1
        return paths

    # How many of `items` fit on one page before PREVIEW_MAX_PAGE_HEIGHT
    # is exceeded, given a PREVIEW_COLUMNS-wide grid of
    # PREVIEW_CELL_SIZE cells. Used by both the logo and color-scheme
    # previews (the two that are actually grids; titles/fonts are
    # short one-column lists that never come close to needing this).
    def _paginateGridItems(self, items):
        row_height = PREVIEW_CELL_SIZE + PREVIEW_CELL_LABEL_HEIGHT + PREVIEW_CELL_GAP
        header_height = PREVIEW_MARGIN * 2 + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP
        rows_per_page = max(1, (PREVIEW_MAX_PAGE_HEIGHT - header_height) // row_height)
        cells_per_page = int(rows_per_page * PREVIEW_COLUMNS)
        if not items:
            return [[]]
        return [items[i:i + cells_per_page] for i in range(0, len(items), cells_per_page)]

    # Renders one page of a PREVIEW_COLUMNS-wide grid. `draw_cell(image,
    # draw, x, y, size, item)` draws whatever goes inside each cell (a
    # pasted logo, a color swatch, etc.), `label(item)` supplies the
    # text under it. Shared by the logo and color-scheme previews,
    # which only differ in what a cell actually looks like.
    def _renderPreviewGridPage(self, title, items, draw_cell, label):
        accent_color = self._hexToRgb(CARD_DEFAULT_ACCENT_COLOR, TEAM_CARD_FALLBACK_ACCENT_COLOR)
        background_color = self._hexToRgb(CARD_DEFAULT_BACKGROUND_COLOR, (37, 26, 91))
        text_color = self._hexToRgb(CARD_DEFAULT_TEXT_COLOR, BRACKET_TEXT_COLOR)

        title_font = self._loadFont(CHAKRA_PETCH_BOLD, PREVIEW_TITLE_FONT_SIZE)
        label_font = self._loadFont(IBM_PLEX_SANS, PREVIEW_LABEL_FONT_SIZE, "Medium")

        rows = math.ceil(len(items) / PREVIEW_COLUMNS) if items else 1
        row_height = PREVIEW_CELL_SIZE + PREVIEW_CELL_LABEL_HEIGHT + PREVIEW_CELL_GAP
        header_height = PREVIEW_MARGIN * 2 + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP
        width = PREVIEW_MARGIN * 2 + PREVIEW_COLUMNS * PREVIEW_CELL_SIZE + (PREVIEW_COLUMNS - 1) * PREVIEW_CELL_GAP
        height = int(header_height + rows * row_height + PREVIEW_MARGIN - PREVIEW_CELL_GAP)

        image = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(image)
        draw.text((width / 2, PREVIEW_MARGIN), title, font=title_font, fill=accent_color, anchor="ma")
        rule_y = PREVIEW_MARGIN + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP / 2
        draw.line([(PREVIEW_MARGIN, rule_y), (width - PREVIEW_MARGIN, rule_y)], fill=accent_color, width=2)

        for i, item in enumerate(items):
            col, row = i % PREVIEW_COLUMNS, i // PREVIEW_COLUMNS
            x = PREVIEW_MARGIN + col * (PREVIEW_CELL_SIZE + PREVIEW_CELL_GAP)
            y = header_height + row * row_height
            draw_cell(image, draw, x, y, PREVIEW_CELL_SIZE, item)
            draw.text(
                (x + PREVIEW_CELL_SIZE / 2, y + PREVIEW_CELL_SIZE + PREVIEW_CELL_LABEL_HEIGHT / 2),
                label(item), font=label_font, fill=text_color, anchor="mm"
            )

        return image

    def _drawLogoPreviewCell(self, image, draw, x, y, size, logo_name):
        accent_color = self._hexToRgb(CARD_DEFAULT_ACCENT_COLOR, TEAM_CARD_FALLBACK_ACCENT_COLOR)
        draw.rounded_rectangle([x, y, x + size, y + size], radius=14, outline=accent_color, width=2)

        logo_path = self._resolveLogoPath(logo_name)
        if logo_path is None:
            return
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((size - 20, size - 20), Image.LANCZOS)
        image.paste(logo, (x + (size - logo.width) // 2, y + (size - logo.height) // 2), logo)

    # Every logo in TEAM_LOGO_DIR, gridded with its own name underneath,
    # the exact string /team set's logo param (and its autocomplete)
    # takes. So this doubles as a lookup table for "what do I actually
    # type" as well as a gallery.
    def _renderLogoPreviewImages(self):
        names = self.listAvailableLogos()
        pages = self._paginateGridItems(names)
        return [
            self._renderPreviewGridPage(
                "Logos" if len(pages) == 1 else f"Logos ({i}/{len(pages)})",
                page_names, self._drawLogoPreviewCell, lambda n: n,
            )
            for i, page_names in enumerate(pages, start=1)
        ]

    def _drawColorSchemePreviewCell(self, image, draw, x, y, size, scheme):
        _name, accent_hex, background_hex = scheme
        accent = self._hexToRgb(accent_hex, TEAM_CARD_FALLBACK_ACCENT_COLOR)
        background = self._hexToRgb(background_hex, (37, 26, 91))
        draw.rounded_rectangle([x, y, x + size, y + size], radius=14, fill=background)
        inner = size * 0.45
        ix, iy = x + (size - inner) / 2, y + (size - inner) / 2
        draw.ellipse([ix, iy, ix + inner, iy + inner], fill=accent)

    # Every color scheme's own background as the swatch's fill and its
    # accent as the circle in the middle, the same two colors that
    # actually drive an equipped card (background + accent), rather than
    # some unrelated stand-in shape.
    def _renderColorSchemePreviewImages(self):
        schemes = [(CARD_DEFAULT_SCHEME_NAME, CARD_DEFAULT_ACCENT_COLOR, CARD_DEFAULT_BACKGROUND_COLOR)]
        schemes += [
            (name, info["accent_color"], info["background_color"])
            for name, info in CARD_SHOP_COLOR_SCHEMES.items()
        ]
        pages = self._paginateGridItems(schemes)
        return [
            self._renderPreviewGridPage(
                "Color Schemes" if len(pages) == 1 else f"Color Schemes ({i}/{len(pages)})",
                page_schemes, self._drawColorSchemePreviewCell, lambda s: s[0],
            )
            for i, page_schemes in enumerate(pages, start=1)
        ]

    # No grid: a font style has nothing to lay out in columns, just one
    # sample line per style, each actually rendered in its own
    # typeface (via _cardFontPaths, the same lookup an equipped card
    # itself uses), so this is a genuine preview of the difference, not
    # just a label list. The style's own key is drawn in a neutral font
    # beside its sample, since "Cyber" rendered only in Orbitron could
    # otherwise be hard to make out as a word at a glance.
    def _renderFontPreviewImage(self):
        font_styles = [CARD_DEFAULT_FONT_STYLE] + list(CARD_SHOP_FONT_STYLES.keys())
        accent_color = self._hexToRgb(CARD_DEFAULT_ACCENT_COLOR, TEAM_CARD_FALLBACK_ACCENT_COLOR)
        background_color = self._hexToRgb(CARD_DEFAULT_BACKGROUND_COLOR, (37, 26, 91))
        text_color = self._hexToRgb(CARD_DEFAULT_TEXT_COLOR, BRACKET_TEXT_COLOR)

        title_font = self._loadFont(CHAKRA_PETCH_BOLD, PREVIEW_TITLE_FONT_SIZE)
        key_font = self._loadFont(IBM_PLEX_SANS, PREVIEW_LABEL_FONT_SIZE, "Medium")
        sample_size = 40
        row_height = sample_size + PREVIEW_CELL_GAP * 2

        header_height = PREVIEW_MARGIN * 2 + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP
        width = 640
        height = int(header_height + len(font_styles) * row_height + PREVIEW_MARGIN)

        image = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(image)
        draw.text((width / 2, PREVIEW_MARGIN), "Fonts", font=title_font, fill=accent_color, anchor="ma")
        rule_y = PREVIEW_MARGIN + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP / 2
        draw.line([(PREVIEW_MARGIN, rule_y), (width - PREVIEW_MARGIN, rule_y)], fill=accent_color, width=2)

        for i, font_style in enumerate(font_styles):
            paths = self._cardFontPaths(font_style)
            sample_font = self._loadFont(paths["name_font"], sample_size, paths["name_variation"])
            row_y = header_height + i * row_height + row_height / 2
            draw.text((PREVIEW_MARGIN, row_y), font_style, font=sample_font, fill=text_color, anchor="lm")
            draw.text(
                (width - PREVIEW_MARGIN, row_y), f"({font_style})", font=key_font, fill=accent_color,
                anchor="rm"
            )

        return image

    # Also no grid: a title is just an equippable string, nothing
    # visual differs between two titles beyond the text itself. So
    # this is a plain list rendered in the card's own default title
    # font for a consistent feel rather than a plain unstyled listing.
    def _renderCardTitlePreviewImage(self):
        titles = [CARD_DEFAULT_TITLE] + list(CARD_TITLE_CATALOG.values())
        accent_color = self._hexToRgb(CARD_DEFAULT_ACCENT_COLOR, TEAM_CARD_FALLBACK_ACCENT_COLOR)
        background_color = self._hexToRgb(CARD_DEFAULT_BACKGROUND_COLOR, (37, 26, 91))
        text_color = self._hexToRgb(CARD_DEFAULT_TEXT_COLOR, BRACKET_TEXT_COLOR)

        title_font = self._loadFont(CHAKRA_PETCH_BOLD, PREVIEW_TITLE_FONT_SIZE)
        item_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, 26)
        row_height = 26 + PREVIEW_CELL_GAP * 2

        header_height = PREVIEW_MARGIN * 2 + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP
        width = 500
        height = int(header_height + len(titles) * row_height + PREVIEW_MARGIN)

        image = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(image)
        draw.text((width / 2, PREVIEW_MARGIN), "Card Titles", font=title_font, fill=accent_color, anchor="ma")
        rule_y = PREVIEW_MARGIN + PREVIEW_TITLE_FONT_SIZE + PREVIEW_CELL_GAP / 2
        draw.line([(PREVIEW_MARGIN, rule_y), (width - PREVIEW_MARGIN, rule_y)], fill=accent_color, width=2)

        for i, title in enumerate(titles):
            row_y = header_height + i * row_height + row_height / 2
            draw.text((width / 2, row_y), title, font=item_font, fill=text_color, anchor="mm")

        return image

    def _renderPreviewImages(self, preview_type):
        if preview_type == "Logos":
            return self._renderLogoPreviewImages()
        if preview_type == "Color Schemes":
            return self._renderColorSchemePreviewImages()
        if preview_type == "Fonts":
            return [self._renderFontPreviewImage()]
        return [self._renderCardTitlePreviewImage()]

    # Renders (if not already cached, see _cachedPreviewFiles) and posts
    # every option for `preview_type`, as one or more attachments.
    # Nothing here is guild- or player-specific (unlike /card-set's own
    # choice lists), so the same cached files serve every server.
    async def previewHelper(self, ctx, preview_type):
        stem = self.PREVIEW_FILE_STEMS[preview_type]
        cached = self._cachedPreviewFiles(stem)

        if not cached:
            images = await asyncio.to_thread(self._renderPreviewImages, preview_type)
            os.makedirs(PREVIEW_DIR, exist_ok=True)
            for i, image in enumerate(images, start=1):
                path = os.path.join(PREVIEW_DIR, f"{stem}-{i}.png")
                image.save(path, format="PNG")
                cached.append(path)

        files = [discord.File(path) for path in cached]
        page_note = f" ({len(files)} images)" if len(files) > 1 else ""
        await ctx.response.send_message(f"**{preview_type}** preview{page_note}:", files=files)

    # Sets `user_id`'s equipped trading-card font. Trusts `font_style` is
    # already validated (see cardSetHelper); this is the internal
    # write half only, same shape setCardTitle/setCardColorScheme have.
    # Also marks the row customized=1 for the same reason those two do.
    def setCardFontStyle(self, guild_id, user_id, font_style):
        self.ensureCardSettings(guild_id, user_id)
        self.cursor.execute(
            "UPDATE trading_cards SET font_style=?, customized=1 WHERE guildId=? AND userId=?",
            (font_style, guild_id, user_id)
        )
        self.db.commit()

    # /card-set: equips any combination of an unlocked title, color
    # scheme, and/or font in one call. Every provided field is
    # validated against its own unlock catalog before ANY of them is
    # applied, so a bad value in one field (a typo'd font, say) can't
    # leave the other two half-applied. Either the whole call goes
    # through or none of it does.
    async def cardSetHelper(self, ctx, title, color_scheme, font_style):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        if title is None and color_scheme is None and font_style is None:
            await ctx.response.send_message(
                "Give at least one of title, color_scheme, or font_style to equip.", ephemeral=True
            )
            return

        if title is not None and title not in self.getAvailableCardTitles(guild_id, user_id):
            await ctx.response.send_message(
                f"You haven't unlocked **{title}**. Pick one of your unlocked titles from the "
                "autocomplete list, or check /stats to see what you've earned.",
                ephemeral=True,
            )
            return

        schemes = {s["name"]: s for s in self.getAvailableCardColorSchemes(guild_id, user_id)}
        if color_scheme is not None and color_scheme not in schemes:
            await ctx.response.send_message(
                f"You haven't unlocked the **{color_scheme}** color scheme. Pick one of your unlocked "
                "schemes from the autocomplete list, or check /stats to see what you've earned.",
                ephemeral=True,
            )
            return

        if font_style is not None and font_style not in self.getAvailableCardFontStyles(guild_id, user_id):
            await ctx.response.send_message(
                f"You haven't unlocked the **{font_style}** font. Pick one of your unlocked fonts "
                "from the autocomplete list, or check /shop browse to see what's available.",
                ephemeral=True,
            )
            return

        applied = []
        if title is not None:
            self.setCardTitle(guild_id, user_id, title)
            applied.append(f'title **"{title}"**')
        if color_scheme is not None:
            chosen = schemes[color_scheme]
            self.setCardColorScheme(
                guild_id, user_id, chosen["accent_color"], chosen["background_color"], scheme_name=color_scheme
            )
            applied.append(f'the **{color_scheme}** color scheme')
        if font_style is not None:
            self.setCardFontStyle(guild_id, user_id, font_style)
            applied.append(f'the **{font_style}** font')

        if len(applied) == 1:
            summary = applied[0]
        elif len(applied) == 2:
            summary = f"{applied[0]} and {applied[1]}"
        else:
            summary = f"{', '.join(applied[:-1])}, and {applied[-1]}"

        # _cardPreviewEmbedAndFile fetches the caller's own avatar (a
        # real network round trip) and renders the card with Pillow,
        # either of which can push this past Discord's ~3 second ack
        # window. Post a placeholder immediately and edit it in place
        # once the real card is ready, rather than risking "This
        # interaction failed".
        await ctx.response.send_message(f"Updating your trading card to use {summary}, please wait...")
        embed, file = await self._cardPreviewEmbedAndFile(ctx, ctx.user)
        await ctx.edit_original_response(
            content=f"Your trading card now uses {summary}.", embed=embed, attachments=[file]
        )

    # Whether `user_id` already owns `item_key` (any shop item type) in
    # this guild, shared by getShopCatalog (to mark what's already owned)
    # and shopBuyHelper (to refuse selling the same thing twice).
    def _shopItemOwned(self, guild_id, user_id, item_type, item_key):
        self.cursor.execute(
            "SELECT 1 FROM card_unlocks WHERE guildId=? AND userId=? AND itemType=? AND itemKey=?",
            (guild_id, user_id, item_type, item_key)
        )
        return self.cursor.fetchone() is not None

    # Every purchasable item across all three CARD_SHOP_* catalogs, each
    # as {type, name, price, owned}, what /shop browse displays and
    # /shop buy's own autocomplete filters down to just what's still
    # unowned.
    def getShopCatalog(self, guild_id, user_id):
        catalog = []
        for name, price in CARD_SHOP_TITLES.items():
            catalog.append({
                "type": "title", "name": name, "price": price,
                "owned": self._shopItemOwned(guild_id, user_id, "title", name),
            })
        for name, entry in CARD_SHOP_COLOR_SCHEMES.items():
            catalog.append({
                "type": "color_scheme", "name": name, "price": entry["price"],
                "owned": self._shopItemOwned(guild_id, user_id, "color_scheme", name),
            })
        for name, price in CARD_SHOP_FONT_STYLES.items():
            catalog.append({
                "type": "font_style", "name": name, "price": price,
                "owned": self._shopItemOwned(guild_id, user_id, "font_style", name),
            })
        return catalog

    # `item`'s (type, price) from whichever CARD_SHOP_* catalog
    # actually has it, or (None, None) if it's not a real shop item at
    # all. The one place shopBuyHelper needs to know which catalog
    # (and which command) a purchased name belongs to.
    def _resolveShopItem(self, item):
        if item in CARD_SHOP_TITLES:
            return "title", CARD_SHOP_TITLES[item]
        if item in CARD_SHOP_COLOR_SCHEMES:
            return "color_scheme", CARD_SHOP_COLOR_SCHEMES[item]["price"]
        if item in CARD_SHOP_FONT_STYLES:
            return "font_style", CARD_SHOP_FONT_STYLES[item]
        return None, None

    # The embed both shopHelper's initial post and every later
    # ShopSortView button click render, kept as one method so a re-sort
    # can never drift from what /shop browse itself would show. Each
    # category's items keep their own catalog order when sort_key is
    # None (the original behavior, still what a fresh /shop browse call
    # gets). "price" sorts cheapest-first and "owned" sorts
    # unowned-first, each reversed by `descending`. Sorting only ever
    # reorders each category's own lines. Titles/Color Schemes/Fonts
    # never mix together, so the three-field layout below stays the
    # same shape either way. Python's sort is stable, so items tied on
    # the sort key keep their catalog order relative to each other.
    def _buildShopEmbed(self, guild_id, user_id, sort_key=None, descending=False):
        balance = self.getEconomy(guild_id, user_id, "balance")
        catalog = self.getShopCatalog(guild_id, user_id)

        embed = discord.Embed(
            title="Trading Card Shop", description=f"Your balance: **{balance} gold**",
            color=discord.Color.gold()
        )
        for label, item_type in (
            ("Titles", "title"), ("Color Schemes", "color_scheme"), ("Fonts", "font_style")
        ):
            items = [item for item in catalog if item["type"] == item_type]
            if sort_key == "price":
                items.sort(key=lambda item: item["price"], reverse=descending)
            elif sort_key == "owned":
                items.sort(key=lambda item: item["owned"], reverse=descending)

            lines = []
            for item in items:
                status = f"{item['price']} gold" + (" ✅" if item["owned"] else "")
                lines.append(f"**{item['name']}** - {status}")
            # __underline__ (Discord markdown) rather than just bold, so
            # each category heading reads distinctly from the item lines'
            # own bolded names underneath it.
            embed.add_field(name=f"__{label}__", value="\n".join(lines), inline=False)

        footer = "/shop buy to purchase; equip with /card-set"
        if sort_key is not None:
            sort_label = "price" if sort_key == "price" else "owned status"
            direction = "descending" if descending else "ascending"
            footer += f" - sorted by {sort_label} ({direction})"
        embed.set_footer(text=footer)
        return embed

    # /shop browse: every purchasable cosmetic, grouped by category,
    # with its price and the caller's current balance so they can see
    # at a glance what they can actually afford. An owned item still
    # shows its price (rather than hiding it behind an "Owned" marker)
    # with a ✅ appended, so comparing costs across a whole category
    # never loses an already-bought item's price from view.
    # ShopSortView's buttons let the caller re-sort by price or owned
    # status, either direction, without re-running the command.
    async def shopHelper(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id
        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        embed = self._buildShopEmbed(guild_id, user_id)
        view = ShopSortView(self, guild_id, user_id)
        await ctx.response.send_message(embed=embed, view=view)
        view.message = await ctx.original_response()

    # {key: (current, threshold)} for every achievement that has a plain
    # numeric progress toward it - the same snapshot _checkAchievements
    # itself gathers to decide who's newly qualified, reused here instead
    # of re-deriving it, so the two can't quietly drift apart. Left out
    # entirely for a key with no meaningful fraction: first_blood and
    # captain are one-off/binary, and high_roller/jackpot/giant_slayer/
    # tournament_champion/onboarded are each tied to a specific event
    # rather than an accumulating count. game defaults to the currently-
    # tracked game, matching _checkAchievements' own default, so a
    # locked veteran/on_fire tier's progress reflects whichever game
    # would actually unlock it next.
    def _achievementProgress(self, guild_id, user_id, game=None):
        if game is None:
            game = self._currentGame(guild_id)
        self.cursor.execute("SELECT wins, losses FROM economy WHERE guildId=? AND userId=?", (guild_id, user_id))
        row = self.cursor.fetchone()
        bet_wins, bet_losses = row if row is not None else (0, 0)

        self.cursor.execute(
            "SELECT game_wins, game_losses, current_win_streak FROM game_stats "
            "WHERE guildId=? AND userId=? AND game=?",
            (guild_id, user_id, game)
        )
        game_row = self.cursor.fetchone()
        game_wins, game_losses, current_win_streak = game_row if game_row is not None else (0, 0, 0)

        progress = {key: (game_wins, threshold) for threshold, key in self.CARD_ACHIEVEMENT_VETERAN_LADDER}
        progress.update(
            {key: (current_win_streak, threshold) for threshold, key in self.CARD_ACHIEVEMENT_ON_FIRE_LADDER}
        )
        progress["iron_will"] = (game_losses, CARD_ACHIEVEMENT_IRON_WILL_LOSSES)
        progress["gambler"] = (bet_wins + bet_losses, CARD_ACHIEVEMENT_GAMBLER_BETS)
        progress["team_player"] = (
            len(self.getTeamsForPlayer(guild_id, user_id)), CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS
        )
        progress["big_spender"] = (self._countShopPurchases(guild_id, user_id), CARD_ACHIEVEMENT_BIG_SPENDER_ITEMS)
        return progress

    # Every CARD_ACHIEVEMENT_TITLES entry as {key, name, description,
    # earned, progress}, what /achievements displays. Earned state reads
    # straight off card_unlocks, the exact same table (and same
    # itemType='title' shape) getUnlockedCardTitles already reads for
    # tier rewards, special grants, and shop purchases. progress is None
    # once earned (nothing left to show) or for a key _achievementProgress
    # has no fraction for at all, otherwise a (current, threshold) pair.
    def getAchievementCatalog(self, guild_id, user_id):
        self.cursor.execute(
            "SELECT itemKey FROM card_unlocks WHERE guildId=? AND userId=? AND itemType='title'",
            (guild_id, user_id)
        )
        earned_keys = {row[0] for row in self.cursor.fetchall()}
        progress_by_key = self._achievementProgress(guild_id, user_id)
        return [
            {
                "key": key, "name": name, "description": CARD_ACHIEVEMENT_DESCRIPTIONS.get(key, ""),
                "earned": key in earned_keys,
                "progress": None if key in earned_keys else progress_by_key.get(key),
            }
            for key, name in CARD_ACHIEVEMENT_TITLES.items()
        ]

    # /achievements: browses the full catalog with earned/not-earned
    # state, grouped into fields the same way /shop browse's own
    # shopHelper groups by item type (embed.add_field per category
    # rather than one flat description). Veteran and On Fire are each a
    # ladder of several rising thresholds (see
    # CARD_ACHIEVEMENT_VETERAN_LADDER/CARD_ACHIEVEMENT_ON_FIRE_LADDER),
    # so each gets its own field with its tiers listed lowest-to-highest
    # instead of its rungs being scattered through one long list
    # alongside every unrelated achievement. Runs the snapshot self-heal
    # first (same as _buildStatsEmbed does) so anyone who already
    # qualified sees it reflected immediately rather than needing a
    # /stats call first.
    # Discord caps an embed field's value at 1024 characters. __Other__
    # only grows as new non-ladder achievements are added, so this keeps
    # whatever whole lines fit and notes how many were cut instead of
    # risking add_field/send outright failing once the catalog is large
    # enough. No need for real pagination here yet (see _teamButtonLabel
    # for the same "just truncate" call on an unrelated 80-char limit).
    def _joinEmbedFieldLines(self, lines, limit=1024):
        joined = "\n".join(lines)
        if len(joined) <= limit:
            return joined

        kept = []
        length = 0
        for line in lines:
            added = len(line) + (1 if kept else 0)
            if length + added > limit - 40:
                break
            kept.append(line)
            length += added

        remaining = len(lines) - len(kept)
        return "\n".join(kept) + f"\n*+{remaining} more*"

    async def achievementsHelper(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id
        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        self._checkAchievements(guild_id, user_id)

        catalog = {item["key"]: item for item in self.getAchievementCatalog(guild_id, user_id)}

        def render(key):
            item = catalog[key]
            status = "✅" if item["earned"] else "🔒"
            line = f"{status} **{item['name']}** - {item['description']}"
            if item["progress"] is not None:
                current, threshold = item["progress"]
                line += f" ({min(current, threshold)}/{threshold})"
            return line

        embed = discord.Embed(title="Achievements", color=discord.Color.gold())

        ladder_keys = set()
        for label, ladder in (
            ("Veteran", self.CARD_ACHIEVEMENT_VETERAN_LADDER), ("On Fire", self.CARD_ACHIEVEMENT_ON_FIRE_LADDER)
        ):
            keys = [key for _threshold, key in ladder]
            ladder_keys.update(keys)
            embed.add_field(name=f"__{label}__", value="\n".join(render(key) for key in keys), inline=False)

        other_lines = [render(key) for key in CARD_ACHIEVEMENT_TITLES if key not in ladder_keys]
        embed.add_field(name="__Other__", value=self._joinEmbedFieldLines(other_lines), inline=False)

        embed.set_footer(text="Earned achievements unlock their title for /card-set")
        await ctx.response.send_message(embed=embed)

    # /shop buy: spends gold to permanently unlock one CARD_SHOP_* item,
    # writes to card_unlocks exactly like a tier reward does (see
    # _unlockCardReward). So a purchased item shows up through the exact same
    # getUnlockedCardTitles/getUnlockedCardColorSchemes/
    # getUnlockedCardFontStyles reads those use, with no separate "did
    # I buy this" concept anywhere else in the code.
    async def shopBuyHelper(self, ctx, item):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        item_type, price = self._resolveShopItem(item)
        if item_type is None:
            await ctx.response.send_message(f"**{item}** isn't in the shop.", ephemeral=True)
            return

        if self._shopItemOwned(guild_id, user_id, item_type, item):
            await ctx.response.send_message(f"You already own **{item}**.", ephemeral=True)
            return

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        balance = self.getEconomy(guild_id, user_id, "balance")
        if balance < price:
            await ctx.response.send_message(
                f"**{item}** costs {price} gold, but you only have {balance}.", ephemeral=True
            )
            return

        self.cursor.execute(
            "UPDATE economy SET balance = balance - ? WHERE guildId=? AND userId=?",
            (price, guild_id, user_id)
        )
        self.cursor.execute(
            "INSERT OR IGNORE INTO card_unlocks(guildId, userId, itemType, itemKey) VALUES(?, ?, ?, ?)",
            (guild_id, user_id, item_type, item)
        )
        self.db.commit()

        await ctx.response.send_message(
            f"Purchased **{item}** for {price} gold! Equip it with /card-set."
        )

    # This tier's real emoji artwork (see ELO_BADGE_DIR), loaded,
    # resized to fit CARD_ELO_BADGE_RADIUS, and cached
    # (_elo_badge_cache) so repeat calls for the same tier (the
    # overwhelmingly common case, since most cards render for
    # whichever handful of tiers this guild's players actually sit at)
    # don't re-hit disk. Returns None (nothing to paste) if the asset
    # is missing, e.g. a dev checkout that hasn't generated
    # assets/elo-badges/, the same "degrade instead of crash" reasoning
    # listAvailableLogos' own empty-folder handling uses.
    def _eloBadgeImage(self, elo):
        path = self.eloRankBadgeImagePath(elo)
        size = CARD_ELO_BADGE_RADIUS * 2
        cache_key = (path, size)
        if cache_key not in _elo_badge_cache:
            if os.path.isfile(path):
                badge = Image.open(path).convert("RGBA")
                badge.thumbnail((size, size), Image.LANCZOS)
                _elo_badge_cache[cache_key] = badge
            else:
                _elo_badge_cache[cache_key] = None
        return _elo_badge_cache[cache_key]

    # Pastes this tier's real emoji artwork centered at (x, y), standing
    # in for the emoji eloRankLabel shows in a real embed. PIL's
    # bundled TTF fonts can't render color emoji glyphs (the same class
    # of issue the roster's captain star ran into), so the card pastes
    # the actual saved image (see ELO_BADGE_DIR) instead of
    # hand-drawing an approximation of it. Silently draws nothing if
    # the asset's missing rather than crashing the whole card render
    # over one small badge.
    def _drawEloBadge(self, image, x, y, elo):
        badge = self._eloBadgeImage(elo)
        if badge is None:
            return
        paste_x = int(x - badge.width / 2)
        paste_y = int(y - badge.height / 2)
        image.paste(badge, (paste_x, paste_y), badge)

    # Every other bundled font stays comfortably under the card's width
    # even for a full 32-character Discord username at
    # CARD_NAME_FONT_SIZE. PRESS_START_2P ("Retro") is the one
    # exception: its near-monospace, unusually-wide-per-glyph metrics
    # can push a long real username well past the card's edge (a
    # 22-character name measured at exactly CARD_WIDTH itself in
    # testing, with nowhere left to safely draw a border). Rather than
    # special-case that one font, this shrinks whichever font/variation
    # was actually chosen down toward CARD_NAME_MIN_FONT_SIZE until it
    # fits, a no-op in practice for every other style, since they're
    # never close to the limit to begin with. Only the drawn font size
    # changes. name_y/title_y and the rest of the layout stay anchored
    # to the fixed CARD_NAME_FONT_SIZE slot regardless, so a shrunk
    # name just leaves a little extra breathing room under it rather
    # than needing the whole card re-laid-out.
    def _fitNameFont(self, font_path, variation, text, max_width):
        size = CARD_NAME_FONT_SIZE
        font = self._loadFont(font_path, size, variation)
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        while size > CARD_NAME_MIN_FONT_SIZE and measurer.textlength(text, font=font) > max_width:
            size -= 2 * BRACKET_SUPERSAMPLE
            font = self._loadFont(font_path, size, variation)
        return font

    # Pure rendering: a portrait trading card built entirely from
    # already-fetched data (no DB/network access here, see
    # _swapStatsForTradingCard for the async half that gathers all of
    # this). `avatar_image` is an already-opened PIL image (the
    # player's real avatar, or a plain fallback tile if it couldn't be
    # fetched). `settings` is a getCardSettings()-shaped dict. `stats`
    # is {elo, elo_rank, ranked_wins, ranked_losses, ranked_win_rate}.
    # `teams` is every persistent Team (see getTeamsForPlayer) this
    # player is rostered on in this guild, most relevant first, each
    # one's own logo (self-healing, see _ensureLogo) is pasted alongside
    # its name. `username` (optional, every existing caller predates
    # it, hence the default) is the player's actual Discord account
    # name (`member.name`), distinct from `display_name` (their
    # nickname, if they have one), drawn small in the header's
    # top-right, mirroring the logo/guild-name block's own top-left
    # placement, so the card identifies exactly who it belongs to even
    # for a player known mainly by a nickname.
    def _renderTradingCardImage(self, guild_name, display_name, avatar_image, settings, stats, teams, username=None):
        accent_color = self._hexToRgb(settings["accent_color"], BRACKET_TITLE_COLOR)
        text_color = self._hexToRgb(settings["text_color"], BRACKET_TEXT_COLOR)
        background_color = self._hexToRgb(settings["background_color"], BRACKET_BACKGROUND)
        fonts = self._cardFontPaths(settings["font_style"])

        name_font = self._fitNameFont(
            fonts["name_font"], fonts["name_variation"], display_name, CARD_WIDTH - BRACKET_MARGIN * 2
        )
        title_font = self._loadFont(fonts["title_font"], CARD_TITLE_FONT_SIZE, fonts["title_variation"])
        label_font = self._loadFont(fonts["body_font"], CARD_STAT_LABEL_FONT_SIZE, fonts["label_weight"])
        value_font = self._loadFont(fonts["body_font"], CARD_STAT_VALUE_FONT_SIZE, fonts["value_weight"])
        team_font = self._loadFont(fonts["body_font"], CARD_STAT_LABEL_FONT_SIZE, fonts["team_weight"])
        username_font = self._loadFont(fonts["body_font"], CARD_STAT_LABEL_FONT_SIZE, fonts["team_weight"])

        stat_rows = [
            ("ELO", f"{stats['elo']} ({stats['elo_rank']})"),
            ("RANKED RECORD", f"{stats['ranked_wins']}W - {stats['ranked_losses']}L"),
            ("RANKED WIN RATE", stats["ranked_win_rate"]),
        ]
        shown_teams = teams[:CARD_MAX_TEAM_ROWS]
        extra_team_count = len(teams) - len(shown_teams)

        # Two-pass layout: measure first (a throwaway Draw, same
        # approach every other renderer in this file uses) so
        # labels/values can be column-aligned without guessing at their
        # widths ahead of time. The whole point of stacking one stat
        # per line (see CARD_STAT_LINE_HEIGHT) is giving each one the
        # full card width to avoid clipping, so getting that width
        # measurement right matters here more than it did for the old
        # 3-column layout.
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        label_column_width = max(measurer.textlength(label, font=label_font) for label, _value in stat_rows)
        value_x = BRACKET_MARGIN + label_column_width + BRACKET_PADDING * 2

        header_height = self._bracketHeaderHeight(None)
        avatar_top = header_height + BRACKET_PADDING * 2
        avatar_cx = CARD_WIDTH / 2
        name_y = avatar_top + CARD_AVATAR_SIZE + BRACKET_PADDING * 2
        title_y = name_y + CARD_NAME_FONT_SIZE + BRACKET_PADDING
        rule_y = title_y + CARD_TITLE_FONT_SIZE + BRACKET_PADDING * 2
        stats_top = rule_y + BRACKET_PADDING * 2
        stats_bottom = stats_top + CARD_STAT_LINE_HEIGHT * len(stat_rows)

        teams_top = stats_bottom + BRACKET_PADDING * 2
        if shown_teams:
            team_rows = len(shown_teams) + (1 if extra_team_count > 0 else 0)
            bottom = teams_top + team_rows * (CARD_TEAM_ROW_HEIGHT + CARD_TEAM_ROW_GAP)
        else:
            bottom = stats_bottom
        height = int(bottom + BRACKET_MARGIN)

        # background_color is the one customizable color, standing in
        # for the vignette's "edge" shade. _lightenColor derives a
        # matching lighter "center" from it, the same relationship
        # BRACKET_BACKGROUND_CENTER has to BRACKET_BACKGROUND by
        # default.
        background_center = self._lightenColor(background_color, 0.3)
        image, draw = self._createBracketCanvas(
            CARD_WIDTH, height, accent_color, background=background_color, background_center=background_center
        )
        self._drawBracketHeader(image, draw, guild_name, None, accent_color, CARD_WIDTH, bold_title=True)
        if username:
            draw.text(
                (CARD_WIDTH - BRACKET_MARGIN, BRACKET_MARGIN + BRACKET_LOGO_HEIGHT / 2), f"@{username}",
                font=username_font, fill=accent_color, anchor="rm"
            )

        # Avatar: circular crop via a mask (paste() only respects alpha
        # on the SOURCE image being pasted, hence converting to RGBA
        # first), ringed in the card's accent color.
        avatar = avatar_image.convert("RGBA").resize((CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), Image.LANCZOS)
        mask = Image.new("L", (CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, CARD_AVATAR_SIZE, CARD_AVATAR_SIZE], fill=255)
        avatar_x = int(avatar_cx - CARD_AVATAR_SIZE / 2)
        image.paste(avatar, (avatar_x, int(avatar_top)), mask)
        draw.ellipse(
            [
                avatar_x - CARD_AVATAR_BORDER, avatar_top - CARD_AVATAR_BORDER,
                avatar_x + CARD_AVATAR_SIZE + CARD_AVATAR_BORDER, avatar_top + CARD_AVATAR_SIZE + CARD_AVATAR_BORDER,
            ],
            outline=accent_color, width=CARD_AVATAR_BORDER
        )

        draw.text((avatar_cx, name_y), display_name, font=name_font, fill=text_color, anchor="ma")
        draw.text(
            (avatar_cx, title_y), f"“{settings['title']}”", font=title_font, fill=accent_color, anchor="ma"
        )

        draw.line(
            [(BRACKET_MARGIN, rule_y), (CARD_WIDTH - BRACKET_MARGIN, rule_y)],
            fill=accent_color, width=BRACKET_RULE_WIDTH
        )

        for i, (label, value) in enumerate(stat_rows):
            row_y = stats_top + i * CARD_STAT_LINE_HEIGHT + CARD_STAT_LINE_HEIGHT / 2
            draw.text((BRACKET_MARGIN, row_y), label, font=label_font, fill=accent_color, anchor="lm")
            text_x = value_x
            if label == "ELO":
                self._drawEloBadge(image, value_x + CARD_ELO_BADGE_RADIUS, row_y, stats["elo"])
                text_x = value_x + CARD_ELO_BADGE_RADIUS * 2 + BRACKET_PADDING / 2
            draw.text((text_x, row_y), value, font=value_font, fill=text_color, anchor="lm")

        for i, team in enumerate(shown_teams):
            row_y = teams_top + i * (CARD_TEAM_ROW_HEIGHT + CARD_TEAM_ROW_GAP)
            logo_path = team.get_logo_path()
            text_x = BRACKET_MARGIN
            if logo_path is not None and os.path.isfile(logo_path):
                logo = Image.open(logo_path).convert("RGBA")
                logo.thumbnail((CARD_TEAM_LOGO_SIZE, CARD_TEAM_LOGO_SIZE), Image.LANCZOS)
                logo_y = int(row_y + (CARD_TEAM_ROW_HEIGHT - logo.height) / 2)
                image.paste(logo, (BRACKET_MARGIN, logo_y), logo)
                text_x = BRACKET_MARGIN + CARD_TEAM_LOGO_SIZE + BRACKET_PADDING
            draw.text(
                (text_x, row_y + CARD_TEAM_ROW_HEIGHT / 2), team.get_name(), font=team_font, fill=text_color,
                anchor="lm"
            )

        if extra_team_count > 0:
            row_y = teams_top + len(shown_teams) * (CARD_TEAM_ROW_HEIGHT + CARD_TEAM_ROW_GAP)
            draw.text(
                (BRACKET_MARGIN, row_y + CARD_TEAM_ROW_HEIGHT / 2), f"+{extra_team_count} more team"
                f"{'s' if extra_team_count != 1 else ''}", font=label_font, fill=BRACKET_LINE_COLOR, anchor="lm"
            )

        return image

    # The avatar-fetching half shared by _swapStatsForTradingCard and
    # its own avatar-toggle re-render. `use_global_avatar` picks
    # between `member`'s per-server picture (the default) and the
    # account-wide one a plain discord.User carries, mirroring
    # _resolveMemberAvatarUrl/_resolveGlobalAvatarUrl's own
    # server-vs-global split for the plain /stats embed's thumbnail
    # toggle. Falls back to None (caller draws a plain tile) rather
    # than failing the whole card over one image request. A
    # missing/unfetchable avatar shouldn't be fatal.
    async def _resolveCardAvatarImage(self, member, use_global_avatar):
        source = member
        if use_global_avatar and member is not None:
            global_user = self.client.get_user(member.id) if self.client is not None else None
            if global_user is None:
                try:
                    global_user = await self.client.fetch_user(member.id)
                except discord.HTTPException:
                    global_user = None
            if global_user is not None:
                source = global_user
        if source is None:
            return None
        try:
            avatar_bytes = await source.display_avatar.with_format("png").read()
            return Image.open(io.BytesIO(avatar_bytes))
        except Exception:
            return None

    # The async half of the trading card: gathers everything
    # _renderTradingCardImage needs (a live member for the avatar/display
    # name, fresh economy stats, persistent teams, and card_settings)
    # and posts the result in place of the /stats embed. A
    # missing/unfetchable avatar falls back to a plain tile rather than
    # failing the whole card over one image request. `use_global_avatar`
    # is the trading-card half of the same STATS_AVATAR_TOGGLE_EMOJI
    # button the plain embed uses, see _handleStatsAvatarToggleClick,
    # which re-calls this in place to redraw the card with the other
    # avatar rather than posting a new message. `view`, when given, is
    # included in the same edit call that swaps the image in, see
    # TeamStatsView helpers' own `view` param for why view=None (the
    # default) omits the kwarg rather than passing it through.
    async def _swapStatsForTradingCard(
        self, message, guild_id, guild_name, target_user_id, use_global_avatar=False, view=None
    ):
        member = await self._resolveGuildMember(guild_id, target_user_id)
        display_name = member.display_name if member is not None else f"Player {target_user_id}"

        game = self._currentGame(guild_id)
        self.ensureEconomyRow(guild_id, target_user_id, display_name)
        self.ensureGameStatsRow(guild_id, target_user_id, display_name, game)
        self.cursor.execute(
            "SELECT elo, ranked_wins, ranked_losses FROM game_stats WHERE guildId=? AND userId=? AND game=?",
            (guild_id, target_user_id, game)
        )
        elo, ranked_wins, ranked_losses = self.cursor.fetchone()
        ranked_games = ranked_wins + ranked_losses
        stats = {
            "elo": elo, "elo_rank": self.eloRankLabelPlain(elo),
            "ranked_wins": ranked_wins, "ranked_losses": ranked_losses,
            "ranked_win_rate": f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A",
        }

        teams = [team for _, team in self.getTeamsForPlayer(guild_id, target_user_id)]
        settings = self.getCardSettings(guild_id, target_user_id)

        avatar_image = await self._resolveCardAvatarImage(member, use_global_avatar)
        if avatar_image is None:
            avatar_image = Image.new("RGBA", (CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), BRACKET_BACKGROUND_CENTER)

        username = member.name if member is not None else None
        card_image = await asyncio.to_thread(
            self._renderTradingCardImage,
            guild_name, display_name, avatar_image, settings, stats, teams, username=username
        )
        file = self._imageToFile(card_image, "trading_card.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        edit_kwargs = {"embed": embed, "attachments": [file]}
        if view is not None:
            edit_kwargs["view"] = view
        await message.edit(**edit_kwargs)

    # The reverse of _swapStatsForTradingCard: rebuilds the plain
    # /stats embed (via _buildStatsEmbed, the same one statsHelper
    # itself posts) and puts it back in place of the trading card
    # image. attachments=[] is required here, not just omitted. The
    # message currently has the card's PNG attached, and message.edit()
    # otherwise leaves existing attachments alone. See
    # _swapStatsForTradingCard on `view`. `use_global_avatar` carries the
    # card's own avatar choice over onto the embed's thumbnail
    # (_buildStatsEmbed itself always starts on the server avatar, same
    # as a fresh /stats post), see _handleStatsReturnClick.
    async def _swapTradingCardForStats(self, message, guild_id, target_user_id, use_global_avatar=False, view=None):
        member = await self._resolveGuildMember(guild_id, target_user_id)
        if member is None:
            return
        embed = self._buildStatsEmbed(guild_id, member)
        if use_global_avatar:
            global_url = await self._resolveGlobalAvatarUrl(target_user_id)
            if global_url is not None:
                embed.set_thumbnail(url=global_url)
        edit_kwargs = {"embed": embed, "attachments": []}
        if view is not None:
            edit_kwargs["view"] = view
        await message.edit(**edit_kwargs)

    # StatsView's Card button callback, swaps the plain embed for the
    # trading-card image and re-renders with a Back button in place of
    # Card (see StatsView). Whichever avatar the embed happened to be
    # showing (server or global - see _handleStatsAvatarToggleClick's own
    # embed-thumbnail comparison, mirrored here) carries straight over
    # onto the card, rather than always starting the card back on the
    # server avatar.
    async def _handleStatsShowCardClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT targetUserId FROM stats_views WHERE guildId=? AND messageId=? AND cardShown=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This stats view is no longer live.", ephemeral=True)
            return
        target_user_id = row[0]

        use_global = False
        if message.embeds:
            embed = message.embeds[0]
            server_url = await self._resolveMemberAvatarUrl(guild_id, target_user_id)
            if server_url is not None and embed.thumbnail is not None:
                use_global = embed.thumbnail.url != server_url

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        await self._swapStatsForTradingCard(
            message, guild_id, guild_name, target_user_id, use_global_avatar=use_global,
            view=StatsView(self, card_shown=True)
        )
        self.cursor.execute(
            "UPDATE stats_views SET cardShown=1, cardAvatarGlobal=? WHERE guildId=? AND messageId=?",
            (1 if use_global else 0, guild_id, message.id)
        )
        self.db.commit()

    # StatsView's Back button callback, the reverse swap. cardAvatarGlobal
    # carries straight over onto the embed's thumbnail (see
    # _swapTradingCardForStats) rather than resetting to the server
    # avatar, and is deliberately left untouched here (not reset to 0 the
    # way it used to be) so a later Card press picks the same avatar back
    # up too - see _handleStatsShowCardClick.
    async def _handleStatsReturnClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT targetUserId, cardAvatarGlobal FROM stats_views WHERE guildId=? AND messageId=? AND cardShown=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This stats view is no longer live.", ephemeral=True)
            return
        target_user_id, card_avatar_global = row

        await interaction.response.defer()
        await self._swapTradingCardForStats(
            message, guild_id, target_user_id, use_global_avatar=bool(card_avatar_global),
            view=StatsView(self, card_shown=False)
        )
        self.cursor.execute(
            "UPDATE stats_views SET cardShown=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # StatsView's Avatar button callback, branches on cardShown. Off
    # the card, it flips the embed's thumbnail between the per-server
    # and regular avatar (comparing the embed's own thumbnail URL
    # against a freshly-resolved server URL). On the card, it flips
    # cardAvatarGlobal and re-renders the whole card image in place,
    # since the avatar there is baked into a PNG rather than a
    # swappable embed thumbnail URL. Available on both sides, unlike
    # Card/Back, since both the embed and the card have their own
    # avatar to toggle.
    async def _handleStatsAvatarToggleClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT targetUserId, cardShown, cardAvatarGlobal FROM stats_views "
            "WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This stats view is no longer live.", ephemeral=True)
            return
        target_user_id, card_shown, card_avatar_global = row

        await interaction.response.defer()

        if card_shown:
            guild_name = interaction.guild.name if interaction.guild is not None else ""
            new_global = not card_avatar_global
            await self._swapStatsForTradingCard(
                message, guild_id, guild_name, target_user_id, use_global_avatar=new_global
            )
            self.cursor.execute(
                "UPDATE stats_views SET cardAvatarGlobal=? WHERE guildId=? AND messageId=?",
                (1 if new_global else 0, guild_id, message.id)
            )
            self.db.commit()
            return

        if not message.embeds:
            return
        embed = message.embeds[0]
        server_url = await self._resolveMemberAvatarUrl(guild_id, target_user_id)
        if server_url is None:
            return
        currently_server = embed.thumbnail is not None and embed.thumbnail.url == server_url

        if currently_server:
            new_url = await self._resolveGlobalAvatarUrl(target_user_id)
            if new_url is None:
                return
        else:
            new_url = server_url

        embed.set_thumbnail(url=new_url)
        await message.edit(embed=embed)

    # ---------------- Leaderboard ----------------

    # One dict per player with an economy row in this guild, raw
    # columns plus the same computed rates/totals /stats shows (win
    # rates, net gold), so every LEADERBOARD_STAT_LABELS key is
    # directly readable off each entry with entry[stat]. Win rates are
    # None (not 0) when a player has no games/bets yet, so they can
    # sort to the bottom instead of looking like the worst possible
    # rate.
    # Scoped to the server's CURRENT game (see /set game): a player who's
    # never played it (only ever bet, or only played a different game)
    # still has an economy row, so a LEFT JOIN (rather than requiring a
    # game_stats row to exist) is what lets them show up at all - with
    # elo/game_wins/etc. all reading as 0/default, the same "hasn't
    # played this game" shape _filterLeaderboardEntries already treats as
    # not belonging on a game-record-based leaderboard.
    def getLeaderboardEntries(self, guild_id):
        game = self._currentGame(guild_id)
        self.cursor.execute(
            "SELECT e.userId, e.username, e.balance, e.wins, e.losses, e.gold_wagered, e.gold_won, "
            "e.gold_lost, COALESCE(g.game_wins, 0), COALESCE(g.game_losses, 0), "
            "COALESCE(g.ranked_wins, 0), COALESCE(g.ranked_losses, 0), COALESCE(g.elo, ?) "
            "FROM economy e LEFT JOIN game_stats g "
            "ON g.guildId = e.guildId AND g.userId = e.userId AND g.game = ? "
            "WHERE e.guildId=?",
            (self._defaultEloForGuild(guild_id), game, guild_id)
        )
        entries = []
        for (user_id, username, balance, bet_wins, bet_losses, gold_wagered,
             gold_won, gold_lost, game_wins, game_losses, ranked_wins, ranked_losses,
             elo) in self.cursor.fetchall():
            bet_games = bet_wins + bet_losses
            game_games = game_wins + game_losses
            ranked_games = ranked_wins + ranked_losses
            # casual = the non-ranked slice of game_wins/game_losses.
            # Every reported game is either ranked or not, so there's
            # nothing separate to store for this side (see
            # computeGameDeltas).
            casual_wins = game_wins - ranked_wins
            casual_losses = game_losses - ranked_losses
            casual_games = casual_wins + casual_losses
            entries.append({
                "user_id": user_id,
                "username": username,
                "balance": balance,
                "bet_wins": bet_wins,
                "bet_losses": bet_losses,
                "gold_wagered": gold_wagered,
                "net_gold": gold_won - gold_lost,
                "game_wins": game_wins,
                "game_losses": game_losses,
                "ranked_wins": ranked_wins,
                "ranked_losses": ranked_losses,
                "casual_wins": casual_wins,
                "casual_losses": casual_losses,
                "elo": elo,
                "bet_win_rate": (bet_wins / bet_games) if bet_games > 0 else None,
                "game_win_rate": (game_wins / game_games) if game_games > 0 else None,
                "ranked_win_rate": (ranked_wins / ranked_games) if ranked_games > 0 else None,
                "casual_win_rate": (casual_wins / casual_games) if casual_games > 0 else None,
            })
        return entries

    # Drops 0W-0L entries for whichever record LEADERBOARD_RECORD_KEYS
    # says `stat` is about (a player who's never done anything in that
    # category), leaving every entry untouched for stats with no
    # wins/losses concept.
    def _filterLeaderboardEntries(self, entries, stat):
        keys = LEADERBOARD_RECORD_KEYS.get(stat)
        if keys is None:
            return entries
        wins_key, losses_key = keys
        return [entry for entry in entries if entry[wins_key] or entry[losses_key]]

    # Sorts by entry[stat] (highest first for order="desc", lowest first
    # for order="asc") with entries missing that stat (None, e.g. a win
    # rate with no games played yet) always sinking to the bottom
    # regardless of direction, rather than flipping to the top on "asc".
    def _sortLeaderboardEntries(self, entries, stat, order):
        def sort_key(entry):
            value = entry[stat]
            if value is None:
                return (1, 0)
            return (0, -value if order == "desc" else value)

        return sorted(entries, key=sort_key)

    def _formatLeaderboardStat(self, entry, stat):
        value = entry[stat]
        if stat in ("game_win_rate", "bet_win_rate", "ranked_win_rate", "casual_win_rate"):
            return f"{value * 100:.1f}%" if value is not None else "N/A"
        if stat == "elo":
            return f"{value} ({self.eloRankLabel(value)})"
        if stat in ("balance", "gold_wagered"):
            return f"{value} gold"
        if stat == "net_gold":
            return f"{value:+d} gold"
        return str(value)

    def _leaderboardPageCount(self, entries):
        return max(1, -(-len(entries) // LEADERBOARD_PAGE_SIZE))  # ceil division

    # Builds one page of the leaderboard embed. `stat` is None for the
    # default overview (sorted by elo, showing elo alongside the ranked
    # win/loss record, not the combined game_wins/game_losses total,
    # since elo itself only ever moves from ranked games) or one of
    # LEADERBOARD_STAT_LABELS for a single stat.
    def _renderLeaderboardEmbed(self, guild_name, entries_sorted, stat, order, page, game):
        total_pages = self._leaderboardPageCount(entries_sorted)
        start = page * LEADERBOARD_PAGE_SIZE
        page_entries = entries_sorted[start:start + LEADERBOARD_PAGE_SIZE]

        title = (
            f"\U0001f3c6 {guild_name} {game} Leaderboard - Overview" if stat is None
            else f"\U0001f3c6 {guild_name} {game} Leaderboard - {LEADERBOARD_STAT_LABELS[stat]}"
        )

        lines = []
        for i, entry in enumerate(page_entries):
            rank = start + i + 1
            if stat is None:
                lines.append(
                    f"**#{rank}.** {entry['username']} - Elo: {entry['elo']} | "
                    f"Ranked: {entry['ranked_wins']}W-{entry['ranked_losses']}L"
                )
            else:
                lines.append(
                    f"**#{rank}.** {entry['username']} - {self._formatLeaderboardStat(entry, stat)}"
                )

        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "Nobody on this page.",
            color=discord.Color.gold(),
        )
        order_label = "Ascending" if order == "asc" else "Descending"
        embed.set_footer(text=f"Page {page + 1}/{total_pages} · {order_label}")
        return embed

    # cards:true's stats-side rendering: one player's full /stats embed
    # per page instead of a compact ranked row, sourced from the same
    # entries_sorted list the summary list itself pages through (so
    # it's whichever stat/order was actually asked for, not always
    # elo). Reuses _buildStatsEmbed outright (same fields, same lazy
    # tier-reward/achievement self-heal /stats itself gets) rather than
    # rebuilding its field layout from the entry dict a second time, at
    # the cost of one live member/user resolution per page.
    # _resolveGuildMemberOrUser (unlike /stats' own target, always
    # someone currently in the guild to have run the command) has to
    # cope with paging past someone who's since left, so a plain
    # discord.User (global account, no per-server data) still works,
    # and only the astronomically rare "account doesn't resolve at all
    # anymore" case falls back to a bare embed built straight from the
    # entry dict's own username.
    async def _renderLeaderboardEntryStatsEmbed(self, guild_id, entries_sorted, page):
        entry = entries_sorted[page]
        target = await self._resolveGuildMemberOrUser(guild_id, entry["user_id"])
        if target is not None:
            embed = self._buildStatsEmbed(guild_id, target)
        else:
            embed = discord.Embed(title=f"{entry['username']}'s Stats", color=discord.Color.gold())
        embed.set_footer(text=f"Player {page + 1}/{len(entries_sorted)}")
        return embed

    # Posts the first page with its own LeaderboardPagingView. Clicking
    # a button (_handleLeaderboardPageClick) edits this same message
    # rather than posting a new one, so the current view is tracked by
    # messageId here. Always starts as the ranked list. The view's own
    # Cards button (_handleLeaderboardViewCardsClick) is what switches
    # over to _renderLeaderboardEntryStatsEmbed's one-player-per-page
    # rendering, /team lookup-style, with its own Card/Back toggle over
    # to that player's actual trading card. No longer something you
    # have to pre-select before the command even runs.
    async def leaderboardHelper(self, ctx, stat, order):
        guild_id = ctx.guild.id

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        if not entries:
            await ctx.response.send_message(
                "Nobody has any stats to show yet in this server!", ephemeral=True
            )
            return

        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        view = LeaderboardPagingView(self)
        embed = self._renderLeaderboardEmbed(
            ctx.guild.name, entries_sorted, stat, order, page=0, game=self._currentGame(guild_id)
        )
        await ctx.response.send_message(embed=embed, view=view)
        msg = await ctx.original_response()

        self.cursor.execute(
            "INSERT OR REPLACE INTO leaderboards"
            "(messageId, guildId, channelId, filter, sort_order, page, cards, cardShown) "
            "VALUES(?, ?, ?, ?, ?, 0, 0, 0)",
            (msg.id, guild_id, ctx.channel.id, stat, order)
        )
        self.db.commit()

    # LeaderboardPagingView's button callback, no-ops (with a plain
    # ephemeral note) unless the interaction's message still matches an
    # active leaderboard page view. cardShown (only meaningful in cards
    # mode) carries across the flip, same reasoning
    # _handleTeamListPageClick's own cardShown branch already established.
    async def _handleLeaderboardPageClick(self, interaction, direction=None, target_page=None):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT filter, sort_order, page, cards, cardShown FROM leaderboards "
            "WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order, page, cards, card_shown = row
        cards = bool(cards)
        card_shown = bool(card_shown)

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        if cards and not entries_sorted:
            # Everyone who ever had an economy row got cleared out from
            # under this view (/clear clear_economy since it was
            # posted), same empty-list guard _handleTeamListPageClick
            # needs for the same reason
            # (_renderLeaderboardEntryStatsEmbed indexes straight into
            # entries_sorted[page]).
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(entries_sorted) if cards else self._leaderboardPageCount(entries_sorted)
        page = min(page, total_pages - 1)
        new_page = self._computeNewPage(direction, page, total_pages, target_page)

        if new_page == page:
            await interaction.response.defer()
            return

        guild_name = interaction.guild.name if interaction.guild is not None else ""
        if cards:
            if card_shown:
                entry = entries_sorted[new_page]
                target = await self._resolveGuildMemberOrUser(guild_id, entry["user_id"])
                embed, file = await self._renderLeaderboardCardEmbed(guild_id, guild_name, entries_sorted, new_page, target)
                await interaction.response.edit_message(embed=embed, attachments=[file])
            else:
                embed = await self._renderLeaderboardEntryStatsEmbed(guild_id, entries_sorted, new_page)
                await interaction.response.edit_message(embed=embed, attachments=[])
        else:
            embed = self._renderLeaderboardEmbed(
                guild_name, entries_sorted, stat, order, new_page, game=self._currentGame(guild_id)
            )
            await interaction.response.edit_message(embed=embed)

        self.cursor.execute(
            "UPDATE leaderboards SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, interaction.message.id)
        )
        self.db.commit()

    # LeaderboardPagingView's Page # button: opens _PageJumpModal rather
    # than moving one step at a time. Same "no longer live"/empty-cards
    # guards _handleLeaderboardPageClick itself needs, since the modal's
    # own on_submit calls straight back into that handler and can't do
    # anything useful with target_page if this message isn't a live
    # leaderboard view at all.
    async def _handleLeaderboardJumpClick(self, interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT filter, sort_order, cards FROM leaderboards WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order, cards = row
        cards = bool(cards)

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        if cards and not entries_sorted:
            await interaction.response.defer()
            return
        total_pages = self._myTeamsPageCount(entries_sorted) if cards else self._leaderboardPageCount(entries_sorted)
        await interaction.response.send_modal(
            _PageJumpModal(self, "_handleLeaderboardPageClick", total_pages)
        )

    # /leaderboard's own Ascending/Descending buttons (see
    # LeaderboardPagingView). Re-sorts the same filter/mode in the
    # other direction without re-running the command, resetting to
    # page 0 since ascending and descending page N generally show
    # entirely different players. Whichever of the three renderings
    # (list, stats card, or trading card) is currently active stays
    # active, only the order changes.
    async def _handleLeaderboardOrderClick(self, interaction, order):
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        self.cursor.execute(
            "SELECT filter, sort_order, cards, cardShown FROM leaderboards WHERE guildId=? AND messageId=?",
            (guild_id, interaction.message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, current_order, cards, card_shown = row
        cards = bool(cards)
        card_shown = bool(card_shown)

        if order == current_order:
            await interaction.response.defer()
            return

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        guild_name = interaction.guild.name if interaction.guild is not None else ""

        if cards and not entries_sorted:
            await interaction.response.defer()
            return

        if cards:
            if card_shown:
                entry = entries_sorted[0]
                target = await self._resolveGuildMemberOrUser(guild_id, entry["user_id"])
                embed, file = await self._renderLeaderboardCardEmbed(guild_id, guild_name, entries_sorted, 0, target)
                await interaction.response.edit_message(embed=embed, attachments=[file])
            else:
                embed = await self._renderLeaderboardEntryStatsEmbed(guild_id, entries_sorted, 0)
                await interaction.response.edit_message(embed=embed, attachments=[])
        else:
            embed = self._renderLeaderboardEmbed(
                guild_name, entries_sorted, stat, order, 0, game=self._currentGame(guild_id)
            )
            await interaction.response.edit_message(embed=embed)

        self.cursor.execute(
            "UPDATE leaderboards SET sort_order=?, page=0 WHERE guildId=? AND messageId=?",
            (order, guild_id, interaction.message.id)
        )
        self.db.commit()

    # The trading-card counterpart to _renderLeaderboardEntryStatsEmbed:
    # same (entries_sorted, page) -> (embed, file) shape, but the
    # player's actual trading card (_renderTradingCardImage, via the
    # same avatar/settings/teams lookups _swapStatsForTradingCard
    # already does) with a "Player X/N" footer so paging still has
    # something to orient by while looking at cards instead of stats.
    # `target`, already resolved by the caller (both callers need it
    # for other reasons, see _resolveGuildMemberOrUser's own
    # None-tolerance), is passed straight through rather than
    # re-resolved here.
    async def _renderLeaderboardCardEmbed(self, guild_id, guild_name, entries_sorted, page, target):
        entry = entries_sorted[page]
        user_id = entry["user_id"]
        display_name = target.display_name if target is not None else entry["username"]

        # entry already carries the current game's elo/ranked record
        # (see getLeaderboardEntries), no separate query needed - and,
        # unlike re-querying game_stats directly, it's already
        # None-tolerant for a player with no row for this game yet.
        elo, ranked_wins, ranked_losses = entry["elo"], entry["ranked_wins"], entry["ranked_losses"]
        ranked_games = ranked_wins + ranked_losses
        stats = {
            "elo": elo, "elo_rank": self.eloRankLabelPlain(elo),
            "ranked_wins": ranked_wins, "ranked_losses": ranked_losses,
            "ranked_win_rate": f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A",
        }

        teams = [team for _, team in self.getTeamsForPlayer(guild_id, user_id)]
        settings = self.getCardSettings(guild_id, user_id)

        avatar_image = await self._resolveCardAvatarImage(target, False)
        if avatar_image is None:
            avatar_image = Image.new("RGBA", (CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), BRACKET_BACKGROUND_CENTER)

        username = target.name if target is not None else None
        card_image = await asyncio.to_thread(
            self._renderTradingCardImage,
            guild_name, display_name, avatar_image, settings, stats, teams, username=username
        )
        file = self._imageToFile(card_image, "trading_card.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        embed.set_footer(text=f"Player {page + 1}/{len(entries_sorted)}")
        return embed, file

    # LeaderboardPagingView's Card button callback (cards mode only),
    # swaps the currently-paged player's stats card for their actual
    # trading card. Re-derives which player is "current" from the view's
    # own stored filter/order/page rather than trusting a fixed user_id,
    # same reasoning _handleTeamListShowCardClick already established for
    # /team list cards:true.
    async def _handleLeaderboardShowCardClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT filter, sort_order, page FROM leaderboards "
            "WHERE guildId=? AND messageId=? AND cards=1 AND cardShown=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order, page = row

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        if not entries_sorted:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        page = min(page, len(entries_sorted) - 1)

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        entry = entries_sorted[page]
        target = await self._resolveGuildMemberOrUser(guild_id, entry["user_id"])
        embed, file = await self._renderLeaderboardCardEmbed(guild_id, guild_name, entries_sorted, page, target)
        await message.edit(
            embed=embed, attachments=[file], view=LeaderboardPagingView(self, cards=True, card_shown=True)
        )
        self.cursor.execute(
            "UPDATE leaderboards SET cardShown=1 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # LeaderboardPagingView's Back button callback, the reverse swap.
    async def _handleLeaderboardReturnClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT filter, sort_order, page FROM leaderboards "
            "WHERE guildId=? AND messageId=? AND cards=1 AND cardShown=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order, page = row

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        if not entries_sorted:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        page = min(page, len(entries_sorted) - 1)

        await interaction.response.defer()
        embed = await self._renderLeaderboardEntryStatsEmbed(guild_id, entries_sorted, page)
        await message.edit(
            embed=embed, attachments=[], view=LeaderboardPagingView(self, cards=True, card_shown=False)
        )
        self.cursor.execute(
            "UPDATE leaderboards SET cardShown=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # LeaderboardPagingView's Cards button callback (list mode only):
    # switches from the ranked list to cards mode, one player's stats
    # card per page, starting fresh at page 0 rather than trying to map
    # the list's own page/index onto cards mode's different page size.
    async def _handleLeaderboardViewCardsClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT filter, sort_order FROM leaderboards WHERE guildId=? AND messageId=? AND cards=0",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order = row

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        if not entries_sorted:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return

        await interaction.response.defer()
        embed = await self._renderLeaderboardEntryStatsEmbed(guild_id, entries_sorted, 0)
        await message.edit(embed=embed, attachments=[], view=LeaderboardPagingView(self, cards=True, card_shown=False))
        self.cursor.execute(
            "UPDATE leaderboards SET cards=1, cardShown=0, page=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # LeaderboardPagingView's List button callback, the reverse of
    # viewCards above: back to the ranked list, page 0, from either cards
    # sub-state (stats card or trading card).
    async def _handleLeaderboardBackToListClick(self, interaction):
        guild_id = interaction.guild_id
        message = interaction.message

        self.cursor.execute(
            "SELECT filter, sort_order FROM leaderboards WHERE guildId=? AND messageId=? AND cards=1",
            (guild_id, message.id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await interaction.response.send_message("This leaderboard is no longer live.", ephemeral=True)
            return
        stat, order = row

        entries = self._filterLeaderboardEntries(self.getLeaderboardEntries(guild_id), stat)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)

        await interaction.response.defer()
        guild_name = interaction.guild.name if interaction.guild is not None else ""
        embed = self._renderLeaderboardEmbed(
            guild_name, entries_sorted, stat, order, 0, game=self._currentGame(guild_id)
        )
        await message.edit(embed=embed, attachments=[], view=LeaderboardPagingView(self))
        self.cursor.execute(
            "UPDATE leaderboards SET cards=0, cardShown=0, page=0 WHERE guildId=? AND messageId=?",
            (guild_id, message.id)
        )
        self.db.commit()

    # Cancels the running betting timer (if any) and, if the game had an
    # unresolved bet round (open or closed-but-unreported), refunds
    # every active bet. Also clears active_tournament_match_id: whatever
    # match this round belonged to (if any) is being abandoned along
    # with it, so a later, unrelated recordResult shouldn't inherit its
    # bracket-advance hook. Used both by cancelGameHelper (an explicit
    # cancel) and by _openBetting itself, to silently clear out a stale
    # previous round before a fresh one opens. This alone never moves
    # anyone back to the original channel, since a stale-round clear
    # isn't the same as the player-facing "the game was cancelled"
    # cancelGameHelper handles.
    async def cancelBettingHelper(self, guild_id, channel):
        state = self.get(guild_id, "betting_state")

        self._cancelBettingTimerTask(guild_id)

        if state not in ("OPEN", "CLOSED"):
            return

        self.cursor.execute(
            "SELECT userId, amount FROM wagers WHERE guildId=?", (guild_id,)
        )
        refunds = self.cursor.fetchall()

        for user_id, amount in refunds:
            self.cursor.execute(
                "UPDATE economy SET balance = balance + ? WHERE guildId=? AND userId=?",
                (amount, guild_id, user_id)
            )

        self.cursor.execute("DELETE FROM wagers WHERE guildId=?", (guild_id,))
        self.update(guild_id, "betting_state", "NONE")
        self.update(guild_id, "betting_message_id", None)
        self.update(guild_id, "active_tournament_match_id", None)
        self.db.commit()

        if refunds:
            await channel.send("Bets have been refunded since the game ended before a winner was recorded.")