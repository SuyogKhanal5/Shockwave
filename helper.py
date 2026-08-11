import discord
from TourneyClasses import (
    Team, Tournament, Match, Player, BracketNode, serialize_bracket, deserialize_bracket,
    serialize_losers_rounds, deserialize_losers_rounds,
)
import random
import asyncio
import datetime
import io
import itertools
import json
import math
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Every bracket/matchup image is actually drawn at BRACKET_SUPERSAMPLE times
# its final size, then downscaled with LANCZOS resampling in _imageToFile —
# PIL's ImageDraw has no antialiasing of its own (lines and glyph edges come
# out visibly jagged at 1x), and rendering bigger then shrinking down is the
# standard way around that. Every pixel-valued constant below is already
# expressed at supersampled scale (hence "* BRACKET_SUPERSAMPLE" throughout)
# so the actual drawing code just uses them as-is and never has to think
# about the scale factor itself.
BRACKET_SUPERSAMPLE = 2

# Bracket-image layout constants (see _renderTreeImage and friends) — plain
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
# badge (see _drawChampionLabel) — the star itself plus a little breathing
# room on each side of it.
BRACKET_CHAMPION_BADGE_GAP = BRACKET_CHAMPION_STAR_RADIUS * 2 + BRACKET_PADDING * 2
BRACKET_LOGO_HEIGHT = 26 * BRACKET_SUPERSAMPLE
BRACKET_SUBTITLE_GAP = 6 * BRACKET_SUPERSAMPLE           # between the title row and the subtitle
BRACKET_HEADER_RULE_GAP = 10 * BRACKET_SUPERSAMPLE       # between the header text block and its accent rule
BRACKET_HEADER_RULE_MARGIN = 12 * BRACKET_SUPERSAMPLE    # between the accent rule and whatever's drawn below it
BRACKET_BORDER_RADIUS = 14 * BRACKET_SUPERSAMPLE
# Connector lines, the accent rule, and the outer frame all used bare
# hardcoded width= literals before — named and scaled now so they shrink
# back down to the same relative thickness post-downscale instead of
# staying full-width on a now-3x-bigger canvas.
BRACKET_LINE_WIDTH = 2 * BRACKET_SUPERSAMPLE
BRACKET_RULE_WIDTH = 1 * BRACKET_SUPERSAMPLE
BRACKET_LOGO_PATH = os.path.join(os.path.dirname(__file__), "shockwave-site", "assets", "img", "logo-mark.png")
# Built-in Clash faction/region logos a team can pick from (see
# /team-set-logo and _ensureLogo) — one file per available logo, named after
# it (e.g. "Demacia.png"), no subfolders.
TEAM_LOGO_DIR = os.path.join(os.path.dirname(__file__), "assets", "clash-logos")

# Real TTF fonts instead of PIL's built-in default font, which is a small
# bitmap face that looks noticeably rough/pixelated once scaled up to
# heading sizes. Same two families shockwave-site's own CSS uses
# (--font-display / --font-body — see styles.css) so the images read as the
# same brand as the site, not just the same color palette. Chakra Petch for
# anything headline-ish (titles, team names, "VS"); IBM Plex Sans for body
# text (roster names, round headers) — both Google/SIL-OFL-licensed and
# bundled under assets/fonts rather than linked, so rendering doesn't depend
# on network access or the host having them installed.
FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
CHAKRA_PETCH_REGULAR = os.path.join(FONTS_DIR, "ChakraPetch-Regular.ttf")
CHAKRA_PETCH_SEMIBOLD = os.path.join(FONTS_DIR, "ChakraPetch-SemiBold.ttf")
CHAKRA_PETCH_BOLD = os.path.join(FONTS_DIR, "ChakraPetch-Bold.ttf")
IBM_PLEX_SANS = os.path.join(FONTS_DIR, "IBMPlexSans.ttf")  # variable weight — see _loadFont

# Colors lifted straight from shockwave-site/assets/styles.css's :root
# palette, so the bracket image reads as part of the same brand instead of
# a plain black-on-white chart dropped into a dark-themed Discord client.
BRACKET_BACKGROUND = (21, 11, 34)      # --ink
BRACKET_BACKGROUND_CENTER = (30, 19, 48)   # --surface — lighter center of the canvas's radial vignette
BRACKET_TEXT_COLOR = (243, 239, 250)   # --text
BRACKET_TITLE_COLOR = (237, 198, 67)   # --gold
BRACKET_LINE_COLOR = (118, 106, 148)   # --muted-dim
# The losers bracket's own accent, standing in for gold everywhere a
# winners-bracket image would use it (title, champion label, frame) — makes
# the two images readable as "which one is this" at a glance, without
# relying on remembering which caption belongs to which attachment.
BRACKET_LOSERS_ACCENT_COLOR = (231, 76, 60)   # --team-red

# /matchup image (see _renderMatchupImage) — posted alongside the existing
# text announcement whenever a tournament match is created (_postMatchReport,
# _postReadyCheck). Team 1/2's accent colors come straight from
# shockwave-site's own --team-blue/--team-red palette (see the
# .discord-embed.blue/.discord-embed.red rules in styles.css) — a different
# pairing than the bracket image's gold/red winners/losers split, since this
# is about telling team 1 from team 2, not winners bracket from losers
# bracket (TEAM2_ACCENT_COLOR's value happens to match
# BRACKET_LOSERS_ACCENT_COLOR only because both trace back to the same site
# palette, not because they mean the same thing).
TEAM1_ACCENT_COLOR = (52, 152, 219)   # --team-blue
TEAM2_ACCENT_COLOR = (231, 76, 60)    # --team-red
MATCHUP_LOGO_SIZE = 96 * BRACKET_SUPERSAMPLE
MATCHUP_COLUMN_GAP = 56 * BRACKET_SUPERSAMPLE   # width reserved for the "VS" divider between columns
MATCHUP_VS_FONT_SIZE = 30 * BRACKET_SUPERSAMPLE

# A bracket this many rounds deep (16+ teams) splits into two halves that
# grow toward the center instead of one long strip growing left-to-right —
# the same idea as a printed tournament bracket poster, and a lot more
# compact (half the height, since each side only stacks half the leaves).
# Only the winners bracket ever uses this: its champion's two children are
# always exactly even halves (buildBracket produces a perfectly balanced
# tree), which is what makes a symmetric two-sided split look right. The
# losers bracket has no such guarantee — its final round pairs a whole
# survivor subtree against a single fresh drop-in leaf (see
# buildLosersBracket), so splitting it the same way would just be lopsided.
BRACKET_TWO_SIDED_MIN_ROUNDS = 4

# Lazily loaded and resized once, then reused for every bracket image for
# the rest of the process — module-level (not per-`helpers` instance) since
# it's a static asset every instance would otherwise reload identically.
# False (not None) means loading was already tried and failed, so it isn't
# retried on every single render.
_bracket_logo_cache = None

# TrueType fonts loaded once per (path, size, variation) and reused for
# every image rendered for the rest of the process, same idea as
# _bracket_logo_cache — module-level since it's static, and there's no
# reassignment involved so this dict can just be mutated directly (no
# `global` needed the way _bracket_logo_cache's None/False swap requires).
_font_cache = {}

BETTING_DURATION_SECONDS = 60
# _openConcurrentTournamentBetting multiplies a guild's configured
# per-match timer by however many matches are in the round — this caps
# the result so a generous base times a big bracket's first round can't
# leave betting open for an unreasonable stretch.
MAX_CONCURRENT_BETTING_SECONDS = 1800
WINNER_REPORT_DELAY_SECONDS = 3
DAILY_GOLD_AMOUNT = 1000
# /test's simulated wagers (see _postSimulatedWagers) — clearly-fake names
# so nobody mistakes these for real bettors, and a handful of round gold
# amounts rather than anything trying to look like a "real" distribution.
FAKE_BETTOR_NAMES = [
    "Test Bettor 1", "Test Bettor 2", "Test Bettor 3", "Test Bettor 4", "Test Bettor 5", "Test Bettor 6",
]
FAKE_WAGER_AMOUNTS = [25, 50, 75, 100, 150, 200, 250]
TEAM_EMOJIS = {1: "🔵", 2: "🔴"}   # blue for team 1, red for team 2 — matches TEAM1_ACCENT_COLOR/TEAM2_ACCENT_COLOR
WINNER_EMOJIS = {emoji: team for team, emoji in TEAM_EMOJIS.items()}
DEFAULT_ELO = 1000
ELO_K_FACTOR = 32
# +/- range randomly added to each player's elo before balancing ranked
# teams — keeps matchups from being the exact same optimal split every
# time, at the cost of the balance being only "roughly" fair.
ELO_BALANCE_JITTER = 100
# How long the /clear confirmation buttons stay clickable before the
# reset is abandoned on its own.
CLEAR_CONFIRM_TIMEOUT_SECONDS = 30
# Same idea for /tournament-create's overwrite confirmation.
TOURNAMENT_CONFIRM_TIMEOUT_SECONDS = 30
# ...and for /team-set-voice-channel's already-in-use confirmation.
TEAM_CONFIRM_TIMEOUT_SECONDS = 30
# /team-invite: react to accept, same idea as duel/team-game acceptance.
TEAM_INVITE_ACCEPT_EMOJI = "✅"

# /tournament-start (sequential mode): react to mark a queued match ready
# to begin. Simultaneous-mode match results reuse TEAM_EMOJIS/WINNER_EMOJIS
# above — same reporting reactions as a normal game, just scoped to a specific
# tournament_matches row instead of the guild's single betting_message_id.
TOURNAMENT_READY_EMOJI = "✅"

# /stats: react to toggle the shown avatar between the player's real one
# and a generic placeholder (see handleStatsReaction) — same reaction
# either direction, flipping based on whichever's currently showing.
# Discord's own "embed/avatars/0.png" is one of its built-in default-avatar
# images (0-5, no real user tied to it), so this needs no locally-hosted
# asset for the placeholder half of the toggle.
STATS_PLACEHOLDER_EMOJI = "\U0001f5bc️"  # 🖼️
STATS_PLACEHOLDER_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"
# /stats: react to blow the whole embed away and replace it with the
# player's trading card (see _renderTradingCardImage) — a one-way swap, not
# part of the avatar toggle above, and it disables that toggle afterward
# (see handleStatsReaction) since a card isn't shaped like a normal /stats
# embed anymore.
STATS_CARD_EMOJI = "\U0001f3b4"  # 🎴

# Trading-card layout (see _renderTradingCardImage) — a portrait card
# roughly the shape of a real trading card, reusing the same canvas/header
# building blocks (_createBracketCanvas, _drawBracketHeader) every other
# rendered image in this file already uses, so it reads as the same
# product rather than a bolted-on fourth visual style.
CARD_WIDTH = 360 * BRACKET_SUPERSAMPLE
CARD_AVATAR_SIZE = 176 * BRACKET_SUPERSAMPLE
CARD_AVATAR_BORDER = 3 * BRACKET_SUPERSAMPLE
CARD_NAME_FONT_SIZE = 20 * BRACKET_SUPERSAMPLE
CARD_TITLE_FONT_SIZE = 14 * BRACKET_SUPERSAMPLE
CARD_STAT_LABEL_FONT_SIZE = 11 * BRACKET_SUPERSAMPLE
CARD_STAT_VALUE_FONT_SIZE = 15 * BRACKET_SUPERSAMPLE
CARD_STAT_ROW_HEIGHT = 40 * BRACKET_SUPERSAMPLE

# trading_cards' defaults — Shockwave's own site palette (see BRACKET_*
# above) and font pairing, so a player who's never customized their card
# gets exactly the same look every other rendered image already has,
# rather than something generic. Colors are stored as "#RRGGBB" hex in the
# table (portable, human-editable) and converted back to RGB tuples at
# render time (see _hexToRgb).
CARD_DEFAULT_TITLE = "Rookie"
CARD_DEFAULT_ACCENT_COLOR = "#EDC643"      # --gold, same as BRACKET_TITLE_COLOR
CARD_DEFAULT_BACKGROUND_COLOR = "#150B22"  # --ink, same as BRACKET_BACKGROUND
CARD_DEFAULT_TEXT_COLOR = "#F3EFFA"        # --text, same as BRACKET_TEXT_COLOR
CARD_DEFAULT_FONT_STYLE = "default"        # Chakra Petch + IBM Plex Sans — see _cardFontPaths

# League-style rank tiers for /stats. Each tier spans 250 elo, with
# DEFAULT_ELO (1000) landing every new player in the middle at Platinum —
# ascending order, (elo threshold, tier name, emoji).
ELO_TIERS = [
    (0, "Iron", "⚙️"),
    (250, "Bronze", "\U0001f949"),
    (500, "Silver", "\U0001f948"),
    (750, "Gold", "\U0001f947"),
    (1000, "Platinum", "\U0001f537"),
    (1250, "Diamond", "\U0001f48e"),
    (1500, "Master", "\U0001f7e3"),
    (1750, "Grandmaster", "\U0001f534"),
    (2000, "Challenger", "\U0001f451"),
]

# Divisions within a tier, lowest to highest — the same I/II/III/IV split
# League uses, with "I" nearest promotion into the next tier up.
ELO_DIVISIONS = ["IV", "III", "II", "I"]

# Only the first this-many tiers (Iron through Diamond) show a division —
# Master and above show just the tier, same as League showing raw LP
# instead of I-IV once you hit Master.
ELO_DIVISIONED_TIER_COUNT = 6

# /wager-against: a heads-up gold wager between two specific players,
# independent of the team-game betting above. The challenged player
# accepts with a checkmark; once accepted, anyone can react blue/red to
# report who actually won, same as the team-game winner report.
DUEL_ACCEPT_EMOJI = "✅"       # ✅
DUEL_CHALLENGER_EMOJI = "\U0001f535"  # 🔵 — challenger ("player 1") won
DUEL_TARGET_EMOJI = "\U0001f534"      # 🔴 — target ("player 2") won

# /leaderboard: paged via reactions rather than re-running the command —
# clicking one of these edits the existing message instead of posting a
# new one (see handleLeaderboardReaction).
LEADERBOARD_PAGE_SIZE = 10
LEADERBOARD_FIRST_EMOJI = "⏮️"  # ⏮️ jump to the first page
LEADERBOARD_PREV_EMOJI = "◀️"   # ◀️ previous page
LEADERBOARD_NEXT_EMOJI = "▶️"   # ▶️ next page
LEADERBOARD_LAST_EMOJI = "⏭️"   # ⏭️ jump to the last page
LEADERBOARD_NAV_EMOJIS = (
    LEADERBOARD_FIRST_EMOJI, LEADERBOARD_PREV_EMOJI, LEADERBOARD_NEXT_EMOJI, LEADERBOARD_LAST_EMOJI
)

# /team-list: what it can sort by, and its display label — same paging
# (LEADERBOARD_NAV_EMOJIS) and page size as /leaderboard, just over teams
# instead of players.
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

# BUG FIX: this dict used to only live in main.py. helper.py's
# randomRoleHelper did `global roles` and expected to find it — but each
# Python module has its own separate global namespace, so that `global`
# statement pointed at helper.py's (empty) globals and raised a NameError
# the first time the function ran. Defining it here fixes that.
roles = {
    0: "Top - ",
    1: "Jungle - ",
    2: "Mid - ",
    3: "Bottom - ",
    4: "Support - "
}


# Confirm/cancel buttons for /clear's clear_elo and clear_economy flags.
# Both reset state for every player in the server, so neither runs until
# whoever ran the command clicks "Confirm reset" on this view.
class ConfirmResetView(discord.ui.View):
    def __init__(self, helperObj, guild_id, guild_name, invoker_id, clear_economy):
        super().__init__(timeout=CLEAR_CONFIRM_TIMEOUT_SECONDS)
        self.helperObj = helperObj
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.invoker_id = invoker_id
        self.clear_economy = clear_economy
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
        if self.clear_economy:
            self.helperObj.resetEconomyHelper(self.guild_id)
            result = (
                "Economy data (balance, elo, game record, betting record, gold "
                f"wagered/won/lost) has been reset for every player in **{self.guild_name}**."
            )
        else:
            self.helperObj.resetEloHelper(self.guild_id)
            result = f"Elo has been reset to {DEFAULT_ELO} for every player in **{self.guild_name}**."
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(content=result, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._disable_buttons()
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing was reset.", view=self)

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            await self.message.edit(view=self)


# Confirm/cancel buttons for /tournament-create when a tournament already
# exists for the server — creating one is destructive (it replaces the
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
                "Only the person who ran /tournament-create can confirm this.", ephemeral=True
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
            content="Cancelled — the existing tournament was kept.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            await self.message.edit(view=self)


# Confirm/cancel buttons for /team-set-voice-channel when the requested
# channel is already another team's. "Yes" assigns it to this team anyway
# (the other team's own assignment is left alone — this doesn't enforce
# exclusivity, just warns); "No" leaves everything as it was and tells the
# invoker to run the command again with a different channel.
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
                "Only the person who ran /team-set-voice-channel can confirm this.", ephemeral=True
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
            content="Cancelled — run `/team-set-voice-channel` again with a different channel.", view=self
        )

    async def on_timeout(self):
        self._disable_buttons()
        if self.message is not None:
            await self.message.edit(view=self)


class helpers():
    def __init__(self, cursor, db) -> None:
        self.cursor = cursor
        self.db = db
        # Set by bot.py once the discord.Client exists — needed so the
        # background betting timer and the raw-reaction handler (neither of
        # which run inside an Interaction) can still fetch channels/send
        # messages.
        self.client = None
        # guildId -> asyncio.Task for the currently running betting timer,
        # so a /return (or a fresh /start) mid-game can cancel it instead of
        # letting a stale "betting closed" / winner-report message fire later.
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

    # move players into their corresponding team channels
    #
    # BUG FIX: this makes one Discord API call per member via move_to(),
    # which for a big enough group can take longer than the 3-second
    # window Discord allows before the interaction that triggered it must
    # be acknowledged. Callers are responsible for calling
    # `await ctx.response.defer()` *before* invoking this, so the
    # interaction is acknowledged immediately regardless of how long the
    # moves take. This function itself no longer calls
    # ctx.response.send_message (that can only be called once, and would
    # conflict with a caller that already deferred) — it uses
    # ctx.channel.send for its own messages instead.
    async def movefunc(self, ctx):
        channel1name = self.get(ctx.guild.id, "channel1")
        channel2name = self.get(ctx.guild.id, "channel2")
        team1 = self.get(ctx.guild.id, "team1")
        team2 = self.get(ctx.guild.id, "team2")
        new_og = str(ctx.user.voice.channel)

        self.update(ctx.guild.id, "original_channel", new_og)

        team1Obj = Team()
        team1Obj.set_id(1)
        team1Obj.deserializeTeam(team1)
        team2Obj = Team()
        team2Obj.set_id(2)
        team2Obj.deserializeTeam(team2)

        channel1 = discord.utils.get(ctx.guild.channels, name=channel1name)
        channel2 = discord.utils.get(ctx.guild.channels, name=channel2name)

        if channel1 is not None and channel2 is not None:
            for player in team1Obj.players:
                member = discord.utils.get(ctx.guild.members, id=player.id)
                if member is not None:
                    await member.move_to(channel1)

            for player in team2Obj.players:
                member = discord.utils.get(ctx.guild.members, id=player.id)
                if member is not None:
                    await member.move_to(channel2)
        else:
            await ctx.channel.send('Team Channels Not Set! Use "/team-set-channels" to set teams.')

    async def randomizeTeamHelper(self, ctx):
        await self.clearTeamsHelper(ctx)

        members = []
        team1 = Team()
        team2 = Team()
        team1.name = "Team 1"
        team2.name = "Team 2"

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

    # Splits members into two roughly elo-balanced teams. Each player's elo
    # gets a random +/-ELO_BALANCE_JITTER nudge before sorting, so the split
    # isn't the exact same optimal matchup every time — then a snake draft
    # (strongest pick alternates sides each round: 1,2,2,1,1,2,2,1,...)
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

    def averageElo(self, members, elo_by_id):
        if not members:
            return DEFAULT_ELO
        return round(sum(elo_by_id[m.id] for m in members) / len(members))

    # The (emoji, plain-text label) behind eloRankLabel/eloRankLabelPlain —
    # e.g. ("\U0001f537", "Platinum III") or ("\U0001f7e3", "Master") once
    # divisions stop applying. ELO_TIERS is sorted ascending, so the last
    # threshold at or below elo wins — e.g. exactly 1000 is Platinum, not
    # Gold; anything above the top tier's threshold is still Challenger.
    def _eloRankParts(self, elo):
        tier_index = 0
        for i, (threshold, _name, _emoji) in enumerate(ELO_TIERS):
            if elo >= threshold:
                tier_index = i
            else:
                break

        threshold, name, emoji = ELO_TIERS[tier_index]

        if tier_index >= ELO_DIVISIONED_TIER_COUNT:
            return emoji, name

        span = ELO_TIERS[tier_index + 1][0] - threshold
        offset = max(elo - threshold, 0)
        segment_size = span / len(ELO_DIVISIONS)
        division_index = min(int(offset // segment_size), len(ELO_DIVISIONS) - 1)

        return emoji, f"{name} {ELO_DIVISIONS[division_index]}"

    # Maps a raw elo number to a League-style "emoji tier division" label,
    # e.g. "\U0001f537 Platinum III" — what /stats and /leaderboard show,
    # since Discord's own client renders the emoji fine in embed text.
    def eloRankLabel(self, elo):
        emoji, label = self._eloRankParts(elo)
        return f"{emoji} {label}"

    # Same tier/division text, without the leading emoji — for the trading
    # card (_renderTradingCardImage), which draws its stats with PIL and
    # the bundled TTF fonts don't have these glyphs (same class of issue
    # the roster's captain star and the matchup header's bullet separator
    # ran into) — Discord's client-side emoji rendering isn't available
    # there the way it is for a normal embed field.
    def eloRankLabelPlain(self, elo):
        return self._eloRankParts(elo)[1]

    # Forms elo-balanced teams from the caller's voice channel and marks
    # the game as ranked, so elo actually gets updated when the winner is
    # eventually reported (see computeGameDeltas/recordResult). Everything
    # else — moving players, opening betting — is still /start's job, same
    # as /make-teams.
    async def rankedTeamHelper(self, ctx):
        await self.clearTeamsHelper(ctx)

        guild_id = ctx.guild.id
        channel = ctx.user.voice.channel

        members_with_elo = []
        for member in channel.members:
            self.ensureEconomyRow(guild_id, member.id, member.name)
            elo = self.getEconomy(guild_id, member.id, "elo")
            members_with_elo.append((member, elo if elo is not None else DEFAULT_ELO))

        team1_members, team2_members = self.formBalancedTeams(members_with_elo)
        elo_by_id = {member.id: elo for member, elo in members_with_elo}

        team1 = Team()
        team1.name = "Team 1"
        for member in team1_members:
            team1.add_player(Player(member.id, member.name))
        team2 = Team()
        team2.name = "Team 2"
        for member in team2_members:
            team2.add_player(Player(member.id, member.name))

        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "mode", "Ranked")
        self.update(guild_id, "is_ranked", 1)

        team1_avg = self.averageElo(team1_members, elo_by_id)
        team2_avg = self.averageElo(team2_members, elo_by_id)

        await ctx.response.send_message(
            f"Ranked teams created! Team 1 avg elo **{team1_avg}**, Team 2 avg elo **{team2_avg}**. "
            'Use "/start" when you\'re ready to move everyone and open betting.'
        )
        await self.printEmbed(ctx, team1, team2)

    def makeEmbedString(self, team: Team, useRoles=False):
        teamString = ""

        if useRoles and len(team.players) == 5:
            for i in range(5):
                teamString += roles.get(i) + team.players[i].name + "\n"
        else:
            for player in team.players:
                teamString += player.name + "\n"

        return teamString

    # prints teams in discord channel
    # DO NOT PASS NULL TEAMS
    async def printEmbed(self, ctx, team1: Team, team2: Team, playersTeam=None, useRoles=False):
        # BUG FIX: this always called makeEmbedString() with its default
        # useRoles=False, so /make-teams use_roles:True computed and stored
        # role-shuffled results (see randomRoleHelper) that the embed it
        # actually posts never displayed. Forward the flag through.
        team1_embedString = self.makeEmbedString(team1, useRoles)
        team2_embedString = self.makeEmbedString(team2, useRoles)

        team1_embed = discord.Embed(
            title=team1.get_name(), description=team1_embedString, color=discord.Color.blue()
        )
        team2_embed = discord.Embed(
            title=team2.get_name(), description=team2_embedString, color=discord.Color.red()
        )

        # BUG FIX: ctx.response.send_message can only be called once per
        # interaction. printEmbed is now sometimes called from a place
        # (chooseHelper) where the interaction was already responded to
        # earlier in the flow, so calling send_message again would raise.
        # Use channel.send for both embeds here and let the caller decide
        # if/when to do the initial interaction response.
        await ctx.channel.send(embed=team1_embed)
        await ctx.channel.send(embed=team2_embed)

        if playersTeam is not None and len(playersTeam.get_players()) > 0:
            playerString = self.makeEmbedString(playersTeam)
            player_embed = discord.Embed(
                title="PLAYERS", description=playerString, color=discord.Color.purple()
            )
            await ctx.channel.send(embed=player_embed)

    async def setTeamHelper(self, ctx, team1="Team 1", team2="Team 2"):
        guild = ctx.guild

        channel1 = discord.utils.get(ctx.guild.channels, name=team1)

        if channel1 is None:
            await guild.create_voice_channel(name=team1)
            channel1 = discord.utils.get(ctx.guild.channels, name=team1)

        channel2 = discord.utils.get(ctx.guild.channels, name=team2)

        if channel2 is None:
            await guild.create_voice_channel(name=team2)
            channel2 = discord.utils.get(ctx.guild.channels, name=team2)

        self.update(guild.id, "channel1", str(team1))
        self.update(guild.id, "channel2", str(team2))

        await ctx.response.send_message("Channels set!")

    # Points every future betting posting (open/closed/winner-report — see
    # _openBetting) at a specific text channel instead of wherever /start
    # or a tournament match happens to run. Creates the channel if a text
    # channel with that name doesn't already exist.
    async def setWagerChannelHelper(self, ctx, channel_name):
        guild = ctx.guild

        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            channel = await guild.create_text_channel(channel_name)

        self.update(guild.id, "wager_channel", channel.name)

        await ctx.response.send_message(f"All wager postings will now go to {channel.mention}.")

    # How long a betting window stays open, replacing the previously-fixed
    # BETTING_DURATION_SECONDS. For a simultaneous-mode tournament round
    # with several matches open at once, this is the PER-MATCH base — see
    # _openConcurrentTournamentBetting, which multiplies it by however many
    # matches are in that round (capped so a big base times a big bracket's
    # first round can't leave betting open for absurdly long).
    async def setBettingTimerHelper(self, ctx, seconds):
        if seconds <= 0:
            await ctx.response.send_message("Betting timer must be greater than 0 seconds.")
            return
        if seconds > 600:
            await ctx.response.send_message("Betting timer can't be more than 600 seconds (10 minutes).")
            return

        self.update(ctx.guild.id, "betting_timer_seconds", seconds)
        await ctx.response.send_message(
            f"Betting windows now stay open for {seconds} seconds. For a tournament round with several "
            f"matches happening at once, that's multiplied by the number of matches in the round."
        )

    async def both(self, ctx):
        await self.randomizeTeamHelper(ctx)
        await self.randomRoleHelper(ctx)

    async def randomRoleHelper(self, ctx):
        # BUG FIX: this used to fetch the *serialized string* for team1/team2
        # and call random.shuffle() directly on that string, which raises
        # (strings are immutable, shuffle needs a mutable sequence). Then it
        # indexed into the string with team1[i % 5], grabbing a single raw
        # character instead of an actual player. Deserialize into real Team
        # objects and shuffle/read the player list instead.
        result1 = ""
        result2 = ""

        team1Ser = self.get(ctx.guild.id, "team1")
        team2Ser = self.get(ctx.guild.id, "team2")

        team1 = Team()
        team1.deserializeTeam(team1Ser)
        team2 = Team()
        team2.deserializeTeam(team2Ser)

        players1 = team1.get_players()
        players2 = team2.get_players()

        random.shuffle(players1)
        random.shuffle(players2)

        # TODO: hardcoded to 5 roles; extend for other team sizes/games.
        for i in range(min(5, len(players1))):
            result1 += roles.get(i) + players1[i].get_name() + "\n"

        for i in range(min(5, len(players2))):
            result2 += roles.get(i) + players2[i].get_name() + "\n"

        self.update(ctx.guild.id, "result1", result1)
        self.update(ctx.guild.id, "result2", result2)

    async def captainsHelper(self, ctx, captain_1, captain_2, ranked=False):
        # BUG FIX: this validation used to run *after* clearTeamsHelper and
        # after already building `Player(captain_1.id, ...)` from both
        # captains — so a None captain crashed with AttributeError on
        # `captain_1.id` before this check ever ran, instead of showing the
        # message below. bot.py's /captains command happens to reject None
        # captains before calling in here today, which is the only reason
        # this was never hit in practice; checking first makes the guard
        # actually do something if that ever changes.
        if captain_1 is None or captain_2 is None:
            await ctx.response.send_message("Mention two team captains!")
            return
        elif captain_1 == captain_2:
            await ctx.response.send_message("Mention two different people!")
            return

        await self.clearTeamsHelper(ctx)  # also resets is_ranked to 0

        captain1 = Player(captain_1.id, captain_1.name)
        captain2 = Player(captain_2.id, captain_2.name)

        self.update(ctx.guild.id, "captain1", captain1.serializePlayer())
        self.update(ctx.guild.id, "captain2", captain2.serializePlayer())
        self.update(ctx.guild.id, "mode", "Ranked Captains" if ranked else "Captains")
        if ranked:
            self.update(ctx.guild.id, "is_ranked", 1)

        original_channel = ctx.user.voice.channel
        self.update(ctx.guild.id, "original_channel", str(original_channel))

        team1 = Team()
        team2 = Team()

        team1.add_player(captain1)
        team2.add_player(captain2)

        team1.name = "Team 1"
        team2.name = "Team 2"

        self.update(ctx.guild.id, "team1", team1.serializeTeam())
        self.update(ctx.guild.id, "team2", team2.serializeTeam())

        players = Team()
        for player in ctx.user.voice.channel.members:
            if player != captain_1 and player != captain_2:
                players.add_player(Player(player.id, player.name))

        self.update(ctx.guild.id, "players", players.serializeTeam())

        await ctx.response.send_message(
            "Ranked captains selected! Elo will be updated when the winner is reported."
            if ranked else "Captains selected!"
        )
        await self.printEmbed(ctx, team1, team2, players)

        await ctx.channel.send(
            captain_1.mention
            + ', use "/choose  @_____" to pick a player for your team'
        )

    # function for captain to choose a specific team member
    async def chooseFunc(self, ctx, member):
        # BUG FIX: /choose can be called with no `member` and `use_random`
        # left False (its default), which passed member=None all the way
        # down into chooseHelper -> Player(member.id, ...) and crashed with
        # AttributeError: 'NoneType' object has no attribute 'id'. Catch it
        # here with a clear message instead of letting it blow up.
        if member is None:
            await ctx.response.send_message(
                'Please mention a player to choose, e.g. "/choose member:@Name", '
                'or use "/choose use_random:True" to pick one at random.'
            )
            return

        captain1Ser = self.get(ctx.guild.id, "captain1")
        captain2Ser = self.get(ctx.guild.id, "captain2")

        captain1 = Player()
        captain1.deserializePlayer(captain1Ser)
        captain2 = Player()
        captain2.deserializePlayer(captain2Ser)

        playersSer = self.get(ctx.guild.id, "players")
        players = Team()
        players.deserializeTeam(playersSer)

        turn = int(self.get(ctx.guild.id, "turn"))

        # BUG FIX: this mixed ctx.user.id (correct, for slash-command
        # Interactions) with ctx.message.author.id (wrong — Interaction
        # objects don't have .message.author and this would raise
        # AttributeError). Standardized on ctx.user throughout.
        if players.get_players() != []:
            if turn == 1 and ctx.user.id == captain1.id:
                await self.chooseHelper(ctx, member, 1)
            elif turn == 2 and ctx.user.id == captain2.id:
                await self.chooseHelper(ctx, member, 2)
            else:
                if (turn == 1 and ctx.user.id == captain2.id) or (
                    turn == 2 and ctx.user.id == captain1.id
                ):
                    await ctx.response.send_message("Not Your Turn!")
                elif (
                    ctx.user.id != captain1.id
                    and ctx.user.id != captain2.id
                ):
                    await ctx.response.send_message("Only team captains can use this command!")
        else:
            await ctx.response.send_message("There are no players left to choose from!")

    # choose random player from all remaining players
    async def chooseRandomMember(self, ctx):
        randomMember = await self.getRandomMember(ctx)
        if randomMember is None:
            await ctx.response.send_message("There are no players left to choose from!")
            return
        await self.chooseFunc(ctx, randomMember)

    async def getRandomMember(self, ctx):
        playersSer = self.get(ctx.guild.id, "players")

        # BUG FIX: `Team().deserializeTeam(playersSer)` was assigned to
        # `players` — but deserializeTeam() mutates the object in place and
        # returns None, so `players` was always None and the very next line
        # (`players.get_players()`) raised AttributeError. Instantiate first,
        # then call deserializeTeam on the instance.
        players = Team()
        players.deserializeTeam(playersSer)

        player_members = players.get_players()
        if not player_members:
            return None

        m = np.array(player_members, dtype=object)
        np.random.shuffle(m)

        member = discord.utils.get(ctx.guild.members, id=m[0].get_id())
        return member

    # helper fn for choosing team members from players that haven't been chosen
    async def chooseHelper(self, ctx, member, turn):
        captain1Ser = self.get(ctx.guild.id, "captain1")
        captain2Ser = self.get(ctx.guild.id, "captain2")
        playersSer = self.get(ctx.guild.id, "players")
        team1Ser = self.get(ctx.guild.id, "team1")
        team2Ser = self.get(ctx.guild.id, "team2")

        captain1 = Player()
        captain1.deserializePlayer(captain1Ser)
        captain2 = Player()
        captain2.deserializePlayer(captain2Ser)

        players = Team()
        players.deserializeTeam(playersSer)
        team1 = Team()
        team1.deserializeTeam(team1Ser)
        team2 = Team()
        team2.deserializeTeam(team2Ser)

        switch = True
        player = Player(member.id, member.name)

        team1ids = [p.get_id() for p in team1.get_players()]
        team2ids = [p.get_id() for p in team2.get_players()]
        playersids = [p.get_id() for p in players.get_players()]

        if (
            member.id not in team1ids
            and member.id not in team2ids
            and member.id in playersids
        ):
            if turn == 1:
                team1.add_player(player)
                self.update(ctx.guild.id, "team1", team1.serializeTeam())
            else:
                team2.add_player(player)
                self.update(ctx.guild.id, "team2", team2.serializeTeam())

            # remove_player() relies on __eq__/identity match; find the
            # equivalent player object already inside `players` by id
            # rather than trying to remove the freshly-constructed `player`.
            toRemove = next((p for p in players.get_players() if p.get_id() == member.id), None)
            if toRemove is not None:
                players.remove_player(toRemove)

            self.update(ctx.guild.id, "players", players.serializeTeam())

            await ctx.response.send_message(f"{member.name} added to team {turn}!")
            await self.printEmbed(ctx, team1, team2, players)
        else:
            switch = False
            await ctx.response.send_message(
                "Player has already been selected or does not exist in the player list."
            )

        # BUG FIX: `players` is a Team object, never equal to the list
        # literal `[]` — this comparison was always False, so the "draft
        # complete" message never fired. Check the underlying player list.
        #
        # Also prompt once both teams reach team_size, even if the pool
        # still has people left in it — a voice channel with more people
        # than team_size * 2 is expected to leave spectators undrafted, so
        # waiting on the pool to fully empty would never fire at all.
        team_size = self.get(ctx.guild.id, "team_size") or 0
        teams_full = (
            team_size
            and len(team1.get_players()) >= team_size
            and len(team2.get_players()) >= team_size
        )
        if len(players.get_players()) == 0 or teams_full:
            await ctx.channel.send(
                'Both teams are set! Use "/start" to move everyone to the channels!'
            )
            return

        c1member = discord.utils.get(ctx.guild.members, id=captain1.id)
        c2member = discord.utils.get(ctx.guild.members, id=captain2.id)

        if turn == 2 and switch:
            self.update(ctx.guild.id, "turn", 1)
            await ctx.channel.send(
                c1member.mention + ', use "/choose  @_____" to pick a player for your team'
            )
        elif turn == 1 and switch:
            self.update(ctx.guild.id, "turn", 2)
            await ctx.channel.send(
                c2member.mention + ', use "/choose  @_____" to pick a player for your team'
            )
        else:
            if turn == 1:
                await ctx.channel.send(
                    c1member.mention
                    + ', use "/choose  @_____" to pick a player for your team'
                )
            else:
                await ctx.channel.send(
                    c2member.mention
                    + ', use "/choose  @_____" to pick a player for your team'
                )

    # clears all current teams
    async def clearTeamsHelper(self, ctx):
        guild_id = ctx.guild.id

        self.update(guild_id, "original_channel", "")
        self.update(guild_id, "team1", "")
        self.update(guild_id, "team2", "")
        self.update(guild_id, "players", "")
        self.update(guild_id, "team_size", 5)
        self.update(guild_id, "mode", "Normal")
        self.update(guild_id, "turn", 1)
        # Every team-formation path (/make-teams, /captains, either with or
        # without ranked:true) runs through here first — resetting is_ranked
        # to 0 by default means only the ranked-specific helpers, which
        # explicitly set it back to 1 afterward, cause elo to be touched
        # when the winner is eventually reported.
        self.update(guild_id, "is_ranked", 0)

    async def notifyHelper(self, ctx, member: discord.Member):
        team_size = self.get(ctx.guild.id, "team_size")
        channel = await member.create_dm()
        invite_channel = ctx.user.voice.channel
        invite_link = await invite_channel.create_invite(max_uses=1, unique=True)
        content = (
            ctx.user.global_name
            + " has invited you to a "
            + str(team_size * 2)
            + " man!\n\n"
            + str(invite_link)
        )
        await channel.send(content)

    # Moves everyone currently in either team channel (+ spectators) back
    # to the channel they started in. Takes a discord.Guild rather than an
    # Interaction so it can run both from /return and automatically once a
    # winner is reported (recordResult), neither of which always has a
    # command Interaction to work with. Returns False (and moves nobody)
    # if the server was never /start'd — there's no "original channel" on
    # record to send anyone back to.
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

    # move everyone in the team channels (+ spectators) back to the
    # channel they started in, refunding any bets from a game that never
    # got a recorded winner.
    async def returnHelper(self, ctx):
        og = self.get(ctx.guild.id, "original_channel")
        if discord.utils.get(ctx.guild.channels, name=og) is None:
            await ctx.response.send_message(
                'You have not been seperated into team voice channels! Use "/start" first.'
            )
            return

        # See the BUG FIX note that used to live on the /return command in
        # bot.py: move_to() is one API call per member, so defer immediately
        # to avoid blowing the 3-second interaction window.
        await ctx.response.defer()

        await self.moveMembersToOriginalChannel(ctx.guild)
        await self.cancelBettingHelper(ctx.guild.id, ctx.channel)

        await ctx.followup.send('Moved!')

    # ---------------- Economy ----------------

    def ensureEconomyRow(self, guild_id, user_id, username):
        self.cursor.execute(
            "INSERT OR IGNORE INTO economy"
            "(guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, gold_lost, "
            "game_wins, game_losses, elo, last_daily) "
            "VALUES(?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?, NULL)",
            (guild_id, user_id, username, DEFAULT_ELO)
        )
        self.cursor.execute(
            "UPDATE economy SET username=? WHERE guildId=? AND userId=?",
            (username, guild_id, user_id)
        )
        self.db.commit()

    # Wipes every player's currency stats (balance, wins/losses, wagered/won/
    # lost gold, daily-claim cooldown) for a guild. Rows get recreated with
    # fresh defaults the next time each player touches the economy (daily,
    # wager, balance) via ensureEconomyRow.
    def resetEconomyHelper(self, guild_id):
        self.cursor.execute("DELETE FROM economy WHERE guildId=?", (guild_id,))
        self.db.commit()

    # Resets every existing player's elo back to DEFAULT_ELO for a guild,
    # leaving balance/wins/losses/gold untouched — unlike resetEconomyHelper,
    # which wipes the whole row.
    def resetEloHelper(self, guild_id):
        self.cursor.execute(
            "UPDATE economy SET elo=? WHERE guildId=?", (DEFAULT_ELO, guild_id)
        )
        self.db.commit()

    # Posts the confirm/cancel view for /clear's clear_elo and clear_economy
    # flags — neither actually touches player data until the invoker clicks
    # "Confirm reset" on the message this sends.
    async def confirmDestructiveClearHelper(self, ctx, clear_economy):
        if clear_economy:
            warning = (
                "This will **wipe the entire economy** (balance, elo, game record, "
                "betting record, gold wagered/won/lost) for **every player** in "
                f"**{ctx.guild.name}**. This can't be undone."
            )
        else:
            warning = (
                f"This will **reset elo back to {DEFAULT_ELO}** for **every player** "
                f"in **{ctx.guild.name}**. This can't be undone."
            )
        view = ConfirmResetView(self, ctx.guild.id, ctx.guild.name, ctx.user.id, clear_economy)
        view.message = await ctx.followup.send(warning, view=view)

    # ---------------- Tournaments ----------------

    # Writes `tournament` to the guild's row in `tournaments`, replacing
    # whatever tournament (if any) was there before — a server only ever
    # has one. `teams`/`bracket` are JSON since they're variable-length
    # nested data; everything else is a direct column, one per Tournament
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

        # Losers bracket has to be deserialized BEFORE the winners bracket,
        # since a winners-bracket node's `drop_to` resolves against the
        # losers bracket's node list.
        losers_nodes, losers_rounds, wb_dependency = [], [], []
        # "after_winners" here covers both a single-elimination tournament
        # (the setting is meaningless without a losers bracket at all) and
        # a double-elimination one saved before this feature existed —
        # both should keep the original "losers bracket waits for the
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

    # Creates a new (empty — no teams registered yet) tournament for the
    # guild. Only one tournament can exist per server at a time, so if one
    # is already there this doesn't overwrite it immediately — it posts a
    # confirm/cancel view and waits for the invoker to confirm the
    # replacement instead.
    async def createTournamentHelper(self, ctx, name, team_size, num_teams, double_elimination):
        guild_id = ctx.guild.id

        if team_size <= 0:
            await ctx.response.send_message("Team size must be greater than 0.")
            return

        if num_teams <= 1:
            await ctx.response.send_message("A tournament needs at least 2 teams.")
            return

        existing = self.getTournament(guild_id)
        if existing is not None:
            if not ctx.user.guild_permissions.manage_guild:
                await ctx.response.send_message(
                    "Only a member with the Manage Server permission can overwrite an existing tournament."
                )
                return

            view = ConfirmTournamentOverwriteView(
                self, guild_id, ctx.user.id, name, team_size, num_teams, double_elimination
            )
            await ctx.response.send_message(
                f"Tournament **{existing.get_name()}** is already set up for this server. "
                f"Creating **{name}** will overwrite it — are you sure?",
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
    # checks all happen here; register_team on Tournament itself only
    # enforces the "no shared players across registered teams" rule.
    async def registerTeamHelper(self, ctx, team_name):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server — use /tournament-create first."
            )
            return

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.")
            return
        _, team = result

        if not self.isTeamCaptain(team, ctx.user.id):
            await ctx.response.send_message(
                f"Only **{team_name}**'s captain can register it for the tournament."
            )
            return

        if team.get_size() != tournament.get_team_size():
            await ctx.response.send_message(
                f"**{team_name}** has {team.get_size()} player(s), but this tournament needs teams of "
                f"exactly {tournament.get_team_size()}."
            )
            return

        if any(existing.get_id() == team.get_id() for existing in tournament.get_teams()):
            await ctx.response.send_message(f"**{team_name}** is already registered for this tournament.")
            return

        if len(tournament.get_teams()) >= tournament.get_num_teams():
            await ctx.response.send_message("This tournament's bracket is already full.")
            return

        try:
            tournament.register_team(team)
        except ValueError as error:
            await ctx.response.send_message(str(error))
            return

        self.saveTournament(guild_id, tournament)
        await ctx.response.send_message(
            f"**{team_name}** registered for **{tournament.get_name()}**! "
            f"({len(tournament.get_teams())}/{tournament.get_num_teams()} teams)"
        )

    def _nextPowerOfTwo(self, n):
        power = 1
        while power < n:
            power *= 2
        return power

    # Builds a fresh single-elimination bracket tree from `teams`
    # (shuffled for random seeding) — paired nodes share a `next` (the
    # empty node their winner advances into, same as a real bracket), and
    # that node's `previous` is one of the pair (`previous.opponent` gives
    # the other). Slots beyond len(teams), if the count isn't a power of
    # two, are byes (team=None). Returns the flat list of every node
    # across every round; doesn't touch the database.
    def buildBracket(self, teams):
        shuffled = list(teams)
        random.shuffle(shuffled)

        size = self._nextPowerOfTwo(len(shuffled))
        num_byes = size - len(shuffled)

        # BUG FIX: placing every real team first and every bye at the tail
        # (team[i] if i < len(team) else None), then pairing consecutively
        # by index, could seat two byes in the same first-round pair
        # whenever num_byes was even — a "BYE vs BYE" match that never has
        # a winner to report, silently orphaning that slot for the rest of
        # the bracket (whoever the surviving side eventually reaches it
        # gets an unearned second auto-advance instead of a real match).
        # num_byes is always < size // 2 (size is the smallest power of two
        # >= len(teams)), so there are always at least as many pairs as
        # byes — spread one bye per pair instead, guaranteeing every bye
        # is paired against a real team.
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
    # Wires each winners-bracket result node's `drop_to` to the losers-
    # bracket leaf that should receive its loser once that match resolves
    # (see _resolveTournamentMatch) — this is the only thing that links the
    # two trees together; everything else about a losers-bracket match
    # plays out through the exact same round machinery as the winners
    # bracket. Returns (flat node list, rounds) — rounds groups each
    # round's RESULT nodes explicitly, since (unlike the winners bracket)
    # losers-bracket round sizes don't follow a simple halving pattern and
    # can't be recovered from the graph alone.
    #
    # Losers-bracket rounds alternate in a fixed, well-known pattern for a
    # k-round winners bracket (k = log2(bracket size)):
    #   round 1            : winners round 1's losers, paired against each other
    #   round r, r odd > 1  : last round's survivors, paired against each other
    #   round r, r even     : last round's survivors, each paired against a
    #                         fresh loser dropping in from winners round (r//2 + 1)
    # ending after round (2k - 2) with exactly one survivor: the losers-
    # bracket champion. (k <= 1 is a degenerate case — with only one
    # winners-bracket match total, its loser has nobody left to play, so
    # they become the losers-bracket "champion" with no match at all.)
    # Returns (all_nodes, rounds, wb_dependency). `wb_dependency[i]` is the
    # WINNERS-bracket round_index whose losers this losers round NEEDS to
    # have dropped in before it can start (i.e. that winners round must be
    # fully RESOLVED first) — or None if this round only depends on the
    # previous losers round finishing (no NEW winners-bracket input).
    # Derived from exactly which wb_rounds index gets `drop_to` wired to it
    # below: `drop_to` set on wb_rounds[Y] means "the match at winners
    # round_index Y-1 feeds this", since Y is the round the LOSING match's
    # winner (not loser) populates — the loser goes to drop_to instead. See
    # "Interleaved losers bracket scheduling" in readme.md for how this
    # gets used (_readyUnstartedLosersRoundIndex, _advanceInterleavedTournament).
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

    # Builds (or rebuilds — calling this again is an explicit reroll) the
    # tournament's bracket from whichever teams are currently registered.
    # Double elimination also builds a real losers bracket (buildLosersBracket)
    # wired to this winners bracket via each result node's `drop_to`.
    # Wipes this guild's match history before a fresh bracket replaces it
    # (used by both createBracketHelper and /test). `tournament_matches.id`
    # is AUTOINCREMENT specifically so a match id is never reused while
    # ANY guild might still reference it — _settleMatchWagers and the
    # concurrent-betting-close timer both key off matchId alone, with no
    # guildId in the WHERE clause, so a reused id could settle or close out
    # a completely different guild's still-live match. That guarantee is
    # only worth breaking when it's free: if this delete just left the
    # table completely empty (no other guild has a live match either), the
    # id sequence can restart at 1 with no collision risk at all — which
    # is also the one case a human actually notices and wants, since a
    # single test server watching /test expects a fresh bracket to start
    # back at "Match #1" instead of climbing forever.
    def _clearTournamentMatchesForGuild(self, guild_id):
        self.cursor.execute("DELETE FROM tournament_matches WHERE guildId=?", (guild_id,))
        self.cursor.execute("SELECT COUNT(*) FROM tournament_matches")
        if self.cursor.fetchone()[0] == 0:
            # sqlite_sequence only exists once some AUTOINCREMENT table
            # somewhere in the DB has had its first insert — nothing to
            # reset yet on a brand new database.
            self.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            )
            if self.cursor.fetchone() is not None:
                self.cursor.execute("DELETE FROM sqlite_sequence WHERE name='tournament_matches'")
        self.db.commit()

    # Deletes this guild's tournament entirely (see /tournament-create) —
    # /clear's clear_tournament flag. Leaves the persistent `teams` rows
    # themselves untouched, since those exist independently of any one
    # tournament and can just be registered into a new one; this only
    # clears the tournament shell/bracket/registration state and its match
    # history (_clearTournamentMatchesForGuild), same as starting over with
    # a fresh /tournament-create.
    def deleteTournamentHelper(self, guild_id):
        self.cursor.execute("DELETE FROM tournaments WHERE guildId=?", (guild_id,))
        self.db.commit()
        self._clearTournamentMatchesForGuild(guild_id)

    async def createBracketHelper(self, ctx, double_elimination, losers_bracket_timing="after_winners"):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server — use /tournament-create first."
            )
            return

        teams = tournament.get_teams()
        if len(teams) < 2:
            await ctx.response.send_message("Need at least 2 registered teams to build a bracket.")
            return

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
        # completely fresh tournament run — clear out any match rows left
        # over from a previous run. Without this, a finished double-
        # elimination tournament's resolved grand-finals row would still be
        # sitting there under this guildId, and the next tournament's own
        # completion check (which looks up the most recent resolved finals
        # match) could mistake it for having already finished too.
        self._clearTournamentMatchesForGuild(guild_id)
        self.saveTournament(guild_id, tournament)

        elim_style = "double" if double_elimination else "single"
        timing_note = ""
        if double_elimination:
            timing_note = (
                " Losers bracket starts once the winners bracket finishes."
                if losers_bracket_timing == "after_winners" else
                " Losers bracket rounds are interleaved with the winners bracket as they're unlocked."
            )
        await ctx.response.send_message(
            f"Bracket created for **{tournament.get_name()}** — {len(teams)} teams, "
            f"{elim_style} elimination.{timing_note}"
        )
        await self._sendBracketText(ctx.channel, tournament, guild_id)

    # ---------------- Tournament matches (/tournament-start) ----------------

    # Splits a flat bracket node list (leaves first, as buildBracket returns
    # it) back into per-round lists. Round sizes are always size, size/2,
    # ..., 1 for a size-leaf bracket, and len(nodes) == 2*size - 1, so the
    # leaf count is recoverable from the total without storing it separately.
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
    # and cached at module scope — see _bracket_logo_cache. None if the
    # asset couldn't be loaded (e.g. a self-hosted deploy missing the
    # shockwave-site/ folder); every caller treats that as "skip the logo"
    # rather than letting a missing file take bracket rendering down with it.
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

    # A cached TTF font at a given size — `variation` selects a named
    # instance out of a variable font (IBM_PLEX_SANS ships every weight in
    # one file; see its own comment) and is ignored for the static Chakra
    # Petch files, which don't have any. Falls back to PIL's built-in
    # default font if the TTF itself is missing (e.g. a self-hosted deploy
    # that didn't pull assets/fonts) rather than crashing rendering outright
    # — same "degrade gracefully instead of taking the feature down"
    # approach _loadBracketLogo takes for a missing logo file.
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

    # How tall the logo/title/subtitle/rule block is, in total — computed
    # independently of any actual drawing so callers can reserve the right
    # amount of vertical space for it during their own (measurement-first)
    # layout pass, the same two-pass approach the rest of this file uses.
    def _bracketHeaderHeight(self, subtitle):
        height = BRACKET_MARGIN + BRACKET_LOGO_HEIGHT
        if subtitle:
            height += BRACKET_SUBTITLE_GAP + BRACKET_SUBTITLE_FONT_SIZE
        return height + BRACKET_HEADER_RULE_GAP + BRACKET_HEADER_RULE_MARGIN

    # Draws the logo (if available), the title next to it in `accent_color`,
    # an optional muted subtitle line below (the guild's name, see
    # renderBracketImages), and a full-width accent rule under the whole
    # block — the visual "masthead" every bracket/Grand Finals image opens
    # with. `width` is the FINAL canvas width, known by the time this runs
    # (unlike _bracketHeaderHeight, called during layout before it exists).
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

        # bold_title picks CHAKRA_PETCH_BOLD vs _SEMIBOLD above — a real
        # heavier font weight, not a faux-bold trick, so nothing extra is
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

    # The extra canvas width the header block itself needs — the logo (if
    # any) plus its gap before the title, compared against the title's own
    # text and the subtitle's, so a short bracket with a long guild name (or
    # vice versa) still sizes the canvas to whichever is actually widest.
    def _bracketHeaderWidth(self, measurer, title, subtitle, title_font, subtitle_font):
        logo = self._loadBracketLogo()
        logo_width = (logo.width + BRACKET_PADDING) if logo is not None else 0
        title_width = logo_width + measurer.textlength(title, font=title_font)
        subtitle_width = measurer.textlength(subtitle, font=subtitle_font) if subtitle else 0
        return max(title_width, subtitle_width)

    # A fresh bracket-image canvas: a soft radial vignette (`background_
    # center` fading out to `background` at the corners, computed with
    # numpy since a plain flat fill at these pixel counts would otherwise
    # mean a per-pixel Python loop) inside a thin rounded frame in
    # `accent_color`. Returns (image, draw) — every caller needs both
    # anyway, and creating them together keeps that pairing from ever
    # drifting apart. `background`/`background_center` default to
    # Shockwave's own site palette so every existing caller (bracket,
    # matchup, Grand Finals images) is unaffected — only the trading card
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

    # Whether `node`'s own label is still live (still in it, or the match it
    # fed hasn't been decided yet) or eliminated — dimmed in the second
    # case, so a glance at the tree shows who's still alive at a glance.
    # BUG FIX: this used to dim the OPPOSITE case — a node whose team WON
    # and advanced (node.next.team matching this node's own team) got
    # dimmed as a "stale waypoint", while the team that actually LOST here
    # was left in full brightness, backwards from what anyone reading the
    # bracket expects (the winner should stand out, not fade). A node is
    # eliminated exactly when the match it feeds into (node.next) has
    # resolved to someone ELSE'S name.
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
    # champion — "Round of 64" for the leaves of a 64-team bracket, on down
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
    # winners bracket does (drop-ins keep the count uneven — see
    # buildLosersBracket), so "Quarterfinals"-style names don't reliably
    # apply. Plain numbering instead, 1-indexed from the leaves.
    def _losersRoundName(self, round_index):
        return f"Losers Round {round_index + 1}"

    # One text label per round_index, positioned directly above that
    # column's own nodes (same x formula _assignBracketPositions/
    # _drawBracketNode use to place them, just one row higher and with no
    # y-offset of its own) — `offsets`/`x0`/`mirror` need to exactly match
    # whatever positions were already built from them. `max_round_index` is
    # the deepest round_index actually present in THIS `offsets` table —
    # the two-sided renderers pass half_round_index here, not the whole
    # bracket's top_round_index, since each half only goes that deep.
    def _drawRoundHeaders(self, draw, offsets, x0, max_round_index, header_y, header_font, name_fn, mirror=False):
        for round_index in range(max_round_index + 1):
            x = x0 + (-offsets[round_index] if mirror else offsets[round_index])
            draw.text(
                (x, header_y), name_fn(round_index), font=header_font, fill=BRACKET_LINE_COLOR,
                anchor=("ra" if mirror else "la")
            )

    # A small 5-pointed star, standing in for a trophy icon — PIL's bundled
    # default font doesn't reliably have emoji glyphs (see the losers-
    # bracket-merge note above about box-drawing characters, same issue),
    # so this is drawn as plain geometry instead of relying on one.
    def _drawStar(self, draw, cx, cy, radius, color):
        points = []
        for i in range(10):
            angle = -math.pi / 2 + i * math.pi / 5
            r = radius if i % 2 == 0 else radius * 0.42
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=color)

    # A champion/final-result label with its own star badge in the gap
    # BRACKET_CHAMPION_BADGE_GAP already reserves immediately to its left —
    # every caller that positions a champion-style label needs to have
    # added that gap to its own x math first (see _renderTreeImage and
    # friends), same as champion_width already accounts for the text itself.
    def _drawChampionLabel(self, draw, x, y, label, font, color):
        star_cx = x - BRACKET_CHAMPION_BADGE_GAP + BRACKET_PADDING + BRACKET_CHAMPION_STAR_RADIUS
        self._drawStar(draw, star_cx, y, BRACKET_CHAMPION_STAR_RADIUS, color)
        draw.text((x, y), label, font=font, fill=color, anchor="lm")

    # Draws an axis-aligned two-segment path from `from_point` through
    # `corner` to `to_point` with the corner rounded to `radius`, instead of
    # a sharp draw.line — purely cosmetic, softens every elbow in the tree.
    # Falls back to a sharp corner if either segment is too short to fit the
    # requested radius, so short connectors near the leaves never overshoot.
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
        # segments' own directions — the only point equidistant (by r) from
        # both trimmed endpoints that keeps the curve tangent to each line.
        center_x, center_y = cx + hdir * r, cy + vdir * r
        start_angle, end_angle = {
            (1, -1): (90, 180), (1, 1): (180, 270), (-1, -1): (0, 90), (-1, 1): (270, 360),
        }[(hdir, vdir)]
        draw.arc(
            [center_x - r, center_y - r, center_x + r, center_y + r], start_angle, end_angle,
            fill=color, width=width
        )

    # Walks the tree rooted at `node` (a champion node, `round_index` rounds
    # up from its own leaves) via `previous`/`previous.opponent`, recording
    # each node's (label, round_index) — leaves included — into `labels`.
    # A losers-bracket "fresh drop-in" leaf (see buildLosersBracket) renders
    # at the SAME round_index as whatever sibling it's paired against, not
    # at 0 the way every winners-bracket leaf does, which is exactly what
    # makes it land in the right column below (see _assignBracketPositions)
    # without needing any special-casing here.
    def _collectBracketLabels(self, node, round_index, labels):
        labels[id(node)] = (self._bracketNodeLabel(node, round_index), round_index)
        if node.previous is not None:
            self._collectBracketLabels(node.previous, round_index - 1, labels)
            self._collectBracketLabels(node.previous.opponent, round_index - 1, labels)

    # One pixel column per round_index — the X coordinate every node at
    # that round_index gets drawn at — sized to the widest label anywhere
    # in that round_index (plus padding for the connector line into it) so
    # every round lines up in a straight column across the whole image,
    # the same idea the old ASCII renderer used column_widths for.
    # `header_font`/`round_name_fn`, if given, also count that round's own
    # header text (see _drawRoundHeaders) toward its column's width — a
    # small bracket with short team names but a longer header like "Losers
    # Round 1" would otherwise size the column to the names alone and run
    # that header straight into the next one.
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

    # Recursively assigns every node an (x, y) pixel position — x straight
    # from `offsets[round_index]`, y for a leaf from a shared counter (so
    # leaves stack top-to-bottom in traversal order) and for anything else
    # the midpoint of its two children's y — writing into `positions` and
    # returning this node's own y so its caller can average it with its
    # sibling's. Unlike the ASCII renderer, a losers-bracket "fresh drop-in"
    # leaf needs no special handling at all here: it's still just a leaf,
    # its x already comes out right since it's called with the SAME
    # round_index as its sibling, and pixel space doesn't need the leading-
    # blank padding tightly-packed character columns did.
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
    # the ┐/┘ elbow connecting its two children into it, and recurses. Line
    # drawing rather than box-drawing text characters specifically so this
    # never depends on a font actually having those glyphs — plain straight
    # lines render identically on every platform.
    #
    # `mirror` draws the exact horizontal mirror image — labels grow
    # leftward from their anchor (`anchor="rm"` instead of `"lm"`) and every
    # connector offset flips direction — for the right-hand half of a two-
    # sided bracket (see _renderTwoSidedTreeImage), where round_index still
    # increases moving INTO the page (toward the center) but that now means
    # decreasing x instead of increasing it.
    # `skip_own_label`, when True, leaves THIS node's own text undrawn (its
    # connectors and children still get drawn/recursed normally) — for a
    # caller that wants to draw the root itself separately, as a champion
    # label with its own star badge (see _drawChampionLabel) instead of a
    # plain dimmable node.
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

    # Renders one bracket tree (winners or losers) — everything from
    # `champion_node` down to its leaves — as a standalone image sized to
    # exactly fit its content, titled `title`. Positions are computed in a
    # first pass so the canvas can be sized from their actual bounds before
    # anything is drawn, rather than guessing a size up front and risking
    # clipping the bottom/right edge.
    # `accent_color` colors the title and the champion's own label/badge —
    # gold for the winners bracket, BRACKET_LOSERS_ACCENT_COLOR for the
    # losers bracket (see _renderLosersBracketImage), so the two images read
    # as "which one is this" without depending on remembering the caption.
    # `round_name_fn` similarly defaults to the winners-bracket-style
    # Round-of-N/Quarterfinals/... naming; the losers bracket passes
    # _losersRoundName instead (see that method for why).
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
        # for its star badge (see _drawChampionLabel) — done here, before
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
    # halves growing toward the center — see that constant's comment for
    # why this only ever gets called for the winners bracket. `champion_node`'s
    # two children (always exactly even halves — see buildBracket) are laid
    # out independently, the right one mirrored (_drawBracketNode's `mirror`
    # flag), and joined by one final connector into the champion in the
    # middle.
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

        # Each half's own top node (closest to center) sits at its deepest
        # local x — offsets_*[half_round_index], same as max() over its
        # positions — and, since _drawBracketNode anchors a label at its
        # position and extends it AWAY from center, needs its own rendered
        # width added on top of that to know where it actually ends.
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
        # connector_x from left_half — otherwise its own top node reads as
        # a second, unrelated match crowded right next to the actual
        # result instead of a clearly separate half of the bracket.
        right_x0 = champion_x + champion_width + BRACKET_PADDING * 3 + right_top_x + right_half_width

        # The right half is also nudged down a couple of rows from where a
        # plain mirror of the left half would otherwise land. Right_half's
        # own connector line necessarily crosses the champion's x-range now
        # (it sits past it) — without this nudge, any bracket without byes
        # makes both halves the exact same shape, so that line would land
        # on the champion's exact row and run straight through its label.
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

    # The winners bracket as an image — always present once a bracket
    # exists at all. A plain hyphen, not an em dash, in the title: PIL's
    # bundled default font doesn't have a glyph for it, which renders as a
    # visible tofu box — plain ASCII punctuation is guaranteed to exist in
    # any font this ends up running with.
    def _renderWinnersBracketImage(self, tournament, guild_name=None):
        rounds = self._bracketRounds(tournament.get_bracket())
        top_round_index = len(rounds) - 1
        champion_node = rounds[-1][0]
        title = f"{tournament.get_name()} - Winners Bracket"
        subtitle = f"for {guild_name}" if guild_name else None
        if top_round_index >= BRACKET_TWO_SIDED_MIN_ROUNDS:
            return self._renderTwoSidedTreeImage(champion_node, top_round_index, title, subtitle)
        return self._renderTreeImage(champion_node, top_round_index, title, subtitle=subtitle)

    # The losers bracket as an image, for a double-elimination tournament —
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
    # winners bracket does: the losers bracket's last round is ALWAYS a
    # lopsided drop-in (see buildLosersBracket) — one side is the deep
    # surviving lineage, the other is a single bare leaf (whichever team
    # lost the winners-bracket final outright) — so that split would put
    # an entire tree on one side and one bare name on the other.
    #
    # One round earlier is where the two winners-bracket-side lineages
    # actually meet: every losers-bracket round after that keeps
    # winners-left and winners-right losers strictly separate (each
    # drop-in pairs a survivor against a fresh loser from the SAME
    # winners-bracket side — see buildLosersBracket's round-alternation
    # pattern), right up until the second-to-last round, which is always
    # exactly one node — the first, and only, point where they merge. THAT
    # merge is the genuine even split; drawing it two-sided and then
    # extending one more (normal, single-sided) hop past it to reach the
    # true champion keeps things honest about where the real asymmetry is,
    # instead of pushing it into a lopsided top-level split.
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
        # connector_x from left_half — otherwise its own top node reads as
        # a second, unrelated match crowded right next to the merge_node/
        # other_child/champion hop instead of a clearly separate half of
        # the bracket.
        right_x0 = champion_x + champion_width + BRACKET_PADDING * 3 + right_top_x + right_half_width

        # The right half is also nudged well down from where a plain mirror
        # of the left half would otherwise land — more than the minimum
        # needed to keep right_half's own connector line off the hop's rows
        # (that alone only bought ~1.25 rows of clearance, which technically
        # doesn't cross anything but still reads as "another game" sitting
        # right under the champion at a glance). Without any nudge at all,
        # any bracket without byes makes both halves the exact same shape,
        # putting right_half's own top on the hop's exact row.
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

    # A dedicated third image for just the Grand Finals stage: winners-
    # bracket champion vs losers-bracket champion, and the decider "bracket
    # reset" match if the losers-bracket side forced one by winning game 1.
    # None until game 1 has actually been played — not just once both
    # bracket champions exist, since "vs, nothing decided yet" isn't worth
    # its own message (see _sendBracketText, which sends this separately
    # from the winners/losers bracket images, and only when this isn't
    # None). Split from the actual drawing (_buildGrandFinalsImage) so that
    # code can also be exercised without a real guildId or any
    # tournament_matches rows — see /test in bot.py, which simulates a full
    # run entirely in memory.
    def _renderGrandFinalsImage(self, guild_id, tournament, guild_name=None):
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

        return self._buildGrandFinalsImage(
            tournament, wb_champion, lb_champion, game1_winner_name, reset_winner_name, guild_name
        )

    # The pure rendering half of _renderGrandFinalsImage — no DB access, just
    # the two bracket champions and however far Grand Finals has resolved
    # (both winner names None if it hasn't started; reset_winner_name None
    # until a reset was both needed and played).
    def _buildGrandFinalsImage(
        self, tournament, wb_champion, lb_champion, game1_winner_name, reset_winner_name, guild_name=None
    ):
        # A reset match only ever happens if the losers-bracket champion
        # won game 1 (both sides then sit at one loss apiece) — see
        # _resolveFinalsMatch.
        needs_reset = game1_winner_name == lb_champion.get_name()

        font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        title_font = self._loadFont(CHAKRA_PETCH_SEMIBOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        title = f"{tournament.get_name()} - Grand Finals"
        subtitle = f"for {guild_name}" if guild_name else None

        game1_label = game1_winner_name if game1_winner_name is not None else "TBD"
        # BUG FIX: neither stage used to dim its loser at all once a stage
        # resolved — both "top"/"bottom" labels always drew in the same
        # plain color, unlike the main bracket images where a decided
        # match dims the side that lost (see _bracketNodeTextColor). Stays
        # False (no dimming) for whichever side hasn't lost yet — either
        # the stage isn't decided, or that side is the one who won.
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

    # Every bracket image for `tournament`, as ready-to-attach discord.Files
    # — just the winners bracket for single elimination, plus the losers
    # bracket too (skipped only in the 2-team degenerate case), for double.
    # The Grand Finals image is deliberately NOT included here — it's sent
    # as its own separate message, and only once Grand Finals has actually
    # been played, instead of tagging along on every bracket update — see
    # _sendBracketText.
    def renderBracketImages(self, tournament, guild_name=None):
        files = [
            self._imageToFile(self._renderWinnersBracketImage(tournament, guild_name), "winners_bracket.png")
        ]
        if tournament.is_double_elimination():
            losers_image = self._renderLosersBracketImage(tournament, guild_name)
            if losers_image is not None:
                files.append(self._imageToFile(losers_image, "losers_bracket.png"))
        return files

    # Every bracket/matchup image is drawn BRACKET_SUPERSAMPLE times bigger
    # than it's meant to end up (see that constant's own comment) — this is
    # the one place that scale gets undone, since every renderer's output
    # passes through here on its way to becoming a discord.File. The
    # LANCZOS downsize is what actually smooths out jagged text/line edges;
    # drawing at 1x directly never had any antialiasing to begin with.
    def _imageToFile(self, image, filename):
        if BRACKET_SUPERSAMPLE != 1:
            target_size = (image.width // BRACKET_SUPERSAMPLE, image.height // BRACKET_SUPERSAMPLE)
            image = image.resize(target_size, Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=filename)

    # `team`'s roster with its captain floated to the front (if it has one
    # and they're actually still on the roster) — what _renderMatchupImage
    # prints, so "captain at the top" is just "print the list in order"
    # rather than something each caller has to special-case.
    def _orderedRoster(self, team):
        captain = team.get_captain()
        players = team.get_players()
        if isinstance(captain, Player) and any(p.get_id() == captain.get_id() for p in players):
            rest = [p for p in players if p.get_id() != captain.get_id()]
            return [captain] + rest
        return list(players)

    # One team's half of the matchup image: its logo, its name, then its
    # roster with the captain marked with a star (on top of _orderedRoster
    # already having put them first). A persistent team always has a logo
    # of its own by now (see _ensureLogo, called on every load) — a team
    # with none here is really one of the ad-hoc rosters /make-teams,
    # /captains, etc. build on the fly, which never go through that. Rather
    # than draw a bare ring for those, pick a random built-in logo just for
    # this image; not persisted anywhere (there's no stable row to persist
    # it against), so a rerender can land on a different one, but that's
    # fine for a team with no identity to keep consistent in the first
    # place. Only falls back to the ring if the built-in set itself is
    # unavailable (assets folder missing/empty).
    def _drawMatchupColumn(
        self, image, draw, team, roster, cx, logo_top, name_y, roster_top, name_font, team_font, accent_color
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
        star_radius = BRACKET_FONT_SIZE / 3
        for i, player in enumerate(roster):
            is_captain = captain_id is not None and player.get_id() == captain_id
            y = roster_top + i * BRACKET_ROW_HEIGHT
            color = BRACKET_TITLE_COLOR if is_captain else BRACKET_TEXT_COLOR
            if is_captain:
                # A drawn star (same shape _drawChampionLabel uses for the
                # champion badge), not a "★" text glyph — PIL's default
                # font doesn't actually have that character, so it was
                # rendering as a tofu box instead of a star.
                name_width = draw.textlength(player.get_name(), font=name_font)
                star_cx = cx - name_width / 2 - BRACKET_PADDING / 2 - star_radius
                self._drawStar(draw, star_cx, y + BRACKET_FONT_SIZE / 2, star_radius, color)
            draw.text((cx, y), player.get_name(), font=name_font, fill=color, anchor="ma")

    # The "vs" matchup graphic posted alongside the existing text
    # announcement whenever a tournament match is created (_postMatchReport,
    # _postReadyCheck) — team 1 and team 2's logos and rosters facing off,
    # captain on top for each side (see _orderedRoster). Reuses the exact
    # same canvas/header treatment the bracket images use
    # (_createBracketCanvas, _drawBracketHeader) so this reads as the same
    # product instead of a bolted-on second visual style. `round_label` is
    # this match's place in the tournament (see _matchRoundLabel) — the
    # headline, since "what round is this" is the thing someone glancing at
    # the graphic wants first; the tournament/server name and match id are
    # supporting context underneath.
    def _renderMatchupImage(self, match_id, team1, team2, round_label, tournament_name, guild_name):
        # match_id is None for a casual/ranked (non-tournament) matchup —
        # see _sendMatchupImage — which just omits the "Match #N" part of
        # the subtitle.
        name_font = self._loadFont(IBM_PLEX_SANS, BRACKET_FONT_SIZE, "Regular")
        team_font = self._loadFont(CHAKRA_PETCH_BOLD, BRACKET_TITLE_FONT_SIZE)
        vs_font = self._loadFont(CHAKRA_PETCH_BOLD, MATCHUP_VS_FONT_SIZE)
        title_font = self._loadFont(CHAKRA_PETCH_BOLD, BRACKET_TITLE_FONT_SIZE)
        subtitle_font = self._loadFont(IBM_PLEX_SANS, BRACKET_SUBTITLE_FONT_SIZE, "Regular")
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        roster1 = self._orderedRoster(team1)
        roster2 = self._orderedRoster(team2)
        rows = max(len(roster1), len(roster2), 1)

        def column_width(team, roster):
            name_width = measurer.textlength(team.get_name(), font=team_font)
            roster_width = max(
                (measurer.textlength(p.get_name(), font=name_font) for p in roster), default=0
            )
            return max(name_width, roster_width, MATCHUP_LOGO_SIZE)

        column_width_px = max(column_width(team1, roster1), column_width(team2, roster2))
        body_width = BRACKET_MARGIN * 2 + column_width_px * 2 + MATCHUP_COLUMN_GAP

        subtitle_parts = [part for part in (tournament_name, guild_name) if part]
        if match_id is not None:
            subtitle_parts.append(f"Match #{match_id}")
        # Plain hyphen, not "•" — PIL's default font doesn't have that
        # glyph either (same issue the roster's captain star just had).
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
            name_font, team_font, TEAM1_ACCENT_COLOR
        )
        self._drawMatchupColumn(
            image, draw, team2, roster2, right_cx, logo_top, name_y, roster_top,
            name_font, team_font, TEAM2_ACCENT_COLOR
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
    # tournament — /start, right as the match actually begins (see
    # sendCurrentMatchupImage) — same renderer tournament matches use
    # (_renderMatchupImage), just with no match id or tournament name to
    # put in the subtitle.
    async def _sendMatchupImage(self, channel, team1, team2, label):
        guild_name = channel.guild.name if channel.guild is not None else None
        image = self._renderMatchupImage(None, team1, team2, label, None, guild_name)
        await channel.send(file=self._imageToFile(image, "matchup.png"))

    # Maps the "mode" stored per-guild (set by /make-teams, /captains,
    # /team-use) to the matchup image's headline — used by /start, which
    # posts the image from whatever's currently loaded rather than knowing
    # for itself how those teams were formed.
    def _matchupLabelForMode(self, mode):
        return {
            "Normal": "Casual Match",
            "Ranked": "Ranked Match",
            "Captains": "Captains Match",
            "Ranked Captains": "Ranked Captains Match",
        }.get(mode, "Match")

    # The plain-text status that accompanies the bracket images: which
    # team's the (winners-bracket, for double elimination) champion, the
    # losers-bracket champion once it has one, and Grand Finals results
    # once that's started. `guild_id` is only needed for double elimination
    # (Grand Finals state lives in `tournament_matches`, not on
    # `tournament` itself) — omit it and that part is skipped.
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
    # play it — empty string before then (nothing to show yet). Needs
    # `guild_id` because, unlike everything else this renders, Grand
    # Finals state lives only in `tournament_matches`, not on `tournament`
    # itself (see _startGrandFinals).
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

    # Posts a tournament's status: the text from renderBracketText plus one
    # image attachment per bracket (renderBracketImages) — a single
    # message, single API call, no matter the bracket's size. Images render
    # fully inline in Discord with no truncation the way an oversized text
    # message or a big file-attachment preview would have. The Grand
    # Finals image, if Grand Finals has actually been played, follows as
    # its own separate message right after — it's a distinct enough stage
    # that bundling it into the same message as the two full brackets
    # buried it instead of standing out.
    async def _sendBracketText(self, channel, tournament, guild_id=None):
        guild_name = channel.guild.name if channel.guild is not None else None
        await channel.send(
            self.renderBracketText(tournament, guild_id),
            files=self.renderBracketImages(tournament, guild_name)
        )
        if guild_id is not None:
            finals_image = self._renderGrandFinalsImage(guild_id, tournament, guild_name)
            if finals_image is not None:
                await channel.send(files=[self._imageToFile(finals_image, "grand_finals.png")])

    async def printBracketHelper(self, ctx):
        tournament = self.getTournament(ctx.guild.id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server — use /tournament-create first."
            )
            return
        await ctx.response.send_message(
            self.renderBracketText(tournament, ctx.guild.id),
            files=self.renderBracketImages(tournament, ctx.guild.name)
        )
        finals_image = self._renderGrandFinalsImage(ctx.guild.id, tournament, ctx.guild.name)
        if finals_image is not None:
            await ctx.channel.send(files=[self._imageToFile(finals_image, "grand_finals.png")])

    # "Where in the tournament is this match" — the matchup graphic's
    # headline. Winners-bracket rounds get the same "Quarterfinals"/"Round
    # of 8"-style names the bracket image uses (_roundName, which needs the
    # bracket's own top_round_index to know how far from the final this
    # round is); the losers bracket has no such clean naming (see
    # _losersRoundName's own comment) so it's just numbered; Grand Finals is
    # its own two-state thing (the first game, or the bracket-reset decider).
    def _matchRoundLabel(self, tournament, round_index, bracket_type):
        if bracket_type == "losers":
            return self._losersRoundName(round_index)
        if bracket_type == "finals":
            return "Grand Finals" if round_index == 0 else "Grand Finals — Bracket Reset"
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

        tournament = self.getTournament(guild_id)
        round_label = self._matchRoundLabel(tournament, round_index, bracket_type)
        guild_name = channel.guild.name if channel.guild is not None else None
        matchup_file = self._imageToFile(
            self._renderMatchupImage(match_id, team1, team2, round_label, tournament.get_name(), guild_name),
            f"match_{match_id}_vs.png"
        )
        msg = await channel.send(
            f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} — react with "
            f"{TOURNAMENT_READY_EMOJI} when ready to play (either captain)!",
            file=matchup_file
        )
        await msg.add_reaction(TOURNAMENT_READY_EMOJI)

        self.cursor.execute(
            "UPDATE tournament_matches SET state='PENDING_READY', messageId=? WHERE id=?", (msg.id, match_id)
        )
        self.db.commit()

    # Posts the "who won" prompt for a simultaneous-mode match — no ready
    # check, just a direct TEAM_EMOJIS report same as a normal game. Betting on
    # it (alongside every other match in the same round) is opened
    # separately — see _openConcurrentTournamentBetting, called once after
    # every match in the round has its own report prompt posted.
    async def _postMatchReport(self, guild_id, match_id, channel):
        self.cursor.execute(
            "SELECT team1, team2, roundIndex, bracketType FROM tournament_matches WHERE id=?", (match_id,)
        )
        team1_ser, team2_ser, round_index, bracket_type = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        tournament = self.getTournament(guild_id)
        round_label = self._matchRoundLabel(tournament, round_index, bracket_type)
        guild_name = channel.guild.name if channel.guild is not None else None
        matchup_file = self._imageToFile(
            self._renderMatchupImage(match_id, team1, team2, round_label, tournament.get_name(), guild_name),
            f"match_{match_id}_vs.png"
        )
        msg = await channel.send(
            f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} — react with "
            f"{TEAM_EMOJIS[1]} if {team1.get_name()} won, or {TEAM_EMOJIS[2]} if {team2.get_name()} won.",
            file=matchup_file
        )
        await msg.add_reaction(TEAM_EMOJIS[1])
        await msg.add_reaction(TEAM_EMOJIS[2])

        self.cursor.execute(
            "UPDATE tournament_matches SET state='AWAITING_RESULT', messageId=? WHERE id=?", (msg.id, match_id)
        )
        self.db.commit()

    # Opens one shared betting window covering every match in a just-
    # posted simultaneous-mode round. Unlike _openBetting's single-game
    # singleton (one bet per user per GUILD, tracked on the `servers` row),
    # this is keyed by matchId in `tournament_wagers` — several matches can
    # be open at once, and a user can bet on more than one of them,
    # something the old wagers table's PRIMARY KEY(guildId, userId)
    # couldn't represent at all. Duration is the guild's configured
    # per-match base (_getBettingTimerSeconds) times how many matches are
    # in the round, capped so a generous base times a big bracket's first
    # round can't leave betting open for an unreasonable stretch.
    async def _openConcurrentTournamentBetting(self, guild_id, match_ids, channel):
        base = self._getBettingTimerSeconds(guild_id)
        duration = min(base * len(match_ids), MAX_CONCURRENT_BETTING_SECONDS)
        match_list = ", ".join(f"#{match_id}" for match_id in match_ids)
        plural = "es" if len(match_ids) != 1 else ""

        await channel.send(
            f"\U0001f3b2 Betting is open on {len(match_ids)} match{plural} ({match_list})! Use "
            f"`/wager <amount> <team> match_id:<id>` to bet on one. Betting closes in {duration} seconds."
        )
        asyncio.create_task(self._concurrentBettingTimer(match_ids, channel, duration))

    # No cancellation path (unlike cancelBettingHelper for the singleton
    # flow) — tournament rounds have no "/return"-equivalent to cancel one
    # mid-flight. If every match in the round has already resolved by the
    # time this fires, the UPDATE below just touches already-RESOLVED rows
    # harmlessly; each match's own wagers were already settled and cleared
    # at resolution time regardless of what this timer does.
    async def _concurrentBettingTimer(self, match_ids, channel, duration):
        await asyncio.sleep(duration)
        placeholders = ",".join("?" * len(match_ids))
        self.cursor.execute(
            f"UPDATE tournament_matches SET bettingClosed=1 WHERE id IN ({placeholders})", match_ids
        )
        self.db.commit()
        await channel.send("\U0001f512 Betting is now closed for this round's matches!")

    # Pure computation of one match's pari-mutuel payouts (winners split the
    # losing pool proportional to their own wager, on top of getting it
    # back) as a deltas dict in the exact shape applyGameDeltas expects —
    # shared by _settleMatchWagers (the normal path) and
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

        for user_id, username, _team, amount in winning_bets:
            payout = round(amount + (amount / winning_pool) * losing_pool) if winning_pool > 0 else amount
            bump(user_id, username, balance=payout, wins=1, gold_wagered=amount, gold_won=payout - amount)
        for user_id, username, _team, amount in losing_bets:
            bump(user_id, username, losses=1, gold_wagered=amount, gold_lost=amount)

        return deltas

    # Real-money settlement for one tournament match's wagers, via the same
    # deltas/applyGameDeltas machinery /report-correct-winner's casual-game
    # path uses. Scoped to exactly this match_id's rows, so settling one
    # concurrent match never touches another's still-open bets.
    async def _settleMatchWagers(self, guild_id, match_id, winning_team, channel):
        self.cursor.execute(
            "SELECT userId, username, team, amount FROM tournament_wagers WHERE matchId=?", (match_id,)
        )
        wagers = self.cursor.fetchall()
        if not wagers:
            return

        deltas = self._matchWagerDeltas(wagers, winning_team)
        self.applyGameDeltas(guild_id, deltas)

        lines = [f"\U0001f4b0 **Match #{match_id} payouts:**"]
        for user_id, username, team, amount in wagers:
            if team == winning_team:
                lines.append(f"{username} won {deltas[user_id]['balance']} gold (bet {amount})")

        # Snapshotted before the rows disappear — see _correctTournamentMatchHelper,
        # which needs to know exactly who bet what on THIS match if it's
        # ever corrected after the fact, once tournament_wagers itself is gone.
        self.cursor.execute(
            "UPDATE tournament_matches SET settledWagers=? WHERE id=?",
            (json.dumps(wagers), match_id)
        )
        self.cursor.execute("DELETE FROM tournament_wagers WHERE matchId=?", (match_id,))
        self.db.commit()

        if len(lines) > 1:
            await channel.send("\n".join(lines))

    # Queues every real pairing in `round_index` of the WINNERS bracket as a
    # tournament_matches row (byes — a pairing where only one side has a
    # team — auto-advance immediately with no match at all, and produce no
    # loser to drop into the losers bracket) and kicks the round off: the
    # first match's ready-check for sequential, or every match's report
    # prompt at once for simultaneous. Recurses forward through bye-only
    # rounds; once the winners bracket itself is done, a double-elimination
    # tournament moves on to the losers bracket instead of ending outright.
    # --- Interleaved losers-bracket scheduling ------------------------------
    # Only consulted when tournament.get_losers_bracket_timing() ==
    # "interleaved" (see /tournament-create-bracket) — the default
    # "after_winners" timing never calls any of this, and _startRound/
    # _startLosersRound just walk their own round list start to finish
    # exactly as they always have.

    # The smallest winners round_index with no tournament_matches row at
    # all yet — "hasn't been started". A round that was skipped entirely
    # (every pairing a bye — only possible for round 0, but handled
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
    # its own predecessor losers round (if any) is fully resolved, and —
    # per tournament.get_losers_bracket_wb_dependency() — the winners
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

    # The shared "what should play next" decision for interleaved timing —
    # called any time a round (winners or losers) finishes. A losers round
    # that's now unlocked always takes priority (this is what "winners
    # await the previous round's losers" means); otherwise the winners
    # bracket advances if it still has a round to play; otherwise both
    # brackets have nothing left to START (something may still be
    # mid-play, which will call back in here once it resolves) and Grand
    # Finals gets a shot — safe to call unconditionally, it silently
    # no-ops without both champions decided.
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
        # before winners moves on to round_index — "winners await the
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
                    # finished) — let the shared scheduler pick up
                    # wherever that's at, rather than blindly restarting
                    # from round 0.
                    await self._advanceInterleavedTournament(guild_id, tournament, mode, channel)
                else:
                    await self._startLosersRound(guild_id, tournament, 0, mode, channel)
            else:
                await channel.send(f"\U0001f3c6 **{tournament.get_name()}** is complete! Champion: **{name}**")
                await self._postTournamentLeaderboard(channel, guild_id, tournament)
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
                "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType) "
                "VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'winners')",
                (guild_id, round_index, node_index, a.team.serializeTeam(), b.team.serializeTeam(),
                 mode, channel.id)
            )
            self.db.commit()
            match_ids.append(self.cursor.lastrowid)

        plural = "es" if len(match_ids) != 1 else ""
        await channel.send(f"__Round {round_index + 1}__ — {len(match_ids)} match{plural} to play.")

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_ids[0], channel)
        else:
            for match_id in match_ids:
                await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, match_ids, channel)

    # Mirrors _startRound above, but for the LOSERS bracket: `round_nodes`
    # here are the round's RESULT nodes (see buildLosersBracket), so each
    # match's two participants are reached via `result_node.previous` /
    # `.previous.opponent` instead of iterating a flat pairs list directly.
    # A losers-bracket "bye" happens when one feeder never got a team at
    # all (a winners-bracket bye pairing produces no loser to drop down) —
    # same auto-advance treatment as a real bye. If BOTH feeders are empty
    # (two winners-bracket byes landed in the same losers-bracket pairing),
    # that slot just never fills — same as the equivalent winners-bracket
    # edge case. Once every losers round has been played, moves on to
    # Grand Finals.
    async def _startLosersRound(self, guild_id, tournament, round_index, mode, channel):
        lb_rounds = tournament.get_losers_rounds()

        if not lb_rounds or round_index >= len(lb_rounds):
            await self._startGrandFinals(guild_id, tournament, mode, channel)
            return

        if tournament.get_losers_bracket_timing() == "interleaved":
            wb_dependency = tournament.get_losers_bracket_wb_dependency()
            dep = wb_dependency[round_index] if round_index < len(wb_dependency) else None
            if dep is not None and not self._winnersRoundFullyResolved(guild_id, dep):
                # Not unlocked yet — pause the losers bracket here and let
                # the winners bracket continue instead; this exact round
                # gets retried (via _advanceInterleavedTournament) once its
                # dependency resolves.
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
                "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType) "
                "VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'losers')",
                (guild_id, round_index, node_index, a.team.serializeTeam(), b.team.serializeTeam(),
                 mode, channel.id)
            )
            self.db.commit()
            match_ids.append(self.cursor.lastrowid)

        plural = "es" if len(match_ids) != 1 else ""
        await channel.send(
            f"__Losers Bracket Round {round_index + 1}__ — {len(match_ids)} match{plural} to play."
        )

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_ids[0], channel)
        else:
            for match_id in match_ids:
                await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, match_ids, channel)

    # Posts the winners-bracket champion vs losers-bracket champion match.
    # `reset` is True for the second, decider match that's only played if
    # the losers-bracket champion wins game 1 — at that point both sides
    # have exactly one loss, so a single game settles it either way.
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
                f"{lb_champion.get_name()} (one loss) — {lb_champion.get_name()} must win twice "
                f"to take the title."
            )

        self.cursor.execute(
            "INSERT INTO tournament_matches"
            "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType) "
            "VALUES(?, ?, -1, ?, ?, 'QUEUED', ?, NULL, ?, NULL, 'finals')",
            (guild_id, 1 if reset else 0, wb_champion.serializeTeam(), lb_champion.serializeTeam(),
             mode, channel.id)
        )
        self.db.commit()
        match_id = self.cursor.lastrowid

        if mode == "sequential":
            await self._postReadyCheck(guild_id, match_id, channel)
        else:
            await self._postMatchReport(guild_id, match_id, channel)
            await self._openConcurrentTournamentBetting(guild_id, [match_id], channel)

    # The overall tournament champion's name once EVERYTHING (including
    # Grand Finals, and a bracket reset if one was needed) has resolved —
    # None if there's still something left to play. Single elimination has
    # no Grand Finals stage, so its own bracket is the whole story.
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
            # The decider match — whoever wins it is champion outright.
            return winning_team.get_name()

        # round_index == 0: only actually over if the winners-bracket
        # champion won game 1 outright. If the losers-bracket champion won
        # instead, a reset match is needed — and if one had already been
        # played, the query above would have returned that row (roundIndex
        # 1) instead of this one.
        # Compared by name rather than id: team names are guaranteed unique
        # per guild (enforced by /team-create), whereas .get_id() is only
        # ever set once a team's been persisted through _saveNewTeam — a
        # guarantee this comparison shouldn't have to lean on.
        wb_rounds = self._bracketRounds(tournament.get_bracket())
        wb_champion = wb_rounds[-1][0].team
        if wb_champion is not None and winning_team.get_name() == wb_champion.get_name():
            return winning_team.get_name()
        return None

    # Starts (or restarts, if the whole tournament is idle) the current
    # round. Refuses to run while a round is already in progress, or once
    # a champion has already been decided. Only ever kicks off winners
    # bracket round 0 — a double-elimination tournament's losers bracket
    # and Grand Finals play out on their own from there, driven entirely
    # by match resolution (_resolveTournamentMatch), no repeat command
    # needed.
    async def startTournamentHelper(self, ctx, mode):
        guild_id = ctx.guild.id

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message(
                "No tournament set up for this server — use /tournament-create first."
            )
            return

        bracket = tournament.get_bracket()
        if not bracket:
            await ctx.response.send_message("No bracket has been created yet — use /tournament-create-bracket first.")
            return

        champion_name = self._tournamentChampionName(guild_id, tournament)
        if champion_name is not None:
            await ctx.response.send_message(
                f"**{tournament.get_name()}** is already finished — **{champion_name}** is the champion!"
            )
            return

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND state != 'RESOLVED'", (guild_id,)
        )
        if self.cursor.fetchone()[0] > 0:
            await ctx.response.send_message("This tournament's current round is already in progress.")
            return

        await ctx.response.send_message(
            f"Starting **{tournament.get_name()}** — {mode} mode."
        )
        await self._startRound(guild_id, tournament, 0, mode, ctx.channel)

    async def _handleReadyReaction(self, payload):
        guild_id = payload.guild_id
        self.cursor.execute(
            "SELECT id, team1, team2 FROM tournament_matches "
            "WHERE guildId=? AND messageId=? AND state='PENDING_READY'",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        match_id, team1_ser, team2_ser = row
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        if not (self.isTeamCaptain(team1, payload.user_id) or self.isTeamCaptain(team2, payload.user_id)):
            return

        # BUG-PRONE PATTERN AVOIDED: flip state before anything async below,
        # so a double-react can't begin the same match twice.
        self.cursor.execute("UPDATE tournament_matches SET state='AWAITING_RESULT' WHERE id=?", (match_id,))
        self.db.commit()

        channel = self.client.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(payload.channel_id)

        # Route through the exact same team-game cycle a casual/ranked
        # game uses: team1/team2 + betting + the TEAM_EMOJIS report message.
        # active_tournament_match_id is what tells recordResult (once that
        # cycle resolves) to come back here and advance the bracket.
        team1.set_id(1)
        team2.set_id(2)
        self.update(guild_id, "team1", team1.serializeTeam())
        self.update(guild_id, "team2", team2.serializeTeam())
        self.update(guild_id, "original_channel", "")
        self.update(guild_id, "is_ranked", 0)
        self.update(guild_id, "active_tournament_match_id", match_id)

        await channel.send(f"**Match #{match_id}:** {team1.get_name()} vs {team2.get_name()} is starting!")
        await self._openBetting(guild_id, channel)

    async def _handleSimultaneousResultReaction(self, payload, winning_team):
        guild_id = payload.guild_id
        self.cursor.execute(
            "SELECT id FROM tournament_matches WHERE guildId=? AND messageId=? "
            "AND state='AWAITING_RESULT' AND mode='simultaneous'",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        await self._resolveTournamentMatch(guild_id, row[0], winning_team, payload.channel_id)

    # Called from bot.py's on_raw_reaction_add. Dispatches ready-checks
    # (sequential mode) and direct result reports (simultaneous mode only
    # — sequential results resolve through handleWinnerReaction/recordResult
    # instead, same as any other game).
    async def handleTournamentReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)

        if emoji == TOURNAMENT_READY_EMOJI:
            await self._handleReadyReaction(payload)
            return

        winning_team = WINNER_EMOJIS.get(emoji)
        if winning_team is None:
            return
        await self._handleSimultaneousResultReaction(payload, winning_team)

    # Records the winner, advances the bracket (propagating the winning
    # team into the shared "next" node), prints the updated bracket, and
    # either starts the next queued match (sequential, round not done),
    # or moves on to the next round once every match in this one has
    # resolved. Shared by both modes — reached via recordResult's hook for
    # sequential, or directly from a result reaction for simultaneous.
    # Dispatches to the losers-bracket / Grand Finals equivalents below for
    # anything that isn't a winners-bracket match.
    async def _resolveTournamentMatch(self, guild_id, match_id, winning_team, channel_id):
        self.cursor.execute(
            "SELECT roundIndex, nodeIndex, mode, state, bracketType FROM tournament_matches WHERE id=?",
            (match_id,)
        )
        row = self.cursor.fetchone()
        if row is None or row[3] == "RESOLVED":
            return
        round_index, node_index, mode, _, bracket_type = row

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
            await self._resolveFinalsMatch(guild_id, tournament, match_id, round_index, winning_team, mode, channel)
            return

        if bracket_type == "losers":
            await self._resolveLosersMatch(
                guild_id, tournament, match_id, round_index, node_index, winning_team, mode, channel
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
            # loser to drop into the losers bracket — a bye pairing's
            # "winner" never played anyone, so node_a.next.drop_to (if
            # this is a double-elimination tournament) is simply left
            # unfilled, same as the equivalent losers-bracket slot.
            if loser_node.team is not None:
                node_a.next.loser = loser_node.team
                if node_a.next.drop_to is not None:
                    node_a.next.drop_to.team = loser_node.team
        self.saveTournament(guild_id, tournament)
        self._recordMatchResult(guild_id, winner_node.team, loser_node.team)

        await channel.send(f"**Match #{match_id} result:** {winner_node.team.get_name()} wins!")
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

        # Every match in this round is in — announce the round ending and
        # show the freshly-updated bracket before moving on. _startRound
        # (below) is what actually announces the champion once there's no
        # round left to start — this is purely the "round N is over"
        # transition message, distinct from the per-match update above.
        # No sleeps/blocking waits anywhere in this chain — reactions are
        # handled by discord.py as their own tasks, so a round transition
        # (even one that recurses through several bye rounds) never blocks
        # other users from placing bets or running other commands meanwhile.
        await channel.send(f"\U0001f3c1 **Round {round_index + 1} has ended!**")
        await self._sendBracketText(channel, tournament, guild_id)

        await self._startRound(guild_id, tournament, round_index + 1, mode, channel)

    # Mirrors the winners-bracket tail of _resolveTournamentMatch above,
    # for a losers-bracket match: propagate the winner into `.next`,
    # announce, and either advance to the round's next queued match or —
    # once the round's fully resolved — move on to the next losers round
    # (or Grand Finals, once there isn't one). A losers-bracket loser is
    # simply eliminated — nothing further to propagate for them.
    async def _resolveLosersMatch(
        self, guild_id, tournament, match_id, round_index, node_index, winning_team, mode, channel
    ):
        losers_nodes = tournament.get_losers_bracket_nodes()
        node_a = losers_nodes[node_index]
        node_b = node_a.opponent
        winner_node = node_a if winning_team == 1 else node_b
        loser_node = node_b if winning_team == 1 else node_a
        if node_a.next is not None:
            node_a.next.team = winner_node.team
        self.saveTournament(guild_id, tournament)
        self._recordMatchResult(guild_id, winner_node.team, loser_node.team)

        await channel.send(f"**Match #{match_id} result (losers bracket):** {winner_node.team.get_name()} wins!")
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

        await channel.send(f"\U0001f3c1 **Losers Bracket Round {round_index + 1} has ended!**")
        await self._sendBracketText(channel, tournament, guild_id)

        await self._startLosersRound(guild_id, tournament, round_index + 1, mode, channel)

    # Resolves a Grand Finals match. roundIndex 0 is the first game
    # (winners-bracket champion vs losers-bracket champion); if the
    # losers-bracket champion wins that one, both sides now have exactly
    # one loss, so a second, decider match (roundIndex 1) is posted instead
    # of ending the tournament — whoever wins THAT one is champion no
    # matter what.
    async def _resolveFinalsMatch(self, guild_id, tournament, match_id, round_index, winning_team, mode, channel):
        self.cursor.execute("SELECT team1, team2 FROM tournament_matches WHERE id=?", (match_id,))
        team1_ser, team2_ser = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        winner = team1 if winning_team == 1 else team2
        loser = team2 if winning_team == 1 else team1
        # Recorded here regardless of whether this is game 1 or the
        # decider — both are real, played matches, even when game 1's
        # result just leads into a reset rather than ending the tournament.
        self._recordMatchResult(guild_id, winner, loser)
        await self._settleMatchWagers(guild_id, match_id, winning_team, channel)

        if round_index == 0:
            # Compared by name, not id — see _tournamentChampionName.
            wb_rounds = self._bracketRounds(tournament.get_bracket())
            wb_champion = wb_rounds[-1][0].team
            if wb_champion is not None and winner.get_name() != wb_champion.get_name():
                await channel.send(
                    f"**Grand Finals result:** {winner.get_name()} wins! Since the winners-bracket "
                    f"champion has now lost once too, one final decider match settles the tournament."
                )
                await self._startGrandFinals(guild_id, tournament, mode, channel, reset=True)
                return

        await channel.send(
            f"\U0001f3c6 **{tournament.get_name()}** is complete! Champion: **{winner.get_name()}**"
        )
        # Every other match-resolution path (_resolveTournamentMatch,
        # _resolveLosersMatch) reprints the bracket after it updates —
        # Grand Finals resolving is exactly the same kind of update, and
        # skipping it here meant the last bracket image anyone saw was
        # whatever the losers bracket looked like before Grand Finals even
        # started, never showing the actual finals result.
        # _sendBracketText already knows how to post the Grand Finals image
        # too, once _renderGrandFinalsImage finds a resolved finals match.
        await self._sendBracketText(channel, tournament, guild_id)
        await self._postTournamentLeaderboard(channel, guild_id, tournament)

    # /report-correct-winner's match_id path: fixes a specific tournament
    # match's recorded winner, re-propagates the bracket, and — if anyone
    # had money on it — reverses the payouts _settleMatchWagers already made
    # against the wrong winner and reapplies them against the right one
    # (using the settledWagers snapshot _settleMatchWagers leaves behind,
    # since tournament_wagers' own rows are long gone by the time a match
    # is old enough to need correcting). Independent of the guild-wide
    # last_result correction (which only ever covers the single
    # most-recently-resolved team game). Refuses once the next round has
    # already started, rather than risk silently corrupting a bracket
    # that's already moved on.
    async def _correctTournamentMatchHelper(self, ctx, match_id, correct_team):
        guild_id = ctx.guild.id

        self.cursor.execute(
            "SELECT roundIndex, nodeIndex, state, winner, bracketType, settledWagers "
            "FROM tournament_matches WHERE guildId=? AND id=?",
            (guild_id, match_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await ctx.response.send_message(f"No tournament match with id {match_id} in this server.")
            return
        round_index, node_index, state, winner, bracket_type, settled_wagers_json = row

        if bracket_type != "winners":
            await ctx.response.send_message(
                f"Match #{match_id} is a {'losers bracket' if bracket_type == 'losers' else 'Grand Finals'} "
                f"match — correcting those isn't supported yet."
            )
            return

        if state != "RESOLVED":
            await ctx.response.send_message(f"Match #{match_id} hasn't been resolved yet.")
            return

        if winner == correct_team:
            await ctx.response.send_message(f"Match #{match_id} is already recorded as Team {correct_team}.")
            return

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=? AND bracketType='winners'",
            (guild_id, round_index + 1)
        )
        if self.cursor.fetchone()[0] > 0:
            await ctx.response.send_message(
                f"Can't correct Match #{match_id} — the next round has already started."
            )
            return

        tournament = self.getTournament(guild_id)
        if tournament is None:
            await ctx.response.send_message("This server's tournament no longer exists.")
            return

        bracket = tournament.get_bracket()
        node_a = bracket[node_index]
        node_b = node_a.opponent
        correct_winner_node = node_a if correct_team == 1 else node_b
        if node_a.next is not None:
            node_a.next.team = correct_winner_node.team
        self.saveTournament(guild_id, tournament)

        self.cursor.execute("UPDATE tournament_matches SET winner=? WHERE id=?", (correct_team, match_id))

        wager_note = ""
        if settled_wagers_json:
            wagers = json.loads(settled_wagers_json)
            self.applyGameDeltas(guild_id, self._matchWagerDeltas(wagers, winner), sign=-1)
            self.applyGameDeltas(guild_id, self._matchWagerDeltas(wagers, correct_team))
            wager_note = " Bet payouts on this match have been reversed and reapplied."

        self.db.commit()

        await ctx.response.send_message(
            f"Match #{match_id} corrected: **{correct_winner_node.team.get_name()}** actually won.{wager_note}"
        )
        await self._sendBracketText(ctx.channel, tournament, guild_id)

    # ---------------- Persistent teams ----------------

    # (team_id, Team) for the named team in this guild, or None. Team names
    # are unique per guild — enforced by createTeamHelper — so this is
    # always at most one row.
    def getTeamRow(self, guild_id, name):
        self.cursor.execute(
            "SELECT id, data FROM teams WHERE guildId=? AND name=?", (guild_id, name)
        )
        row = self.cursor.fetchone()
        if row is None:
            return None
        team_id, data = row
        team = Team()
        team.deserializeTeam(data)
        self._ensureLogo(team_id, team)
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
        return team

    def getTeamsForGuild(self, guild_id):
        self.cursor.execute("SELECT id, data FROM teams WHERE guildId=?", (guild_id,))
        teams = []
        for team_id, data in self.cursor.fetchall():
            team = Team()
            team.deserializeTeam(data)
            self._ensureLogo(team_id, team)
            teams.append((team_id, team))
        return teams

    # Every team in the guild `user_id` is a rostered player on (captain or
    # not) — what /my-teams pages through. Sorted by team_id so paging
    # stays stable across reactions even though this is recomputed fresh
    # from the DB on every page flip (see handleMyTeamsReaction), the same
    # way getLeaderboardEntries is recomputed fresh rather than snapshotted.
    def getTeamsForPlayer(self, guild_id, user_id):
        teams = self.getTeamsForGuild(guild_id)
        mine = [
            (team_id, team) for team_id, team in teams
            if any(player.get_id() == user_id for player in team.get_players())
        ]
        return sorted(mine, key=lambda entry: entry[0])

    # ---------------- /team-list ----------------

    # Every team in the guild, filtered/sorted for /team-list. `search` is a
    # case-insensitive substring match on the team's name; `recruiting_only`
    # keeps only teams that HAVE a target size (set via /team-create) and
    # haven't reached it yet — a team with no target size is an ephemeral
    # game-formation roster, never "recruiting" in the sense this filter
    # means. `sort`/`order` are always applied, even filtered down to
    # nothing, so a page-flip on an empty result still has a stable (if
    # empty) list to re-render instead of erroring.
    def _filterAndSortTeams(self, guild_id, search, recruiting_only, sort, order):
        teams = self.getTeamsForGuild(guild_id)

        if search:
            needle = search.lower()
            teams = [(team_id, team) for team_id, team in teams if needle in team.get_name().lower()]

        if recruiting_only:
            teams = [
                (team_id, team) for team_id, team in teams
                if team.get_team_size() is not None and team.get_size() < team.get_team_size()
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

    def _renderTeamListEmbed(self, guild_name, teams_sorted, search, recruiting_only, sort, order, page):
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
                f"**#{rank}.** {team.get_name()} — {roster_size} players | "
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
        filter_text = f" · {', '.join(active_filters)}" if active_filters else ""
        order_label = "Ascending" if order == "asc" else "Descending"
        embed.set_footer(
            text=f"Page {page + 1}/{total_pages} · Sorted by {TEAM_LIST_SORT_LABELS[sort]} "
                 f"({order_label}){filter_text}"
        )
        return embed

    # Posts the first page and pre-reacts with the paging emoji, same
    # pattern as leaderboardHelper/myTeamsHelper — clicking them
    # (handleTeamListReaction) edits this same message.
    async def teamListHelper(self, ctx, search, recruiting_only, sort, order):
        guild_id = ctx.guild.id

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order)
        if not teams_sorted:
            message = "No teams have been created in this server yet!" if not (search or recruiting_only) \
                else "No teams match those filters."
            await ctx.response.send_message(message)
            return

        embed = self._renderTeamListEmbed(
            ctx.guild.name, teams_sorted, search, recruiting_only, sort, order, page=0
        )
        await ctx.response.send_message(embed=embed)
        msg = await ctx.original_response()
        for emoji in LEADERBOARD_NAV_EMOJIS:
            await msg.add_reaction(emoji)

        self.cursor.execute(
            "INSERT OR REPLACE INTO team_list_views"
            "(messageId, guildId, channelId, search, recruitingOnly, sort, sort_order, page) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
            (msg.id, guild_id, ctx.channel.id, search, int(recruiting_only), sort, order)
        )
        self.db.commit()

    # Called from bot.py's on_raw_reaction_add — no-ops unless the emoji/
    # message match an active /team-list page view.
    async def handleTeamListReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        if emoji not in LEADERBOARD_NAV_EMOJIS:
            return

        self.cursor.execute(
            "SELECT channelId, search, recruitingOnly, sort, sort_order, page FROM team_list_views "
            "WHERE guildId=? AND messageId=?",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        channel_id, search, recruiting_only, sort, order, page = row
        recruiting_only = bool(recruiting_only)

        teams_sorted = self._filterAndSortTeams(guild_id, search, recruiting_only, sort, order)
        total_pages = self._teamListPageCount(teams_sorted)

        if emoji == LEADERBOARD_FIRST_EMOJI:
            new_page = 0
        elif emoji == LEADERBOARD_PREV_EMOJI:
            new_page = max(0, page - 1)
        elif emoji == LEADERBOARD_NEXT_EMOJI:
            new_page = min(total_pages - 1, page + 1)
        else:
            new_page = total_pages - 1

        if new_page == page:
            return

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        message = await channel.fetch_message(payload.message_id)

        guild = self.client.get_guild(guild_id)
        guild_name = guild.name if guild is not None else ""
        embed = self._renderTeamListEmbed(guild_name, teams_sorted, search, recruiting_only, sort, order, new_page)
        await message.edit(embed=embed)
        await self._clearPagingReaction(message, payload)

        self.cursor.execute(
            "UPDATE team_list_views SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, payload.message_id)
        )
        self.db.commit()

    # Every built-in logo's name (filename minus extension), e.g. "Demacia"
    # for assets/clash-logos/Demacia.png — what /team-set-logo's autocomplete
    # offers and validates against. Empty if the folder isn't there at all
    # (e.g. a dev checkout that never fetched it) rather than raising.
    def listAvailableLogos(self):
        if not os.path.isdir(TEAM_LOGO_DIR):
            return []
        names = [
            os.path.splitext(f)[0] for f in os.listdir(TEAM_LOGO_DIR)
            if os.path.isfile(os.path.join(TEAM_LOGO_DIR, f))
        ]
        return sorted(names)

    # Case-insensitive lookup from a logo's name back to its file path —
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

    # A team with no logo yet gets a random built-in one, persisted right
    # away — called everywhere a team is loaded (not just created), so a
    # team that predates this feature self-heals into having a logo the
    # next time it's touched instead of needing a one-off migration.
    # No-op if the assets folder is missing/empty; a team just stays
    # logo-less rather than this raising.
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

    # Records one played tournament match against each side's PERSISTENT
    # team record — the one /team-list, /my-teams, and /team-stats actually
    # read — called from every match-resolution path (winners bracket,
    # losers bracket, Grand Finals). Looked up by name rather than trusting
    # the bracket node's own embedded Team object: that's just a snapshot
    # from whenever the bracket was last serialized, not the live,
    # incrementally-updated row, so writing straight back through it would
    # silently lose whatever wins/losses had already accumulated since.
    # Either side can be None (a bracket node with no team — shouldn't
    # happen for a match that was ever actually queued, but this is cheap
    # insurance) or simply not a persisted team at all, in which case
    # there's nothing to record and this is a no-op.
    def _recordMatchResult(self, guild_id, winner_team, loser_team):
        for team, won in ((winner_team, True), (loser_team, False)):
            if team is None:
                continue
            result = self.getTeamRow(guild_id, team.get_name())
            if result is None:
                continue
            team_id, persisted_team = result
            if won:
                persisted_team.addWin()
            else:
                persisted_team.addLoss()
            self.updateTeamData(team_id, persisted_team)

    def isTeamCaptain(self, team, user_id):
        captain = team.get_captain()
        return isinstance(captain, Player) and captain.get_id() == user_id

    # Inserts `team`, then stamps the row's own autoincrement id back onto
    # the Team object and re-saves it — the DB row IS the team's id, so it
    # can't be known until after the INSERT.
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
    # sessions and is what /tournament-create's roster registration and
    # /team-invite work against.
    async def createTeamHelper(self, ctx, name, team_size):
        guild_id = ctx.guild.id

        if team_size <= 0:
            await ctx.response.send_message("Team size must be greater than 0.")
            return

        if self.getTeamRow(guild_id, name) is not None:
            await ctx.response.send_message(f"A team named **{name}** already exists in this server.")
            return

        team = Team()
        team.set_name(name)
        team.set_team_size(team_size)
        captain = Player(ctx.user.id, ctx.user.name)
        team.add_player(captain)
        team.set_captain(captain)

        self._saveNewTeam(guild_id, team)

        await ctx.response.send_message(
            f"Team **{name}** created! {ctx.user.mention} is the captain — looking for {team_size} player"
            f"{'s' if team_size != 1 else ''} total."
        )

    # Finds whichever OTHER team (if any) already has `channel_name` set as
    # its voice channel.
    def _findTeamUsingChannel(self, guild_id, channel_name, exclude_team_id):
        for team_id, team in self.getTeamsForGuild(guild_id):
            if team_id != exclude_team_id and team.get_voice_channel() == channel_name:
                return team
        return None

    # Sets (or creates) a team's voice channel. Only the team's captain can
    # do this. Passing no channel creates a brand new one named after the
    # team; passing one that's already assigned to a different team asks
    # for confirmation before reusing it, rather than silently doing it.
    async def setTeamVoiceChannelHelper(self, ctx, team_name, channel):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.")
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id):
            await ctx.response.send_message(f"Only **{team_name}**'s captain can set its voice channel.")
            return

        if channel is None:
            new_channel = await ctx.guild.create_voice_channel(team.get_name())
            team.set_voice_channel(new_channel)
            self.updateTeamData(team_id, team)
            await ctx.response.send_message(
                f"Created {new_channel.mention} and set it as **{team_name}**'s voice channel."
            )
            return

        conflicting = self._findTeamUsingChannel(guild_id, str(channel), team_id)
        if conflicting is not None:
            view = ConfirmVoiceChannelOverwriteView(self, guild_id, ctx.user.id, team_id, team_name, channel)
            await ctx.response.send_message(
                f"**{channel.name}** is already **{conflicting.get_name()}**'s voice channel. "
                f"Set it as **{team_name}**'s too?",
                view=view
            )
            view.message = await ctx.original_response()
            return

        team.set_voice_channel(channel)
        self.updateTeamData(team_id, team)
        await ctx.response.send_message(f"**{team_name}**'s voice channel is now {channel.mention}.")

    # Sets a team's logo to one of the built-in Clash logos (assets/clash-
    # logos) — captain-only, same as the voice-channel/invite commands.
    # `logo_name` is validated against listAvailableLogos() rather than
    # trusted outright, since a client can send an arbitrary string for a
    # slash command option even when it's autocomplete-backed.
    async def setTeamLogoHelper(self, ctx, team_name, logo_name):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.")
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id):
            await ctx.response.send_message(f"Only **{team_name}**'s captain can set its logo.")
            return

        logo_path = self._resolveLogoPath(logo_name)
        if logo_path is None:
            await ctx.response.send_message(
                f"No logo named **{logo_name}** — pick one from the autocomplete list."
            )
            return

        team.set_logo_path(logo_path)
        self.updateTeamData(team_id, team)

        logo_display_name = os.path.splitext(os.path.basename(logo_path))[0]
        await ctx.response.send_message(
            f"Set **{team_name}**'s logo to **{logo_display_name}**.",
            file=discord.File(logo_path)
        )

    # Invites `members` (one or more) to a team the caller captains — posts
    # a single message mentioning everyone valid and reacts once with
    # TEAM_INVITE_ACCEPT_EMOJI; each invited member only actually joins
    # once THEY react themselves (handleTeamInviteReaction), independently
    # of whether anyone else invited alongside them has. Bots, duplicates
    # (the same member passed more than once), and players already on the
    # team are filtered out rather than failing the whole command — with
    # exactly one member given, the old single-invite error messages are
    # preserved verbatim rather than folded into the multi-invite phrasing.
    async def teamInviteHelper(self, ctx, team_name, members):
        guild_id = ctx.guild.id

        result = self.getTeamRow(guild_id, team_name)
        if result is None:
            await ctx.response.send_message(f"No team named **{team_name}** in this server.")
            return
        team_id, team = result

        if not self.isTeamCaptain(team, ctx.user.id):
            await ctx.response.send_message(f"Only **{team_name}**'s captain can invite players.")
            return

        seen_ids = set()
        unique_members = []
        for member in members:
            if member.id in seen_ids:
                continue
            seen_ids.add(member.id)
            unique_members.append(member)

        rostered_ids = {player.get_id() for player in team.get_players()}
        valid, skipped = [], []
        for member in unique_members:
            if member.bot:
                skipped.append((member, "bot"))
            elif member.id in rostered_ids:
                skipped.append((member, "already on the team"))
            else:
                valid.append(member)

        if not valid:
            if len(unique_members) == 1:
                member, reason = skipped[0]
                if reason == "bot":
                    await ctx.response.send_message("You can't invite a bot to a team.")
                else:
                    await ctx.response.send_message(f"{member.display_name} is already on **{team_name}**.")
                return
            reasons = "; ".join(f"{member.display_name} ({reason})" for member, reason in skipped)
            await ctx.response.send_message(f"Nobody to invite — {reasons}.")
            return

        mentions = ", ".join(member.mention for member in valid)
        message = (
            f"{mentions}, {ctx.user.mention} invited you to join **{team_name}**! "
            f"React with {TEAM_INVITE_ACCEPT_EMOJI} to accept."
        )
        if skipped:
            reasons = "; ".join(f"{member.display_name} ({reason})" for member, reason in skipped)
            message += f"\n(Not invited: {reasons}.)"

        await ctx.response.send_message(message)
        msg = await ctx.original_response()
        await msg.add_reaction(TEAM_INVITE_ACCEPT_EMOJI)

        for member in valid:
            self.cursor.execute(
                "INSERT INTO team_invites"
                "(guildId, channelId, messageId, teamId, teamName, inviterId, targetId, targetName) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, ctx.channel.id, msg.id, team_id, team_name, ctx.user.id, member.id, member.name)
            )
        self.db.commit()

    # Called from bot.py's on_raw_reaction_add — no-ops unless the emoji/
    # message match a pending invite for the reactor specifically. Several
    # invitees can now share one messageId (one /team-invite call, several
    # people invited at once), so this is looked up by (messageId, the
    # reactor's own id) together rather than fetching whatever row happens
    # to come back first for the message and comparing targetId after —
    # each invitee accepting only ever resolves their OWN row.
    async def handleTeamInviteReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        if str(payload.emoji) != TEAM_INVITE_ACCEPT_EMOJI:
            return

        self.cursor.execute(
            "SELECT id, channelId, teamId, teamName, targetId, targetName "
            "FROM team_invites WHERE guildId=? AND messageId=? AND targetId=?",
            (guild_id, payload.message_id, payload.user_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        invite_id, channel_id, team_id, team_name, target_id, target_name = row

        # BUG-PRONE PATTERN AVOIDED: delete the invite before anything
        # async below, so a double-click can't add the player twice.
        self.cursor.execute("DELETE FROM team_invites WHERE id=?", (invite_id,))
        self.db.commit()

        team = self.getTeamById(guild_id, team_id)
        if team is None:
            return

        team.add_player(Player(target_id, target_name))
        self.updateTeamData(team_id, team)

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        await channel.send(f"**{target_name}** has joined **{team_name}**!")

    # Builds a team's stats embed — shared by /team-stats and /my-teams's
    # paging, so both stay in sync automatically. Returns (embed, file):
    # file is None whenever there's no logo to attach (the built-in set was
    # unavailable when _ensureLogo ran, or the file's since been removed
    # from disk) — send the embed without a thumbnail rather than erroring
    # on a discord.File() open that can't succeed.
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
            await ctx.response.send_message(f"No team named **{team_name}** in this server.")
            return
        _, team = result

        embed, file = self._renderTeamStatsEmbed(team)
        if file is not None:
            await ctx.response.send_message(embed=embed, file=file)
        else:
            await ctx.response.send_message(embed=embed)

    # ---------------- /my-teams ----------------

    # One team per "page" rather than a batch of rows like /leaderboard —
    # /my-teams is for flipping through each of YOUR teams' full stats
    # cards one at a time, not scanning a ranked list.
    def _myTeamsPageCount(self, teams):
        return max(1, len(teams))

    # Same embed /team-stats uses, plus a "Team X/N" footer so paging has
    # something to orient by (team-stats itself doesn't need one — there's
    # only ever the one team on screen there).
    def _renderMyTeamsEmbed(self, teams, page):
        team_id, team = teams[page]
        embed, file = self._renderTeamStatsEmbed(team)
        embed.set_footer(text=f"Team {page + 1}/{len(teams)}")
        return embed, file

    # Posts the caller's first team and pre-reacts with the same paging
    # emoji /leaderboard uses — clicking them (handleMyTeamsReaction) edits
    # this same message. Tracked by messageId+userId rather than just
    # messageId/guildId the way leaderboards is, since which teams a page
    # flip should show depends on who's paging, not just which server.
    # Clears the reactor's own click after a paginated view (/my-teams,
    # /leaderboard) advances, so they can press the same nav emoji again
    # immediately instead of having to un-react manually first. Tolerates
    # failure — removing someone ELSE's reaction needs Manage Messages,
    # and a server that hasn't granted the bot that permission shouldn't
    # make paging itself break; the click is just left uncleared.
    async def _clearPagingReaction(self, message, payload):
        try:
            await message.remove_reaction(payload.emoji, payload.member)
        except discord.HTTPException:
            pass

    async def myTeamsHelper(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            await ctx.response.send_message("You're not on any teams in this server.")
            return

        embed, file = self._renderMyTeamsEmbed(teams, page=0)
        if file is not None:
            await ctx.response.send_message(embed=embed, file=file)
        else:
            await ctx.response.send_message(embed=embed)
        msg = await ctx.original_response()
        for emoji in LEADERBOARD_NAV_EMOJIS:
            await msg.add_reaction(emoji)

        self.cursor.execute(
            "INSERT OR REPLACE INTO my_team_views(messageId, guildId, channelId, userId, page) "
            "VALUES(?, ?, ?, ?, 0)",
            (msg.id, guild_id, ctx.channel.id, user_id)
        )
        self.db.commit()

    # Called from bot.py's on_raw_reaction_add for every reaction — no-ops
    # unless the emoji/message match an active /my-teams page view. Only
    # the caller who posted it can page it — checked implicitly, since
    # this looks the view up by messageId and re-derives the team list from
    # the stored userId regardless of who actually clicked the reaction;
    # anyone else's click still moves the same shared view. That matches
    # how /leaderboard's paging already behaves (any reactor can page a
    # guild-wide view) — a personal view being paged by someone else just
    # steps through the OWNER's teams, not the clicker's.
    async def handleMyTeamsReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        if emoji not in LEADERBOARD_NAV_EMOJIS:
            return

        self.cursor.execute(
            "SELECT channelId, userId, page FROM my_team_views WHERE guildId=? AND messageId=?",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        channel_id, user_id, page = row

        teams = self.getTeamsForPlayer(guild_id, user_id)
        if not teams:
            return
        total_pages = self._myTeamsPageCount(teams)
        page = min(page, total_pages - 1)

        if emoji == LEADERBOARD_FIRST_EMOJI:
            new_page = 0
        elif emoji == LEADERBOARD_PREV_EMOJI:
            new_page = max(0, page - 1)
        elif emoji == LEADERBOARD_NEXT_EMOJI:
            new_page = min(total_pages - 1, page + 1)
        else:
            new_page = total_pages - 1

        if new_page == page:
            return

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        message = await channel.fetch_message(payload.message_id)

        embed, file = self._renderMyTeamsEmbed(teams, new_page)
        await message.edit(embed=embed, attachments=[file] if file is not None else [])
        await self._clearPagingReaction(message, payload)

        self.cursor.execute(
            "UPDATE my_team_views SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, payload.message_id)
        )
        self.db.commit()

    # Per-team win/loss record scoped to just THIS tournament, computed from
    # resolved tournament_matches rows rather than each team's own persisted
    # (all-time, cross-tournament) wins/losses. tournament_matches has no
    # tournamentId column, but doesn't need one here: a guild has exactly
    # one tournament at a time, and building a fresh bracket always clears
    # out the previous tournament's rows first (see
    # _clearTournamentMatchesForGuild) — so every row still in the table for
    # this guild belongs to THIS tournament. Keyed by team NAME (matching
    # how _recordMatchResult/getTeamRow already resolve a bracket team back
    # to its persisted row), seeded at 0-0 for every registered team so one
    # that never won a game still shows up instead of being left out.
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

    # The "tournament just finished" results embed — every team REGISTERED
    # FOR THIS TOURNAMENT, ranked by its record IN THIS TOURNAMENT (see
    # _tournamentTeamRecords). Deliberately distinct from
    # _renderTeamListEmbed (/team-list), which is server-wide
    # and all-time on purpose — this one exists specifically so a team that
    # played in a dozen past tournaments doesn't show up here with its
    # entire history, and a team that wasn't even in this one doesn't show
    # up at all.
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
            lines.append(f"**#{i}.** {team.get_name()} — {wins}W-{losses}L ({win_rate})")

        title = f"\U0001f3c6 {tournament.get_name()} Results"
        if guild_name:
            title += f" — {guild_name}"
        return discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())

    # Posts the tournament-scoped results embed right after a tournament
    # fully wraps up (both the single-elimination and double-elimination
    # "it's complete" messages call this). No-op if there are somehow no
    # registered teams — shouldn't happen right after a tournament
    # finishes, but _renderTournamentResultsEmbed already handles it
    # cleanly either way.
    async def _postTournamentLeaderboard(self, channel, guild_id, tournament):
        guild_name = channel.guild.name if channel.guild is not None else None
        embed = self._renderTournamentResultsEmbed(guild_id, tournament, guild_name)
        if embed is not None:
            await channel.send(embed=embed)

    # /test's per-match flavor: a handful of clearly-fake bettors wager on
    # one side or the other. Deliberately NOT the real wagers/economy
    # tables — those are built around a single guild-wide betting_state,
    # one active bet per user at a time (see wagerHelper), which can't
    # represent several tournament matches being open at once the way
    # simultaneous mode routinely has. Returns the wager list so
    # _postSimulatedPayout can settle the same bets once the match
    # resolves, without needing a second DB round-trip to reconstruct them.
    async def _postSimulatedWagers(self, match_id, channel):
        self.cursor.execute("SELECT team1, team2 FROM tournament_matches WHERE id=?", (match_id,))
        team1_ser, team2_ser = self.cursor.fetchone()
        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)

        bettors = random.sample(FAKE_BETTOR_NAMES, random.randint(2, min(5, len(FAKE_BETTOR_NAMES))))
        wagers = [(name, random.choice([1, 2]), random.choice(FAKE_WAGER_AMOUNTS)) for name in bettors]

        lines = [f"\U0001f3b2 **Betting on Match #{match_id}** ({team1.get_name()} vs {team2.get_name()}):"]
        for name, team_choice, amount in wagers:
            team_name = team1.get_name() if team_choice == 1 else team2.get_name()
            lines.append(f"{name} wagers {amount} gold on **{team_name}**")
        await channel.send("\n".join(lines))

        return wagers, team1, team2

    # Settles the wagers _postSimulatedWagers just posted, once the match's
    # real winner is known — same pari-mutuel formula computeGameDeltas
    # uses for real bets (winners split the losing pool proportional to
    # their own wager, on top of getting it back), just without an
    # economy table on the other end of it to actually credit.
    async def _postSimulatedPayout(self, channel, wagers, team1, team2, winning_team):
        winning_pool = sum(amount for _, team_choice, amount in wagers if team_choice == winning_team)
        losing_pool = sum(amount for _, team_choice, amount in wagers if team_choice != winning_team)
        winner_name = team1.get_name() if winning_team == 1 else team2.get_name()

        lines = [f"\U0001f4b0 **{winner_name}** won the bet — payouts:"]
        for name, team_choice, amount in wagers:
            if team_choice != winning_team:
                continue
            payout = round(amount + (amount / winning_pool) * losing_pool) if winning_pool > 0 else amount
            lines.append(f"{name} won {payout} gold (bet {amount})")
        if len(lines) == 1:
            lines.append("(nobody bet on the winning side!)")
        await channel.send("\n".join(lines))

    # /test's whole implementation — a full double-elimination tournament
    # run through the REAL pipeline (_startRound, _resolveTournamentMatch,
    # ...) instead of faking a result in memory. Every message that follows
    # is exactly what a real tournament posts: per-match results with
    # updated bracket images, round transitions, Grand Finals, the
    # completion announcement, and the team leaderboard (see
    # _resolveFinalsMatch) — a genuine, scrollable trace of a tournament in
    # chat. Winners are picked randomly and resolved directly instead of
    # waiting on reactions (_resolveTournamentMatch works from any
    # unresolved state, so there's no need to simulate a ready-check
    # reaction first), which is what makes it finish in seconds instead of
    # requiring real people to click through it.
    #
    # This DOES touch real data: it persists `teams` rows (named "TEST Team
    # N") and overwrites whatever tournament this server already has set up
    # with its own. Neither is cleaned up afterward — see /test in bot.py
    # for the caller-facing warning about that.
    async def runSimulatedTournamentHelper(self, ctx, teams, timing_value):
        guild_id = ctx.guild.id
        existing = self.getTournament(guild_id)

        # A previous /test run's fake teams are never cleaned up, so
        # without this a repeat run just adds another batch of
        # identically-named "TEST Team N" rows on top of the old ones —
        # both cluttering /team-leaderboard with stale entries and leaving
        # their old win/loss counts intact instead of starting fresh.
        # GLOB'd to the exact "TEST Team <number>" shape this generates
        # below (rather than a looser LIKE prefix match) so a real team a
        # user happened to name e.g. "TEST Team Alpha's Squad" can't get
        # caught up in it.
        self.cursor.execute(
            "DELETE FROM teams WHERE guildId=? AND name GLOB 'TEST Team [0-9]*'", (guild_id,)
        )
        self.db.commit()

        # 3 fake players per team (the first as captain) rather than empty
        # rosters — /test's whole point is showing what a real tournament
        # looks like end to end, and an empty roster meant the matchup
        # graphic's captain-first roster list (see _orderedRoster) never
        # actually had anything to demonstrate.
        fake_teams = []
        for i in range(teams):
            team = Team()
            team.set_name(f"TEST Team {i + 1}")
            team.set_team_size(3)
            for j in range(3):
                player = Player(1000000 + i * 10 + j, f"P{i + 1}-{j + 1}")
                team.add_player(player)
                if j == 0:
                    team.set_captain(player)
            self._saveNewTeam(guild_id, team)
            fake_teams.append(team)

        tournament = Tournament("TEST Tournament", 1, teams, double_elimination=True)
        for team in fake_teams:
            tournament.register_team(team)

        wb_nodes = self.buildBracket(fake_teams)
        lb_nodes, lb_rounds, lb_wb_dependency = self.buildLosersBracket(wb_nodes)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds, lb_wb_dependency)
        tournament.set_losers_bracket_timing(timing_value)

        # Same guard createBracketHelper uses when building a fresh
        # bracket: without it, a resolved Grand Finals row left over from a
        # previous /test run would make _tournamentChampionName think THIS
        # tournament already finished before a single match has been
        # played. Also resets Match #N back to #1 when it's safe to (see
        # _clearTournamentMatchesForGuild).
        self._clearTournamentMatchesForGuild(guild_id)
        self.saveTournament(guild_id, tournament)

        overwrite_note = (
            f"\n⚠️ This replaced **{existing.get_name()}**, which was already set up here."
            if existing is not None else ""
        )
        timing_note = (
            "losers bracket interleaved with the winners bracket"
            if timing_value == "interleaved" else "losers bracket after winners finishes"
        )
        await ctx.response.send_message(
            f"\U0001f9ea Running a {teams}-team double-elimination tournament ({timing_note}) through the real "
            f"pipeline — everything below is exactly what a live tournament posts, with results auto-picked "
            f"instead of waiting on reactions.{overwrite_note}"
        )

        await self._startRound(guild_id, tournament, 0, "simultaneous", ctx.channel)

        # Drives the tournament to completion: find whatever's currently
        # unresolved and resolve it with a coin flip, which — via
        # _resolveTournamentMatch's own cascade — queues up whatever comes
        # next (the rest of the round, the next round, the losers bracket,
        # Grand Finals, a bracket reset...) until nothing's left open.
        # Re-queried every pass rather than collected once up front, since
        # resolving the last open match of a round is exactly what creates
        # the next round's rows. The 500-iteration cap is a safety net, not
        # an expected outcome — even a 64-team bracket resolves in well
        # under 200.
        for _ in range(500):
            self.cursor.execute(
                "SELECT id FROM tournament_matches WHERE guildId=? AND state != 'RESOLVED'", (guild_id,)
            )
            open_ids = [row[0] for row in self.cursor.fetchall()]
            if not open_ids:
                break
            for match_id in open_ids:
                winning_team = random.choice([1, 2])
                # A handful of fake bettors wager on the match, then get
                # paid out once it resolves — same pari-mutuel math a real
                # bet uses (see _postSimulatedWagers), just entirely
                # separate from the real wagers/economy tables so it can't
                # collide with an actual game running elsewhere in this
                # server.
                wagers, team1, team2 = await self._postSimulatedWagers(match_id, ctx.channel)
                await self._resolveTournamentMatch(guild_id, match_id, winning_team, ctx.channel.id)
                await self._postSimulatedPayout(ctx.channel, wagers, team1, team2, winning_team)
        else:
            await ctx.channel.send("⚠️ Hit the safety cap before the simulated tournament finished.")

    # Loads two persistent teams straight into team1/team2 for a casual or
    # ranked game — the "quickly reuse a tournament team" path, skipping
    # /make-teams'/`/ranked`'s random-split-or-draft entirely. Same
    # "build the roster, then /start" contract as those commands: nobody
    # is moved and no elo/betting starts until /start is run.
    async def useTeamsHelper(self, ctx, team1_name, team2_name, ranked):
        guild_id = ctx.guild.id

        if team1_name == team2_name:
            await ctx.response.send_message("Pick two different teams.")
            return

        result1 = self.getTeamRow(guild_id, team1_name)
        if result1 is None:
            await ctx.response.send_message(f"No team named **{team1_name}** in this server.")
            return

        result2 = self.getTeamRow(guild_id, team2_name)
        if result2 is None:
            await ctx.response.send_message(f"No team named **{team2_name}** in this server.")
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

        ranked_note = " (ranked — elo will update when the winner is reported)" if ranked else ""
        await ctx.response.send_message(
            f"**{team1_name}** vs **{team2_name}** loaded{ranked_note}. "
            'Use "/start" when you\'re ready to move everyone and open betting.'
        )
        await self.printEmbed(ctx, team1, team2)

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
            await ctx.response.send_message(
                "You've already claimed your daily gold today! Come back tomorrow."
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

    # ---------------- Betting ----------------

    # Returns [(user_id, name), ...] for a team column ("team1"/"team2"), or
    # [] if that side hasn't been set up — never crashes on an unset column,
    # unlike Team().deserializeTeam(None/"").
    def getRosterPlayers(self, guild_id, column):
        serialized = self.get(guild_id, column)
        if not serialized:
            return []
        team = Team()
        team.deserializeTeam(serialized)
        return [(p.get_id(), p.get_name()) for p in team.get_players()]

    # True if `user_id` is a rostered player (either side) in the game
    # /start most recently moved into channels — used to stop players from
    # betting on their own game.
    def isPlayerInCurrentGame(self, guild_id, user_id):
        player_ids = {uid for uid, _name in self.getRosterPlayers(guild_id, "team1")}
        player_ids |= {uid for uid, _name in self.getRosterPlayers(guild_id, "team2")}
        return user_id in player_ids

    # Current elo for each (user_id, name) in `roster`, defaulting to
    # DEFAULT_ELO for anyone without an economy row yet.
    def getEloLookup(self, guild_id, roster):
        lookup = {}
        for user_id, _name in roster:
            elo = self.getEconomy(guild_id, user_id, "elo")
            lookup[user_id] = elo if elo is not None else DEFAULT_ELO
        return lookup

    # `match_id`, when given, bets on that ONE specific tournament match
    # (see _openConcurrentTournamentBetting) instead of the single current
    # casual/ranked/sequential-tournament game — a separate path
    # (_placeTournamentWager) since it's scoped by matchId in
    # `tournament_wagers` rather than the guild-wide `wagers` singleton.
    async def wagerHelper(self, ctx, amount: int, team: int, match_id: int = None):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        if amount <= 0:
            await ctx.response.send_message("Wager amount must be greater than 0.")
            return

        if match_id is not None:
            await self._placeTournamentWager(ctx, guild_id, user_id, amount, team, match_id)
            return

        state = self.get(guild_id, "betting_state")
        if state != "OPEN":
            await ctx.response.send_message(
                "Betting is not currently open. Use \"/start\" to start a game and open betting."
            )
            return

        if self.isPlayerInCurrentGame(guild_id, user_id):
            await ctx.response.send_message("You can't wager on a game you're playing in!")
            return

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        balance = self.getEconomy(guild_id, user_id, "balance")

        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}."
            )
            return

        self.cursor.execute(
            "SELECT team FROM wagers WHERE guildId=? AND userId=?", (guild_id, user_id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message("You've already placed a bet on this game.")
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

        await ctx.response.send_message(f"You wagered {amount} gold on Team {team}!")

    # wagerHelper's match_id path. Same shape as the block above it (state
    # check, self-bet guard, balance check, duplicate-bet guard, escrow,
    # insert) but scoped to one match instead of the whole guild — several
    # of these can be running at once for a simultaneous-mode round, each
    # independently.
    async def _placeTournamentWager(self, ctx, guild_id, user_id, amount, team, match_id):
        self.cursor.execute(
            "SELECT team1, team2, state, bettingClosed FROM tournament_matches WHERE id=? AND guildId=?",
            (match_id, guild_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            await ctx.response.send_message(f"No tournament match with id {match_id} in this server.")
            return
        team1_ser, team2_ser, state, betting_closed = row
        if state == "RESOLVED" or betting_closed:
            await ctx.response.send_message(f"Betting is closed for match #{match_id}.")
            return

        team1, team2 = Team(), Team()
        team1.deserializeTeam(team1_ser)
        team2.deserializeTeam(team2_ser)
        rostered_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        if user_id in rostered_ids:
            await ctx.response.send_message("You can't wager on a match you're playing in!")
            return

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)
        balance = self.getEconomy(guild_id, user_id, "balance")
        if amount > balance:
            await ctx.response.send_message(f"You don't have enough gold for that! Your balance is {balance}.")
            return

        self.cursor.execute(
            "SELECT team FROM tournament_wagers WHERE matchId=? AND userId=?", (match_id, user_id)
        )
        if self.cursor.fetchone() is not None:
            await ctx.response.send_message(f"You've already placed a bet on match #{match_id}.")
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

        await ctx.response.send_message(f"You wagered {amount} gold on Team {team} for match #{match_id}!")

    # Kicks off the betting window for the game that was just /start'd.
    async def startBettingHelper(self, ctx):
        await self._openBetting(ctx.guild.id, ctx.channel)

    # Posts the matchup graphic for whatever's currently loaded into
    # team1/team2 — used by /start, right as the match actually begins,
    # using whichever mode (/make-teams, /captains, /team-use, ranked or
    # not) most recently set them up.
    async def sendCurrentMatchupImage(self, ctx):
        team1 = Team()
        team1.deserializeTeam(self.get(ctx.guild.id, "team1"))
        team2 = Team()
        team2.deserializeTeam(self.get(ctx.guild.id, "team2"))
        label = self._matchupLabelForMode(self.get(ctx.guild.id, "mode"))
        await self._sendMatchupImage(ctx.channel, team1, team2, label)

    # This guild's own configured betting-window length (/set-betting-timer),
    # or BETTING_DURATION_SECONDS for a guild that's never set one. Doesn't
    # go through self.get() — that crashes outright if there's no `servers`
    # row for this guild at all, which a real guild always has by the time
    # any command can run (see on_guild_join), but /test's simulated
    # tournament has no reason to require one just to open a betting window.
    def _getBettingTimerSeconds(self, guild_id):
        self.cursor.execute("SELECT betting_timer_seconds FROM servers WHERE guildId=?", (guild_id,))
        row = self.cursor.fetchone()
        if row is None or row[0] is None:
            return BETTING_DURATION_SECONDS
        return int(row[0])

    # Core of the above, taking guild_id/channel directly rather than a
    # full Interaction — /tournament-start's sequential mode calls this
    # too, from a reaction handler that has no ctx to hand it. Cancels/
    # refunds any previous unresolved game first so re-opening never
    # leaves an orphaned timer or stranded bets behind.
    async def _openBetting(self, guild_id, channel):
        # /wager-set-channel redirects the whole cycle (open/closed/report)
        # there instead of wherever /start (or a tournament match) ran —
        # once betting_channel_id below points at it, everything
        # downstream (the timer, the winner report, recordResult) just
        # follows the same channel through naturally.
        wager_channel_name = self.get(guild_id, "wager_channel")
        if wager_channel_name:
            guild = self.client.get_guild(guild_id) if self.client is not None else None
            if guild is not None:
                resolved = discord.utils.get(guild.channels, name=wager_channel_name)
                if resolved is not None:
                    channel = resolved

        await self.cancelBettingHelper(guild_id, channel)

        self.update(guild_id, "betting_state", "OPEN")
        self.update(guild_id, "betting_message_id", None)
        self.update(guild_id, "betting_channel_id", channel.id)

        duration = self._getBettingTimerSeconds(guild_id)
        await channel.send(
            "🎲 Betting is now open! Use `/wager <amount> <team>` to bet on this game. "
            f"Betting closes in {duration} seconds."
        )

        # BUG-PRONE PATTERN AVOIDED: awaiting asyncio.sleep() directly inside
        # this command handler would still (technically) let other
        # interactions run, since asyncio.sleep() yields control. But it
        # would keep this command's own Interaction/task alive and blocked
        # for a full minute, and a cancelled game (/return) would have no
        # way to stop it from firing later. Running it as its own Task makes
        # both of those explicit and lets cancelBettingHelper cancel it.
        task = asyncio.create_task(self._bettingTimer(guild_id, channel, duration))
        self.bettingTasks[guild_id] = task

    async def _bettingTimer(self, guild_id, channel, duration):
        try:
            await asyncio.sleep(duration)

            self.update(guild_id, "betting_state", "CLOSED")
            await channel.send("🔒 Betting is now closed! No more wagers will be accepted for this game.")

            await asyncio.sleep(WINNER_REPORT_DELAY_SECONDS)

            msg = await channel.send(
                f"Which team won? React with {TEAM_EMOJIS[1]} for Team 1 or {TEAM_EMOJIS[2]} for Team 2 "
                f"to record the result and pay out bets."
            )
            await msg.add_reaction(TEAM_EMOJIS[1])
            await msg.add_reaction(TEAM_EMOJIS[2])

            self.update(guild_id, "betting_state", "AWAITING_RESULT")
            self.update(guild_id, "betting_message_id", msg.id)
            self.update(guild_id, "betting_channel_id", channel.id)
        except asyncio.CancelledError:
            # /return (or a fresh /start) cancelled the game before betting
            # closed or a winner was reported — cancelBettingHelper already
            # handles the refund, nothing more to do here.
            pass
        finally:
            self.bettingTasks.pop(guild_id, None)

    # Called from bot.py's on_raw_reaction_add. Resolves the winner from a
    # TEAM_EMOJIS reaction on the stored betting message and pays out bets.
    async def handleWinnerReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        winning_team = WINNER_EMOJIS.get(emoji)
        if winning_team is None:
            return

        state = self.get(guild_id, "betting_state")
        if state != "AWAITING_RESULT":
            return

        stored_message_id = self.get(guild_id, "betting_message_id")
        if stored_message_id is None or int(stored_message_id) != payload.message_id:
            return

        # BUG-PRONE PATTERN AVOIDED: flip the state before doing anything
        # async below, so a second reaction (e.g. both TEAM_EMOJIS clicked
        # near-simultaneously) can't also pass the check above and pay out
        # twice.
        self.update(guild_id, "betting_state", "NONE")

        channel = self.client.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(payload.channel_id)

        guild = self.client.get_guild(guild_id)

        await self.recordResult(guild_id, winning_team, channel, guild)

    # Pari-mutuel payout: winners split the losing side's pool proportional
    # to their own wager, on top of getting their own wager back — so a bet
    # on the less-backed (riskier) side pays out more than a bet on the
    # heavily-favored side. Also moves everyone back to the original
    # channel once the result is settled — reporting a winner ends the
    # game, so no separate /return is needed. `guild` is optional only so
    # callers/tests that don't care about the move can omit it.
    async def recordResult(self, guild_id, winning_team, channel, guild=None):
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
        elo_lookup = self.getEloLookup(guild_id, team1_roster + team2_roster)
        is_ranked = bool(self.get(guild_id, "is_ranked"))

        deltas, summary = self.computeGameDeltas(
            allWagers, team1_roster, team2_roster, elo_lookup, winning_team, is_ranked
        )
        self.applyGameDeltas(guild_id, deltas)
        self.saveLastResult(
            guild_id, winning_team, allWagers, team1_roster, team2_roster, deltas, is_ranked
        )

        await channel.send(self.formatResultMessage(winning_team, summary))

        if guild is not None and await self.moveMembersToOriginalChannel(guild):
            await channel.send("Moved everyone back to the original channel!")

        # /tournament-start (sequential mode) routes its matches through
        # this exact same betting/report cycle by temporarily setting
        # team1/team2 to the match's two teams — active_tournament_match_id
        # is how this function knows the game it just resolved was one of
        # those, so it can also advance the bracket once the normal
        # payout/elo handling above is done.
        active_match_id = self.get(guild_id, "active_tournament_match_id")
        if active_match_id is not None:
            self.update(guild_id, "active_tournament_match_id", None)
            await self._resolveTournamentMatch(guild_id, active_match_id, winning_team, channel.id)

    # ---------------- Duels (/wager-against) ----------------

    # Challenges `member` to a heads-up wager for `amount` gold, independent
    # of any team game — posts a message mentioning them and reacts with
    # DUEL_ACCEPT_EMOJI; the duel only actually escrows gold once they react
    # to accept (see handleDuelReaction), so nothing is held here.
    async def challengeDuelHelper(self, ctx, member, amount):
        guild_id = ctx.guild.id
        challenger = ctx.user

        if member.id == challenger.id:
            await ctx.response.send_message("You can't wager against yourself!")
            return

        if member.bot:
            await ctx.response.send_message("You can't wager against a bot!")
            return

        if amount <= 0:
            await ctx.response.send_message("Wager amount must be greater than 0.")
            return

        self.ensureEconomyRow(guild_id, challenger.id, challenger.name)
        balance = self.getEconomy(guild_id, challenger.id, "balance")
        if amount > balance:
            await ctx.response.send_message(
                f"You don't have enough gold for that! Your balance is {balance}."
            )
            return

        self.cursor.execute(
            "INSERT INTO duels(guildId, channelId, messageId, challengerId, challengerName, "
            "targetId, targetName, amount, state) VALUES(?, ?, NULL, ?, ?, ?, ?, ?, 'PENDING_ACCEPT')",
            (guild_id, ctx.channel.id, challenger.id, challenger.name, member.id, member.name, amount)
        )
        self.db.commit()
        duel_id = self.cursor.lastrowid

        await ctx.response.send_message(
            f"{member.mention}, {challenger.mention} has challenged you to a **{amount} gold** wager! "
            f"React with {DUEL_ACCEPT_EMOJI} to accept."
        )
        msg = await ctx.original_response()
        await msg.add_reaction(DUEL_ACCEPT_EMOJI)

        self.cursor.execute("UPDATE duels SET messageId=? WHERE id=?", (msg.id, duel_id))
        self.db.commit()

    # Called from bot.py's on_raw_reaction_add for every reaction — no-ops
    # immediately unless the emoji/message match a duel currently waiting on
    # exactly that reaction.
    async def handleDuelReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        if emoji not in (DUEL_ACCEPT_EMOJI, DUEL_CHALLENGER_EMOJI, DUEL_TARGET_EMOJI):
            return

        self.cursor.execute(
            "SELECT id, channelId, challengerId, challengerName, targetId, targetName, amount, state "
            "FROM duels WHERE guildId=? AND messageId=?",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        duel_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount, state = row

        if emoji == DUEL_ACCEPT_EMOJI:
            # Only the challenged player can accept their own challenge.
            if state != "PENDING_ACCEPT" or payload.user_id != target_id:
                return
            await self._acceptDuel(
                guild_id, duel_id, channel_id, challenger_id, challenger_name, target_id, target_name, amount
            )
            return

        if state != "AWAITING_RESULT":
            return
        winner_is_challenger = emoji == DUEL_CHALLENGER_EMOJI
        await self._resolveDuel(
            guild_id, duel_id, channel_id, challenger_id, challenger_name,
            target_id, target_name, amount, winner_is_challenger
        )

    # Escrows `amount` from both players and posts the win/loss report
    # message, pre-reacted with the blue/red circle choices.
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
                f"Wager cancelled — one of you no longer has {amount} gold to cover it."
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
            f"\U0001f4b0 **{challenger_name}** vs **{target_name}** — {pot} gold on the line! "
            f"React with {DUEL_CHALLENGER_EMOJI} if {challenger_name} won, "
            f"or {DUEL_TARGET_EMOJI} if {target_name} won."
        )
        await msg.add_reaction(DUEL_CHALLENGER_EMOJI)
        await msg.add_reaction(DUEL_TARGET_EMOJI)

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
        # async below, so a second near-simultaneous reaction can't also
        # pass the AWAITING_RESULT check and pay out twice.
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

    # Pari-mutuel betting payouts (winners split the losing side's pool
    # proportional to their own wager, on top of getting their own wager
    # back) plus a simple team-average elo update for whoever was actually
    # rostered on team1/team2. Pure computation — no DB writes — so the
    # exact same result can be applied once by recordResult and later
    # reversed/reapplied by reportCorrectWinnerHelper without re-deriving
    # the math (which would go wrong once elo ratings have moved on).
    #
    # Returns (deltas, summary):
    #   deltas: user_id -> {username, balance, wins, losses, gold_wagered,
    #           gold_won, gold_lost, game_wins, game_losses, ranked_wins,
    #           ranked_losses, elo} — all values are deltas to ADD to that
    #           user's economy row. ranked_wins/ranked_losses are the
    #           RANKED subset of game_wins/game_losses (0 for a casual
    #           game) — a casual win/loss count is just game_wins minus
    #           ranked_wins (see getLeaderboardEntries), so there's nothing
    #           separate to track for that side.
    #   summary: display-only info for formatResultMessage().
    def computeGameDeltas(self, wagers, team1_roster, team2_roster, elo_lookup, winning_team, is_ranked=False):
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

        for user_id, username, _team, amount in losingBets:
            bump(user_id, username, losses=1, gold_wagered=amount, gold_lost=amount)

        winning_bettors = []
        for user_id, username, _team, amount in winningBets:
            payout = round(amount + (amount / winningPool) * losingPool) if winningPool > 0 else amount
            bump(user_id, username, balance=payout, wins=1, gold_wagered=amount, gold_won=payout - amount)
            winning_bettors.append((username, payout, amount))

        # Game record (game_wins/game_losses) is tracked for every reported
        # game regardless of ranked status. Elo is not — it's exclusive to
        # games started with ranked:true (is_ranked=True),
        # so a casual /make-teams or /captains game never moves anyone's
        # rating.
        elo_changes = []
        if team1_roster or team2_roster:
            elo_delta1 = elo_delta2 = 0
            if is_ranked:
                team1_elos = [elo_lookup.get(uid, DEFAULT_ELO) for uid, _name in team1_roster]
                team2_elos = [elo_lookup.get(uid, DEFAULT_ELO) for uid, _name in team2_roster]
                team1_avg = sum(team1_elos) / len(team1_elos) if team1_elos else DEFAULT_ELO
                team2_avg = sum(team2_elos) / len(team2_elos) if team2_elos else DEFAULT_ELO

                expected1 = 1 / (1 + 10 ** ((team2_avg - team1_avg) / 400))
                actual1 = 1 if winning_team == 1 else 0
                elo_delta1 = round(ELO_K_FACTOR * (actual1 - expected1))
                elo_delta2 = round(ELO_K_FACTOR * ((1 - actual1) - (1 - expected1)))

            for user_id, username in team1_roster:
                bump(
                    user_id, username, elo=elo_delta1,
                    game_wins=1 if winning_team == 1 else 0,
                    game_losses=0 if winning_team == 1 else 1,
                    ranked_wins=(1 if winning_team == 1 else 0) if is_ranked else 0,
                    ranked_losses=(0 if winning_team == 1 else 1) if is_ranked else 0,
                )
            for user_id, username in team2_roster:
                bump(
                    user_id, username, elo=elo_delta2,
                    game_wins=1 if winning_team == 2 else 0,
                    game_losses=0 if winning_team == 2 else 1,
                    ranked_wins=(1 if winning_team == 2 else 0) if is_ranked else 0,
                    ranked_losses=(0 if winning_team == 2 else 1) if is_ranked else 0,
                )

            if is_ranked:
                if team1_roster:
                    elo_changes.append(("Team 1", elo_delta1))
                if team2_roster:
                    elo_changes.append(("Team 2", elo_delta2))

        summary = {
            "no_bets": not wagers,
            "no_winning_bets": bool(wagers) and not winning_bettors,
            "winning_bettors": winning_bettors,
            "elo_changes": elo_changes,
        }
        return deltas, summary

    # Applies (sign=1) or reverses (sign=-1) a deltas dict from
    # computeGameDeltas() against every affected player's economy row.
    def applyGameDeltas(self, guild_id, deltas, sign=1):
        for user_id, d in deltas.items():
            self.ensureEconomyRow(guild_id, user_id, d["username"])
            self.cursor.execute(
                "UPDATE economy SET balance = balance + ?, wins = wins + ?, losses = losses + ?, "
                "gold_wagered = gold_wagered + ?, gold_won = gold_won + ?, gold_lost = gold_lost + ?, "
                "game_wins = game_wins + ?, game_losses = game_losses + ?, "
                "ranked_wins = ranked_wins + ?, ranked_losses = ranked_losses + ?, elo = elo + ? "
                "WHERE guildId=? AND userId=?",
                (
                    sign * d["balance"], sign * d["wins"], sign * d["losses"],
                    sign * d["gold_wagered"], sign * d["gold_won"], sign * d["gold_lost"],
                    sign * d["game_wins"], sign * d["game_losses"],
                    sign * d["ranked_wins"], sign * d["ranked_losses"], sign * d["elo"],
                    guild_id, user_id,
                )
            )
        self.db.commit()

    def formatResultMessage(self, winning_team, summary):
        lines = [f"**Team {winning_team}** wins!"]

        if summary["no_bets"]:
            lines.append("No bets were placed on this game.")
        elif summary["no_winning_bets"]:
            lines.append("Nobody bet on the winning team — all bets were lost.")
        else:
            lines.append("Paying out bets...")
            for username, payout, amount in summary["winning_bettors"]:
                lines.append(f"{username} won {payout} gold (bet {amount})")

        if summary["elo_changes"]:
            lines.append(
                "Elo: " + ", ".join(f"{name} {delta:+d}" for name, delta in summary["elo_changes"])
            )

        return "\n".join(lines)

    # Snapshots exactly what was applied for a resolved game — the wagers,
    # both rosters, and the deltas computeGameDeltas() produced — so
    # reportCorrectWinnerHelper can reverse it precisely later. One row per
    # guild; a new result overwrites the previous snapshot.
    def saveLastResult(self, guild_id, winning_team, wagers, team1_roster, team2_roster, deltas, is_ranked=False):
        payload = {
            "winning_team": winning_team,
            "wagers": [list(w) for w in wagers],
            "team1_roster": [list(p) for p in team1_roster],
            "team2_roster": [list(p) for p in team2_roster],
            "deltas": {str(uid): d for uid, d in deltas.items()},
            "is_ranked": is_ranked,
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
        return payload

    # Admin correction for a misreported /start winner: undoes exactly what
    # was applied for the last resolved game in this guild (bet payouts,
    # win/loss records, elo) and re-applies the same wagers/rosters against
    # the corrected winner. Elo is recomputed rather than reused, since
    # after undoing the wrong result each player's rating is back to its
    # pre-match value — recomputing against that gives the correct
    # alternate-history rating, not a stale or double-applied one.
    #
    # match_id, when given, corrects a specific tournament match instead —
    # a separate, narrower path (see _correctTournamentMatchHelper) that
    # only touches that match's bracket node, not the guild-wide economy
    # snapshot below.
    async def reportCorrectWinnerHelper(self, ctx, correct_team, match_id=None):
        if match_id is not None:
            await self._correctTournamentMatchHelper(ctx, match_id, correct_team)
            return

        guild_id = ctx.guild.id
        last = self.getLastResult(guild_id)

        if last is None:
            await ctx.response.send_message("There's no recent game result to correct.")
            return

        if last["winning_team"] == correct_team:
            await ctx.response.send_message(
                f"Team {correct_team} is already the recorded winner — nothing to correct."
            )
            return

        self.applyGameDeltas(guild_id, last["deltas"], sign=-1)

        team1_roster = last["team1_roster"]
        team2_roster = last["team2_roster"]
        is_ranked = last["is_ranked"]
        elo_lookup = self.getEloLookup(guild_id, team1_roster + team2_roster)
        new_deltas, summary = self.computeGameDeltas(
            last["wagers"], team1_roster, team2_roster, elo_lookup, correct_team, is_ranked
        )
        self.applyGameDeltas(guild_id, new_deltas)
        self.saveLastResult(
            guild_id, correct_team, last["wagers"], team1_roster, team2_roster, new_deltas, is_ranked
        )

        await ctx.response.send_message(
            f"Correction recorded: **Team {correct_team}** actually won (previously recorded as "
            f"Team {last['winning_team']}). Balances, records, and elo have been adjusted."
        )
        await ctx.channel.send(self.formatResultMessage(correct_team, summary))

    async def statsHelper(self, ctx, member=None):
        target = member if member is not None else ctx.user
        guild_id = ctx.guild.id
        user_id = target.id

        self.ensureEconomyRow(guild_id, user_id, target.name)

        self.cursor.execute(
            "SELECT balance, wins, losses, gold_wagered, gold_won, gold_lost, "
            "game_wins, game_losses, ranked_wins, ranked_losses, elo FROM economy WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        (balance, bet_wins, bet_losses, gold_wagered, gold_won, gold_lost, game_wins, game_losses,
         ranked_wins, ranked_losses, elo) = self.cursor.fetchone()

        net_gold = gold_won - gold_lost

        bet_games = bet_wins + bet_losses
        bet_win_rate = f"{(bet_wins / bet_games) * 100:.1f}%" if bet_games > 0 else "N/A"

        games_played = game_wins + game_losses
        game_win_rate = f"{(game_wins / games_played) * 100:.1f}%" if games_played > 0 else "N/A"

        # casual = the non-ranked slice of game_wins/game_losses — see
        # getLeaderboardEntries's identical derivation.
        casual_wins = game_wins - ranked_wins
        casual_losses = game_losses - ranked_losses
        casual_games = casual_wins + casual_losses
        ranked_games = ranked_wins + ranked_losses
        casual_win_rate = f"{(casual_wins / casual_games) * 100:.1f}%" if casual_games > 0 else "N/A"
        ranked_win_rate = f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A"

        elo_rank = self.eloRankLabel(elo)

        embed = discord.Embed(
            title=f"{target.display_name}'s Stats", color=discord.Color.gold()
        )
        # display_avatar (not the possibly-None .avatar) always resolves to
        # something — the member's own custom avatar if they have one, or
        # Discord's default avatar for their account otherwise.
        # BUG FIX: an animated (GIF) avatar's .url pointed at the .gif
        # asset, which Discord's embed thumbnail slot doesn't reliably
        # unfurl — the thumbnail just silently failed to attach at all.
        # with_format("png") forces a static snapshot for animated avatars
        # too (a no-op for already-static ones), trading the animation for
        # actually showing up every time.
        embed.set_thumbnail(url=target.display_avatar.with_format("png").url)
        # Exactly 3 inline fields per row (Discord wraps at 3), grouped
        # ranked / casual+bet / gold top to bottom, with nothing left over
        # to force a row break with — a blank spacer field looks like a
        # good way to end a short row early, but it still renders its own
        # (invisible) name+value line and shows up as a big empty gap
        # instead of a clean break. Elo joins the ranked row (rather than
        # being merged into a record field like the others) specifically
        # to round that row out to 3 — Game/Casual/Bet Record fold their
        # win rate into the same field (see the comment on those below),
        # so 3 fields already covers all of them without needing a filler.
        embed.add_field(name="Elo", value=f"{elo} ({elo_rank})", inline=True)
        embed.add_field(name="Ranked Wins", value=f"{ranked_wins}W - {ranked_losses}L", inline=True)
        embed.add_field(name="Ranked Win Rate", value=ranked_win_rate, inline=True)
        # Record and win rate folded into one field each here (rather than
        # two separate ones) so a pair can't straddle a row boundary the
        # way splitting them would risk.
        embed.add_field(name="Game Record", value=f"{game_wins}W - {game_losses}L ({game_win_rate})", inline=True)
        embed.add_field(
            name="Casual Record", value=f"{casual_wins}W - {casual_losses}L ({casual_win_rate})", inline=True
        )
        embed.add_field(name="Bet Record", value=f"{bet_wins}W - {bet_losses}L ({bet_win_rate})", inline=True)
        embed.add_field(name="Balance", value=f"{balance} gold", inline=True)
        embed.add_field(name="Net Gold Won/Lost", value=f"{net_gold:+d} gold", inline=True)
        embed.add_field(name="Gold Wagered", value=str(gold_wagered), inline=True)

        await ctx.response.send_message(embed=embed)
        msg = await ctx.original_response()
        await msg.add_reaction(STATS_PLACEHOLDER_EMOJI)
        await msg.add_reaction(STATS_CARD_EMOJI)

        self.cursor.execute(
            "INSERT OR REPLACE INTO stats_views(messageId, guildId, targetUserId, cardShown) "
            "VALUES(?, ?, ?, 0)",
            (msg.id, guild_id, user_id)
        )
        self.db.commit()

    # Resolves `user_id` to a live discord.Member of `guild_id` — cache
    # first, then a real API fetch if they're not cached — or None if they
    # can't be resolved at all (left the guild, or some other API hiccup).
    # Shared by the avatar toggle and the trading card, both of which need
    # to look someone back up well after /stats itself first ran.
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

    # The real avatar half of handleStatsReaction's toggle — re-fetched
    # live (rather than snapshotted at /stats time) so a player who
    # changes their avatar later and toggles back off the placeholder sees
    # their current one, same as a fresh /stats would. None if the member
    # can't be resolved at all — the caller just leaves the placeholder
    # showing rather than erroring out over what's ultimately a cosmetic
    # toggle.
    async def _resolveMemberAvatarUrl(self, guild_id, user_id):
        member = await self._resolveGuildMember(guild_id, user_id)
        if member is None:
            return None
        return member.display_avatar.with_format("png").url

    # Converts a "#RRGGBB" hex string (trading_cards' own storage format —
    # portable and human-editable, unlike a raw RGB tuple) back to the
    # (r, g, b) tuple PIL wants. Falls back to `fallback` for anything that
    # doesn't parse — a hand-edited or otherwise corrupted value shouldn't
    # take card rendering down with it.
    def _hexToRgb(self, hex_color, fallback):
        if not isinstance(hex_color, str) or len(hex_color) != 7 or not hex_color.startswith("#"):
            return fallback
        try:
            return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return fallback

    # A lighter shade of `color`, blended toward white by `amount` (0-1) —
    # used to turn a single customizable background_color into the
    # matching (center, edge) pair _createBracketCanvas's vignette wants,
    # without needing a second color column just for that.
    def _lightenColor(self, color, amount):
        return tuple(round(c + (255 - c) * amount) for c in color)

    # Resolves a trading_cards.font_style key to the actual bundled font
    # files to use — (display font for the name, subtitle font for the
    # title/epithet, body font for the stat labels/values). Only
    # "default" (Shockwave's own Chakra Petch + IBM Plex Sans pairing)
    # exists today; anything else falls back to it rather than erroring,
    # the same "unknown preset degrades to the default" approach
    # _hexToRgb takes for a bad color.
    def _cardFontPaths(self, font_style):
        return (CHAKRA_PETCH_BOLD, CHAKRA_PETCH_SEMIBOLD, IBM_PLEX_SANS)

    # A player's trading_cards row, created with Shockwave's own defaults
    # (CARD_DEFAULT_*) the first time it's needed — same self-healing
    # "insert if missing, read either way" shape ensureEconomyRow uses,
    # so a card can be customized (by hand in the database today; a future
    # /card-customize-style command could write the same columns) without
    # ever needing a one-off migration for players who predate that.
    def ensureCardSettings(self, guild_id, user_id):
        self.cursor.execute(
            "INSERT OR IGNORE INTO trading_cards"
            "(guildId, userId, title, accent_color, background_color, text_color, font_style) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, user_id, CARD_DEFAULT_TITLE, CARD_DEFAULT_ACCENT_COLOR,
                CARD_DEFAULT_BACKGROUND_COLOR, CARD_DEFAULT_TEXT_COLOR, CARD_DEFAULT_FONT_STYLE,
            )
        )
        self.db.commit()

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

    # Pure rendering: a portrait trading card built entirely from already-
    # fetched data (no DB/network access here — see _swapStatsForTradingCard
    # for the async half that gathers all of this). `avatar_image` is a
    # already-opened PIL image (the player's real avatar, or a plain
    # fallback tile if it couldn't be fetched); `settings` is a
    # getCardSettings()-shaped dict; `stats` is
    # {balance, game_wins, game_losses, elo, elo_rank}; `team_names` is
    # every persistent team (see getTeamsForPlayer) this player is
    # rostered on in this guild, most relevant first.
    def _renderTradingCardImage(self, guild_name, display_name, avatar_image, settings, stats, team_names):
        accent_color = self._hexToRgb(settings["accent_color"], BRACKET_TITLE_COLOR)
        text_color = self._hexToRgb(settings["text_color"], BRACKET_TEXT_COLOR)
        background_color = self._hexToRgb(settings["background_color"], BRACKET_BACKGROUND)
        name_font_path, title_font_path, body_font_path = self._cardFontPaths(settings["font_style"])

        name_font = self._loadFont(name_font_path, CARD_NAME_FONT_SIZE)
        title_font = self._loadFont(title_font_path, CARD_TITLE_FONT_SIZE)
        label_font = self._loadFont(body_font_path, CARD_STAT_LABEL_FONT_SIZE, "Regular")
        value_font = self._loadFont(body_font_path, CARD_STAT_VALUE_FONT_SIZE, "SemiBold")

        header_height = self._bracketHeaderHeight(None)
        avatar_top = header_height + BRACKET_PADDING * 2
        avatar_cx = CARD_WIDTH / 2
        name_y = avatar_top + CARD_AVATAR_SIZE + BRACKET_PADDING * 2
        title_y = name_y + CARD_NAME_FONT_SIZE + BRACKET_PADDING
        rule_y = title_y + CARD_TITLE_FONT_SIZE + BRACKET_PADDING * 2
        stats_top = rule_y + BRACKET_PADDING * 2
        stats_bottom = stats_top + CARD_STAT_ROW_HEIGHT
        team_y = stats_bottom + BRACKET_PADDING * 2
        height = int((team_y + CARD_STAT_LABEL_FONT_SIZE if team_names else stats_bottom) + BRACKET_MARGIN)

        # background_color is the one customizable color, standing in for
        # the vignette's "edge" shade — _lightenColor derives a matching
        # lighter "center" from it, the same relationship
        # BRACKET_BACKGROUND_CENTER has to BRACKET_BACKGROUND by default.
        background_center = self._lightenColor(background_color, 0.3)
        image, draw = self._createBracketCanvas(
            CARD_WIDTH, height, accent_color, background=background_color, background_center=background_center
        )
        self._drawBracketHeader(image, draw, guild_name, None, accent_color, CARD_WIDTH, bold_title=True)

        # Avatar: circular crop via a mask (paste() only respects alpha on
        # the SOURCE image being pasted, hence converting to RGBA first),
        # ringed in the card's accent color.
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

        stat_entries = [
            ("ELO", f"{stats['elo']} ({stats['elo_rank']})"),
            ("RECORD", f"{stats['game_wins']}W - {stats['game_losses']}L"),
            ("GOLD", str(stats['balance'])),
        ]
        col_width = CARD_WIDTH / len(stat_entries)
        for i, (label, value) in enumerate(stat_entries):
            cx = col_width * i + col_width / 2
            draw.text((cx, stats_top), label, font=label_font, fill=BRACKET_LINE_COLOR, anchor="ma")
            draw.text(
                (cx, stats_top + CARD_STAT_LABEL_FONT_SIZE + BRACKET_PADDING / 2), value, font=value_font,
                fill=text_color, anchor="ma"
            )

        if team_names:
            shown = ", ".join(team_names[:2])
            if len(team_names) > 2:
                shown += f" +{len(team_names) - 2}"
            label = "Teams: " if len(team_names) > 1 else "Team: "
            draw.text((avatar_cx, team_y), label + shown, font=label_font, fill=BRACKET_LINE_COLOR, anchor="ma")

        return image

    # The async half of the trading card: gathers everything
    # _renderTradingCardImage needs (a live member for the avatar/display
    # name, fresh economy stats, persistent teams, and card_settings) and
    # posts the result in place of the /stats embed. A missing/unfetchable
    # avatar falls back to a plain tile rather than failing the whole card
    # over one image request.
    async def _swapStatsForTradingCard(self, message, guild_id, guild_name, target_user_id):
        member = await self._resolveGuildMember(guild_id, target_user_id)
        display_name = member.display_name if member is not None else f"Player {target_user_id}"

        self.ensureEconomyRow(guild_id, target_user_id, display_name)
        self.cursor.execute(
            "SELECT balance, game_wins, game_losses, elo FROM economy WHERE guildId=? AND userId=?",
            (guild_id, target_user_id)
        )
        balance, game_wins, game_losses, elo = self.cursor.fetchone()
        stats = {
            "balance": balance, "game_wins": game_wins, "game_losses": game_losses,
            "elo": elo, "elo_rank": self.eloRankLabelPlain(elo),
        }

        team_names = [team.get_name() for _, team in self.getTeamsForPlayer(guild_id, target_user_id)]
        settings = self.getCardSettings(guild_id, target_user_id)

        avatar_image = None
        if member is not None:
            try:
                avatar_bytes = await member.display_avatar.with_format("png").read()
                avatar_image = Image.open(io.BytesIO(avatar_bytes))
            except Exception:
                avatar_image = None
        if avatar_image is None:
            avatar_image = Image.new("RGBA", (CARD_AVATAR_SIZE, CARD_AVATAR_SIZE), BRACKET_BACKGROUND_CENTER)

        card_image = self._renderTradingCardImage(guild_name, display_name, avatar_image, settings, stats, team_names)
        file = self._imageToFile(card_image, "trading_card.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url=f"attachment://{file.filename}")
        await message.edit(embed=embed, attachments=[file])

    # Called from bot.py's on_raw_reaction_add for every reaction — no-ops
    # unless the emoji/message match a /stats embed still tracked in
    # stats_views. STATS_CARD_EMOJI hands off to _swapStatsForTradingCard
    # above and marks cardShown so the avatar toggle refuses to run on this
    # message afterward (a trading card isn't shaped like a normal /stats
    # embed, so toggling its "thumbnail" would just corrupt it).
    # STATS_PLACEHOLDER_EMOJI toggles the thumbnail based on whichever's
    # currently showing (comparing against STATS_PLACEHOLDER_AVATAR_URL
    # exactly, set by this same handler or by statsHelper's own
    # real-avatar URL) — leaving everything else on the embed untouched
    # either way.
    async def handleStatsReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        if emoji not in (STATS_PLACEHOLDER_EMOJI, STATS_CARD_EMOJI):
            return

        self.cursor.execute(
            "SELECT targetUserId, cardShown FROM stats_views WHERE guildId=? AND messageId=?",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        target_user_id, card_shown = row

        if emoji == STATS_PLACEHOLDER_EMOJI and card_shown:
            return

        channel = self.client.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if not message.embeds:
            return

        if emoji == STATS_CARD_EMOJI:
            guild_name = channel.guild.name if channel.guild is not None else ""
            await self._swapStatsForTradingCard(message, guild_id, guild_name, target_user_id)
            self.cursor.execute(
                "UPDATE stats_views SET cardShown=1 WHERE guildId=? AND messageId=?",
                (guild_id, payload.message_id)
            )
            self.db.commit()
            # The avatar toggle no longer applies to this message at all
            # once it's a trading card — remove it outright rather than
            # leaving a reaction sitting there that just silently no-ops
            # when clicked. Tolerates missing Manage Messages the same way
            # _clearPagingReaction does; the cardShown check above is what
            # actually enforces the disable either way.
            try:
                await message.clear_reaction(STATS_PLACEHOLDER_EMOJI)
            except discord.HTTPException:
                pass
            await self._clearPagingReaction(message, payload)
            return

        embed = message.embeds[0]
        currently_placeholder = (
            embed.thumbnail is not None and embed.thumbnail.url == STATS_PLACEHOLDER_AVATAR_URL
        )

        if currently_placeholder:
            new_url = await self._resolveMemberAvatarUrl(guild_id, target_user_id)
            if new_url is None:
                return
        else:
            new_url = STATS_PLACEHOLDER_AVATAR_URL

        embed.set_thumbnail(url=new_url)
        await message.edit(embed=embed)
        await self._clearPagingReaction(message, payload)

    # ---------------- Leaderboard ----------------

    # One dict per player with an economy row in this guild — raw columns
    # plus the same computed rates/totals /stats shows (win rates, net
    # gold), so every LEADERBOARD_STAT_LABELS key is directly readable off
    # each entry with entry[stat]. Win rates are None (not 0) when a player
    # has no games/bets yet, so they can sort to the bottom instead of
    # looking like the worst possible rate.
    def getLeaderboardEntries(self, guild_id):
        self.cursor.execute(
            "SELECT userId, username, balance, wins, losses, gold_wagered, gold_won, gold_lost, "
            "game_wins, game_losses, ranked_wins, ranked_losses, elo FROM economy WHERE guildId=?",
            (guild_id,)
        )
        entries = []
        for (user_id, username, balance, bet_wins, bet_losses, gold_wagered,
             gold_won, gold_lost, game_wins, game_losses, ranked_wins, ranked_losses,
             elo) in self.cursor.fetchall():
            bet_games = bet_wins + bet_losses
            game_games = game_wins + game_losses
            ranked_games = ranked_wins + ranked_losses
            # casual = the non-ranked slice of game_wins/game_losses — every
            # reported game is either ranked or not, so there's nothing
            # separate to store for this side (see computeGameDeltas).
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

    # Sorts by entry[stat] — highest first for order="desc", lowest first
    # for order="asc" — with entries missing that stat (None, e.g. a win
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
    # default overview (sorted by elo, showing elo/record together) or one
    # of LEADERBOARD_STAT_LABELS for a single ranked stat.
    def _renderLeaderboardEmbed(self, guild_name, entries_sorted, stat, order, page):
        total_pages = self._leaderboardPageCount(entries_sorted)
        start = page * LEADERBOARD_PAGE_SIZE
        page_entries = entries_sorted[start:start + LEADERBOARD_PAGE_SIZE]

        title = (
            f"\U0001f3c6 {guild_name} Leaderboard — Overview" if stat is None
            else f"\U0001f3c6 {guild_name} Leaderboard — {LEADERBOARD_STAT_LABELS[stat]}"
        )

        lines = []
        for i, entry in enumerate(page_entries):
            rank = start + i + 1
            if stat is None:
                lines.append(
                    f"**#{rank}.** {entry['username']} — Elo: {entry['elo']} | "
                    f"Record: {entry['game_wins']}W-{entry['game_losses']}L"
                )
            else:
                lines.append(
                    f"**#{rank}.** {entry['username']} — {self._formatLeaderboardStat(entry, stat)}"
                )

        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "Nobody on this page.",
            color=discord.Color.gold(),
        )
        order_label = "Ascending" if order == "asc" else "Descending"
        embed.set_footer(text=f"Page {page + 1}/{total_pages} · {order_label}")
        return embed

    # Posts the first page and pre-reacts with the paging emoji — clicking
    # them (handleLeaderboardReaction) edits this same message rather than
    # posting a new one, so the current view is tracked by messageId here.
    async def leaderboardHelper(self, ctx, stat, order):
        guild_id = ctx.guild.id

        entries = self.getLeaderboardEntries(guild_id)
        if not entries:
            await ctx.response.send_message("Nobody has any stats to show yet in this server!")
            return

        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        embed = self._renderLeaderboardEmbed(ctx.guild.name, entries_sorted, stat, order, page=0)

        await ctx.response.send_message(embed=embed)
        msg = await ctx.original_response()
        for emoji in LEADERBOARD_NAV_EMOJIS:
            await msg.add_reaction(emoji)

        self.cursor.execute(
            "INSERT OR REPLACE INTO leaderboards(messageId, guildId, channelId, filter, sort_order, page) "
            "VALUES(?, ?, ?, ?, ?, 0)",
            (msg.id, guild_id, ctx.channel.id, stat, order)
        )
        self.db.commit()

    # Called from bot.py's on_raw_reaction_add for every reaction — no-ops
    # unless the emoji/message match an active leaderboard page view.
    async def handleLeaderboardReaction(self, payload):
        guild_id = payload.guild_id
        if guild_id is None:
            return

        emoji = str(payload.emoji)
        if emoji not in LEADERBOARD_NAV_EMOJIS:
            return

        self.cursor.execute(
            "SELECT channelId, filter, sort_order, page FROM leaderboards WHERE guildId=? AND messageId=?",
            (guild_id, payload.message_id)
        )
        row = self.cursor.fetchone()
        if row is None:
            return
        channel_id, stat, order, page = row

        entries = self.getLeaderboardEntries(guild_id)
        entries_sorted = self._sortLeaderboardEntries(entries, stat if stat is not None else "elo", order)
        total_pages = self._leaderboardPageCount(entries_sorted)

        if emoji == LEADERBOARD_FIRST_EMOJI:
            new_page = 0
        elif emoji == LEADERBOARD_PREV_EMOJI:
            new_page = max(0, page - 1)
        elif emoji == LEADERBOARD_NEXT_EMOJI:
            new_page = min(total_pages - 1, page + 1)
        else:
            new_page = total_pages - 1

        if new_page == page:
            return

        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        message = await channel.fetch_message(payload.message_id)

        guild = self.client.get_guild(guild_id)
        guild_name = guild.name if guild is not None else ""
        embed = self._renderLeaderboardEmbed(guild_name, entries_sorted, stat, order, new_page)
        await message.edit(embed=embed)
        await self._clearPagingReaction(message, payload)

        self.cursor.execute(
            "UPDATE leaderboards SET page=? WHERE guildId=? AND messageId=?",
            (new_page, guild_id, payload.message_id)
        )
        self.db.commit()

    # Cancels the running betting timer (if any) and, if the game had an
    # unresolved bet round (open, closed-but-unreported, or awaiting a
    # winner reaction), refunds every active bet.
    async def cancelBettingHelper(self, guild_id, channel):
        state = self.get(guild_id, "betting_state")

        task = self.bettingTasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

        if state not in ("OPEN", "CLOSED", "AWAITING_RESULT"):
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
        self.db.commit()

        if refunds:
            await channel.send("Bets have been refunded since the game ended before a winner was recorded.")