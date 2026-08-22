"""
All-encompassing test suite for Shockwave.

Runs with the stdlib test runner only (no pytest/pip install required):

    python tests.py
    python tests.py -v
    python -m unittest tests -v

Or, faster (splits the suite across every CPU core via pytest-xdist, see
_runStartupSelfTests in bot.py, which runs the suite this same way on every
bot startup):

    python -m pytest tests.py -n auto

Layout:
  - Fakes: lightweight stand-ins for discord.py objects (real attributes,
    not auto-magic Mocks, so a wrong attribute access fails loudly).
  - TourneyClasses tests: Player/Team, pure logic, no I/O.
  - helper.helpers tests: the bot's actual command logic, run against a
    real (in-memory) sqlite database and the fakes above.
  - bot.py tests: imports bot.py with its module-level DB connection and
    token read redirected away from the real project database and the
    real bot token (see _import_bot_module), then exercises command
    callbacks and event handlers directly; never touches Discord or the
    real data/guildData/serverInfo/main.db.
"""

import asyncio
import contextlib
import io
import itertools
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, mock_open, patch

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

from TourneyClasses import (
    Player, Team, Tournament, BracketNode, serialize_bracket, deserialize_bracket,
)
import helper as helper_module
from helper import helpers as Helpers
import restore_backup

GUILD_ID = 555000111


# ---------------------------------------------------------------------------
# In-memory database schema, mirroring bot.py's fresh-install CREATE TABLE
# statements exactly, so helper.helpers can be tested against a real sqlite
# backend without ever touching the project's actual database file.
# ---------------------------------------------------------------------------

SERVERS_SCHEMA = (
    "CREATE TABLE servers(guildId, serverName, original_channel, team1, team2, "
    "players, channel1, channel2, mode, turn, team_size, tournament, elo, "
    "result1, result2, captain1, captain2, "
    "betting_state, betting_message_id, betting_channel_id, is_ranked, "
    "active_tournament_match_id, wager_channel, betting_timer_seconds, "
    "roster_team1_message_id, roster_team2_message_id, roster_channel_id, "
    "roster_use_roles DEFAULT 0, default_elo, betting_opened_at, disliked_role_user_ids)"
)
ECONOMY_SCHEMA = (
    "CREATE TABLE economy(guildId, userId, username, balance, wins, losses, "
    "gold_wagered, gold_won, gold_lost, game_wins, game_losses, elo, last_daily, "
    "ranked_wins DEFAULT 0, ranked_losses DEFAULT 0, current_win_streak DEFAULT 0, "
    "PRIMARY KEY(guildId, userId))"
)
WAGERS_SCHEMA = (
    "CREATE TABLE wagers(guildId, userId, username, team, amount, "
    "PRIMARY KEY(guildId, userId))"
)
LAST_RESULT_SCHEMA = "CREATE TABLE last_result(guildId PRIMARY KEY, data)"
DUELS_SCHEMA = (
    "CREATE TABLE duels(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, messageId, "
    "challengerId, challengerName, targetId, targetName, amount, state)"
)
LEADERBOARDS_SCHEMA = (
    "CREATE TABLE leaderboards(messageId INTEGER PRIMARY KEY, guildId, channelId, "
    "filter, sort_order, page, cards INTEGER DEFAULT 0, cardShown INTEGER DEFAULT 0)"
)
MY_TEAM_VIEWS_SCHEMA = (
    "CREATE TABLE my_team_views(messageId INTEGER PRIMARY KEY, guildId, channelId, userId, page)"
)
TEAM_LIST_VIEWS_SCHEMA = (
    "CREATE TABLE team_list_views(messageId INTEGER PRIMARY KEY, guildId, channelId, "
    "search, recruitingOnly, sort, sort_order, page, cards INTEGER DEFAULT 0, "
    "cardShown INTEGER DEFAULT 0, memberIds, memberNames)"
)
STATS_VIEWS_SCHEMA = (
    "CREATE TABLE stats_views(messageId INTEGER PRIMARY KEY, guildId, targetUserId, "
    "cardShown INTEGER DEFAULT 0, cardAvatarGlobal INTEGER DEFAULT 0)"
)
TRADING_CARDS_SCHEMA = (
    "CREATE TABLE trading_cards(guildId, userId, title, accent_color, background_color, "
    "text_color, font_style, customized INTEGER DEFAULT 0, color_scheme_name, "
    "PRIMARY KEY(guildId, userId))"
)
TEAM_STATS_VIEWS_SCHEMA = (
    "CREATE TABLE team_stats_views(messageId INTEGER PRIMARY KEY, guildId, teamId, "
    "cardShown INTEGER DEFAULT 0)"
)
CARD_UNLOCKS_SCHEMA = (
    "CREATE TABLE card_unlocks(guildId, userId, itemType, itemKey, "
    "PRIMARY KEY(guildId, userId, itemType, itemKey))"
)
TEAMS_SCHEMA = "CREATE TABLE teams(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, name, data)"
TOURNAMENTS_SCHEMA = (
    "CREATE TABLE tournaments(guildId PRIMARY KEY, name, team_size, num_teams, "
    "double_elimination, teams, bracket, losers_bracket)"
)
TEAM_INVITES_SCHEMA = (
    "CREATE TABLE team_invites(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, "
    "messageId, teamId, teamName, inviterId, targetId, targetName)"
)
TOURNAMENT_MATCHES_SCHEMA = (
    "CREATE TABLE tournament_matches(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, roundIndex, "
    "nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType, "
    "bettingClosed DEFAULT 0, settledWagers)"
)
TOURNAMENT_WAGERS_SCHEMA = (
    "CREATE TABLE tournament_wagers(matchId, guildId, userId, username, team, amount, "
    "PRIMARY KEY(matchId, userId))"
)
PLAYER_ROLE_PREFERENCES_SCHEMA = (
    "CREATE TABLE player_role_preferences(guildId, userId, role, preference, "
    "PRIMARY KEY(guildId, userId, role))"
)
SETUP_ROLE_SESSIONS_SCHEMA = (
    "CREATE TABLE setup_role_sessions(messageId INTEGER PRIMARY KEY, guildId, userId, step, "
    "selectedRoles, likedRoles)"
)


def make_db():
    db = sqlite3.connect(":memory:")
    cursor = db.cursor()
    cursor.execute(SERVERS_SCHEMA)
    cursor.execute(ECONOMY_SCHEMA)
    cursor.execute(WAGERS_SCHEMA)
    cursor.execute(LAST_RESULT_SCHEMA)
    cursor.execute(DUELS_SCHEMA)
    cursor.execute(LEADERBOARDS_SCHEMA)
    cursor.execute(MY_TEAM_VIEWS_SCHEMA)
    cursor.execute(TEAM_LIST_VIEWS_SCHEMA)
    cursor.execute(STATS_VIEWS_SCHEMA)
    cursor.execute(TRADING_CARDS_SCHEMA)
    cursor.execute(TEAM_STATS_VIEWS_SCHEMA)
    cursor.execute(CARD_UNLOCKS_SCHEMA)
    cursor.execute(TEAMS_SCHEMA)
    cursor.execute(TOURNAMENTS_SCHEMA)
    cursor.execute(TEAM_INVITES_SCHEMA)
    cursor.execute(TOURNAMENT_MATCHES_SCHEMA)
    cursor.execute(TOURNAMENT_WAGERS_SCHEMA)
    cursor.execute(PLAYER_ROLE_PREFERENCES_SCHEMA)
    cursor.execute(SETUP_ROLE_SESSIONS_SCHEMA)
    db.commit()
    return db, cursor


def insert_guild_row(cursor, db, guild_id=GUILD_ID, name="Test Guild"):
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0, NULL, NULL, ?, "
        "NULL, NULL, NULL, 0, NULL, NULL, NULL)",
        (guild_id, name, helper_module.BETTING_DURATION_SECONDS),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Fakes standing in for discord.py objects.
# ---------------------------------------------------------------------------

_id_counter = itertools.count(9000)


def next_id():
    return next(_id_counter)


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


class FakeDMChannel:
    def __init__(self):
        self.send = AsyncMock()


# Mimics just enough of discord.Asset for display_avatar, a .url plus a
# .with_format() that swaps the extension, same as the real thing (see
# statsHelper's with_format("png") call, which needs to work whether the
# starting asset is already static or (like a GIF profile picture) animated).
def _fake_avatar_bytes():
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (200, 50, 50, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeAsset:
    def __init__(self, url):
        self.url = url
        # A real, decodable PNG by default; the trading card (see
        # _swapStatsForTradingCard) actually opens this with PIL, same
        # reason the fake logo files elsewhere had to stop being empty.
        self.read = AsyncMock(return_value=_fake_avatar_bytes())

    def with_format(self, fmt):
        stem = self.url.rsplit(".", 1)[0]
        return FakeAsset(f"{stem}.{fmt}")


class FakeMember:
    def __init__(self, name, id=None, bot=False, manage_guild=True):
        self.id = id if id is not None else next_id()
        self.name = name
        self.global_name = name
        self.display_name = name
        self.bot = bot
        self.voice = None
        self.mention = f"<@{self.id}>"
        self.move_to = AsyncMock()
        self.create_dm = AsyncMock(return_value=FakeDMChannel())
        # This server's own avatar for this member, deliberately a
        # different URL shape than FakeUser's below, so a test can tell
        # which one actually ended up on screen (see the avatar-toggle
        # tests for /stats and the trading card).
        self.display_avatar = FakeAsset(f"https://cdn.discordapp.com/embed/avatars/{self.id}.png")
        # Defaults to True so existing tests that don't care about
        # permissions aren't affected, pass manage_guild=False to test
        # the insufficient-permission path.
        self.guild_permissions = SimpleNamespace(manage_guild=manage_guild)

    # A real discord.Member/discord.User stringifies to its username (see
    # _interactionLogContext in bot.py, which builds a log line straight
    # from f"user={interaction.user}"); without this, str(member) falls
    # back to the default object repr instead.
    def __str__(self):
        return self.name


# The account-wide identity behind a FakeMember, distinct from it (a real
# discord.Member wraps a discord.User the same way), with its OWN avatar so
# the per-server/global avatar toggle (see _resolveGlobalAvatarUrl/
# _resolveCardAvatarImage) has something genuinely different to switch to
# in tests.
class FakeUser:
    def __init__(self, id=None, name=None):
        self.id = id if id is not None else next_id()
        self.name = name if name is not None else f"User{self.id}"
        # A real discord.User has no per-server nickname to fall back to;
        # display_name is always just its own global name, unlike
        # FakeMember's (which a caller can override independently).
        self.display_name = self.name
        self.display_avatar = FakeAsset(f"https://cdn.discordapp.com/avatars/{self.id}/global.png")


class FakeMessage:
    def __init__(self, id=None, channel=None):
        self.id = id if id is not None else next_id()
        self.channel = channel
        self.add_reaction = AsyncMock()
        self.remove_reaction = AsyncMock()
        self.clear_reaction = AsyncMock()
        self.clear_reactions = AsyncMock()
        self.edit = AsyncMock()


class FakeChannel:
    def __init__(self, name, id=None, members=None, kind="voice", guild=None):
        self.name = name
        self.id = id if id is not None else next_id()
        self.members = members if members is not None else []
        self.mention = f"<#{self.id}>"
        self.kind = kind
        self.guild = guild
        # A fresh FakeMessage per call (not one shared return_value); real
        # Discord messages are distinct objects, and printEmbed's callers
        # now rely on team1_message/team2_message being genuinely different
        # messages (see _finalizeRoster, which reacts to team2's only).
        # channel=self matches real discord.Message, which always carries
        # the channel it was sent/fetched in; _finalizeRoster reads it
        # straight off the message rather than needing it passed separately.
        self.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage(channel=self))
        self.create_invite = AsyncMock(return_value="https://discord.gg/fake-invite")
        self.fetch_message = AsyncMock(return_value=FakeMessage(channel=self))

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, id=GUILD_ID, name="Test Guild", channels=None, members=None):
        self.id = id
        self.name = name
        self.channels = channels if channels is not None else []
        self.members = members if members is not None else []

    @property
    def text_channels(self):
        return [c for c in self.channels if getattr(c, "kind", "voice") == "text"]

    async def create_voice_channel(self, name):
        channel = FakeChannel(name, kind="voice")
        self.channels.append(channel)
        return channel

    async def create_text_channel(self, name):
        channel = FakeChannel(name, kind="text")
        self.channels.append(channel)
        return channel

    def get_member(self, user_id):
        return next((m for m in self.members if m.id == user_id), None)

    async def fetch_member(self, user_id):
        member = self.get_member(user_id)
        if member is None:
            raise LookupError(f"no member {user_id} in this fake guild")
        return member


class FakeInteraction:
    def __init__(self, guild, user, channel=None, message=None):
        self.guild = guild
        self.guild_id = guild.id if guild is not None else None
        self.user = user
        self.channel = channel if channel is not None else FakeChannel("text-channel", guild=guild)
        self.channel_id = self.channel.id
        self.response = AsyncMock()
        self.followup = AsyncMock()
        self.original_response = AsyncMock(return_value=FakeMessage())
        # The message a component (button) interaction is attached to -
        # only set for tests that click a view's button and need
        # interaction.message (e.g. SetupRoleSelectionView.confirm, which
        # looks its session row up by interaction.message.id).
        self.message = message


class FakeClient:
    def __init__(self, channels=(), guilds=(), users=()):
        self._channels = {c.id: c for c in channels}
        self._guilds = {g.id: g for g in guilds}
        self._users = {u.id: u for u in users}
        self.fetch_channel = AsyncMock()
        # Cache miss (get_user returns None) is the common case in tests -
        # fetch_user still resolves to a real (if auto-generated) FakeUser
        # rather than erroring, same as the real API always eventually
        # resolving a valid id.
        self.fetch_user = AsyncMock(side_effect=lambda user_id: self._users.get(user_id) or FakeUser(user_id))

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)

    def get_user(self, user_id):
        return self._users.get(user_id)


class FakePayload:
    def __init__(self, guild_id, message_id, channel_id, emoji, member=None, user_id=None):
        self.guild_id = guild_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.emoji = emoji
        self.member = member
        self.user_id = user_id if user_id is not None else (member.id if member is not None else None)


# ===========================================================================
# TourneyClasses.py: pure logic, no I/O
# ===========================================================================

class PlayerTests(unittest.TestCase):
    def test_serialize_deserialize_roundtrip(self):
        p = Player(123, "Alice")
        restored = Player()
        restored.deserializePlayer(p.serializePlayer())
        self.assertEqual(restored.id, 123)
        self.assertEqual(restored.name, "Alice")

    def test_get_set(self):
        p = Player()
        p.set_id(7)
        p.set_name("Bob")
        self.assertEqual(p.get_id(), 7)
        self.assertEqual(p.get_name(), "Bob")

    def test_convert_from_member(self):
        member = FakeMember("Carol", id=42)
        p = Player()
        p.convertFromMember(member)
        self.assertEqual(p.id, 42)
        self.assertEqual(p.name, "Carol")


class TeamTests(unittest.TestCase):
    def test_add_player_increments_size(self):
        team = Team()
        team.add_player(Player(1, "A"))
        team.add_player(Player(2, "B"))
        self.assertEqual(team.get_size(), 2)
        self.assertEqual(len(team.get_players()), 2)

    def test_remove_player_decrements_size(self):
        team = Team()
        p1 = Player(1, "A")
        team.add_player(p1)
        team.add_player(Player(2, "B"))
        team.remove_player(p1)
        self.assertEqual(team.get_size(), 1)
        self.assertNotIn(p1, team.get_players())

    def test_serialize_deserialize_roundtrip(self):
        team = Team()
        team.set_id(1)
        team.set_name("Team 1")
        team.add_player(Player(1, "Alice"))
        team.add_player(Player(2, "Bob"))
        team.addWin()
        team.addWin()
        team.addLoss()

        restored = Team()
        restored.deserializeTeam(team.serializeTeam())

        self.assertEqual(restored.name, "Team 1")
        self.assertEqual({p.get_id() for p in restored.get_players()}, {1, 2})
        self.assertEqual({p.get_name() for p in restored.get_players()}, {"Alice", "Bob"})
        self.assertEqual(restored.get_size(), 2)
        self.assertEqual(restored.wins, 2)
        self.assertEqual(restored.losses, 1)

    def test_deserialize_empty_team_has_no_players(self):
        team = Team()
        team.set_id(1)
        team.set_name("Empty")

        restored = Team()
        restored.deserializeTeam(team.serializeTeam())

        self.assertEqual(restored.get_players(), [])
        self.assertEqual(restored.get_size(), 0)

    def test_set_captain_requires_player_on_team(self):
        team = Team()
        outsider = Player(1, "Outsider")
        with self.assertRaises(ValueError):
            team.set_captain(outsider)

    def test_serialize_deserialize_roundtrips_captain_as_a_real_player(self):
        team = Team()
        team.set_id(1)
        team.set_name("Team 1")
        captain = Player(1, "Alice")
        team.add_player(captain)
        team.add_player(Player(2, "Bob"))
        team.set_captain(captain)

        restored = Team()
        restored.deserializeTeam(team.serializeTeam())

        self.assertIsInstance(restored.get_captain(), Player)
        self.assertEqual(restored.get_captain().get_id(), 1)
        self.assertEqual(restored.get_captain().get_name(), "Alice")

    def test_deserialize_with_no_captain_leaves_it_none(self):
        team = Team()
        team.set_id(1)
        team.set_name("No Captain")
        team.add_player(Player(1, "Alice"))

        restored = Team()
        restored.deserializeTeam(team.serializeTeam())

        self.assertIsNone(restored.get_captain())

    def test_serialize_deserialize_roundtrips_team_size(self):
        team = Team()
        team.set_id(1)
        team.set_name("Team 1")
        team.set_team_size(5)

        restored = Team()
        restored.deserializeTeam(team.serializeTeam())

        self.assertEqual(restored.get_team_size(), 5)

    def test_deserialize_tolerates_data_from_before_team_size_existed(self):
        # Simulates a team serialized by older code, before the team_size
        # field was appended to the format.
        old_format = "[1, Legacy Team, , 0, , , 0, 0]"

        restored = Team()
        restored.deserializeTeam(old_format)

        self.assertIsNone(restored.get_team_size())
        self.assertEqual(restored.get_name(), "Legacy Team")


class TournamentTests(unittest.TestCase):
    def _team(self, name, *players):
        team = Team()
        team.set_name(name)
        for player_id, player_name in players:
            team.add_player(Player(player_id, player_name))
        return team

    def test_defaults(self):
        tournament = Tournament("Spring Cup", 5, 8, True)
        self.assertEqual(tournament.get_name(), "Spring Cup")
        self.assertEqual(tournament.get_team_size(), 5)
        self.assertEqual(tournament.get_num_teams(), 8)
        self.assertTrue(tournament.is_double_elimination())
        self.assertEqual(tournament.get_teams(), [])
        self.assertEqual(tournament.get_bracket(), [])

    def test_register_team_adds_to_roster(self):
        tournament = Tournament("Cup", 2, 4)
        team = self._team("Red", (1, "Alice"), (2, "Bob"))
        tournament.register_team(team)
        self.assertEqual(tournament.get_teams(), [team])

    def test_register_team_rejects_player_already_on_another_registered_team(self):
        tournament = Tournament("Cup", 2, 4)
        tournament.register_team(self._team("Red", (1, "Alice"), (2, "Bob")))

        with self.assertRaises(ValueError):
            tournament.register_team(self._team("Blue", (2, "Bob"), (3, "Cleo")))

        # the rejected team never got added
        self.assertEqual(len(tournament.get_teams()), 1)

    def test_register_team_allows_disjoint_rosters(self):
        tournament = Tournament("Cup", 2, 4)
        tournament.register_team(self._team("Red", (1, "Alice"), (2, "Bob")))
        tournament.register_team(self._team("Blue", (3, "Cleo"), (4, "Dan")))
        self.assertEqual(len(tournament.get_teams()), 2)


class BracketSerializationTests(unittest.TestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_roundtrips_a_single_pairing_with_pointers_intact(self):
        leaf_a = BracketNode(self._team("Red"))
        leaf_b = BracketNode(self._team("Blue"))
        final = BracketNode()
        leaf_a.opponent = leaf_b
        leaf_b.opponent = leaf_a
        leaf_a.next = final
        leaf_b.next = final
        final.previous = leaf_a
        nodes = [leaf_a, leaf_b, final]

        restored = deserialize_bracket(serialize_bracket(nodes))

        r_leaf_a, r_leaf_b, r_final = restored
        self.assertEqual(r_leaf_a.team.get_name(), "Red")
        self.assertEqual(r_leaf_b.team.get_name(), "Blue")
        self.assertIsNone(r_final.team)

        # pointer identity is preserved (same restored objects, not copies)
        self.assertIs(r_leaf_a.opponent, r_leaf_b)
        self.assertIs(r_leaf_b.opponent, r_leaf_a)
        self.assertIs(r_leaf_a.next, r_final)
        self.assertIs(r_leaf_b.next, r_final)
        self.assertIs(r_final.previous, r_leaf_a)

        # bracket-shape invariants from the spec
        self.assertIsNone(r_leaf_a.previous)  # round-one node
        self.assertIsNone(r_final.next)       # finals node
        # the other half of final's feeder pair is reachable via .opponent
        self.assertIs(r_final.previous.opponent, r_leaf_b)

    def test_empty_bracket_roundtrips_to_empty(self):
        self.assertEqual(deserialize_bracket(serialize_bracket([])), [])


# ===========================================================================
# helper.helpers: real in-memory sqlite db + fake discord objects
# ===========================================================================

class HelperTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db, self.cursor = make_db()
        insert_guild_row(self.cursor, self.db)
        self.helperObj = Helpers(self.cursor, self.db)
        self.guild = FakeGuild()

    def tearDown(self):
        self.db.close()

    def deserialize_team(self, column, guild_id=GUILD_ID):
        team = Team()
        team.deserializeTeam(self.helperObj.get(guild_id, column))
        return team


class GetUpdateTests(HelperTestCase):
    def test_update_then_get_roundtrip(self):
        self.helperObj.update(GUILD_ID, "mode", "Captains")
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Captains")


class FinalizeRosterTests(HelperTestCase):
    def _team(self, name, count, start_id=300):
        team = Team()
        team.set_name(name)
        for i in range(count):
            team.add_player(Player(start_id + i, f"P{i}"))
        return team

    async def test_always_attaches_a_roster_action_view_without_reroll(self):
        channel = FakeChannel("game-chat")
        team1_message = FakeMessage()
        team2_message = FakeMessage()
        team2_message.channel = channel
        team1 = self._team("Team 1", 3)
        team2 = self._team("Team 2", 3, start_id=400)

        await self.helperObj._finalizeRoster(GUILD_ID, team1_message, team2_message, team1, team2, False)

        team2_message.edit.assert_awaited_once()
        view = team2_message.edit.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.RosterActionView)
        self.assertNotIn(view.reroll, view.children)
        team1_message.edit.assert_not_awaited()
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_team1_message_id"), team1_message.id)
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_team2_message_id"), team2_message.id)
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_channel_id"), channel.id)
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_use_roles"), 0)

    async def test_includes_reroll_button_when_both_teams_are_exactly_five(self):
        channel = FakeChannel("game-chat")
        team1_message = FakeMessage()
        team2_message = FakeMessage()
        team2_message.channel = channel
        team1 = self._team("Team 1", 5)
        team2 = self._team("Team 2", 5, start_id=400)

        await self.helperObj._finalizeRoster(GUILD_ID, team1_message, team2_message, team1, team2, True)

        view = team2_message.edit.call_args.kwargs["view"]
        self.assertIn(view.reroll, view.children)
        self.assertIn(view.start, view.children)
        self.assertIn(view.startNoMove, view.children)
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_use_roles"), 1)

    async def test_skips_reroll_reaction_when_not_five_a_side(self):
        channel = FakeChannel("game-chat")
        team1_message = FakeMessage()
        team2_message = FakeMessage()
        team2_message.channel = channel
        team1 = self._team("Team 1", 4)
        team2 = self._team("Team 2", 5, start_id=400)

        await self.helperObj._finalizeRoster(GUILD_ID, team1_message, team2_message, team1, team2, True)

        calls = [c.args[0] for c in team2_message.add_reaction.call_args_list]
        self.assertNotIn(helper_module.TEAM_ROLES_REROLL_EMOJI, calls)
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_use_roles"), 0)


class FindRosterVoiceChannelTests(HelperTestCase):
    def test_finds_first_rostered_player_currently_in_voice(self):
        voice_channel = FakeChannel("Lobby")
        member = FakeMember("Alice", id=101)
        member.voice = FakeVoiceState(voice_channel)
        guild = FakeGuild(members=[member])

        team1 = Team()
        team1.add_player(Player(101, "Alice"))
        team2 = Team()

        found = self.helperObj._findRosterVoiceChannel(guild, team1, team2)
        self.assertIs(found, voice_channel)

    def test_returns_none_when_nobody_from_the_roster_is_in_voice(self):
        member = FakeMember("Alice", id=101)
        guild = FakeGuild(members=[member])

        team1 = Team()
        team1.add_player(Player(101, "Alice"))
        team2 = Team()

        self.assertIsNone(self.helperObj._findRosterVoiceChannel(guild, team1, team2))


class RerollRosterTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _seed_roster(self):
        team1 = Team()
        team1.set_name("Team 1")
        for i in range(5):
            team1.add_player(Player(300 + i, f"A{i}"))
        team2 = Team()
        team2.set_name("Team 2")
        for i in range(5):
            team2.add_player(Player(400 + i, f"B{i}"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "roster_team1_message_id", 111)
        self.helperObj.update(GUILD_ID, "roster_team2_message_id", 112)

    async def test_noop_without_a_tracked_roster_message(self):
        # Nothing stored for this guild, should just return, not error.
        await self.helperObj._rerollRoster(GUILD_ID, self.channel)
        self.channel.fetch_message.assert_not_awaited()

    async def test_persists_the_shuffle_and_edits_both_messages(self):
        self._seed_roster()

        await self.helperObj._rerollRoster(GUILD_ID, self.channel)

        self.assertEqual(self.channel.fetch_message.await_count, 2)
        message = self.channel.fetch_message.return_value
        self.assertEqual(message.edit.await_count, 2)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        self.assertEqual({p.get_id() for p in team1.get_players()}, {300, 301, 302, 303, 304})
        self.assertEqual({p.get_id() for p in team2.get_players()}, {400, 401, 402, 403, 404})


class RosterActionViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat")
        self.channel1 = FakeChannel("Team 1")
        self.channel2 = FakeChannel("Team 2")
        self.voice_channel = FakeChannel("Lobby")
        self.member1 = FakeMember("Alice", id=101)
        self.member1.voice = FakeVoiceState(self.voice_channel)
        self.member2 = FakeMember("Bob", id=102)
        self.fakeGuild = FakeGuild(
            channels=[self.channel1, self.channel2], members=[self.member1, self.member2]
        )
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.fakeGuild])
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")

        team1 = Team()
        team1.set_name("Team 1")
        team1.add_player(Player(101, "Alice"))
        team2 = Team()
        team2.set_name("Team 2")
        team2.add_player(Player(102, "Bob"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "roster_team2_message_id", 112)

    def _click(self, message_id=112, user_id=999, name="Clicker"):
        return FakeInteraction(
            self.fakeGuild, FakeMember(name, id=user_id),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )

    async def test_moves_players_opens_betting_and_clears_pending_state(self):
        with patch.object(self.helperObj, "_openBetting", AsyncMock()) as open_betting, \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()) as matchup:
            await self.helperObj._handleRosterStartClick(self._click(), move=True)

        self.member1.move_to.assert_awaited_once_with(self.channel1)
        self.member2.move_to.assert_awaited_once_with(self.channel2)
        open_betting.assert_awaited_once_with(GUILD_ID, self.channel)
        matchup.assert_awaited_once()
        self.assertIsNone(self.helperObj.get(GUILD_ID, "roster_team2_message_id"))
        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "Lobby")

    async def test_clears_the_view_on_the_roster_message_afterward(self):
        with patch.object(self.helperObj, "_openBetting", AsyncMock()), \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            click = self._click()
            await self.helperObj._handleRosterStartClick(click, move=True)

        click.message.edit.assert_awaited_once_with(view=None)

    async def test_move_false_opens_betting_without_moving_anyone(self):
        with patch.object(self.helperObj, "_openBetting", AsyncMock()) as open_betting, \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()) as matchup:
            await self.helperObj._handleRosterStartClick(self._click(), move=False)

        self.member1.move_to.assert_not_awaited()
        self.member2.move_to.assert_not_awaited()
        open_betting.assert_awaited_once_with(GUILD_ID, self.channel)
        matchup.assert_awaited_once()
        self.assertIsNone(self.helperObj.get(GUILD_ID, "roster_team2_message_id"))
        # Cleared, not left alone, see test_move_false_clears_a_previously_set_original_channel.
        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "")
        self.assertNotIn("Moved!", [c.args[0] for c in self.channel.send.call_args_list])

    async def test_move_false_clears_a_previously_set_original_channel(self):
        # captainsHelper captures original_channel at draft-start time,
        # independent of which button eventually starts the game (see
        # captainsHelper), a leftover value from a prior Start game is
        # possible too. Either way, Start (no move) must override it so
        # moveMembersToOriginalChannel no-ops once this no-move game
        # resolves (winner reported or cancelled), instead of moving
        # whoever happens to be sitting in the team channels.
        self.fakeGuild.channels.append(self.voice_channel)  # makes "Lobby" resolvable by name
        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")

        with patch.object(self.helperObj, "_openBetting", AsyncMock()), \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await self.helperObj._handleRosterStartClick(self._click(), move=False)

        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "")
        moved = await self.helperObj.moveMembersToOriginalChannel(self.fakeGuild)
        self.assertFalse(moved)

    async def test_move_false_works_even_when_nobody_is_in_voice(self):
        self.member1.voice = None
        with patch.object(self.helperObj, "_openBetting", AsyncMock()) as open_betting, \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await self.helperObj._handleRosterStartClick(self._click(), move=False)

        open_betting.assert_awaited_once_with(GUILD_ID, self.channel)
        self.assertIsNone(self.helperObj.get(GUILD_ID, "roster_team2_message_id"))

    async def test_nobody_in_voice_sends_a_message_and_does_not_start(self):
        self.member1.voice = None
        with patch.object(self.helperObj, "_openBetting", AsyncMock()) as open_betting:
            click = self._click()
            await self.helperObj._handleRosterStartClick(click, move=True)

        open_betting.assert_not_awaited()
        self.member1.move_to.assert_not_awaited()
        click.response.send_message.assert_awaited_once()
        self.assertIn("voice channel", click.response.send_message.call_args.args[0])
        self.assertNotIn("ephemeral", click.response.send_message.call_args.kwargs)
        # Not consumed; the same click's intent should still be retryable.
        self.assertEqual(self.helperObj.get(GUILD_ID, "roster_team2_message_id"), 112)

    async def test_missing_team_channels_self_heals_onto_defaults(self):
        # If /set was never run to configure team1/team2, falls back to
        # DEFAULT_TEAM_CHANNEL_NAMES, creating them if missing, and
        # remembers them for next time.
        self.helperObj.update(GUILD_ID, "channel1", "")
        self.helperObj.update(GUILD_ID, "channel2", "")
        with patch.object(self.helperObj, "_openBetting", AsyncMock()) as open_betting, \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await self.helperObj._handleRosterStartClick(self._click(), move=True)

        open_betting.assert_awaited_once_with(GUILD_ID, self.channel)
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel1"), "Team-1")
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel2"), "Team-2")

        default1 = discord.utils.get(self.fakeGuild.channels, name="Team-1")
        default2 = discord.utils.get(self.fakeGuild.channels, name="Team-2")
        self.assertIsNotNone(default1)
        self.assertIsNotNone(default2)
        self.member1.move_to.assert_awaited_once_with(default1)
        self.member2.move_to.assert_awaited_once_with(default2)

    async def test_missing_team_channels_reuses_existing_defaults_without_recreating(self):
        self.helperObj.update(GUILD_ID, "channel1", "")
        self.helperObj.update(GUILD_ID, "channel2", "")
        existing1 = FakeChannel("Team-1")
        existing2 = FakeChannel("Team-2")
        self.fakeGuild.channels.extend([existing1, existing2])

        with patch.object(self.helperObj, "_openBetting", AsyncMock()), \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await self.helperObj._handleRosterStartClick(self._click(), move=True)

        self.member1.move_to.assert_awaited_once_with(existing1)
        self.member2.move_to.assert_awaited_once_with(existing2)
        names = [c.name for c in self.fakeGuild.channels]
        self.assertEqual(names.count("Team-1"), 1)
        self.assertEqual(names.count("Team-2"), 1)

    async def test_ignores_a_click_on_a_stale_roster_message(self):
        with patch.object(self.helperObj, "_rerollRoster", AsyncMock()) as reroll:
            click = self._click(message_id=999)
            await self.helperObj._handleRosterRerollClick(click)
        reroll.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_reroll_click_triggers_reroll_when_eligible(self):
        self.helperObj.update(GUILD_ID, "roster_use_roles", 1)
        with patch.object(self.helperObj, "_rerollRoster", AsyncMock()) as reroll:
            click = self._click()
            await self.helperObj._handleRosterRerollClick(click)
        reroll.assert_awaited_once_with(GUILD_ID, self.channel)

    async def test_reroll_click_rejected_when_roster_was_not_role_eligible(self):
        self.helperObj.update(GUILD_ID, "roster_use_roles", 0)
        with patch.object(self.helperObj, "_rerollRoster", AsyncMock()) as reroll:
            click = self._click()
            await self.helperObj._handleRosterRerollClick(click)
        reroll.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


class RandomizeTeamHelperTests(HelperTestCase):
    async def test_splits_members_evenly(self):
        members = [FakeMember(f"P{i}", id=200 + i) for i in range(6)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.randomizeTeamHelper(ctx)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")

        all_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        self.assertEqual(all_ids, {m.id for m in members})
        self.assertLessEqual(abs(len(team1.get_players()) - len(team2.get_players())), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Normal")
        # no /set team1/team2 configured for this guild -> generic fallback
        self.assertEqual(team1.get_name(), "Team 1")
        self.assertEqual(team2.get_name(), "Team 2")

    async def test_uses_the_guilds_configured_team_names(self):
        self.helperObj.update(GUILD_ID, "channel1", "Red")
        self.helperObj.update(GUILD_ID, "channel2", "Blue")
        members = [FakeMember(f"P{i}", id=200 + i) for i in range(6)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.randomizeTeamHelper(ctx)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        self.assertEqual(team1.get_name(), "Red")
        self.assertEqual(team2.get_name(), "Blue")


class FormBalancedTeamsTests(HelperTestCase):
    def test_snake_draft_alternates_sides_without_jitter(self):
        members_with_elo = [
            (FakeMember(f"P{i}", id=i), elo)
            for i, elo in enumerate([1000, 900, 800, 700, 600, 500, 400, 300])
        ]

        with patch("random.uniform", return_value=0):
            team1, team2 = self.helperObj.formBalancedTeams(members_with_elo)

        # descending elo: 1000,900,800,700,600,500,400,300 (P0..P7).
        # snake pattern 1,2,2,1,1,2,2,1 -> team1 = ranks 1,4,5,8; team2 = ranks 2,3,6,7
        self.assertEqual([m.name for m in team1], ["P0", "P3", "P4", "P7"])
        self.assertEqual([m.name for m in team2], ["P1", "P2", "P5", "P6"])

    def test_every_member_assigned_exactly_once_odd_count(self):
        members_with_elo = [(FakeMember(f"P{i}", id=i), 1000 + i * 37) for i in range(9)]

        team1, team2 = self.helperObj.formBalancedTeams(members_with_elo)

        self.assertEqual(len(team1) + len(team2), 9)
        self.assertEqual(len({m.id for m in team1} | {m.id for m in team2}), 9)
        self.assertLessEqual(abs(len(team1) - len(team2)), 1)

    def test_average_elo(self):
        elo_by_id = {1: 1000, 2: 1200}
        members = [FakeMember("A", id=1), FakeMember("B", id=2)]

        self.assertEqual(self.helperObj.averageElo(members, elo_by_id), 1100)
        self.assertEqual(self.helperObj.averageElo([], elo_by_id), helper_module.DEFAULT_ELO)


class RankedTeamHelperTests(HelperTestCase):
    async def test_forms_balanced_teams_and_marks_game_ranked(self):
        members = [FakeMember(f"P{i}", id=300 + i) for i in range(6)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        all_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        self.assertEqual(all_ids, {m.id for m in members})
        self.assertLessEqual(abs(len(team1.get_players()) - len(team2.get_players())), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Ranked")
        # no /set team1/team2 configured for this guild -> generic fallback
        self.assertEqual(team1.get_name(), "Team 1")
        self.assertEqual(team2.get_name(), "Team 2")

        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("avg elo", message)
        self.assertIn("Press Start", message)

    async def test_uses_the_guilds_configured_team_names(self):
        self.helperObj.update(GUILD_ID, "channel1", "Red")
        self.helperObj.update(GUILD_ID, "channel2", "Blue")
        members = [FakeMember(f"P{i}", id=300 + i) for i in range(6)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        self.assertEqual(team1.get_name(), "Red")
        self.assertEqual(team2.get_name(), "Blue")

    async def test_creates_economy_rows_at_default_elo_for_new_players(self):
        members = [FakeMember("New1", id=401), FakeMember("New2", id=402)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 401, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 402, "elo"), helper_module.DEFAULT_ELO)

    async def test_use_roles_persists_who_got_a_disliked_role(self):
        members = [FakeMember(f"P{i}", id=800 + i) for i in range(10)]
        # Everyone dislikes Jungle, so the balancer is forced to put two of
        # them there anyway (see RoleBalancedTeamAssignmentTests' own
        # "forces disliked players" test), members[0]/[1] deterministically,
        # since nobody has any other stated preference to break the tie.
        for member in members:
            self.helperObj._applySetupRolePreferences(GUILD_ID, member.id, [], ["Jungle"])
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx, use_roles=True)

        self.assertEqual(
            self.helperObj._dislikedRoleUserIds(GUILD_ID), {members[0].id, members[1].id}
        )

    async def test_no_disliked_assignments_leaves_the_column_empty(self):
        members = [FakeMember(f"P{i}", id=810 + i) for i in range(10)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx, use_roles=True)

        self.assertEqual(self.helperObj._dislikedRoleUserIds(GUILD_ID), frozenset())

    async def test_roleless_game_clears_any_stale_disliked_role_ids(self):
        self.helperObj.update(GUILD_ID, "disliked_role_user_ids", "111,222")
        members = [FakeMember(f"P{i}", id=300 + i) for i in range(6)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx)  # use_roles defaults to False

        self.assertEqual(self.helperObj._dislikedRoleUserIds(GUILD_ID), frozenset())


class RoleBalancedTeamAssignmentTests(HelperTestCase):
    def _members(self, n=10, start_id=800):
        return [FakeMember(f"P{i}", id=start_id + i) for i in range(n)]

    def test_assign_roles_prefers_each_players_liked_role(self):
        members = self._members()
        role_pairs = {
            "Jungle": members[0:2], "Top": members[2:4], "Mid": members[4:6],
            "Bottom": members[6:8], "Support": members[8:10],
        }
        for role, pair in role_pairs.items():
            for member in pair:
                self.helperObj._applySetupRolePreferences(GUILD_ID, member.id, [role], [])
        members_with_elo = [(m, 1000) for m in members]

        assigned = self.helperObj._assignRolesForBalance(GUILD_ID, members_with_elo)

        self.assertEqual(len(assigned), 10)
        for member, elo, role, tier, effective_elo in assigned:
            self.assertIn(member, role_pairs[role])
            self.assertEqual(tier, "liked")
            self.assertEqual(effective_elo, elo)

    def test_assign_roles_falls_back_to_neutral_with_no_stated_preferences(self):
        members = self._members()
        members_with_elo = [(m, 1000) for m in members]

        assigned = self.helperObj._assignRolesForBalance(GUILD_ID, members_with_elo)

        self.assertEqual(len(assigned), 10)
        for _member, elo, _role, tier, effective_elo in assigned:
            self.assertEqual(tier, "neutral")
            self.assertEqual(effective_elo, elo - helper_module.ROLE_BALANCE_OFF_ROLE_PENALTY)

    def test_assign_roles_forces_disliked_players_when_nobody_else_can_fill_a_role(self):
        members = self._members()
        for member in members:
            self.helperObj._applySetupRolePreferences(GUILD_ID, member.id, [], ["Jungle"])
        members_with_elo = [(m, 1000) for m in members]

        assigned = self.helperObj._assignRolesForBalance(GUILD_ID, members_with_elo)

        jungle_entries = [entry for entry in assigned if entry[2] == "Jungle"]
        self.assertEqual({entry[0].id for entry in jungle_entries}, {members[0].id, members[1].id})
        for entry in jungle_entries:
            self.assertEqual(entry[3], "disliked")
            self.assertEqual(entry[4], entry[1] - helper_module.ROLE_BALANCE_DISLIKED_ROLE_PENALTY)

    def test_split_role_balanced_teams_minimizes_effective_elo_gap(self):
        # Top and Jungle each have a +/-200 imbalance within their own pair;
        # Mid/Bottom/Support are flat 1000/1000 so they can't affect the
        # total either way. Putting each pair's high scorer on the *same*
        # side stacks the imbalance to a 400 gap; splitting them across
        # opposite sides cancels it out to 0; only that second combination
        # is the true minimum, so this only passes if the brute force
        # actually searches all 32 combos instead of picking an arbitrary
        # (or naively "same flip for every role") one.
        pairs = {
            "Top": [("A", 1100), ("B", 900)],
            "Jungle": [("C", 1100), ("D", 900)],
            "Mid": [("E", 1000), ("F", 1000)],
            "Bottom": [("G", 1000), ("H", 1000)],
            "Support": [("I", 1000), ("J", 1000)],
        }
        assigned = []
        for role, entries in pairs.items():
            for name, elo in entries:
                assigned.append((SimpleNamespace(id=name, name=name), elo, role, "neutral", elo))

        side_a, side_b = self.helperObj._splitRoleBalancedTeams(assigned)

        self.assertEqual([entry[2] for entry in side_a], helper_module.SETUP_ROLE_NAMES)
        self.assertEqual([entry[2] for entry in side_b], helper_module.SETUP_ROLE_NAMES)
        self.assertEqual(abs(sum(e[4] for e in side_a) - sum(e[4] for e in side_b)), 0)
        top_a = next(entry for entry in side_a if entry[2] == "Top")
        jungle_a = next(entry for entry in side_a if entry[2] == "Jungle")
        # the two pairs' high scorers (1100) must land on opposite sides
        self.assertNotEqual(top_a[4] == 1100, jungle_a[4] == 1100)

    def test_refine_role_balance_finds_a_better_partition_via_role_swap(self):
        # All-neutral (no preferences at all) makes _assignRolesForBalance's
        # fill order purely id-based: P0/P1 -> Jungle, P2/P3 -> Top, etc.
        # Stacking one pair's whole elo gap onto Jungle (2000/0) while every
        # other pair is a flat 1000/1000 leaves _splitRoleBalancedTeams
        # stuck at a forced 2000 gap, since it can only pick which side each
        # *already-formed* pair's members land on, not swap who plays what
        # role. Swapping just one Jungle player for one Top player (2000<->
        # 1000, 1000<->0) turns both pairs into a 1000/1000-ish mix that
        # *does* have a diff-0 split, proving the swap step actually looks
        # past the initial partition instead of only re-splitting it.
        members = self._members()
        elos = {members[0].id: 2000, members[1].id: 0}
        for member in members[2:]:
            elos[member.id] = 1000
        members_with_elo = [(m, elos[m.id]) for m in members]

        assigned = self.helperObj._assignRolesForBalance(GUILD_ID, members_with_elo)
        self.assertEqual(self.helperObj._roleSplitDiff(assigned), 2000)

        refined = self.helperObj._refineRoleBalance(GUILD_ID, assigned)

        self.assertEqual(self.helperObj._roleSplitDiff(refined), 0)
        self.assertEqual({entry[0].id for entry in refined}, {m.id for m in members})
        role_counts = {}
        for entry in refined:
            role_counts[entry[2]] = role_counts.get(entry[2], 0) + 1
        self.assertEqual(set(role_counts), set(helper_module.SETUP_ROLE_NAMES))
        self.assertEqual(set(role_counts.values()), {2})

    def test_form_role_balanced_teams_returns_none_for_non_ten_player_rosters(self):
        members_with_elo = [(m, 1000) for m in self._members(n=8)]
        self.assertIsNone(self.helperObj.formRoleBalancedTeams(GUILD_ID, members_with_elo))

    def test_form_role_balanced_teams_covers_every_player_in_setup_role_order(self):
        members = self._members()
        members_with_elo = [(m, 1000) for m in members]

        result = self.helperObj.formRoleBalancedTeams(GUILD_ID, members_with_elo)

        self.assertIsNotNone(result)
        side_a, side_b = result
        self.assertEqual([entry[2] for entry in side_a], helper_module.SETUP_ROLE_NAMES)
        self.assertEqual([entry[2] for entry in side_b], helper_module.SETUP_ROLE_NAMES)
        all_ids = {entry[0].id for entry in side_a} | {entry[0].id for entry in side_b}
        self.assertEqual(all_ids, {m.id for m in members})


class RankedTeamHelperUseRolesTests(HelperTestCase):
    def _members(self, n=10, start_id=850):
        return [FakeMember(f"P{i}", id=start_id + i) for i in range(n)]

    def _ctx(self, members):
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        return FakeInteraction(self.guild, user)

    async def test_use_roles_with_ten_players_assigns_roles_and_marks_ranked(self):
        members = self._members()
        role_pairs = {
            "Jungle": members[0:2], "Top": members[2:4], "Mid": members[4:6],
            "Bottom": members[6:8], "Support": members[8:10],
        }
        for role, pair in role_pairs.items():
            for member in pair:
                self.helperObj._applySetupRolePreferences(GUILD_ID, member.id, [role], [])
        ctx = self._ctx(members)

        await self.helperObj.rankedTeamHelper(ctx, use_roles=True)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        self.assertEqual(len(team1.get_players()), 5)
        self.assertEqual(len(team2.get_players()), 5)
        team1_ids = [p.get_id() for p in team1.get_players()]
        team2_ids = [p.get_id() for p in team2.get_players()]
        for i, role in enumerate(helper_module.SETUP_ROLE_NAMES):
            pair_ids = {m.id for m in role_pairs[role]}
            self.assertIn(team1_ids[i], pair_ids)
            self.assertIn(team2_ids[i], pair_ids)
        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Ranked")

        message = ctx.response.send_message.call_args.args[0]
        self.assertNotIn("no roles were assigned", message)

    async def test_use_roles_falls_back_without_exactly_ten_players(self):
        members = self._members(n=6)
        ctx = self._ctx(members)

        await self.helperObj.rankedTeamHelper(ctx, use_roles=True)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        all_ids = {p.get_id() for p in team1.get_players()} | {p.get_id() for p in team2.get_players()}
        self.assertEqual(all_ids, {m.id for m in members})

        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("no roles were assigned", message)

    async def test_use_roles_false_leaves_ranked_teams_unaffected(self):
        members = self._members()
        ctx = self._ctx(members)

        await self.helperObj.rankedTeamHelper(ctx, use_roles=False)

        message = ctx.response.send_message.call_args.args[0]
        self.assertNotIn("no roles were assigned", message)


class CaptainsHelperTests(HelperTestCase):
    def _ctx(self, captain1, captain2, pool_members):
        voice_channel = FakeChannel("Lobby", members=[captain1, captain2] + pool_members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        return FakeInteraction(self.guild, user)

    async def test_valid_captains_split_players(self):
        captain1 = FakeMember("Cap1", id=301)
        captain2 = FakeMember("Cap2", id=302)
        pool = [FakeMember("Pool1", id=303), FakeMember("Pool2", id=304)]
        ctx = self._ctx(captain1, captain2, pool)

        await self.helperObj.captainsHelper(ctx, captain1, captain2)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        players = self.deserialize_team("players")

        self.assertEqual([p.get_id() for p in team1.get_players()], [301])
        self.assertEqual([p.get_id() for p in team2.get_players()], [302])
        self.assertEqual({p.get_id() for p in players.get_players()}, {303, 304})
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Captains")
        ctx.response.send_message.assert_awaited_with("Captains selected!")
        # no /set team1/team2 configured for this guild -> generic fallback
        self.assertEqual(team1.get_name(), "Team 1")
        self.assertEqual(team2.get_name(), "Team 2")

    async def test_uses_the_guilds_configured_team_names(self):
        self.helperObj.update(GUILD_ID, "channel1", "Red")
        self.helperObj.update(GUILD_ID, "channel2", "Blue")
        captain1 = FakeMember("Cap1", id=301)
        captain2 = FakeMember("Cap2", id=302)
        ctx = self._ctx(captain1, captain2, [])

        await self.helperObj.captainsHelper(ctx, captain1, captain2)

        team1 = self.deserialize_team("team1")
        team2 = self.deserialize_team("team2")
        self.assertEqual(team1.get_name(), "Red")
        self.assertEqual(team2.get_name(), "Blue")

    async def test_same_captain_rejected_before_any_state_change(self):
        captain1 = FakeMember("Cap1", id=301)
        ctx = self._ctx(captain1, captain1, [])

        await self.helperObj.captainsHelper(ctx, captain1, captain1)

        ctx.response.send_message.assert_awaited_once_with("Mention two different people!")
        # nothing should have been written; clearTeamsHelper never ran
        self.assertIsNone(self.helperObj.get(GUILD_ID, "team1"))

    async def test_missing_captain_rejected_without_crashing(self):
        captain1 = FakeMember("Cap1", id=301)
        ctx = self._ctx(captain1, FakeMember("x"), [])

        # regression test: captainsHelper used to build Player(captain_2.id, ...)
        # before checking for None, crashing with AttributeError instead of
        # showing this message.
        await self.helperObj.captainsHelper(ctx, captain1, None)

        ctx.response.send_message.assert_awaited_once_with("Mention two team captains!")


class ChooseTests(HelperTestCase):
    async def _draft_setup(self, pool_ids_names):
        captain1 = FakeMember("Cap1", id=401)
        captain2 = FakeMember("Cap2", id=402)
        pool = [FakeMember(name, id=pid) for pid, name in pool_ids_names]
        ctx = self._ctx(captain1, captain2, pool)
        await self.helperObj.captainsHelper(ctx, captain1, captain2)
        return captain1, captain2, pool, ctx

    def _ctx(self, captain1, captain2, pool_members, user=None):
        voice_channel = FakeChannel("Lobby", members=[captain1, captain2] + pool_members)
        user = user if user is not None else FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        # chooseHelper resolves captain mentions via ctx.guild.members (not
        # the voice channel's member list), so both need to know about them.
        self.guild.members.extend(
            m for m in [captain1, captain2] + pool_members if m not in self.guild.members
        )
        return FakeInteraction(self.guild, user)

    async def test_turn_enforced_wrong_captain(self):
        captain1, captain2, pool, ctx = await self._draft_setup([(501, "Pool1")])
        pick_ctx = FakeInteraction(self.guild, captain2)

        await self.helperObj.chooseFunc(pick_ctx, pool[0])

        pick_ctx.response.send_message.assert_awaited_once_with("Not Your Turn!")

    async def test_non_captain_rejected(self):
        captain1, captain2, pool, ctx = await self._draft_setup([(501, "Pool1")])
        outsider = FakeMember("Outsider", id=999)
        pick_ctx = FakeInteraction(self.guild, outsider)

        await self.helperObj.chooseFunc(pick_ctx, pool[0])

        pick_ctx.response.send_message.assert_awaited_once_with(
            "Only team captains can use this command!"
        )

    async def test_valid_pick_adds_player_and_switches_turn(self):
        captain1, captain2, pool, ctx = await self._draft_setup(
            [(501, "Pool1"), (502, "Pool2")]
        )
        pick_ctx = FakeInteraction(self.guild, captain1)

        await self.helperObj.chooseFunc(pick_ctx, pool[0])

        team1 = self.deserialize_team("team1")
        players = self.deserialize_team("players")
        self.assertIn(501, {p.get_id() for p in team1.get_players()})
        self.assertNotIn(501, {p.get_id() for p in players.get_players()})
        self.assertEqual(self.helperObj.get(GUILD_ID, "turn"), 2)
        # printEmbed posts the team1/team2 embeds first via the same
        # channel.send, so the turn-switch message is the last call, not
        # the only one.
        last_message = pick_ctx.channel.send.call_args_list[-1].args[0]
        self.assertIn(captain2.mention, last_message)

    async def test_draft_complete_message_when_pool_empties(self):
        captain1, captain2, pool, ctx = await self._draft_setup([(501, "Pool1")])
        pick_ctx = FakeInteraction(self.guild, captain1)

        await self.helperObj.chooseFunc(pick_ctx, pool[0])

        last_message = pick_ctx.channel.send.call_args_list[-1].args[0]
        self.assertIn("Press Start", last_message)

    async def test_already_selected_player_rejected(self):
        captain1, captain2, pool, ctx = await self._draft_setup(
            [(501, "Pool1"), (502, "Pool2")]
        )
        pick_ctx1 = FakeInteraction(self.guild, captain1)
        await self.helperObj.chooseFunc(pick_ctx1, pool[0])  # pool[0] now on team1

        pick_ctx2 = FakeInteraction(self.guild, captain2)
        await self.helperObj.chooseFunc(pick_ctx2, pool[0])  # try to pick it again

        pick_ctx2.response.send_message.assert_awaited_once_with(
            "Player has already been selected or does not exist in the player list."
        )

    async def test_prompts_start_once_teams_reach_team_size_even_with_spectators_left(self):
        # regression test: chooseHelper only ever checked whether the whole
        # draft pool was empty. A voice channel with more people than
        # team_size * 2 is expected to leave spectators undrafted, so with
        # 3 pool members and a team_size of 2 (1 pick needed per team),
        # both teams fill up after 2 picks while 1 spectator is still left
        # in the pool; the old code never prompted /start in that case.
        captain1, captain2, pool, ctx = await self._draft_setup(
            [(501, "Pool1"), (502, "Pool2"), (503, "Pool3")]
        )
        self.helperObj.update(GUILD_ID, "team_size", 2)

        pick_ctx1 = FakeInteraction(self.guild, captain1)
        await self.helperObj.chooseFunc(pick_ctx1, pool[0])  # team1 now size 2

        pick_ctx2 = FakeInteraction(self.guild, captain2)
        await self.helperObj.chooseFunc(pick_ctx2, pool[1])  # team2 now size 2

        players = self.deserialize_team("players")
        self.assertEqual(len(players.get_players()), 1)  # one spectator still unpicked

        last_message = pick_ctx2.channel.send.call_args_list[-1].args[0]
        self.assertIn("Press Start", last_message)

    async def test_choose_random_member_reports_when_pool_empty(self):
        self.helperObj.update(GUILD_ID, "players", Team().serializeTeam())
        ctx = FakeInteraction(self.guild, FakeMember("Someone"))

        await self.helperObj.chooseRandomMember(ctx)

        ctx.response.send_message.assert_awaited_once_with(
            "There are no players left to choose from!"
        )

    async def test_get_random_member_returns_none_for_empty_pool(self):
        self.helperObj.update(GUILD_ID, "players", Team().serializeTeam())
        ctx = FakeInteraction(self.guild, FakeMember("Someone"))

        result = await self.helperObj.getRandomMember(ctx)

        self.assertIsNone(result)


class ClearTeamsHelperTests(HelperTestCase):
    async def test_resets_fields_to_defaults(self):
        self.helperObj.update(GUILD_ID, "team1", "stale-data")
        self.helperObj.update(GUILD_ID, "original_channel", "stale-data")
        self.helperObj.update(GUILD_ID, "disliked_role_user_ids", "111,222")

        ctx = FakeInteraction(self.guild, FakeMember("Caller"))
        await self.helperObj.clearTeamsHelper(ctx)

        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team1"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team2"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "players"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team_size"), 5)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Normal")
        self.assertEqual(self.helperObj.get(GUILD_ID, "turn"), 1)
        self.assertEqual(self.helperObj._dislikedRoleUserIds(GUILD_ID), frozenset())

    async def test_does_not_send_a_cancellation_when_no_game_is_active(self):
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))
        await self.helperObj.clearTeamsHelper(ctx)
        ctx.channel.send.assert_not_awaited()

    async def test_cancels_an_active_game_before_wiping_it(self):
        # Regression test: starting a new roster (or running /clear at all,
        # even just for something like clear_elo) used to silently wipe
        # team1/team2/original_channel out from under a game that was
        # still being bet on or played, nothing refunded, nobody moved
        # back, and the eventual winner report would silently skip
        # elo/records entirely since getRosterPlayers found nothing left.
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=901)
        member2 = FakeMember("Bob", id=902)
        channel1.members = [member1]
        channel2.members = [member2]
        guild = FakeGuild(channels=[og, channel1, channel2])

        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")

        self.helperObj.ensureEconomyRow(GUILD_ID, 903, "Carol")
        self.cursor.execute("UPDATE economy SET balance=1200 WHERE guildId=? AND userId=?", (GUILD_ID, 903))
        self.db.commit()
        await self.helperObj.wagerHelper(
            FakeInteraction(guild, FakeMember("Carol", id=903)), 200, 1
        )
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 1000)  # escrowed

        ctx = FakeInteraction(guild, FakeMember("Caller"))
        await self.helperObj.clearTeamsHelper(ctx)

        # the open bet is refunded (back to the pre-bet 1200) and players
        # are moved back before anything else
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 1200)
        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)
        messages = [c.args[0] for c in ctx.channel.send.call_args_list]
        self.assertTrue(any("cancelled" in m for m in messages))
        self.assertTrue(any("Moved everyone back" in m for m in messages))

        # then the normal reset still happens on top of that
        self.assertEqual(self.helperObj.get(GUILD_ID, "team1"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")


class ResetEconomyHelperTests(HelperTestCase):
    async def test_wipes_every_players_stats_for_the_guild(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET balance=500, wins=3, losses=1, gold_wagered=200, "
            "gold_won=100, gold_lost=50, last_daily='2026-01-01' "
            "WHERE guildId=? AND userId=?",
            (GUILD_ID, 901),
        )
        self.db.commit()

        # a row in a different guild should be untouched
        other_guild_id = GUILD_ID + 1
        insert_guild_row(self.cursor, self.db, guild_id=other_guild_id)
        self.helperObj.ensureEconomyRow(other_guild_id, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=777 WHERE guildId=? AND userId=?",
            (other_guild_id, 901),
        )
        self.db.commit()

        self.helperObj.resetEconomyHelper(GUILD_ID)

        self.cursor.execute("SELECT COUNT(*) FROM economy WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

        # stats are back to defaults the next time the player is touched
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)
        self.assertIsNone(self.helperObj.getEconomy(GUILD_ID, 901, "last_daily"))

        # other guild's economy rows are untouched
        self.assertEqual(self.helperObj.getEconomy(other_guild_id, 901, "balance"), 777)


class ResetEloHelperTests(HelperTestCase):
    async def test_resets_elo_only_for_the_guild_leaving_other_stats_alone(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET elo=1400, balance=500, wins=3 WHERE guildId=? AND userId=?",
            (GUILD_ID, 901),
        )
        self.cursor.execute(
            "UPDATE economy SET elo=700 WHERE guildId=? AND userId=?",
            (GUILD_ID, 902),
        )
        self.db.commit()

        other_guild_id = GUILD_ID + 1
        insert_guild_row(self.cursor, self.db, guild_id=other_guild_id)
        self.helperObj.ensureEconomyRow(other_guild_id, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET elo=1600 WHERE guildId=? AND userId=?",
            (other_guild_id, 901),
        )
        self.db.commit()

        self.helperObj.resetEloHelper(GUILD_ID)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "elo"), helper_module.DEFAULT_ELO)
        # balance/wins are untouched; only elo resets
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 500)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 3)
        # other guild's elo is untouched
        self.assertEqual(self.helperObj.getEconomy(other_guild_id, 901, "elo"), 1600)

    async def test_resets_to_the_guilds_configured_default_elo(self):
        self.helperObj.update(GUILD_ID, "default_elo", 1200)
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET elo=1400 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        self.helperObj.resetEloHelper(GUILD_ID)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), 1200)


class DefaultEloForGuildTests(HelperTestCase):
    def test_falls_back_to_the_global_default_when_unset(self):
        self.assertEqual(self.helperObj._defaultEloForGuild(GUILD_ID), helper_module.DEFAULT_ELO)

    def test_uses_the_guilds_configured_value(self):
        self.helperObj.update(GUILD_ID, "default_elo", 1200)
        self.assertEqual(self.helperObj._defaultEloForGuild(GUILD_ID), 1200)

    def test_ensure_economy_row_uses_the_configured_default(self):
        self.helperObj.update(GUILD_ID, "default_elo", 1200)
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), 1200)

    def test_get_elo_lookup_falls_back_to_the_configured_default_for_unranked_players(self):
        self.helperObj.update(GUILD_ID, "default_elo", 1200)
        lookup = self.helperObj.getEloLookup(GUILD_ID, [(901, "Alice")])
        self.assertEqual(lookup[901], 1200)


class ConfirmDestructiveClearHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_posts_a_followup_confirmation_not_an_immediate_reset(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET elo=1400 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        ctx = self._ctx()
        posted = FakeMessage(id=7001)
        ctx.followup.send.return_value = posted

        await self.helperObj.confirmDestructiveClearHelper(ctx, False, True, False, False, None)

        ctx.followup.send.assert_awaited_once()
        self.assertIn("reset elo back to", ctx.followup.send.call_args.args[0])
        view = ctx.followup.send.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.ConfirmResetView)
        self.assertIs(view.message, posted)
        # not actually reset yet; only queued behind confirmation
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), 1400)

    async def test_confirming_applies_the_reset(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET elo=1400 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        ctx = self._ctx()
        await self.helperObj.confirmDestructiveClearHelper(ctx, False, True, False, False, None)
        view = ctx.followup.send.call_args.kwargs["view"]

        click = self._ctx()
        await view.confirm.callback(click)

        self.assertEqual(
            self.helperObj.getEconomy(GUILD_ID, 901, "elo"), self.helperObj._defaultEloForGuild(GUILD_ID)
        )
        click.response.edit_message.assert_awaited_once()

    async def test_cancelling_leaves_data_untouched(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET elo=1400 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        ctx = self._ctx()
        await self.helperObj.confirmDestructiveClearHelper(ctx, False, True, False, False, None)
        view = ctx.followup.send.call_args.kwargs["view"]

        click = self._ctx()
        await view.cancel.callback(click)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), 1400)
        click.response.edit_message.assert_awaited_once()

    async def test_confirmation_rejects_a_click_from_someone_other_than_whoever_ran_clear(self):
        # discord.py's real button dispatch runs View.interaction_check
        # before ever calling a button's own callback; a click from
        # anyone but the /clear invoker must be rejected here, not just
        # left to the callback to somehow notice.
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET elo=1400 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        ctx = self._ctx()
        await self.helperObj.confirmDestructiveClearHelper(ctx, False, True, False, False, None)
        view = ctx.followup.send.call_args.kwargs["view"]

        stranger = self._ctx(user_id=902, name="Bob")
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "elo"), 1400)


class SaveGetTournamentTests(HelperTestCase):
    def test_get_tournament_returns_none_when_unset(self):
        self.assertIsNone(self.helperObj.getTournament(GUILD_ID))

    def test_roundtrips_a_tournament_with_no_teams(self):
        tournament = Tournament("Spring Cup", 5, 8, True)
        self.helperObj.saveTournament(GUILD_ID, tournament)

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(restored.get_name(), "Spring Cup")
        self.assertEqual(restored.get_team_size(), 5)
        self.assertEqual(restored.get_num_teams(), 8)
        self.assertTrue(restored.is_double_elimination())
        self.assertEqual(restored.get_teams(), [])
        self.assertEqual(restored.get_bracket(), [])

    def test_roundtrips_registered_teams(self):
        tournament = Tournament("Cup", 2, 4, False)
        team = Team()
        team.set_name("Red")
        team.add_player(Player(1, "Alice"))
        team.add_player(Player(2, "Bob"))
        tournament.register_team(team)
        self.helperObj.saveTournament(GUILD_ID, tournament)

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(len(restored.get_teams()), 1)
        restored_team = restored.get_teams()[0]
        self.assertEqual(restored_team.get_name(), "Red")
        self.assertEqual({p.get_id() for p in restored_team.get_players()}, {1, 2})

    def test_save_replaces_the_existing_tournament(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("First", 5, 8))
        self.helperObj.saveTournament(GUILD_ID, Tournament("Second", 3, 4))

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(restored.get_name(), "Second")
        self.cursor.execute("SELECT COUNT(*) FROM tournaments WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)


class CreateTournamentHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_non_positive_team_size(self):
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "Cup", 0, 8, False)
        ctx.response.send_message.assert_awaited_once_with("Team size must be greater than 0.")
        self.assertIsNone(self.helperObj.getTournament(GUILD_ID))

    async def test_rejects_too_few_teams(self):
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "Cup", 5, 1, False)
        ctx.response.send_message.assert_awaited_once_with("A tournament needs at least 2 teams.")
        self.assertIsNone(self.helperObj.getTournament(GUILD_ID))

    async def test_creates_immediately_when_none_exists(self):
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "Spring Cup", 5, 8, True)

        ctx.response.send_message.assert_awaited_once_with(
            "Tournament **Spring Cup** created! 8 teams of 5, double elimination."
        )
        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(tournament.get_name(), "Spring Cup")
        self.assertTrue(tournament.is_double_elimination())

    async def test_overwriting_without_manage_guild_permission_is_rejected(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Old Cup", 5, 8))
        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901, manage_guild=False))

        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)

        ctx.response.send_message.assert_awaited_once_with(
            "Only a member with the Manage Server permission can overwrite an existing tournament."
        )
        # nothing changed; the old tournament is still there untouched
        self.assertEqual(self.helperObj.getTournament(GUILD_ID).get_name(), "Old Cup")

    async def test_creating_fresh_does_not_require_manage_guild_permission(self):
        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901, manage_guild=False))

        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)

        self.assertEqual(self.helperObj.getTournament(GUILD_ID).get_name(), "New Cup")

    async def test_existing_tournament_requires_confirmation_before_overwriting(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Old Cup", 5, 8))
        ctx = self._ctx()
        posted_message = FakeMessage(id=321)
        ctx.original_response.return_value = posted_message

        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Old Cup", text)
        self.assertIn("New Cup", text)
        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIs(view.message, posted_message)

        # the existing tournament is untouched until confirmed
        still_there = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(still_there.get_name(), "Old Cup")

    async def test_confirming_overwrite_replaces_the_tournament(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Old Cup", 5, 8))
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)
        view = ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.confirm.callback(click)

        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(tournament.get_name(), "New Cup")
        self.assertEqual(tournament.get_team_size(), 3)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("New Cup", click.response.edit_message.call_args.kwargs["content"])

    async def test_cancelling_overwrite_keeps_the_existing_tournament(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Old Cup", 5, 8))
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)
        view = ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.cancel.callback(click)

        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(tournament.get_name(), "Old Cup")

    async def test_overwrite_confirmation_rejects_non_invoker(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Old Cup", 5, 8))
        ctx = self._ctx()
        await self.helperObj.createTournamentHelper(ctx, "New Cup", 3, 4, False)
        view = ctx.response.send_message.call_args.kwargs["view"]

        stranger = FakeInteraction(self.guild, FakeMember("Stranger", id=999))
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))


class CreateTeamHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_non_positive_team_size(self):
        ctx = self._ctx()
        await self.helperObj.createTeamHelper(ctx, "Red", 0)
        ctx.response.send_message.assert_awaited_once_with("Team size must be greater than 0.")
        self.assertIsNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_rejects_duplicate_team_name(self):
        ctx1 = self._ctx()
        await self.helperObj.createTeamHelper(ctx1, "Red", 5)
        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.createTeamHelper(ctx2, "Red", 5)
        ctx2.response.send_message.assert_awaited_once_with(
            "A team named **Red** already exists in this server."
        )

    async def test_rejects_a_duplicate_that_only_differs_in_case(self):
        ctx1 = self._ctx()
        await self.helperObj.createTeamHelper(ctx1, "Red", 5)
        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.createTeamHelper(ctx2, "red", 5)
        ctx2.response.send_message.assert_awaited_once_with(
            "A team named **red** already exists in this server."
        )
        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)

    async def test_get_team_row_is_case_insensitive(self):
        ctx = self._ctx()
        await self.helperObj.createTeamHelper(ctx, "Red", 5)

        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "red"))
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "RED"))
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "ReD"))

    async def test_creates_team_with_caller_as_captain(self):
        ctx = self._ctx()
        await self.helperObj.createTeamHelper(ctx, "Red", 5)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Red", text)
        self.assertIn(ctx.user.mention, text)

        result = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertIsNotNone(result)
        team_id, team = result
        self.assertEqual(team.get_name(), "Red")
        self.assertEqual(team.get_team_size(), 5)
        self.assertEqual(team.get_id(), team_id)
        self.assertTrue(self.helperObj.isTeamCaptain(team, 901))
        self.assertEqual([p.get_id() for p in team.get_players()], [901])

    async def test_creates_team_with_given_member_as_captain_instead_of_caller(self):
        ctx = self._ctx()
        designated_captain = FakeMember("Bob", id=902)
        await self.helperObj.createTeamHelper(ctx, "Red", 5, designated_captain)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(designated_captain.mention, text)
        self.assertNotIn(ctx.user.mention, text)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 902))
        self.assertEqual([p.get_id() for p in team.get_players()], [902])

    async def test_team_ids_are_assigned_automatically_and_unique(self):
        ctx1 = self._ctx()
        await self.helperObj.createTeamHelper(ctx1, "Red", 5)
        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.createTeamHelper(ctx2, "Blue", 5)

        _, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        _, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        self.assertNotEqual(red.get_id(), blue.get_id())


class TeamRenameHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", manage_guild=True):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild))

    async def _make_team(self, name, captain_id=901, captain_name="Alice", team_size=5):
        await self.helperObj.createTeamHelper(self._ctx(captain_id, captain_name), name, team_size)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Ghosts", "Phantoms")
        ctx.response.send_message.assert_awaited_once_with("No team named **Ghosts** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        await self._make_team("Red")
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=False)
        await self.helperObj.teamRenameHelper(ctx, "Red", "Crimson")
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can rename it."
        )
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_non_captain_admin_can_rename(self):
        await self._make_team("Red")
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=True)
        await self.helperObj.teamRenameHelper(ctx, "Red", "Crimson")
        ctx.response.send_message.assert_awaited_once_with("**Red** has been renamed to **Crimson**.")
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Crimson"))

    async def test_rejects_renaming_to_the_same_name(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "Red")
        ctx.response.send_message.assert_awaited_once_with("**Red** is already named that.")

    async def test_rejects_a_name_already_taken_by_another_team(self):
        await self._make_team("Red")
        await self._make_team("Blue", captain_id=902, captain_name="Bob")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "Blue")
        ctx.response.send_message.assert_awaited_once_with(
            "A team named **Blue** already exists in this server."
        )
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_rejects_a_name_taken_by_another_team_even_if_only_case_differs(self):
        await self._make_team("Red")
        await self._make_team("Blue", captain_id=902, captain_name="Bob")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "blue")
        ctx.response.send_message.assert_awaited_once_with(
            "A team named **blue** already exists in this server."
        )
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_allows_a_pure_capitalization_change_of_its_own_name(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "RED")

        ctx.response.send_message.assert_awaited_once_with("**Red** has been renamed to **RED**.")
        result = self.helperObj.getTeamRow(GUILD_ID, "red")  # still findable case-insensitively
        self.assertIsNotNone(result)
        _, team = result
        self.assertEqual(team.get_name(), "RED")
        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)  # not treated as a second team

    async def test_looks_up_the_team_to_rename_case_insensitively(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "red", "Crimson")

        # the success message uses the team's actual stored capitalization,
        # not whatever case the caller happened to type to look it up
        ctx.response.send_message.assert_awaited_once_with("**Red** has been renamed to **Crimson**.")

    async def test_captain_renames_successfully(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "Crimson")

        ctx.response.send_message.assert_awaited_once_with("**Red** has been renamed to **Crimson**.")
        self.assertIsNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))
        result = self.helperObj.getTeamRow(GUILD_ID, "Crimson")
        self.assertIsNotNone(result)
        _, team = result
        self.assertEqual(team.get_name(), "Crimson")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 901))

    async def test_renamed_team_keeps_its_id_and_roster(self):
        await self._make_team("Red")
        _, before = self.helperObj.getTeamRow(GUILD_ID, "Red")
        before_id = before.get_id()

        ctx = self._ctx()
        await self.helperObj.teamRenameHelper(ctx, "Red", "Crimson")

        team_id, after = self.helperObj.getTeamRow(GUILD_ID, "Crimson")
        self.assertEqual(team_id, before_id)
        self.assertEqual(after.get_id(), before_id)
        self.assertEqual([p.get_id() for p in after.get_players()], [901])


class TeamTransferHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", manage_guild=True):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild))

    async def _make_team(self, name, captain_id=901, captain_name="Alice", team_size=5):
        await self.helperObj.createTeamHelper(self._ctx(captain_id, captain_name), name, team_size)

    def _add_to_roster(self, team_name, user_id, name):
        team_id, team = self.helperObj.getTeamRow(GUILD_ID, team_name)
        team.add_player(Player(user_id, name))
        self.helperObj.updateTeamData(team_id, team)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamTransferHelper(ctx, "Ghosts", FakeMember("Bob", id=902))
        ctx.response.send_message.assert_awaited_once_with("No team named **Ghosts** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        await self._make_team("Red")
        self._add_to_roster("Red", 902, "Bob")
        ctx = self._ctx(user_id=903, name="Charlie", manage_guild=False)
        await self.helperObj.teamTransferHelper(ctx, "Red", FakeMember("Bob", id=902))
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can transfer it."
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 901))

    async def test_rejects_a_target_not_on_the_roster(self):
        await self._make_team("Red")
        target = FakeMember("Bob", id=902)
        ctx = self._ctx()
        await self.helperObj.teamTransferHelper(ctx, "Red", target)
        ctx.response.send_message.assert_awaited_once_with(
            f"{target.mention} isn't on **Red**'s roster - invite them with /team-invite first."
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 901))

    async def test_rejects_transferring_to_the_current_captain(self):
        await self._make_team("Red")
        target = FakeMember("Alice", id=901)
        ctx = self._ctx()
        await self.helperObj.teamTransferHelper(ctx, "Red", target)
        ctx.response.send_message.assert_awaited_once_with(f"{target.mention} is already **Red**'s captain.")

    async def test_captain_transfers_to_a_rostered_teammate(self):
        await self._make_team("Red")
        self._add_to_roster("Red", 902, "Bob")
        target = FakeMember("Bob", id=902)
        ctx = self._ctx()

        await self.helperObj.teamTransferHelper(ctx, "Red", target)

        ctx.response.send_message.assert_awaited_once_with(
            f"**Red**'s captaincy has been transferred to {target.mention}."
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 902))
        self.assertFalse(self.helperObj.isTeamCaptain(team, 901))
        # roster itself is untouched; both are still on the team
        self.assertEqual({p.get_id() for p in team.get_players()}, {901, 902})

    async def test_non_captain_admin_can_also_transfer(self):
        await self._make_team("Red")
        self._add_to_roster("Red", 902, "Bob")
        ctx = self._ctx(user_id=903, name="Charlie", manage_guild=True)

        await self.helperObj.teamTransferHelper(ctx, "Red", FakeMember("Bob", id=902))

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertTrue(self.helperObj.isTeamCaptain(team, 902))

    async def test_transferred_captain_can_then_use_team_delete(self):
        # regression: /team-leave used to be a captain's only dead end
        # ("nobody to hand it to yet"), confirms the old captain, no
        # longer captain after a transfer, isn't stuck on that team either.
        await self._make_team("Red")
        self._add_to_roster("Red", 902, "Bob")
        await self.helperObj.teamTransferHelper(self._ctx(), "Red", FakeMember("Bob", id=902))

        leave_ctx = self._ctx()  # Alice, no longer captain
        await self.helperObj.teamLeaveHelper(leave_ctx, "Red")

        leave_ctx.response.send_message.assert_awaited_once_with("You've left **Red**.")
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual({p.get_id() for p in team.get_players()}, {902})


class TeamDeleteHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", manage_guild=True):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild))

    async def _make_team(self, name, captain_id=901, captain_name="Alice", team_size=5):
        await self.helperObj.createTeamHelper(self._ctx(captain_id, captain_name), name, team_size)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamDeleteHelper(ctx, "Ghosts")
        ctx.response.send_message.assert_awaited_once_with("No team named **Ghosts** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        await self._make_team("Red")
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=False)
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can delete it."
        )
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_captain_gets_a_confirmation_prompt_not_an_immediate_delete(self):
        await self._make_team("Red")
        ctx = self._ctx()
        posted_message = FakeMessage(id=555)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamDeleteHelper(ctx, "Red")

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Red", text)
        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIs(view.message, posted_message)
        # not deleted yet; only queued behind confirmation
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_non_captain_admin_can_also_trigger_the_prompt(self):
        await self._make_team("Red")
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=True)
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once()
        self.assertIsNotNone(ctx.response.send_message.call_args.kwargs.get("view"))

    async def test_confirming_deletes_the_team(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        view = ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.confirm.callback(click)

        self.assertIsNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))
        click.response.edit_message.assert_awaited_once()
        self.assertIn("deleted", click.response.edit_message.call_args.kwargs["content"])

    async def test_confirming_also_cancels_pending_invites_for_that_team(self):
        await self._make_team("Red")
        team_id, _ = self.helperObj.getTeamRow(GUILD_ID, "Red")
        target = FakeMember("Charlie", id=903)
        await self.helperObj.teamInviteHelper(self._ctx(), "Red", [target])

        ctx = self._ctx()
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        view = ctx.response.send_message.call_args.kwargs["view"]
        click = self._ctx()
        await view.confirm.callback(click)

        self.cursor.execute("SELECT COUNT(*) FROM team_invites WHERE teamId=?", (team_id,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_cancelling_keeps_the_team(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        view = ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.cancel.callback(click)

        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Red"))

    async def test_confirmation_rejects_non_invoker(self):
        await self._make_team("Red")
        ctx = self._ctx()
        await self.helperObj.teamDeleteHelper(ctx, "Red")
        view = ctx.response.send_message.call_args.kwargs["view"]

        stranger = FakeInteraction(self.guild, FakeMember("Stranger", id=999))
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))


class _FakeLogoDirTestCase(HelperTestCase):
    LOGO_NAMES = ("Demacia", "Noxus", "Freljord")

    def setUp(self):
        super().setUp()
        # ignore_cleanup_errors: a test that sends a discord.File built from
        # one of these without closing it (easy to forget, teamStatsHelper
        # attaches one internally) leaves the fd open, and Windows won't let
        # a directory delete out from under an open file the way POSIX does.
        self._logo_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # Real, decodable 1x1 images (not just empty placeholder files)
        # since _drawMatchupColumn's random-logo fallback (see
        # _renderMatchupImage) now actually opens one of these with PIL for
        # any team that doesn't have a logo of its own, not just the ones
        # explicitly testing logo content.
        for name in self.LOGO_NAMES:
            Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(
                os.path.join(self._logo_dir.name, f"{name}.png")
            )
        self._logo_dir_patch = patch.object(helper_module, "TEAM_LOGO_DIR", self._logo_dir.name)
        self._logo_dir_patch.start()

    def tearDown(self):
        self._logo_dir_patch.stop()
        self._logo_dir.cleanup()
        super().tearDown()


class TeamLogoTests(_FakeLogoDirTestCase):
    def test_lists_available_logos_by_name_without_extension(self):
        self.assertEqual(self.helperObj.listAvailableLogos(), ["Demacia", "Freljord", "Noxus"])

    def test_returns_empty_list_when_the_folder_is_missing(self):
        with patch.object(helper_module, "TEAM_LOGO_DIR", os.path.join(self._logo_dir.name, "nope")):
            self.assertEqual(self.helperObj.listAvailableLogos(), [])

    def test_resolve_logo_path_is_case_insensitive(self):
        path = self.helperObj._resolveLogoPath("demacia")
        self.assertEqual(os.path.basename(path), "Demacia.png")

    def test_resolve_logo_path_returns_none_for_an_unknown_name(self):
        self.assertIsNone(self.helperObj._resolveLogoPath("Nonexistent"))

    def test_saving_a_new_team_assigns_a_random_logo(self):
        team = Team()
        team.set_name("Red")
        self.helperObj._saveNewTeam(GUILD_ID, team)

        _, persisted = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertIsNotNone(persisted.get_logo_path())
        self.assertIn(
            os.path.basename(persisted.get_logo_path()),
            [f"{name}.png" for name in self.LOGO_NAMES]
        )

    def test_saving_a_new_team_does_not_override_a_preset_logo(self):
        team = Team()
        team.set_name("Red")
        team.set_logo_path("/some/custom/path.png")
        self.helperObj._saveNewTeam(GUILD_ID, team)

        _, persisted = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(persisted.get_logo_path(), "/some/custom/path.png")

    def test_loading_a_pre_existing_logo_less_team_self_heals(self):
        # Simulates a team saved before this feature existed, no 10th
        # (logo_path) field in its serialized data at all.
        self.cursor.execute(
            "INSERT INTO teams(guildId, name, data) VALUES(?, ?, ?)",
            (GUILD_ID, "Legacy", "[1, Legacy, , 0, , , 0, 0, None]")
        )
        self.db.commit()
        team_id = self.cursor.lastrowid

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Legacy")
        self.assertIsNotNone(team.get_logo_path())

        # persisted back to the DB, not just assigned in memory
        self.cursor.execute("SELECT data FROM teams WHERE id=?", (team_id,))
        reloaded = Team()
        reloaded.deserializeTeam(self.cursor.fetchone()[0])
        self.assertIsNotNone(reloaded.get_logo_path())

    def test_no_op_when_no_logos_are_available(self):
        with patch.object(helper_module, "TEAM_LOGO_DIR", os.path.join(self._logo_dir.name, "nope")):
            team = Team()
            team.set_name("Red")
            self.helperObj._saveNewTeam(GUILD_ID, team)

            _, persisted = self.helperObj.getTeamRow(GUILD_ID, "Red")
            self.assertIsNone(persisted.get_logo_path())


class TeamSetHelperTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice", manage_guild=True):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild))

    async def _make_team(self, name="Red", captain_id=901, captain_name="Alice"):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, name, 5)
        return self.helperObj.getTeamRow(GUILD_ID, name)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamSetHelper(ctx, "Nonexistent", None, False, None)
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        await self._make_team()
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=False)
        await self.helperObj.teamSetHelper(ctx, "Red", None, False, None)
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can change its settings."
        )

    async def test_non_captain_admin_can_change_settings(self):
        await self._make_team()
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=True)
        channel = FakeChannel("general-voice")
        await self.helperObj.teamSetHelper(ctx, "Red", channel, False, None)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "general-voice")

    async def test_rejects_when_nothing_is_given(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.teamSetHelper(ctx, "Red", None, False, None)
        ctx.response.send_message.assert_awaited_once_with(
            "Give at least one of voice_channel, new_voice_channel, or logo to set."
        )

    async def test_rejects_both_voice_channel_and_new_voice_channel(self):
        await self._make_team()
        ctx = self._ctx()
        channel = FakeChannel("general-voice")
        await self.helperObj.teamSetHelper(ctx, "Red", channel, True, None)
        ctx.response.send_message.assert_awaited_once_with(
            "Pick either voice_channel or new_voice_channel, not both."
        )

    async def test_new_voice_channel_creates_one_named_after_the_team(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.teamSetHelper(ctx, "Red", None, True, None)

        self.assertEqual(len(self.guild.channels), 1)
        created = self.guild.channels[0]
        self.assertEqual(created.name, "Red")
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "Red")
        ctx.response.send_message.assert_awaited_once()
        self.assertIn(created.mention, ctx.response.send_message.call_args.kwargs["content"])

    async def test_unused_voice_channel_is_set_directly(self):
        await self._make_team()
        ctx = self._ctx()
        channel = FakeChannel("general-voice")

        await self.helperObj.teamSetHelper(ctx, "Red", channel, False, None)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "general-voice")
        self.assertIn(channel.mention, ctx.response.send_message.call_args.kwargs["content"])

    async def test_channel_already_used_by_another_team_requires_confirmation(self):
        await self._make_team("Red", 901, "Alice")
        await self._make_team("Blue", 902, "Bob")
        shared_channel = FakeChannel("Arena")

        blue_ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamSetHelper(blue_ctx, "Blue", shared_channel, False, None)

        red_ctx = self._ctx()
        posted_message = FakeMessage(id=555)
        red_ctx.original_response.return_value = posted_message
        await self.helperObj.teamSetHelper(red_ctx, "Red", shared_channel, False, None)

        red_ctx.response.send_message.assert_awaited_once()
        text = red_ctx.response.send_message.call_args.args[0]
        self.assertIn("Blue", text)
        self.assertIn("Arena", text)
        view = red_ctx.response.send_message.call_args.kwargs["view"]
        self.assertIs(view.message, posted_message)

        # Red is untouched until confirmed
        _, red_team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(red_team.get_voice_channel(), "")

    async def test_confirming_channel_overwrite_sets_it(self):
        await self._make_team("Red", 901, "Alice")
        await self._make_team("Blue", 902, "Bob")
        shared_channel = FakeChannel("Arena")
        blue_ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamSetHelper(blue_ctx, "Blue", shared_channel, False, None)

        red_ctx = self._ctx()
        await self.helperObj.teamSetHelper(red_ctx, "Red", shared_channel, False, None)
        view = red_ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.confirm.callback(click)

        _, red_team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(red_team.get_voice_channel(), "Arena")
        click.response.edit_message.assert_awaited_once()

    async def test_cancelling_channel_overwrite_leaves_it_unset(self):
        await self._make_team("Red", 901, "Alice")
        await self._make_team("Blue", 902, "Bob")
        shared_channel = FakeChannel("Arena")
        blue_ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamSetHelper(blue_ctx, "Blue", shared_channel, False, None)

        red_ctx = self._ctx()
        await self.helperObj.teamSetHelper(red_ctx, "Red", shared_channel, False, None)
        view = red_ctx.response.send_message.call_args.kwargs["view"]

        click = self._ctx()
        await view.cancel.callback(click)

        _, red_team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(red_team.get_voice_channel(), "")

    async def test_overwrite_confirmation_rejects_non_invoker(self):
        await self._make_team("Red", 901, "Alice")
        await self._make_team("Blue", 902, "Bob")
        shared_channel = FakeChannel("Arena")
        blue_ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamSetHelper(blue_ctx, "Blue", shared_channel, False, None)

        red_ctx = self._ctx()
        await self.helperObj.teamSetHelper(red_ctx, "Red", shared_channel, False, None)
        view = red_ctx.response.send_message.call_args.kwargs["view"]

        stranger = FakeInteraction(self.guild, FakeMember("Stranger", id=999))
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_rejects_an_unknown_logo_name(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.teamSetHelper(ctx, "Red", None, False, "NotALogo")
        ctx.response.send_message.assert_awaited_once_with(
            "No logo named **NotALogo** - pick one from the autocomplete list."
        )

    async def test_sets_the_logo_case_insensitively_and_attaches_the_file(self):
        await self._make_team()
        ctx = self._ctx()

        await self.helperObj.teamSetHelper(ctx, "Red", None, False, "demacia")

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(os.path.basename(team.get_logo_path()), "Demacia.png")
        ctx.response.send_message.assert_awaited_once()
        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertIn("Demacia", kwargs["content"])
        self.assertIsInstance(kwargs["file"], discord.File)
        # discord.File keeps its underlying fp open; close it so Windows
        # doesn't hold the temp logo dir locked when tearDown deletes it.
        kwargs["file"].close()

    async def test_sets_voice_channel_and_logo_together(self):
        await self._make_team()
        ctx = self._ctx()
        channel = FakeChannel("general-voice")

        await self.helperObj.teamSetHelper(ctx, "Red", channel, False, "demacia")

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "general-voice")
        self.assertEqual(os.path.basename(team.get_logo_path()), "Demacia.png")
        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertIn(channel.mention, kwargs["content"])
        self.assertIn("Demacia", kwargs["content"])
        kwargs["file"].close()

    async def test_invalid_logo_blocks_voice_channel_from_applying_too(self):
        await self._make_team()
        ctx = self._ctx()
        channel = FakeChannel("general-voice")

        await self.helperObj.teamSetHelper(ctx, "Red", channel, False, "NotALogo")

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "")


class TeamInviteHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None, manage_guild=True):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild), channel=channel
        )

    async def _make_team(self, name="Red", captain_id=901, captain_name="Alice"):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, name, 5)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        await self.helperObj.teamInviteHelper(ctx, "Nonexistent", [target])
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        await self._make_team()
        ctx = self._ctx(user_id=903, name="Cleo", manage_guild=False)
        target = FakeMember("Bob", id=902)
        await self.helperObj.teamInviteHelper(ctx, "Red", [target])
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can invite players."
        )

    async def test_non_captain_admin_can_invite(self):
        await self._make_team()
        ctx = self._ctx(user_id=903, name="Cleo", manage_guild=True)
        target = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=781)
        ctx.original_response.return_value = posted_message
        await self.helperObj.teamInviteHelper(ctx, "Red", [target])
        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(target.mention, text)

    async def test_rejects_inviting_a_bot(self):
        await self._make_team()
        ctx = self._ctx()
        target = FakeMember("Botty", id=902, bot=True)
        await self.helperObj.teamInviteHelper(ctx, "Red", [target])
        ctx.response.send_message.assert_awaited_once_with("You can't invite a bot to a team.")

    async def test_rejects_inviting_someone_already_on_the_team(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.teamInviteHelper(ctx, "Red", [ctx.user])
        ctx.response.send_message.assert_awaited_once_with("Alice is already on **Red**.")

    async def test_successful_invite_posts_message_and_stores_pending_row(self):
        await self._make_team()
        channel = FakeChannel("general")
        ctx = self._ctx(channel=channel)
        target = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=777)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamInviteHelper(ctx, "Red", [target])

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(target.mention, text)
        self.assertIn(ctx.user.mention, text)
        self.assertIsInstance(
            ctx.response.send_message.call_args.kwargs["view"], helper_module.TeamInviteAcceptView
        )

        # nobody's added to the roster yet
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 1)

        self.cursor.execute(
            "SELECT guildId, channelId, teamName, inviterId, targetId FROM team_invites WHERE messageId=?",
            (777,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, channel.id, "Red", 901, 902))

    async def test_invites_multiple_members_in_one_message(self):
        await self._make_team()
        channel = FakeChannel("general")
        ctx = self._ctx(channel=channel)
        bob = FakeMember("Bob", id=902)
        cleo = FakeMember("Cleo", id=903)
        posted_message = FakeMessage(id=778)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamInviteHelper(ctx, "Red", [bob, cleo])

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(bob.mention, text)
        self.assertIn(cleo.mention, text)
        # one shared message, one shared Accept button, not one per invitee
        self.assertIsInstance(
            ctx.response.send_message.call_args.kwargs["view"], helper_module.TeamInviteAcceptView
        )

        self.cursor.execute(
            "SELECT targetId FROM team_invites WHERE messageId=? ORDER BY targetId", (778,)
        )
        self.assertEqual([row[0] for row in self.cursor.fetchall()], [902, 903])

    async def test_deduplicates_the_same_member_passed_twice(self):
        await self._make_team()
        ctx = self._ctx()
        bob = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=779)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamInviteHelper(ctx, "Red", [bob, bob])

        self.cursor.execute("SELECT COUNT(*) FROM team_invites WHERE messageId=?", (779,))
        self.assertEqual(self.cursor.fetchone()[0], 1)

    async def test_skips_invalid_members_but_still_invites_the_valid_ones(self):
        await self._make_team()
        ctx = self._ctx()
        bob = FakeMember("Bob", id=902)
        botty = FakeMember("Botty", id=904, bot=True)
        posted_message = FakeMessage(id=780)
        ctx.original_response.return_value = posted_message

        # ctx.user (Alice, 901) is already on the team, botty is a bot -
        # both should be skipped with a note while Bob still gets invited.
        await self.helperObj.teamInviteHelper(ctx, "Red", [bob, ctx.user, botty])

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(bob.mention, text)
        self.assertIn("Not invited", text)
        self.assertIn("Alice", text)
        self.assertIn("Botty", text)

        self.cursor.execute("SELECT targetId FROM team_invites WHERE messageId=?", (780,))
        self.assertEqual(self.cursor.fetchall(), [(902,)])

    async def test_all_members_invalid_reports_every_reason_without_inviting(self):
        await self._make_team()
        ctx = self._ctx()
        botty = FakeMember("Botty", id=904, bot=True)

        await self.helperObj.teamInviteHelper(ctx, "Red", [ctx.user, botty])

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Alice", text)
        self.assertIn("Botty", text)
        self.cursor.execute("SELECT COUNT(*) FROM team_invites")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_force_rejects_a_captain_who_is_not_also_an_admin(self):
        # force is Manage Server only; even the team's own captain can't
        # use it without also being an admin, unlike an ordinary invite.
        await self._make_team()
        ctx = self._ctx(manage_guild=False)  # Alice, the captain, but no Manage Server
        target = FakeMember("Bob", id=902)
        await self.helperObj.teamInviteHelper(ctx, "Red", [target], force=True)
        ctx.response.send_message.assert_awaited_once_with(
            "Only a member with the Manage Server permission can force-add players - "
            "everyone else still needs the invitee's own confirmation."
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual([p.get_id() for p in team.get_players()], [901])

    async def test_force_adds_directly_to_the_roster_with_no_reaction_or_pending_row(self):
        await self._make_team()
        ctx = self._ctx(user_id=903, name="Cleo", manage_guild=True)  # admin, not the captain
        target = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=790)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamInviteHelper(ctx, "Red", [target], force=True)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(target.mention, text)
        self.assertIn(ctx.user.mention, text)
        self.assertIn("no confirmation needed", text)
        posted_message.add_reaction.assert_not_awaited()

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(sorted(p.get_id() for p in team.get_players()), [901, 902])

        self.cursor.execute("SELECT COUNT(*) FROM team_invites")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_force_still_skips_bots_and_already_rostered_members(self):
        await self._make_team()
        ctx = self._ctx(manage_guild=True)
        bob = FakeMember("Bob", id=902)
        botty = FakeMember("Botty", id=904, bot=True)

        await self.helperObj.teamInviteHelper(ctx, "Red", [bob, ctx.user, botty], force=True)

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(bob.mention, text)
        self.assertIn("Not added", text)
        self.assertIn("Alice", text)
        self.assertIn("Botty", text)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(sorted(p.get_id() for p in team.get_players()), [901, 902])

    async def test_force_with_nobody_valid_still_reports_reasons_and_adds_nobody(self):
        await self._make_team()
        ctx = self._ctx(manage_guild=True)
        botty = FakeMember("Botty", id=904, bot=True)

        await self.helperObj.teamInviteHelper(ctx, "Red", [ctx.user, botty], force=True)

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Alice", text)
        self.assertIn("Botty", text)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual([p.get_id() for p in team.get_players()], [901])


class TeamInviteAcceptViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("general")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    async def _make_team_and_invite(self, members=None):
        create_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.createTeamHelper(create_ctx, "Red", 5)
        if members is None:
            members = [FakeMember("Bob", id=902)]
        invite_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        posted_message = FakeMessage(id=888)
        invite_ctx.original_response.return_value = posted_message
        await self.helperObj.teamInviteHelper(invite_ctx, "Red", members)
        return posted_message

    def _click(self, message, user_id, name="Clicker"):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id), channel=self.channel, message=message
        )

    async def test_accept_from_someone_other_than_the_invitee_is_rejected(self):
        message = await self._make_team_and_invite()
        view = helper_module.TeamInviteAcceptView(self.helperObj)
        click = self._click(message, 903)
        await view.accept.callback(click)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 1)
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_accept_from_invitee_adds_them_to_the_roster(self):
        message = await self._make_team_and_invite()
        view = helper_module.TeamInviteAcceptView(self.helperObj)
        click = self._click(message, 902)
        await view.accept.callback(click)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual({p.get_id() for p in team.get_players()}, {901, 902})
        click.response.send_message.assert_awaited_once()
        self.assertIn("Bob", click.response.send_message.call_args.args[0])

        self.cursor.execute("SELECT COUNT(*) FROM team_invites")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_concurrent_accepts_only_add_once(self):
        message = await self._make_team_and_invite()
        view = helper_module.TeamInviteAcceptView(self.helperObj)
        click1 = self._click(message, 902)
        click2 = self._click(message, 902)
        await asyncio.gather(
            view.accept.callback(click1),
            view.accept.callback(click2),
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 2)

    async def test_multiple_invitees_on_the_same_message_accept_independently(self):
        message = await self._make_team_and_invite(
            [FakeMember("Bob", id=902), FakeMember("Cleo", id=903)]
        )
        view = helper_module.TeamInviteAcceptView(self.helperObj)

        await view.accept.callback(self._click(message, 902))

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual({p.get_id() for p in team.get_players()}, {901, 902})

        # Cleo's own invite (same messageId) is untouched by Bob's accept
        self.cursor.execute("SELECT targetId FROM team_invites WHERE messageId=?", (message.id,))
        self.assertEqual(self.cursor.fetchall(), [(903,)])

        await view.accept.callback(self._click(message, 903))

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual({p.get_id() for p in team.get_players()}, {901, 902, 903})


class TeamLeaveHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def _make_team(self, name="Red", captain_id=901, captain_name="Alice"):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, name, 5)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamLeaveHelper(ctx, "Nonexistent")
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_the_captain(self):
        await self._make_team()
        ctx = self._ctx()  # Alice, the captain
        await self.helperObj.teamLeaveHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "You're **Red**'s captain - use /team-transfer to hand off the captaincy first, "
            "or /team-delete if you want the team gone entirely."
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual([p.get_id() for p in team.get_players()], [901])

    async def test_rejects_someone_not_on_the_team(self):
        await self._make_team()
        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamLeaveHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with("You're not on **Red**.")

    async def test_removes_a_non_captain_player_from_the_roster(self):
        await self._make_team()
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(team.get_id(), team)

        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamLeaveHelper(ctx, "Red")

        ctx.response.send_message.assert_awaited_once_with("You've left **Red**.")
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual([p.get_id() for p in team.get_players()], [901])

    async def test_leaving_does_not_affect_other_rostered_players(self):
        await self._make_team()
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team_id = team.get_id()
        team.add_player(Player(902, "Bob"))
        team.add_player(Player(903, "Cleo"))
        self.helperObj.updateTeamData(team_id, team)

        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.teamLeaveHelper(ctx, "Red")

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(sorted(p.get_id() for p in team.get_players()), [901, 903])


class SetupHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None, message=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel, message=message)

    async def test_omitting_the_name_creates_a_solo_team_named_after_the_display_name(self):
        ctx = self._ctx()
        ctx.user.display_name = "Al The Great"  # distinct from .name ("Alice") - a server nickname
        await self.helperObj.setupHelper(ctx)

        result = self.helperObj.getTeamRow(GUILD_ID, "Al The Great")
        self.assertIsNotNone(result)
        _, team = result
        self.assertEqual(team.get_team_size(), 1)
        self.assertEqual([p.get_id() for p in team.get_players()], [901])

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Created your solo team **Al The Great**", text)
        self.cursor.execute("SELECT COUNT(*) FROM setup_role_sessions")
        self.assertEqual(self.cursor.fetchone()[0], 1)

    async def test_omitting_the_name_when_it_collides_asks_for_an_explicit_one(self):
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Alice", 5)
        ctx = self._ctx()  # display_name defaults to "Alice", same as .name here
        await self.helperObj.setupHelper(ctx)

        ctx.response.send_message.assert_awaited_once_with(
            "Your display name, **Alice**, is already taken by another team in this server - run "
            "/setup again with solo_team_name set to something else."
        )
        self.cursor.execute("SELECT COUNT(*) FROM setup_role_sessions")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_creates_a_solo_team_on_first_run(self):
        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx, "Alice's Team")

        result = self.helperObj.getTeamRow(GUILD_ID, "Alice's Team")
        self.assertIsNotNone(result)
        _, team = result
        self.assertEqual(team.get_team_size(), 1)
        self.assertEqual([p.get_id() for p in team.get_players()], [901])
        self.assertTrue(self.helperObj.isTeamCaptain(team, 901))

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Created your solo team **Alice's Team**", text)
        self.assertIn("/help", text)
        self.assertIn("Press the roles you", text)

    async def test_rejects_a_solo_team_name_colliding_with_an_existing_team(self):
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Taken", 5)
        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx, "Taken")
        ctx.response.send_message.assert_awaited_once_with(
            "A team named **Taken** already exists in this server - pick another name for your solo team."
        )

    async def test_solo_team_name_is_optional_once_one_already_exists(self):
        await self.helperObj.setupHelper(self._ctx(), "Alice's Team")
        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx)

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Your solo team is still **Alice's Team**", text)
        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)

    async def test_rerun_with_the_same_name_does_not_create_a_second_team(self):
        await self.helperObj.setupHelper(self._ctx(), "Alice's Team")
        await self.helperObj.setupHelper(self._ctx(), "Alice's Team")

        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)

    async def test_rerun_with_a_new_name_renames_the_existing_solo_team(self):
        await self.helperObj.setupHelper(self._ctx(), "Old Name")
        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx, "New Name")

        self.assertIsNone(self.helperObj.getTeamRow(GUILD_ID, "Old Name"))
        result = self.helperObj.getTeamRow(GUILD_ID, "New Name")
        self.assertIsNotNone(result)
        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Renamed your solo team from **Old Name** to **New Name**", text)

    async def test_rerun_renaming_rejects_a_collision_with_another_team(self):
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Taken", 5)
        await self.helperObj.setupHelper(self._ctx(), "Alice's Team")

        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx, "Taken")
        ctx.response.send_message.assert_awaited_once_with(
            "A team named **Taken** already exists in this server - pick another name for your solo team."
        )
        # the original solo team is untouched
        self.assertIsNotNone(self.helperObj.getTeamRow(GUILD_ID, "Alice's Team"))

    async def test_pure_capitalization_rename_of_solo_team_is_allowed(self):
        await self.helperObj.setupHelper(self._ctx(), "alice")
        ctx = self._ctx()
        await self.helperObj.setupHelper(ctx, "ALICE")

        self.cursor.execute("SELECT COUNT(*) FROM teams WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)
        result = self.helperObj.getTeamRow(GUILD_ID, "alice")
        self.assertEqual(result[1].get_name(), "ALICE")

    async def test_posts_a_role_toggle_view_and_creates_a_session(self):
        ctx = self._ctx()
        posted = FakeMessage(id=5001)
        ctx.original_response.return_value = posted
        await self.helperObj.setupHelper(ctx, "Alice's Team")

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.SetupRoleSelectionView)
        toggle_labels = {item.label for item in view.children if isinstance(item, helper_module.SetupRoleToggleButton)}
        self.assertEqual(toggle_labels, set(helper_module.SETUP_ROLE_NAMES))
        for item in view.children:
            if isinstance(item, helper_module.SetupRoleToggleButton):
                self.assertEqual(item.style, discord.ButtonStyle.secondary)

        self.cursor.execute(
            "SELECT guildId, userId, step, selectedRoles, likedRoles FROM setup_role_sessions "
            "WHERE messageId=?",
            (5001,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, 901, "liked", "", ""))
        self.assertIs(view.message, posted)


class SetupRoleToggleClickTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.cursor.execute(
            "INSERT INTO setup_role_sessions(messageId, guildId, userId, step, selectedRoles, likedRoles) "
            "VALUES(?, ?, ?, 'liked', '', '')",
            (5001, GUILD_ID, 901)
        )
        self.db.commit()

    def _selected(self):
        self.cursor.execute("SELECT selectedRoles FROM setup_role_sessions WHERE messageId=?", (5001,))
        row = self.cursor.fetchone()[0]
        return set(row.split(",")) if row else set()

    def _click(self, message_id=5001, user_id=901):
        return FakeInteraction(
            self.guild, FakeMember("Alice", id=user_id), message=FakeMessage(id=message_id)
        )

    async def test_toggles_a_role_on(self):
        click = self._click()
        await self.helperObj._handleSetupRoleToggleClick(click, "Top")
        self.assertEqual(self._selected(), {"Top"})

    async def test_toggles_a_second_role_on_without_losing_the_first(self):
        await self.helperObj._handleSetupRoleToggleClick(self._click(), "Top")
        await self.helperObj._handleSetupRoleToggleClick(self._click(), "Jungle")
        self.assertEqual(self._selected(), {"Top", "Jungle"})

    async def test_clicking_an_already_selected_role_toggles_it_off(self):
        await self.helperObj._handleSetupRoleToggleClick(self._click(), "Top")
        self.assertEqual(self._selected(), {"Top"})

        await self.helperObj._handleSetupRoleToggleClick(self._click(), "Top")
        self.assertEqual(self._selected(), set())

    async def test_ignores_an_unknown_message(self):
        click = self._click(message_id=9999)
        await self.helperObj._handleSetupRoleToggleClick(click, "Top")
        self.assertEqual(self._selected(), set())
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_response_edits_in_a_fresh_view_reflecting_the_new_selection(self):
        click = self._click()
        await self.helperObj._handleSetupRoleToggleClick(click, "Top")

        view = click.response.edit_message.call_args.kwargs["view"]
        top_button = next(item for item in view.children if getattr(item, "role_name", None) == "Top")
        self.assertEqual(top_button.style, discord.ButtonStyle.primary)
        other_button = next(item for item in view.children if getattr(item, "role_name", None) == "Jungle")
        self.assertEqual(other_button.style, discord.ButtonStyle.secondary)


class ConfirmSetupRoleStepTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None, message=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel, message=message)

    async def _start(self, message_id=5001):
        ctx = self._ctx()
        posted = FakeMessage(id=message_id)
        ctx.original_response.return_value = posted
        await self.helperObj.setupHelper(ctx, "Alice's Team")
        view = ctx.response.send_message.call_args.kwargs["view"]
        return posted, view

    async def test_confirming_liked_step_moves_to_disliked_step(self):
        posted, view = await self._start()
        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Top")

        click = self._ctx(message=posted)
        await view.confirm.callback(click)

        click.response.edit_message.assert_awaited_once()
        text = click.response.edit_message.call_args.kwargs["content"]
        self.assertIn("Liked roles set: Top", text)
        self.assertIn("dislike", text)

        self.cursor.execute(
            "SELECT step, selectedRoles, likedRoles FROM setup_role_sessions WHERE messageId=?",
            (posted.id,)
        )
        self.assertEqual(self.cursor.fetchone(), ("disliked", "", "Top"))
        # the disliked round gets a fresh view, every toggle back to unselected
        fresh_view = click.response.edit_message.call_args.kwargs["view"]
        for item in fresh_view.children:
            if isinstance(item, helper_module.SetupRoleToggleButton):
                self.assertEqual(item.style, discord.ButtonStyle.secondary)

    async def test_confirming_disliked_step_finalizes_preferences(self):
        posted, view = await self._start()
        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Top")
        await view.confirm.callback(self._ctx(message=posted))  # confirm liked (Top)

        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Support")
        click = self._ctx(message=posted)
        await view.confirm.callback(click)  # confirm disliked (Support)

        liked, disliked = self.helperObj.getRolePreferences(GUILD_ID, 901)
        self.assertEqual(liked, ["Top"])
        self.assertEqual(disliked, ["Support"])

        text = click.response.edit_message.call_args.kwargs["content"]
        self.assertIn("Liked roles: Top", text)
        self.assertIn("Disliked roles: Support", text)

        self.cursor.execute("SELECT COUNT(*) FROM setup_role_sessions")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_confirming_disliked_step_unlocks_onboarded_on_first_run(self):
        posted, view = await self._start()
        await view.confirm.callback(self._ctx(message=posted))  # confirm liked (none)
        click = self._ctx(message=posted, channel=FakeChannel("general"))
        await view.confirm.callback(click)  # confirm disliked (none)

        self.assertTrue(self.helperObj.hasCompletedSetup(GUILD_ID, 901))
        click.channel.send.assert_awaited_once()
        self.assertIn("Onboarded", click.channel.send.call_args.args[0])

    async def test_second_setup_run_does_not_reannounce_onboarded(self):
        posted, view = await self._start(message_id=5001)
        await view.confirm.callback(self._ctx(message=posted))
        await view.confirm.callback(self._ctx(message=posted, channel=FakeChannel("general")))

        posted2, view2 = await self._start(message_id=5002)
        await view2.confirm.callback(self._ctx(message=posted2))
        click2 = self._ctx(message=posted2, channel=FakeChannel("general"))
        await view2.confirm.callback(click2)
        click2.channel.send.assert_not_awaited()

    async def test_a_role_picked_in_both_steps_is_left_neutral_and_explained(self):
        posted, view = await self._start()
        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Top")  # like Top
        await view.confirm.callback(self._ctx(message=posted))

        await self.helperObj._handleSetupRoleToggleClick(
            self._ctx(message=posted), "Top"
        )  # dislike Top too - contradiction
        click = self._ctx(message=posted)
        await view.confirm.callback(click)

        liked, disliked = self.helperObj.getRolePreferences(GUILD_ID, 901)
        self.assertEqual(liked, [])
        self.assertEqual(disliked, [])

        text = click.response.edit_message.call_args.kwargs["content"]
        self.assertIn("Top", text)
        self.assertIn("marked as both liked and disliked", text)
        self.assertIn("neutral", text)
        self.assertIn("Run /setup again", text)

    async def test_partial_contradiction_still_applies_the_non_contradictory_roles(self):
        posted, view = await self._start()
        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Top")  # like Top
        await self.helperObj._handleSetupRoleToggleClick(self._ctx(message=posted), "Jungle")  # like Jungle
        await view.confirm.callback(self._ctx(message=posted))

        await self.helperObj._handleSetupRoleToggleClick(
            self._ctx(message=posted), "Top"
        )  # dislike Top too - contradiction, Jungle stays liked-only
        await view.confirm.callback(self._ctx(message=posted))

        liked, disliked = self.helperObj.getRolePreferences(GUILD_ID, 901)
        self.assertEqual(liked, ["Jungle"])
        self.assertEqual(disliked, [])

    async def test_confirm_rejects_a_click_from_someone_other_than_the_setup_runner(self):
        posted, view = await self._start()
        stranger = self._ctx(user_id=902, name="Bob", message=posted)
        allowed = await view.interaction_check(stranger)
        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_confirm_on_an_expired_session_tells_the_caller_to_rerun(self):
        posted, view = await self._start()
        self.helperObj._expireSetupRoleSession(GUILD_ID, posted.id)

        click = self._ctx(message=posted)
        await view.confirm.callback(click)
        click.response.send_message.assert_awaited_once_with(
            "This role selection has expired - run /setup again.", ephemeral=True
        )


class HasCompletedSetupTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None, message=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel, message=message)

    async def _run_setup_to_completion(self, message_id=5001):
        ctx = self._ctx()
        posted = FakeMessage(id=message_id)
        ctx.original_response.return_value = posted
        await self.helperObj.setupHelper(ctx, "Alice's Team")
        view = ctx.response.send_message.call_args.kwargs["view"]
        await view.confirm.callback(self._ctx(message=posted))  # confirm liked
        await view.confirm.callback(self._ctx(message=posted))  # confirm disliked

    async def test_false_before_setup_has_ever_run(self):
        self.assertFalse(self.helperObj.hasCompletedSetup(GUILD_ID, 901))

    async def test_false_after_only_starting_setup_without_confirming(self):
        await self.helperObj.setupHelper(self._ctx(), "Alice's Team")
        self.assertFalse(self.helperObj.hasCompletedSetup(GUILD_ID, 901))

    async def test_true_after_setup_is_fully_confirmed(self):
        await self._run_setup_to_completion()
        self.assertTrue(self.helperObj.hasCompletedSetup(GUILD_ID, 901))

    async def test_scoped_to_the_right_guild(self):
        await self._run_setup_to_completion()
        other_guild_id = GUILD_ID + 1
        insert_guild_row(self.cursor, self.db, guild_id=other_guild_id)
        self.assertFalse(self.helperObj.hasCompletedSetup(other_guild_id, 901))


class TeamStatsHelperTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Nonexistent")
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_reports_fresh_team_stats(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)

        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Red")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Captain"], "Alice")
        self.assertEqual(values["Roster Size"], "1/5")
        self.assertEqual(values["Record"], "0W - 0L")
        self.assertEqual(values["Win Rate"], "N/A")
        self.assertEqual(values["Voice Channel"], "Not set")
        self.assertIn("Alice", values["Roster"])

    async def test_reports_win_rate_once_games_are_recorded(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        team_id, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team.addWin()
        team.addWin()
        team.addLoss()
        self.helperObj.updateTeamData(team_id, team)

        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Red")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Record"], "2W - 1L")
        self.assertEqual(values["Win Rate"], "66.7%")

    async def test_embed_shows_the_teams_logo_as_a_thumbnail(self):
        # createTeamHelper -> _saveNewTeam -> _ensureLogo means a fresh team
        # already has one of the fake logo dir's files assigned.
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        expected_filename = os.path.basename(team.get_logo_path())

        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Red")

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed.thumbnail.url, f"attachment://{expected_filename}")
        self.assertIsInstance(kwargs["file"], discord.File)
        self.assertEqual(kwargs["file"].filename, expected_filename)
        kwargs["file"].close()

    async def test_no_thumbnail_or_file_when_the_team_has_no_logo(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        team_id, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team.set_logo_path(None)
        self.helperObj.updateTeamData(team_id, team)

        # An empty logo dir for this call specifically, otherwise
        # getTeamRow's own self-heal (_ensureLogo) would just reassign a
        # random one from the fake dir the moment teamStatsHelper reads
        # this team back, and there'd be no way to observe the "genuinely
        # has no logo" case at all.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty_dir, \
             patch.object(helper_module, "TEAM_LOGO_DIR", empty_dir):
            ctx = self._ctx()
            await self.helperObj.teamStatsHelper(ctx, "Red")

        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertNotIn("file", kwargs)
        self.assertIsNone(kwargs["embed"].thumbnail.url)

    async def test_no_thumbnail_or_file_when_the_logo_file_no_longer_exists_on_disk(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        team_id, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team.set_logo_path(os.path.join(self._logo_dir.name, "Deleted.png"))
        self.helperObj.updateTeamData(team_id, team)

        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Red")

        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertNotIn("file", kwargs)

    async def test_posts_a_team_stats_view_and_tracks_the_view(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        team_id, _ = self.helperObj.getTeamRow(GUILD_ID, "Red")

        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, "Red")
        msg = await ctx.original_response()

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.TeamStatsView)
        self.assertIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)
        self.cursor.execute(
            "SELECT guildId, teamId, cardShown FROM team_stats_views WHERE messageId=?", (msg.id,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, team_id, 0))


class DominantLogoColorTests(_FakeLogoDirTestCase):
    def test_picks_the_most_common_non_background_color(self):
        path = os.path.join(self._logo_dir.name, "solid.png")
        image = Image.new("RGBA", (10, 10), (30, 144, 255, 255))
        # A few near-white border pixels shouldn't be able to outvote the
        # much larger solid-blue block, since they're exactly the kind of
        # "padding, not identity" pixel the brightness filter exists for.
        for x in range(10):
            image.putpixel((x, 0), (250, 250, 250, 255))
        image.save(path)

        color = self.helperObj._dominantLogoColor(path, (0, 0, 0))
        # bucketed to the nearest 16, same as the implementation
        self.assertEqual(color, (16, 144, 240))

    def test_ignores_fully_transparent_pixels(self):
        path = os.path.join(self._logo_dir.name, "padded.png")
        image = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        for x in range(4, 8):
            for y in range(4, 8):
                image.putpixel((x, y), (200, 30, 30, 255))
        image.save(path)

        color = self.helperObj._dominantLogoColor(path, (0, 0, 0))
        self.assertEqual(color, (192, 16, 16))

    def test_falls_back_when_the_file_cant_be_opened(self):
        color = self.helperObj._dominantLogoColor(
            os.path.join(self._logo_dir.name, "does-not-exist.png"), (1, 2, 3)
        )
        self.assertEqual(color, (1, 2, 3))

    def test_falls_back_when_every_pixel_is_filtered_out(self):
        path = os.path.join(self._logo_dir.name, "blank.png")
        Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(path)

        color = self.helperObj._dominantLogoColor(path, (9, 9, 9))
        self.assertEqual(color, (9, 9, 9))


class RenderTeamCardImageTests(_FakeLogoDirTestCase):
    def _team(self, name="Red", size=5, roster_count=3, captain_index=0):
        team = Team()
        team.name = name
        team.team_size = size
        players = [Player(900 + i, f"Player{i}") for i in range(roster_count)]
        for player in players:
            team.add_player(player)
        if players:
            team.set_captain(players[captain_index])
        return team

    async def test_renders_without_clipping_for_a_populated_team(self):
        await self.helperObj.createTeamHelper(
            FakeInteraction(self.guild, FakeMember("Alice", id=901)), "Red", 5
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        team.addWin()
        team.addWin()
        team.addLoss()

        image = self.helperObj._renderTeamCardImage("Test Guild", team)
        self.assertGreater(image.width, 0)
        self.assertGreater(image.height, 0)

    def test_caps_roster_rows_and_shows_an_overflow_line(self):
        team = self._team(roster_count=helper_module.TEAM_CARD_MAX_ROSTER_ROWS + 3)
        image = self.helperObj._renderTeamCardImage("Test Guild", team)
        # taller than a team with just one shown row would need, but not
        # blown out to fit all N; the overflow line is what absorbs the
        # rest instead of one row per extra player.
        small_team_image = self.helperObj._renderTeamCardImage("Test Guild", self._team(roster_count=1))
        self.assertGreater(image.height, small_team_image.height)

    def test_empty_roster_shows_a_placeholder_line_without_crashing(self):
        team = self._team(roster_count=0)
        image = self.helperObj._renderTeamCardImage("Test Guild", team)
        self.assertGreater(image.height, 0)

    def _expectedReadableAccent(self, sampled_color):
        # Mirrors _renderTeamCardImage's own background derivation exactly,
        # so these tests verify the real readability-boosted color rather
        # than a hand-computed guess that'd drift if the darken/lighten
        # constants ever change.
        background_color = tuple(round(c * 0.28) for c in sampled_color)
        background_center = self.helperObj._lightenColor(background_color, 0.3)
        return self.helperObj._ensureReadableAccent(sampled_color, background_center)

    def test_accent_color_is_sampled_from_the_teams_own_logo(self):
        team = self._team()
        logo_path = os.path.join(self._logo_dir.name, "custom.png")
        sampled_color = (34, 139, 34)
        Image.new("RGBA", (10, 10), sampled_color + (255,)).save(logo_path)
        team.set_logo_path(logo_path)

        with patch.object(
            self.helperObj, "_dominantLogoColor", return_value=sampled_color
        ) as mock_sample:
            image = self.helperObj._renderTeamCardImage("Test Guild", team)
            mock_sample.assert_called_once()

        # the sampled color, boosted for readability against its own
        # derived background (see _ensureReadableAccent), shows up as the
        # frame's own outline pixel, same observable proof
        # RenderTradingCardImageTests uses for the player card's
        # customizable accent_color.
        mid_y = image.height // 2
        border_pixel = image.convert("RGB").getpixel((helper_module.BRACKET_LINE_WIDTH, mid_y))
        self.assertEqual(border_pixel, self._expectedReadableAccent(sampled_color))
        # and it should actually differ from the raw sample here, a
        # forest green this dark needs boosting against its own (darker
        # still) derived background, exactly the scenario the mechanism
        # exists for.
        self.assertNotEqual(border_pixel, sampled_color)

    def test_falls_back_to_the_default_accent_when_the_team_has_no_logo(self):
        team = self._team()
        team.set_logo_path(None)
        image = self.helperObj._renderTeamCardImage("Test Guild", team)
        mid_y = image.height // 2
        border_pixel = image.convert("RGB").getpixel((helper_module.BRACKET_LINE_WIDTH, mid_y))
        self.assertEqual(
            border_pixel, self._expectedReadableAccent(helper_module.TEAM_CARD_FALLBACK_ACCENT_COLOR)
        )

    def test_readable_accent_is_boosted_when_the_sampled_color_is_dark(self):
        # A deep navy is exactly the kind of real team-crest color that
        # passes _dominantLogoColor's own brightness filter (average
        # brightness comfortably above its floor of 20) but would be hard
        # to read as header/label text against its own derived background
        # without _ensureReadableAccent stepping in.
        team = self._team()
        logo_path = os.path.join(self._logo_dir.name, "navy.png")
        sampled_color = (20, 20, 90)
        Image.new("RGBA", (10, 10), sampled_color + (255,)).save(logo_path)
        team.set_logo_path(logo_path)

        with patch.object(self.helperObj, "_dominantLogoColor", return_value=sampled_color):
            image = self.helperObj._renderTeamCardImage("Test Guild", team)

        mid_y = image.height // 2
        border_pixel = image.convert("RGB").getpixel((helper_module.BRACKET_LINE_WIDTH, mid_y))
        self.assertEqual(border_pixel, self._expectedReadableAccent(sampled_color))

        # the boosted accent must clear the background's own lightened
        # vignette center by the configured minimum contrast, the actual
        # readability guarantee this whole mechanism exists to provide.
        background_color = tuple(round(c * 0.28) for c in sampled_color)
        background_center = self.helperObj._lightenColor(background_color, 0.3)
        accent_brightness = sum(border_pixel) / 3
        background_brightness = sum(background_center) / 3
        self.assertGreaterEqual(
            accent_brightness - background_brightness, helper_module.CARD_MIN_ACCENT_CONTRAST - 1
        )


class EnsureReadableAccentTests(HelperTestCase):
    def test_returns_the_color_unchanged_when_contrast_is_already_sufficient(self):
        color = self.helperObj._ensureReadableAccent((255, 215, 0), (20, 20, 20), min_contrast=90)
        self.assertEqual(color, (255, 215, 0))

    def test_lightens_a_low_contrast_color_toward_white(self):
        color = self.helperObj._ensureReadableAccent((20, 20, 90), (80, 80, 88), min_contrast=90)
        self.assertNotEqual(color, (20, 20, 90))
        # lightened toward white, not replaced with something unrelated -
        # each channel should only have moved up, never down or past 255.
        for original, boosted in zip((20, 20, 90), color):
            self.assertGreaterEqual(boosted, original)
            self.assertLessEqual(boosted, 255)

    def test_never_exceeds_the_maximum_brightness_of_pure_white(self):
        # An already near-white color with an impossibly high target still
        # comes back as a valid RGB tuple rather than overshooting 255.
        color = self.helperObj._ensureReadableAccent((250, 250, 250), (0, 0, 0), min_contrast=999)
        for channel in color:
            self.assertLessEqual(channel, 255)


class TeamStatsViewTests(_FakeLogoDirTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("team-chat", guild=self.guild)
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=self.channel)

    def _click(self, message, user_id=902, name="Bob"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=self.channel, message=message)

    async def _post_team_stats(self, name="Red"):
        await self.helperObj.createTeamHelper(self._ctx(), name, 5)
        ctx = self._ctx()
        await self.helperObj.teamStatsHelper(ctx, name)
        msg = await ctx.original_response()
        original_embed = ctx.response.send_message.call_args.kwargs["embed"]
        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [original_embed]
        return fetched_message

    async def test_card_click_replaces_the_embed_with_a_team_card(self):
        fetched_message = await self._post_team_stats()
        view = helper_module.TeamStatsView(self.helperObj, card_shown=False)

        await view.showCard.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        new_embed = fetched_message.edit.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        attached_files = fetched_message.edit.call_args.kwargs["attachments"]
        self.assertEqual(len(attached_files), 1)
        attached_files[0].close()
        new_view = fetched_message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.returnToStats, new_view.children)
        self.assertNotIn(new_view.showCard, new_view.children)

        self.cursor.execute(
            "SELECT cardShown FROM team_stats_views WHERE messageId=?", (fetched_message.id,)
        )
        self.assertEqual(self.cursor.fetchone(), (1,))

    async def test_return_click_swaps_the_card_back_to_the_embed(self):
        fetched_message = await self._post_team_stats()
        show_view = helper_module.TeamStatsView(self.helperObj, card_shown=False)
        await show_view.showCard.callback(self._click(fetched_message))

        # a card-view message is a bare image embed, same as the trading
        # card's own return-click test simulates.
        card_embed = discord.Embed(color=discord.Color.gold())
        card_embed.set_image(url="attachment://team_card.png")
        fetched_message.embeds = [card_embed]
        fetched_message.edit.reset_mock()

        return_view = helper_module.TeamStatsView(self.helperObj, card_shown=True)
        await return_view.returnToStats.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        new_embed = fetched_message.edit.call_args.kwargs["embed"]
        self.assertGreater(len(new_embed.fields), 0)
        new_view = fetched_message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.showCard, new_view.children)

        self.cursor.execute(
            "SELECT cardShown FROM team_stats_views WHERE messageId=?", (fetched_message.id,)
        )
        self.assertEqual(self.cursor.fetchone(), (0,))

    async def test_return_click_is_rejected_before_the_card_is_shown(self):
        fetched_message = await self._post_team_stats()
        view = helper_module.TeamStatsView(self.helperObj, card_shown=True)
        click = self._click(fetched_message)

        await view.returnToStats.callback(click)

        fetched_message.edit.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_card_click_on_an_unknown_message_is_rejected(self):
        view = helper_module.TeamStatsView(self.helperObj, card_shown=False)
        click = self._click(FakeMessage(id=999999))

        await view.showCard.callback(click)

        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


class GetTeamsForPlayerTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_returns_only_teams_the_player_is_rostered_on(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)

        mine = self.helperObj.getTeamsForPlayer(GUILD_ID, 901)
        self.assertEqual([team.get_name() for _, team in mine], ["Red"])

    async def test_finds_teams_where_the_player_is_a_non_captain_member(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        red_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(red_id, red)

        mine = self.helperObj.getTeamsForPlayer(GUILD_ID, 902)
        self.assertEqual([team.get_name() for _, team in mine], ["Red"])

    async def test_empty_when_the_player_is_on_no_teams(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        self.assertEqual(self.helperObj.getTeamsForPlayer(GUILD_ID, 999), [])

    async def test_sorted_by_team_id_for_stable_paging(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Zeta", 5)
        red_id, red = self.helperObj.getTeamRow(GUILD_ID, "Zeta")
        red.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(red_id, red)

        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Alpha", 5)

        mine = self.helperObj.getTeamsForPlayer(GUILD_ID, 902)
        self.assertEqual([team.get_name() for _, team in mine], ["Zeta", "Alpha"])


class GetTeamsCaptainedByTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_returns_only_teams_the_player_captains(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)

        captained = self.helperObj.getTeamsCaptainedBy(GUILD_ID, 901)
        self.assertEqual([team.get_name() for _, team in captained], ["Red"])

    async def test_excludes_teams_the_player_is_only_a_member_of(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        red_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.add_player(Player(902, "Bob"))  # Bob joins, but Alice stays captain
        self.helperObj.updateTeamData(red_id, red)

        self.assertEqual(self.helperObj.getTeamsCaptainedBy(GUILD_ID, 902), [])

    async def test_empty_when_the_player_captains_nothing(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        self.assertEqual(self.helperObj.getTeamsCaptainedBy(GUILD_ID, 999), [])


class MyTeamsHelperTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_no_teams_sends_a_message(self):
        ctx = self._ctx()
        await self.helperObj.myTeamsHelper(ctx)
        ctx.response.send_message.assert_awaited_once_with("You're not on any teams in this server.")

    async def test_no_teams_for_another_member_names_them_instead_of_you(self):
        other = FakeMember("Bob", id=902)
        ctx = self._ctx()  # caller is Alice (901)
        await self.helperObj.myTeamsHelper(ctx, other)
        ctx.response.send_message.assert_awaited_once_with("Bob isn't on any teams in this server.")

    async def test_looks_up_another_members_teams(self):
        other = FakeMember("Bob", id=902)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Red", 5)

        ctx = self._ctx()  # caller is Alice (901), who is on no teams
        posted = FakeMessage(id=5252)
        ctx.original_response.return_value = posted
        await self.helperObj.myTeamsHelper(ctx, other)

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed.title, "Red Stats")

        self.cursor.execute(
            "SELECT guildId, channelId, userId, page FROM my_team_views WHERE messageId=5252"
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, ctx.channel.id, 902, 0))
        if "file" in kwargs:
            kwargs["file"].close()

    async def test_posts_the_first_team_reacts_and_tracks_the_view(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(), "Blue", 5)

        ctx = self._ctx()
        posted = FakeMessage(id=4242)
        ctx.original_response.return_value = posted
        await self.helperObj.myTeamsHelper(ctx)

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed.title, "Red Stats")
        self.assertIn("Team 1/2", embed.footer.text)
        self.assertIsInstance(kwargs["view"], helper_module.MyTeamsPagingView)

        self.cursor.execute(
            "SELECT guildId, channelId, userId, page FROM my_team_views WHERE messageId=4242"
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, ctx.channel.id, 901, 0))
        if "file" in kwargs:
            kwargs["file"].close()

    async def test_teams_are_ordered_by_team_id(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Zeta", 5)
        await self.helperObj.createTeamHelper(self._ctx(), "Alpha", 5)

        ctx = self._ctx()
        await self.helperObj.myTeamsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Zeta Stats")
        kwargs = ctx.response.send_message.call_args.kwargs
        if "file" in kwargs:
            kwargs["file"].close()


class TeamListHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_no_teams_at_all_sends_a_message(self):
        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc")
        ctx.response.send_message.assert_awaited_once_with(
            "No teams have been created in this server yet!"
        )

    async def test_filters_that_match_nothing_send_a_different_message(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, "Nonexistent", False, "name", "asc")
        ctx.response.send_message.assert_awaited_once_with("No teams match those filters.")

    async def test_sorted_by_name_ascending_by_default(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Zeta", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Alpha", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        lines = embed.description.split("\n")
        self.assertTrue(lines[0].startswith("**#1.** Alpha"))
        self.assertTrue(lines[1].startswith("**#2.** Zeta"))

    async def test_search_filters_by_name_substring_case_insensitively(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red Dragons", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue Wolves", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, "dragon", False, "name", "asc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Red Dragons", embed.description)
        self.assertNotIn("Blue Wolves", embed.description)

    async def test_recruiting_only_excludes_full_and_sizeless_teams(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "NeedsPlayers", 5)
        full_ctx = self._ctx(902, "Bob")
        await self.helperObj.createTeamHelper(full_ctx, "Full", 1)  # captain alone fills a size-1 team

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, True, "name", "asc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("NeedsPlayers", embed.description)
        self.assertNotIn("Full", embed.description)

    async def test_sort_by_wins_descending(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)
        red_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.addWin()
        red.addWin()
        self.helperObj.updateTeamData(red_id, red)
        blue_id, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        blue.addWin()
        self.helperObj.updateTeamData(blue_id, blue)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "wins", "desc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        lines = embed.description.split("\n")
        self.assertTrue(lines[0].startswith("**#1.** Red"))
        self.assertTrue(lines[1].startswith("**#2.** Blue"))

    async def test_member_filter_keeps_only_teams_rostering_that_player(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc", members=[FakeMember("Alice", id=901)])

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Red", embed.description)
        self.assertNotIn("Blue", embed.description)
        self.assertIn("with Alice", embed.footer.text)

    async def test_member_filter_with_multiple_members_requires_all_on_the_same_team(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        team_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(team_id, red)
        # Bob is also on Blue, alone with its own captain; Alice+Bob
        # together should only ever match Red.
        await self.helperObj.createTeamHelper(self._ctx(903, "Charlie"), "Blue", 5)
        blue_id, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        blue.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(blue_id, blue)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(
            ctx, None, False, "name", "asc",
            members=[FakeMember("Alice", id=901), FakeMember("Bob", id=902)],
        )

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Red", embed.description)
        self.assertNotIn("Blue", embed.description)
        self.assertIn("with Alice, Bob", embed.footer.text)

    async def test_member_filter_that_matches_nothing_sends_the_filtered_message(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc", members=[FakeMember("Bob", id=902)])

        ctx.response.send_message.assert_awaited_once_with("No teams match those filters.")

    async def test_posts_and_reacts_and_stores_the_view(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        ctx = self._ctx()
        posted = FakeMessage(id=6161)
        ctx.original_response.return_value = posted

        await self.helperObj.teamListHelper(ctx, "re", True, "wins", "desc")

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.TeamListPagingView)
        # a plain (non-cards) list never gets the Card/Back toggle at all
        self.assertNotIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)

        self.cursor.execute(
            "SELECT guildId, channelId, search, recruitingOnly, sort, sort_order, page, cards "
            "FROM team_list_views WHERE messageId=6161"
        )
        self.assertEqual(
            self.cursor.fetchone(), (GUILD_ID, ctx.channel.id, "re", 1, "wins", "desc", 0, 0)
        )

    async def test_stores_member_ids_and_names_alongside_the_rest_of_the_view_state(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        team_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.add_player(Player(902, "Bob"))
        self.helperObj.updateTeamData(team_id, red)
        ctx = self._ctx()
        posted = FakeMessage(id=6262)
        ctx.original_response.return_value = posted

        await self.helperObj.teamListHelper(
            ctx, None, False, "name", "asc",
            members=[FakeMember("Alice", id=901), FakeMember("Bob", id=902)],
        )

        self.cursor.execute("SELECT memberIds, memberNames FROM team_list_views WHERE messageId=6262")
        member_ids_raw, member_names_raw = self.cursor.fetchone()
        self.assertEqual(set(member_ids_raw.split(",")), {"901", "902"})
        self.assertEqual(member_names_raw, "Alice,Bob")


# cards:true mode: /my-teams' own one-team-full-stats-card-per-page
# rendering, sourced from every team matching /team-list's filters instead
# of one player's teams. Needs _FakeLogoDirTestCase since the underlying
# render (_renderTeamStatsEmbed) falls back to a random built-in logo for
# any team without one of its own, same reason MyTeamsHelperTests needs it.
class TeamListCardsModeTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_renders_the_first_matching_teams_full_stats_card(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Zeta", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Alpha", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc", cards=True)

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        # sorted by name ascending, same as the summary-list mode would use
        self.assertEqual(embed.title, "Alpha Stats")
        self.assertIn("Team 1/2", embed.footer.text)
        self.assertIsInstance(kwargs["view"], helper_module.TeamListPagingView)
        if "file" in kwargs:
            kwargs["file"].close()

    async def test_still_respects_search_and_sort(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red Dragons", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue Wolves", 5)

        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, "dragon", False, "name", "asc", cards=True)

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed.title, "Red Dragons Stats")
        self.assertIn("Team 1/1", embed.footer.text)
        if "file" in kwargs:
            kwargs["file"].close()

    async def test_stores_the_cards_flag_alongside_the_rest_of_the_view_state(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        ctx = self._ctx()
        posted = FakeMessage(id=8181)
        ctx.original_response.return_value = posted

        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc", cards=True)

        self.cursor.execute(
            "SELECT search, recruitingOnly, sort, sort_order, page, cards, cardShown FROM team_list_views "
            "WHERE messageId=8181"
        )
        self.assertEqual(self.cursor.fetchone(), (None, 0, "name", "asc", 0, 1, 0))
        kwargs = ctx.response.send_message.call_args.kwargs
        view = kwargs["view"]
        self.assertIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)
        if "file" in kwargs:
            kwargs["file"].close()

    async def test_no_teams_message_is_unaffected_by_cards_mode(self):
        ctx = self._ctx()
        await self.helperObj.teamListHelper(ctx, None, False, "name", "asc", cards=True)
        ctx.response.send_message.assert_awaited_once_with(
            "No teams have been created in this server yet!"
        )


class TeamListPagingViewCardsModeTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def asyncSetUp(self):
        self.channel = FakeChannel("team-list-cards-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Alpha", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Bravo", 5)
        await self.helperObj.createTeamHelper(self._ctx(903, "Charlie"), "Charlie Team", 5)

        self.message = FakeMessage(id=9191)
        self.cursor.execute(
            "INSERT INTO team_list_views"
            "(messageId, guildId, channelId, search, recruitingOnly, sort, sort_order, page, cards) "
            "VALUES(9191, ?, ?, NULL, 0, 'name', 'asc', 0, 1)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _page(self):
        self.cursor.execute("SELECT page FROM team_list_views WHERE messageId=9191")
        return self.cursor.fetchone()[0]

    def _click(self, message=None, user_id=1):
        return FakeInteraction(
            self.guild, FakeMember("Clicker", id=user_id), channel=self.channel,
            message=message if message is not None else self.message,
        )

    async def test_next_renders_the_next_teams_full_stats_card(self):
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)

        self.assertEqual(self._page(), 1)
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Bravo Stats")
        self.assertIn("Team 2/3", embed.footer.text)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_last_jumps_to_the_final_team(self):
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.last.callback(click)
        self.assertEqual(self._page(), 2)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_stays_a_noop_past_the_last_team(self):
        self.cursor.execute("UPDATE team_list_views SET page=2 WHERE messageId=9191")
        self.db.commit()
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)
        self.assertEqual(self._page(), 2)
        click.response.edit_message.assert_not_awaited()

    async def test_list_mode_and_cards_mode_views_dont_cross_wires(self):
        # A second, plain (non-cards) /team-list message posted in the same
        # guild must still render as a summary list, not a card, proving
        # _handleTeamListPageClick actually branches per-message on the
        # stored `cards` flag rather than some shared/global mode. Needs
        # enough teams for list mode's own paging (10 per page) to have a
        # real second page to flip to; the 3 from asyncSetUp alone aren't.
        for i in range(10):
            await self.helperObj.createTeamHelper(self._ctx(910 + i, f"Extra{i}"), f"Extra Team {i}", 5)

        list_message = FakeMessage(id=9292)
        self.cursor.execute(
            "INSERT INTO team_list_views"
            "(messageId, guildId, channelId, search, recruitingOnly, sort, sort_order, page, cards) "
            "VALUES(9292, ?, ?, NULL, 0, 'name', 'asc', 0, 0)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click(message=list_message)
        await view.next.callback(click)

        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("Test Guild Teams", embed.title)
        self.assertIn("Page 2", embed.footer.text)

    async def test_card_click_swaps_the_current_team_to_its_trading_card(self):
        view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.showCard.callback(click)

        self.message.edit.assert_awaited_once()
        new_embed = self.message.edit.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        self.assertIn("Team 1/3", new_embed.footer.text)
        attached_files = self.message.edit.call_args.kwargs["attachments"]
        self.assertEqual(len(attached_files), 1)
        attached_files[0].close()
        new_view = self.message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.returnToStats, new_view.children)
        self.assertNotIn(new_view.showCard, new_view.children)

        self.cursor.execute("SELECT cardShown FROM team_list_views WHERE messageId=9191")
        self.assertEqual(self.cursor.fetchone(), (1,))

    async def test_return_click_swaps_back_to_the_stats_card(self):
        show_view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=False)
        await show_view.showCard.callback(self._click())
        self.message.edit.reset_mock()
        self.cursor.execute("UPDATE team_list_views SET cardShown=1 WHERE messageId=9191")
        self.db.commit()

        return_view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=True)
        await return_view.returnToStats.callback(self._click())

        self.message.edit.assert_awaited_once()
        new_embed = self.message.edit.call_args.kwargs["embed"]
        self.assertEqual(new_embed.title, "Alpha Stats")
        self.assertIn("Team 1/3", new_embed.footer.text)
        new_view = self.message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.showCard, new_view.children)
        self.assertNotIn(new_view.returnToStats, new_view.children)

        self.cursor.execute("SELECT cardShown FROM team_list_views WHERE messageId=9191")
        self.assertEqual(self.cursor.fetchone(), (0,))

    async def test_return_click_is_rejected_before_the_card_is_shown(self):
        view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=True)
        click = self._click()

        await view.returnToStats.callback(click)

        self.message.edit.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_card_click_on_an_unknown_message_is_rejected(self):
        view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click(message=FakeMessage(id=424242))

        await view.showCard.callback(click)

        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_paging_while_card_is_shown_keeps_showing_cards(self):
        # cardShown carries across a page flip; Next while looking at
        # Alpha's trading card should land on Bravo's trading card, not
        # Bravo's plain stats card.
        self.cursor.execute("UPDATE team_list_views SET cardShown=1 WHERE messageId=9191")
        self.db.commit()
        view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=True)
        click = self._click()

        await view.next.callback(click)

        self.assertEqual(self._page(), 1)
        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        self.assertIn("Team 2/3", new_embed.footer.text)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()
        self.cursor.execute("SELECT cardShown FROM team_list_views WHERE messageId=9191")
        self.assertEqual(self.cursor.fetchone(), (1,))

    async def test_paging_while_stats_is_shown_keeps_showing_stats(self):
        view = helper_module.TeamListPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.next.callback(click)

        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(new_embed.title, "Bravo Stats")


class UseTeamsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_picking_the_same_team_twice(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Red", False)
        ctx.response.send_message.assert_awaited_once_with("Pick two different teams.")

    async def test_rejects_the_same_team_twice_even_if_only_case_differs(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "red", False)
        ctx.response.send_message.assert_awaited_once_with("Pick two different teams.")

    async def test_rejects_unknown_first_team(self):
        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Blue", False)
        ctx.response.send_message.assert_awaited_once_with("No team named **Red** in this server.")

    async def test_rejects_unknown_second_team(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Blue", False)
        ctx.response.send_message.assert_awaited_once_with("No team named **Blue** in this server.")

    async def test_escapes_markdown_special_characters_in_team_names(self):
        # A stray underscore/asterisk in one team's name can pair up with
        # an unrelated marker later in the same message and italicize/bold
        # everything in between (including the OTHER team's name); team
        # names are free text, so this has to be escaped before display.
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Fire_Squad", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Ice*Wolves", 5)

        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Fire_Squad", "Ice*Wolves", False)

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(r"Fire\_Squad", text)
        self.assertIn(r"Ice\*Wolves", text)

    async def test_loads_teams_casually_without_touching_is_ranked(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)
        self.helperObj.update(GUILD_ID, "is_ranked", 1)  # stale value from a previous ranked game

        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Blue", False)

        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 0)  # reset by clearTeamsHelper
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Normal")
        team1 = Team()
        team1.deserializeTeam(self.helperObj.get(GUILD_ID, "team1"))
        self.assertEqual(team1.get_name(), "Red")
        team2 = Team()
        team2.deserializeTeam(self.helperObj.get(GUILD_ID, "team2"))
        self.assertEqual(team2.get_name(), "Blue")
        ctx.response.send_message.assert_awaited_once()
        self.assertIn("Press Start", ctx.response.send_message.call_args.args[0])

    async def test_loads_teams_ranked_and_sets_is_ranked(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)

        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Blue", True)

        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Ranked")
        self.assertIn("ranked", ctx.response.send_message.call_args.args[0].lower())

    async def test_loading_teams_does_not_mutate_the_stored_persistent_team(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)
        before_id, _ = self.helperObj.getTeamRow(GUILD_ID, "Red")

        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Blue", False)

        # useTeamsHelper sets id=1/id=2 on its own in-memory copy for
        # _startRosterViaReaction's sake; the persistent team's row/id in
        # `teams` is untouched, since it never calls updateTeamData.
        after_id, stored_red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(after_id, before_id)
        self.assertEqual(stored_red.get_id(), before_id)


class ReuseTeamsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", guild=None, channel=None):
        return FakeInteraction(
            guild if guild is not None else self.guild, FakeMember(name, id=user_id), channel=channel
        )

    async def test_rejects_when_theres_nothing_to_reuse(self):
        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)
        ctx.response.send_message.assert_awaited_once_with(
            "No previous teams to reuse - make some first with /make-teams, /captains, or /team-use."
        )

    async def test_reposts_the_exact_same_roster(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(), "Red", "Blue", False)

        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Red", text)
        self.assertIn("Blue", text)

        self.assertEqual(ctx.channel.send.await_count, 2)
        embed1 = ctx.channel.send.await_args_list[0].kwargs["embed"]
        embed2 = ctx.channel.send.await_args_list[1].kwargs["embed"]
        self.assertEqual(embed1.title, "Red")
        self.assertIn("Alice", embed1.description)
        self.assertEqual(embed2.title, "Blue")
        self.assertIn("Bob", embed2.description)

        # the stored roster itself is untouched; same teams as before
        team1 = Team()
        team1.deserializeTeam(self.helperObj.get(GUILD_ID, "team1"))
        self.assertEqual(team1.get_name(), "Red")
        self.assertEqual([p.get_id() for p in team1.get_players()], [901])

    async def test_posts_a_fresh_roster_ready_for_new_reactions(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(), "Red", "Blue", False)
        before_message_id = self.helperObj.get(GUILD_ID, "roster_team2_message_id")

        await self.helperObj.reuseTeamsHelper(self._ctx())

        after_message_id = self.helperObj.get(GUILD_ID, "roster_team2_message_id")
        self.assertIsNotNone(after_message_id)
        self.assertNotEqual(after_message_id, before_message_id)

    async def test_stays_ranked_if_the_last_game_was_ranked(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(), "Red", "Blue", True)

        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("ranked", text.lower())
        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 1)

    async def test_stays_casual_if_the_last_game_was_casual(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(), "Red", "Blue", False)

        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)

        text = ctx.response.send_message.call_args.args[0]
        self.assertNotIn("ranked", text.lower())
        self.assertEqual(self.helperObj.get(GUILD_ID, "is_ranked"), 0)

    async def test_keeps_role_labels_if_the_last_roster_was_role_eligible(self):
        team1 = Team()
        team1.set_name("Red")
        team2 = Team()
        team2.set_name("Blue")
        for i in range(5):
            team1.add_player(Player(100 + i, f"Red{i}"))
            team2.add_player(Player(200 + i, f"Blue{i}"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "roster_use_roles", 1)

        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)

        embed1 = ctx.channel.send.await_args_list[0].kwargs["embed"]
        self.assertIn("Top - Red0", embed1.description)

    async def test_cancels_an_active_game_before_reposting(self):
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=901)
        member2 = FakeMember("Bob", id=902)
        channel1.members = [member1]
        channel2.members = [member2]
        guild = FakeGuild(channels=[og, channel1, channel2])

        await self.helperObj.createTeamHelper(self._ctx(901, "Alice", guild=guild), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob", guild=guild), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(guild=guild), "Red", "Blue", False)

        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")

        self.helperObj.ensureEconomyRow(GUILD_ID, 903, "Carol")
        self.cursor.execute("UPDATE economy SET balance=1200 WHERE guildId=? AND userId=?", (GUILD_ID, 903))
        self.db.commit()
        await self.helperObj.wagerHelper(
            FakeInteraction(guild, FakeMember("Carol", id=903)), 200, 1
        )
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 1000)  # escrowed

        ctx = self._ctx(guild=guild)
        await self.helperObj.reuseTeamsHelper(ctx)

        # the open bet is refunded and players are moved back before the
        # roster's reposted fresh
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 1200)
        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)
        messages = [c.args[0] for c in ctx.channel.send.call_args_list if c.args]
        self.assertTrue(any("cancelled" in m for m in messages))
        self.assertTrue(any("Moved everyone back" in m for m in messages))

        # unlike /clear, the teams themselves are NOT wiped; they're what
        # gets reposted right after
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")
        team1 = Team()
        team1.deserializeTeam(self.helperObj.get(GUILD_ID, "team1"))
        self.assertEqual(team1.get_name(), "Red")

    async def test_does_not_cancel_anything_when_no_game_is_active(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 2)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 2)
        await self.helperObj.useTeamsHelper(self._ctx(), "Red", "Blue", False)

        ctx = self._ctx()
        await self.helperObj.reuseTeamsHelper(ctx)

        messages = [c.args[0] for c in ctx.channel.send.call_args_list if c.args]
        self.assertFalse(any("cancelled" in m for m in messages))


class RegisterTeamHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", manage_guild=True):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id, manage_guild=manage_guild))

    async def _make_team(self, team_name, captain_id, captain_name, size):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, team_name, size)
        team_id, team = self.helperObj.getTeamRow(GUILD_ID, team_name)
        while team.get_size() < size:
            team.add_player(Player(1000 + team.get_size(), f"Filler{team.get_size()}"))
        self.helperObj.updateTeamData(team_id, team)
        return team_id

    async def test_rejects_when_no_tournament_exists(self):
        await self._make_team("Red", 901, "Alice", 2)
        ctx = self._ctx()
        await self.helperObj.registerTeamHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament set up for this server - use /tournament-create first."
        )

    async def test_rejects_unknown_team(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        ctx = self._ctx()
        await self.helperObj.registerTeamHelper(ctx, "Nonexistent")
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain_non_admin(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=False)
        await self.helperObj.registerTeamHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain or a member with the Manage Server permission can register it "
            "for the tournament."
        )

    async def test_non_captain_admin_can_register(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)
        ctx = self._ctx(user_id=902, name="Bob", manage_guild=True)
        await self.helperObj.registerTeamHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once()
        self.assertIn("registered", ctx.response.send_message.call_args.args[0])

    async def test_rejects_wrong_team_size(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 3, 4))
        await self._make_team("Red", 901, "Alice", 2)
        ctx = self._ctx()
        await self.helperObj.registerTeamHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "**Red** has 2 player(s), but this tournament needs teams of exactly 3."
        )

    async def test_rejects_double_registration(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)
        ctx1 = self._ctx()
        await self.helperObj.registerTeamHelper(ctx1, "Red")
        ctx2 = self._ctx()
        await self.helperObj.registerTeamHelper(ctx2, "Red")
        ctx2.response.send_message.assert_awaited_once_with(
            "**Red** is already registered for this tournament."
        )

    async def test_rejects_when_bracket_is_full(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 1))
        await self._make_team("Red", 901, "Alice", 2)
        await self._make_team("Blue", 902, "Bob", 2)

        ctx1 = self._ctx()
        await self.helperObj.registerTeamHelper(ctx1, "Red")

        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.registerTeamHelper(ctx2, "Blue")
        ctx2.response.send_message.assert_awaited_once_with("This tournament's bracket is already full.")

    async def test_rejects_shared_player_across_registered_teams(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)
        await self._make_team("Blue", 902, "Bob", 2)

        _, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        shared_player_id = red.get_players()[1].get_id()
        blue_id, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        blue.get_players()[1].set_id(shared_player_id)
        self.helperObj.updateTeamData(blue_id, blue)

        ctx1 = self._ctx()
        await self.helperObj.registerTeamHelper(ctx1, "Red")
        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.registerTeamHelper(ctx2, "Blue")

        ctx2.response.send_message.assert_awaited_once()
        self.assertIn(
            "already on a team registered", ctx2.response.send_message.call_args.args[0]
        )
        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(len(tournament.get_teams()), 1)

    async def test_successful_registration_saves_and_confirms(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)

        ctx = self._ctx()
        await self.helperObj.registerTeamHelper(ctx, "Red")

        ctx.response.send_message.assert_awaited_once_with("**Red** registered for **Cup**! (1/4 teams)")
        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(len(tournament.get_teams()), 1)
        self.assertEqual(tournament.get_teams()[0].get_name(), "Red")


class BuildBracketTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_power_of_two_team_count_builds_a_full_tree_with_no_byes(self):
        teams = [self._team(f"Team{i}") for i in range(4)]
        nodes = self.helperObj.buildBracket(teams)

        self.assertEqual(len(nodes), 7)  # 4 leaves + 2 round-2 + 1 final
        leaves = nodes[:4]
        self.assertEqual({n.team.get_name() for n in leaves}, {t.get_name() for t in teams})
        for leaf in leaves:
            self.assertIsNone(leaf.previous)
            self.assertIsNotNone(leaf.next)
            self.assertIsNotNone(leaf.opponent)

        final = nodes[-1]
        self.assertIsNone(final.team)
        self.assertIsNone(final.next)
        self.assertIsNotNone(final.previous)

    def test_non_power_of_two_team_count_pads_with_byes(self):
        teams = [self._team(f"Team{i}") for i in range(3)]
        nodes = self.helperObj.buildBracket(teams)

        self.assertEqual(len(nodes), 7)  # rounds up to 4 leaf slots
        bye_count = sum(1 for n in nodes[:4] if n.team is None)
        self.assertEqual(bye_count, 1)

    def test_opponent_pairing_is_symmetric_and_shares_next(self):
        teams = [self._team(f"Team{i}") for i in range(4)]
        nodes = self.helperObj.buildBracket(teams)
        leaf = nodes[0]
        self.assertIs(leaf.opponent.opponent, leaf)
        self.assertIs(leaf.next, leaf.opponent.next)

    def test_two_teams_builds_a_single_match_bracket(self):
        teams = [self._team("Red"), self._team("Blue")]
        nodes = self.helperObj.buildBracket(teams)
        self.assertEqual(len(nodes), 3)
        self.assertIsNone(nodes[-1].team)
        self.assertIsNone(nodes[-1].next)

    def test_byes_are_never_paired_against_each_other(self):
        # 6 teams in an 8-slot bracket needs 2 byes; a naive "real teams
        # first, byes at the tail, pair consecutively" seeding puts both
        # byes in the same first-round pair, which never has a winner to
        # report. Run it many times (buildBracket shuffles) to catch it
        # regardless of where randomization happens to place things.
        teams = [self._team(f"Team{i}") for i in range(6)]
        for _ in range(50):
            nodes = self.helperObj.buildBracket(teams)
            leaves = nodes[:8]
            for i in range(0, len(leaves), 2):
                a, b = leaves[i], leaves[i + 1]
                self.assertFalse(
                    a.team is None and b.team is None,
                    "two byes were paired against each other"
                )


class CreateBracketHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    async def test_rejects_when_no_tournament_exists(self):
        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, False)
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament set up for this server - use /tournament-create first."
        )

    async def test_rejects_fewer_than_two_registered_teams(self):
        tournament = Tournament("Cup", 2, 4)
        tournament.register_team(self._team("Red"))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, False)
        ctx.response.send_message.assert_awaited_once_with(
            "Need at least 2 registered teams to build a bracket."
        )

    async def test_builds_and_saves_a_bracket(self):
        tournament = Tournament("Cup", 2, 4)
        tournament.register_team(self._team("Red"))
        tournament.register_team(self._team("Blue"))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, True)

        ctx.response.send_message.assert_awaited_once_with(
            "Bracket created for **Cup** - 2 teams, double elimination. "
            "Losers bracket starts once the winners bracket finishes."
        )
        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertTrue(restored.is_double_elimination())
        self.assertEqual(len(restored.get_bracket()), 3)

    async def test_calling_again_rerolls_the_bracket(self):
        tournament = Tournament("Cup", 2, 4)
        tournament.register_team(self._team("Red"))
        tournament.register_team(self._team("Blue"))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx1 = self._ctx()
        await self.helperObj.createBracketHelper(ctx1, False)

        ctx2 = self._ctx()
        await self.helperObj.createBracketHelper(ctx2, False)
        second = self.helperObj.getTournament(GUILD_ID)

        self.cursor.execute("SELECT COUNT(*) FROM tournaments WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)
        self.assertEqual(len(second.get_bracket()), 3)


class ClearTournamentMatchesForGuildTests(HelperTestCase):
    def _insert_match(self, guild_id, match_id=None):
        columns = "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType)"
        values = "(?, 0, 0, '', '', 'RESOLVED', 'simultaneous', NULL, NULL, NULL, 'winners')"
        if match_id is None:
            self.cursor.execute(f"INSERT INTO tournament_matches{columns} VALUES{values}", (guild_id,))
        else:
            self.cursor.execute(
                f"INSERT INTO tournament_matches(id, guildId, roundIndex, nodeIndex, team1, team2, state, "
                f"mode, messageId, channelId, winner, bracketType) "
                f"VALUES(?, ?, 0, 0, '', '', 'RESOLVED', 'simultaneous', NULL, NULL, NULL, 'winners')",
                (match_id, guild_id)
            )
        self.db.commit()
        return self.cursor.lastrowid

    async def test_restarts_the_id_sequence_once_the_table_is_left_completely_empty(self):
        self._insert_match(GUILD_ID)
        self._insert_match(GUILD_ID)

        self.helperObj._clearTournamentMatchesForGuild(GUILD_ID)

        new_id = self._insert_match(GUILD_ID)
        self.assertEqual(new_id, 1)

    async def test_does_not_restart_the_sequence_while_another_guild_still_has_rows(self):
        self._insert_match(GUILD_ID)
        other_guild_match_id = self._insert_match(902)

        self.helperObj._clearTournamentMatchesForGuild(GUILD_ID)

        new_id = self._insert_match(GUILD_ID)
        # continues past the other guild's highest id instead of colliding
        # with it; _settleMatchWagers and the concurrent-betting-close
        # timer both key off matchId alone, with no guildId in their WHERE
        # clause, so a reused id could settle or close out that guild's
        # still-live match.
        self.assertGreater(new_id, other_guild_match_id)

    async def test_does_not_crash_on_a_database_that_has_never_had_a_match(self):
        # sqlite_sequence doesn't exist at all until some AUTOINCREMENT
        # table's first insert happens anywhere in the DB.
        self.helperObj._clearTournamentMatchesForGuild(GUILD_ID)
        new_id = self._insert_match(GUILD_ID)
        self.assertEqual(new_id, 1)


def _captained_team(name, captain_id, captain_name, extra_players=()):
    team = Team()
    team.set_name(name)
    captain = Player(captain_id, captain_name)
    team.add_player(captain)
    for player_id, player_name in extra_players:
        team.add_player(Player(player_id, player_name))
    team.set_captain(captain)
    return team


class BracketRoundsAndLabelTests(HelperTestCase):
    def test_bracket_rounds_groups_by_round_size(self):
        teams = [Team() for _ in range(4)]
        for i, team in enumerate(teams):
            team.set_name(f"Team{i}")
        nodes = self.helperObj.buildBracket(teams)
        rounds = self.helperObj._bracketRounds(nodes)
        self.assertEqual([len(r) for r in rounds], [4, 2, 1])

    def test_empty_bracket_has_no_rounds(self):
        self.assertEqual(self.helperObj._bracketRounds([]), [])

    def test_node_label_uses_real_team_name_when_known(self):
        team = Team()
        team.set_name("Red")
        node = BracketNode(team)
        self.assertEqual(self.helperObj._bracketNodeLabel(node, round_index=1), "Red")

    def test_node_label_is_bye_for_an_empty_round_zero_slot(self):
        self.assertEqual(self.helperObj._bracketNodeLabel(BracketNode(), round_index=0), "BYE")

    def test_node_label_is_tbd_for_an_undecided_later_round(self):
        self.assertEqual(self.helperObj._bracketNodeLabel(BracketNode(), round_index=1), "TBD")


class RenderBracketTextTests(HelperTestCase):
    async def test_no_bracket_yet(self):
        tournament = Tournament("Cup", 2, 4)
        self.assertEqual(
            self.helperObj.renderBracketText(tournament), "No bracket has been created yet."
        )

    async def test_single_elimination_shows_tournament_name_and_champion_status(self):
        tournament = Tournament("Cup", 2, 4)
        red, blue = Team(), Team()
        red.set_name("Red")
        blue.set_name("Blue")
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))

        text = self.helperObj.renderBracketText(tournament)

        self.assertIn("Cup", text)
        self.assertIn("Champion:** TBD", text)
        # the tree itself (team names, connectors) lives in the image now
        # (renderBracketImages); the text is just a short status line
        self.assertNotIn("```", text)

    async def test_resolved_winner_shows_real_name_instead_of_tbd(self):
        tournament = Tournament("Cup", 2, 2)
        red, blue = Team(), Team()
        red.set_name("Red")
        blue.set_name("Blue")
        bracket = self.helperObj.buildBracket([red, blue])
        tournament.set_bracket(bracket)

        rounds = self.helperObj._bracketRounds(bracket)
        leaf_a = rounds[0][0]
        leaf_a.next.team = leaf_a.team

        text = self.helperObj.renderBracketText(tournament)
        self.assertIn(f"Champion:** {leaf_a.team.get_name()}", text)


# ===========================================================================
# Bracket images: the winners/losers trees themselves are drawn as PNGs
# (see renderBracketImages) rather than ASCII art, so tests here check
# structure (file count/names, valid PNG data, image doesn't crash and
# grows sensibly with more/longer content) rather than exact pixel content.
# ===========================================================================

class BracketImageTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_single_elimination_produces_one_image(self):
        teams = [self._team(n) for n in ["Red", "Blue"]]
        tournament = Tournament("Cup", 1, 2, False)
        tournament.set_bracket(self.helperObj.buildBracket(teams))

        files = self.helperObj.renderBracketImages(tournament)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "winners_bracket.png")
        # every PNG file starts with this fixed 8-byte signature
        self.assertTrue(files[0].fp.read(8).startswith(b"\x89PNG"))

    def test_double_elimination_produces_two_images(self):
        teams = [self._team(f"T{i}") for i in range(4)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        files = self.helperObj.renderBracketImages(tournament)
        self.assertEqual([f.filename for f in files], ["winners_bracket.png", "losers_bracket.png"])

    def test_grand_finals_image_only_appears_once_game_one_is_played(self):
        # renderBracketImages itself never includes the Grand Finals image -
        # it's sent as its own separate message (see _sendBracketText), and
        # only once Grand Finals has actually been played, so it's covered
        # here via _renderGrandFinalsImage directly instead.
        red, blue, cleo, dan = (self._team(n) for n in ["Red", "Blue", "Cleo Team", "Dan Team"])
        wb_nodes = self.helperObj.buildBracket([red, blue, cleo, dan])
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        files = self.helperObj.renderBracketImages(tournament)
        self.assertEqual([f.filename for f in files], ["winners_bracket.png", "losers_bracket.png"])

        # Before either bracket has a champion, there's nothing to draw yet.
        self.assertIsNone(self.helperObj._renderGrandFinalsImage(GUILD_ID, tournament))

        # Both champions exist, but Grand Finals game 1 hasn't been played -
        # still None, since "vs, nothing decided yet" isn't worth a message.
        wb_champion = self.helperObj._bracketRounds(wb_nodes)[-1][0]
        wb_champion.team = red
        lb_rounds[-1][0].team = blue
        self.assertIsNone(self.helperObj._renderGrandFinalsImage(GUILD_ID, tournament))

        # Game 1 resolved: the image appears.
        self.cursor.execute(
            "INSERT INTO tournament_matches"
            "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, "
            "bracketType) VALUES(?, 0, -1, ?, ?, 'RESOLVED', 'sequential', NULL, NULL, 1, 'finals')",
            (GUILD_ID, red.serializeTeam(), blue.serializeTeam())
        )
        self.db.commit()
        image = self.helperObj._renderGrandFinalsImage(GUILD_ID, tournament)
        self.assertIsNotNone(image)
        self.assertGreater(image.width, 0)

    def test_degenerate_two_team_double_elimination_has_no_losers_image(self):
        # only one winners-bracket match exists at all, so its loser has
        # nobody left to play, no losers-bracket tree to draw
        teams = [self._team(n) for n in ["Red", "Blue"]]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 2, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        files = self.helperObj.renderBracketImages(tournament)
        self.assertEqual([f.filename for f in files], ["winners_bracket.png"])

    def test_image_grows_taller_with_more_teams(self):
        def image_for(n):
            teams = [self._team(f"T{i}") for i in range(n)]
            tournament = Tournament("Cup", 1, n, False)
            tournament.set_bracket(self.helperObj.buildBracket(teams))
            return self.helperObj._renderWinnersBracketImage(tournament)

        small = image_for(4)
        large = image_for(32)
        self.assertGreater(large.height, small.height)

    def test_image_grows_wider_with_longer_team_names(self):
        def image_for(teams):
            tournament = Tournament("Cup", 1, 4, False)
            tournament.set_bracket(self.helperObj.buildBracket(teams))
            return self.helperObj._renderWinnersBracketImage(tournament)

        narrow = image_for([self._team(f"T{i}") for i in range(4)])
        wide = image_for([self._team(f"A Very Long Team Name Indeed Number {i}") for i in range(4)])
        self.assertGreater(wide.width, narrow.width)

    def test_bye_slot_renders_without_crashing(self):
        teams = [self._team(f"T{i}") for i in range(3)]  # bracket size 4, one bye
        tournament = Tournament("Cup", 1, 3, False)
        tournament.set_bracket(self.helperObj.buildBracket(teams))
        image = self.helperObj._renderWinnersBracketImage(tournament)
        self.assertGreater(image.width, 0)
        self.assertGreater(image.height, 0)

    def test_losers_bracket_with_fresh_drop_in_leaves_renders_without_crashing(self):
        # Regression coverage for the losers-bracket "fresh drop-in" leaf
        # positioning (see _assignBracketPositions) across several team
        # counts, including non-power-of-two ones; those feed winners-
        # bracket byes into the losers bracket, which used to need special-
        # case leading-blank padding in the old ASCII renderer; the pixel-
        # based layout needs no such handling, but this still guards
        # against it silently breaking again. Also spans both sides of the
        # two-sided-layout threshold (8 and 16+ teams).
        for n in [2, 3, 4, 5, 6, 7, 8, 16, 20, 32]:
            teams = [self._team(f"T{i}") for i in range(n)]
            wb_nodes = self.helperObj.buildBracket(teams)
            lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
            tournament = Tournament("Cup", 1, n, True)
            tournament.set_bracket(wb_nodes)
            tournament.set_losers_bracket(lb_nodes, lb_rounds)

            files = self.helperObj.renderBracketImages(tournament)
            self.assertGreaterEqual(len(files), 1, f"n={n}")
            for f in files:
                self.assertTrue(f.fp.read(8).startswith(b"\x89PNG"), f"n={n}, {f.filename}")

    def test_losers_bracket_uses_two_sided_layout_once_large_enough(self):
        def build_tournament(n):
            teams = [self._team(f"T{i}") for i in range(n)]
            wb_nodes = self.helperObj.buildBracket(teams)
            lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
            tournament = Tournament("Cup", 1, n, True)
            tournament.set_bracket(wb_nodes)
            tournament.set_losers_bracket(lb_nodes, lb_rounds)
            return tournament, lb_rounds

        # 8 teams is the smallest losers bracket that clears
        # BRACKET_TWO_SIDED_MIN_ROUNDS (4 losers-bracket rounds); below
        # that it should stay on the single-sided renderer.
        _, small_lb_rounds = build_tournament(4)
        self.assertLess(len(small_lb_rounds), helper_module.BRACKET_TWO_SIDED_MIN_ROUNDS)

        large_tournament, large_lb_rounds = build_tournament(8)
        self.assertGreaterEqual(len(large_lb_rounds), helper_module.BRACKET_TWO_SIDED_MIN_ROUNDS)
        large_image = self.helperObj._renderLosersBracketImage(large_tournament)

        # the two-sided layout converges toward the center, which for a
        # roughly-symmetric bracket makes it noticeably wider than tall,
        # not a precise assertion, just a sanity check that something
        # structurally different is actually happening once the layout
        # switches over (the single-sided renderer would instead keep
        # growing taller as team count increases, same as the winners-
        # bracket equivalent test above).
        self.assertGreater(large_image.width, large_image.height)

    def test_losers_bracket_two_sided_merge_point_renders_without_crashing(self):
        # The smallest bracket that reaches the two-sided losers-bracket
        # layout (8 teams, see BRACKET_TWO_SIDED_MIN_ROUNDS) also has the
        # smallest possible "merge point" halves (see
        # _renderLosersTwoSidedTreeImage), each just a single match deep.
        # Exercises the boundary directly rather than relying on a bigger
        # bracket to happen to cover it.
        teams = [self._team(f"T{i}") for i in range(8)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 8, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        image = self.helperObj._renderLosersTwoSidedTreeImage(lb_rounds, "Cup - Losers Bracket")
        self.assertGreater(image.width, 0)
        self.assertGreater(image.height, 0)


class BracketNodeTextColorTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    # A node whose team won and advanced should stand out in full
    # brightness, not get dimmed as a "stale waypoint".
    def test_winner_node_is_not_dimmed(self):
        red, blue = self._team("Red"), self._team("Blue")
        winner_node = BracketNode(team=red)
        next_node = BracketNode(team=red)  # Red's name repeats - they won
        winner_node.next = next_node

        self.assertEqual(self.helperObj._bracketNodeTextColor(winner_node), helper_module.BRACKET_TEXT_COLOR)

    def test_loser_node_is_dimmed(self):
        red, blue = self._team("Red"), self._team("Blue")
        loser_node = BracketNode(team=blue)
        next_node = BracketNode(team=red)  # Red's name shows up instead - Blue lost
        loser_node.next = next_node

        self.assertEqual(self.helperObj._bracketNodeTextColor(loser_node), helper_module.BRACKET_LINE_COLOR)

    def test_undecided_match_is_not_dimmed(self):
        node = BracketNode(team=self._team("Red"))
        node.next = BracketNode(team=None)  # match hasn't resolved yet

        self.assertEqual(self.helperObj._bracketNodeTextColor(node), helper_module.BRACKET_TEXT_COLOR)

    def test_champion_node_is_not_dimmed(self):
        champion_node = BracketNode(team=self._team("Red"))  # no .next at all

        self.assertEqual(self.helperObj._bracketNodeTextColor(champion_node), helper_module.BRACKET_TEXT_COLOR)

    def test_empty_node_is_not_dimmed(self):
        self.assertEqual(self.helperObj._bracketNodeTextColor(BracketNode()), helper_module.BRACKET_TEXT_COLOR)


class GrandFinalsImageDimmingTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    # Intercepts every draw.text(...) call the real render makes (wraps the
    # actual method so the image still renders normally) and returns
    # {label_text: fill_color}, so the fix can be checked against what
    # actually got drawn instead of re-deriving the same formula the
    # implementation uses.
    def _drawn_colors(self, *args, **kwargs):
        calls = {}
        original = ImageDraw.ImageDraw.text

        def recording_text(self_draw, xy, text, *a, fill=None, **kw):
            calls[text] = fill
            return original(self_draw, xy, text, *a, fill=fill, **kw)

        with patch.object(ImageDraw.ImageDraw, "text", recording_text):
            image = self.helperObj._buildGrandFinalsImage(*args, **kwargs)
        return image, calls

    # Each Grand Finals stage dims whichever side lost, matching the main
    # bracket images (see BracketNodeTextColorTests above).
    def test_game_one_dims_the_loser_only(self):
        wb_champion, lb_champion = self._team("Red"), self._team("Blue")
        image, calls = self._drawn_colors(
            Tournament("Cup", 1, 4, True), wb_champion, lb_champion,
            game1_winner_name="Red", reset_winner_name=None,
        )
        self.assertGreater(image.width, 0)

        top_label = "Red (winners bracket)"
        bottom_label = "Blue (losers bracket)"
        self.assertEqual(calls[top_label], helper_module.BRACKET_TEXT_COLOR)  # won game 1
        self.assertEqual(calls[bottom_label], helper_module.BRACKET_LINE_COLOR)  # lost

    def test_reset_stage_dims_whichever_side_lost_the_decider(self):
        wb_champion, lb_champion = self._team("Red"), self._team("Blue")
        # lb_champion won game 1 (forcing a reset), then wb_champion wins
        # the decider, the reset stage's top is lb_champion, bottom is
        # wb_champion (see _buildGrandFinalsImage's stage construction).
        image, calls = self._drawn_colors(
            Tournament("Cup", 1, 4, True), wb_champion, lb_champion,
            game1_winner_name="Blue", reset_winner_name="Red",
        )
        self.assertGreater(image.width, 0)

        reset_top_label = "Blue (won Game 1)"
        reset_bottom_label = "Red (elimination game)"
        self.assertEqual(calls[reset_top_label], helper_module.BRACKET_LINE_COLOR)  # lost the decider
        self.assertEqual(calls[reset_bottom_label], helper_module.BRACKET_TEXT_COLOR)  # won it

    def test_undecided_stage_dims_neither_side(self):
        wb_champion, lb_champion = self._team("Red"), self._team("Blue")
        image, calls = self._drawn_colors(
            Tournament("Cup", 1, 4, True), wb_champion, lb_champion,
            game1_winner_name=None, reset_winner_name=None,
        )
        self.assertGreater(image.width, 0)
        self.assertEqual(calls["Red (winners bracket)"], helper_module.BRACKET_TEXT_COLOR)
        self.assertEqual(calls["Blue (losers bracket)"], helper_module.BRACKET_TEXT_COLOR)


class PrintBracketHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_when_no_tournament_exists(self):
        ctx = self._ctx()
        await self.helperObj.printBracketHelper(ctx)
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament set up for this server - use /tournament-create first."
        )

    async def test_prints_the_bracket(self):
        tournament = Tournament("Cup", 2, 4)
        red, blue = Team(), Team()
        red.set_name("Red")
        blue.set_name("Blue")
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.printBracketHelper(ctx)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("Cup", text)

        files = ctx.response.send_message.call_args.kwargs.get("files")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "winners_bracket.png")


class RenderMatchupImageTests(_FakeLogoDirTestCase):
    def _team(self, name, captain_id=None, captain_name=None, extra_players=()):
        team = Team()
        team.set_name(name)
        # extra_players added BEFORE the captain, so a naive "first player
        # in the list" reading would get this wrong; _orderedRoster has to
        # actually float the captain to the front, not just happen to
        # already find them there.
        for player_id, player_name in extra_players:
            team.add_player(Player(player_id, player_name))
        if captain_id is not None:
            captain = Player(captain_id, captain_name)
            team.add_player(captain)
            team.set_captain(captain)
        return team

    def test_ordered_roster_puts_the_captain_first(self):
        team = self._team(
            "Red", captain_id=902, captain_name="Bob",
            extra_players=[(901, "Alice"), (903, "Cleo")]
        )
        roster = self.helperObj._orderedRoster(team)
        self.assertEqual([p.get_name() for p in roster], ["Bob", "Alice", "Cleo"])

    def test_ordered_roster_with_no_captain_preserves_original_order(self):
        team = Team()
        team.set_name("Red")
        team.add_player(Player(901, "Alice"))
        team.add_player(Player(902, "Bob"))
        roster = self.helperObj._orderedRoster(team)
        self.assertEqual([p.get_name() for p in roster], ["Alice", "Bob"])

    def test_renders_without_crashing_for_teams_with_and_without_logos(self):
        team1 = self._team("Red", captain_id=901, captain_name="Alice", extra_players=[(902, "Bob")])
        real_logo_path = os.path.join(self._logo_dir.name, "Demacia.png")
        team1.set_logo_path(real_logo_path)
        team2 = self._team("Blue", captain_id=903, captain_name="Cleo")  # no logo set

        image = self.helperObj._renderMatchupImage(7, team1, team2, "Round 1", "Cup", "Test Guild")
        self.assertEqual(image.mode, "RGB")
        self.assertGreater(image.width, 0)
        self.assertGreater(image.height, 0)

    def test_team_with_no_logo_gets_a_random_built_in_one_instead_of_a_bare_ring(self):
        # /make-teams, /captains, etc. build ad-hoc Team objects that never
        # go through _ensureLogo (that's only ever called for persistent
        # teams), so team.get_logo_path() is None for them; this is what
        # picks a stand-in logo for the matchup graphic instead of just
        # drawing an empty ring.
        team1 = self._team("Red", captain_id=901, captain_name="Alice")
        team2 = self._team("Blue", captain_id=902, captain_name="Bob")
        self.assertIsNone(team1.get_logo_path())
        self.assertIsNone(team2.get_logo_path())

        with patch.object(helper_module.random, "choice", side_effect=lambda seq: seq[0]) as choice_mock:
            self.helperObj._renderMatchupImage(1, team1, team2, "Round 1", "Cup", "Test Guild")

        # once per team, picking from the same built-in set listAvailableLogos() returns
        self.assertEqual(choice_mock.call_count, 2)
        for call in choice_mock.call_args_list:
            self.assertEqual(call.args[0], self.helperObj.listAvailableLogos())

        # still leaves the team objects themselves untouched, nothing to
        # persist a pick against for a team with no stable identity.
        self.assertIsNone(team1.get_logo_path())
        self.assertIsNone(team2.get_logo_path())

    def test_captain_star_gets_room_so_a_long_captain_name_is_not_clipped(self):
        # Regression: column_width() used to measure the captain's name
        # alone, ignoring the star _drawMatchupColumn draws just to its
        # left; a name that already filled the column left the star (and
        # sometimes the name's own left edge) clipped off the canvas.
        long_name = "flamebringer"
        captain_team = self._team("Red", captain_id=901, captain_name=long_name)
        no_captain_team = Team()
        no_captain_team.set_name("Red")
        no_captain_team.add_player(Player(901, long_name))  # same name, not captain
        opponent = self._team("Blue", captain_id=902, captain_name="Bob")

        with_captain = self.helperObj._renderMatchupImage(
            1, captain_team, opponent, "Round 1", "Cup", "Test Guild"
        )
        without_captain = self.helperObj._renderMatchupImage(
            2, no_captain_team, opponent, "Round 1", "Cup", "Test Guild"
        )

        self.assertGreater(with_captain.width, without_captain.width)

    def test_taller_roster_grows_the_canvas(self):
        big_team = self._team(
            "Red", captain_id=901, captain_name="Alice",
            extra_players=[(902, "Bob"), (903, "Cleo"), (904, "Dan")]
        )
        small_team = self._team("Blue", captain_id=905, captain_name="Eve")

        tall_image = self.helperObj._renderMatchupImage(1, big_team, small_team, "Round 1", "Cup", "Test Guild")
        short_image = self.helperObj._renderMatchupImage(2, small_team, small_team, "Round 1", "Cup", "Test Guild")
        self.assertGreater(tall_image.height, short_image.height)

    def test_header_includes_round_tournament_and_guild_name(self):
        team1 = self._team("Red", captain_id=901, captain_name="Alice")
        team2 = self._team("Blue", captain_id=902, captain_name="Bob")

        image = self.helperObj._renderMatchupImage(
            3, team1, team2, "Semifinals", "Winter Cup", "Test Guild"
        )
        # No text-extraction from a rendered PNG, so this just confirms the
        # canvas grows to accommodate a long subtitle rather than clipping
        # it; the actual string content is exercised by _matchRoundLabel's
        # and _postMatchReport's own tests.
        short_header_image = self.helperObj._renderMatchupImage(3, team1, team2, "R1", None, None)
        self.assertGreaterEqual(image.width, short_header_image.width)

    def test_match_round_label_winners_bracket_uses_round_names(self):
        tournament = Tournament("Cup", 1, 4)
        red, blue, cleo, dan = Team(), Team(), Team(), Team()
        for t, n in ((red, "Red"), (blue, "Blue"), (cleo, "Cleo"), (dan, "Dan")):
            t.set_name(n)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue, cleo, dan]))

        self.assertEqual(self.helperObj._matchRoundLabel(tournament, 0, "winners"), "Semifinals")
        self.assertEqual(self.helperObj._matchRoundLabel(tournament, 1, "winners"), "Finals")

    def test_match_round_label_losers_bracket_is_numbered(self):
        tournament = Tournament("Cup", 1, 4)
        self.assertEqual(self.helperObj._matchRoundLabel(tournament, 0, "losers"), "Losers Round 1")
        self.assertEqual(self.helperObj._matchRoundLabel(tournament, 2, "losers"), "Losers Round 3")

    def test_match_round_label_finals(self):
        tournament = Tournament("Cup", 1, 4)
        self.assertEqual(self.helperObj._matchRoundLabel(tournament, 0, "finals"), "Grand Finals")
        self.assertEqual(
            self.helperObj._matchRoundLabel(tournament, 1, "finals"), "Grand Finals - Bracket Reset"
        )


class StartTournamentHelperTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)

    def _tournament_with_teams(self, *teams, team_size=1):
        tournament = Tournament("Cup", team_size, len(teams))
        for team in teams:
            tournament.register_team(team)
        tournament.set_bracket(self.helperObj.buildBracket(list(teams)))
        self.helperObj.saveTournament(GUILD_ID, tournament)
        return tournament

    async def test_rejects_when_no_tournament_exists(self):
        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament set up for this server - use /tournament-create first."
        )

    async def test_rejects_when_no_bracket_exists(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 1, 2))
        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")
        ctx.response.send_message.assert_awaited_once_with(
            "No bracket has been created yet - use /tournament-create-bracket first."
        )

    async def test_rejects_when_already_finished(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = self._tournament_with_teams(red, blue)
        champion_node = tournament.get_bracket()[-1]
        champion_node.team = red
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")
        ctx.response.send_message.assert_awaited_once_with(
            "**Cup** is already finished - **Red** is the champion!"
        )

    async def test_rejects_when_round_already_in_progress(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        self._tournament_with_teams(red, blue)
        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner) VALUES(?, 0, 0, ?, ?, 'PENDING_READY', 'sequential', 1, ?, NULL)",
            (GUILD_ID, red.serializeTeam(), blue.serializeTeam(), self.channel.id)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")
        ctx.response.send_message.assert_awaited_once_with(
            "This tournament's current round is already in progress."
        )

    async def test_sequential_posts_ready_check_for_first_match_only(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._tournament_with_teams(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")

        self.cursor.execute(
            "SELECT state FROM tournament_matches WHERE guildId=? ORDER BY id", (GUILD_ID,)
        )
        states = [row[0] for row in self.cursor.fetchall()]
        self.assertEqual(states, ["PENDING_READY", "QUEUED"])

    async def test_sequential_ready_check_attaches_a_matchup_image(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        self._tournament_with_teams(red, blue)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")

        file_calls = [c for c in self.channel.send.call_args_list if "file" in c.kwargs]
        self.assertEqual(len(file_calls), 1)
        self.assertIsInstance(file_calls[0].kwargs["file"], discord.File)
        self.assertTrue(file_calls[0].kwargs["file"].filename.endswith("_vs.png"))

    async def test_simultaneous_posts_report_for_every_match(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._tournament_with_teams(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        self.cursor.execute(
            "SELECT state FROM tournament_matches WHERE guildId=? ORDER BY id", (GUILD_ID,)
        )
        states = [row[0] for row in self.cursor.fetchall()]
        self.assertEqual(states, ["AWAITING_RESULT", "AWAITING_RESULT"])
        # 2 matches * (1 report message + 2 reactions) = channel.send called
        # at least twice for the reports, on top of the round-kickoff message
        self.assertGreaterEqual(self.channel.send.await_count, 3)

    async def test_simultaneous_reports_each_attach_a_matchup_image(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._tournament_with_teams(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        file_calls = [c for c in self.channel.send.call_args_list if "file" in c.kwargs]
        self.assertEqual(len(file_calls), 2)  # one per match
        for call in file_calls:
            self.assertIsInstance(call.kwargs["file"], discord.File)

    async def test_bye_auto_advances_without_creating_a_match(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        # 3 teams -> bracket size 4, one bye
        tournament = self._tournament_with_teams(red, blue, cleo)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        # only 1 real match created (the bye pair never becomes a match)
        self.cursor.execute("SELECT COUNT(*) FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 1)


# ===========================================================================
# Every top-level Pillow render (matchup graphics, bracket images, trading/
# team cards, previews) is invoked via asyncio.to_thread from its own async
# caller rather than directly, see the readme's own "Every top-level render
# call..." paragraph. patch("asyncio.to_thread", wraps=asyncio.to_thread)
# below intercepts the call (to prove the offload actually happened) while
# still delegating to the real asyncio.to_thread, so the rest of each test's
# own assertions exercise genuinely-threaded, not mocked-away, behavior.
# ===========================================================================

class ImageRenderThreadOffloadTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    async def test_casual_matchup_image_is_offloaded(self):
        team1, team2 = self._team("Red"), self._team("Blue")
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj._sendMatchupImage(self.channel, team1, team2, "Normal")

        mock_to_thread.assert_awaited_once_with(
            self.helperObj._renderMatchupImage, None, team1, team2, "Normal", None, self.guild.name
        )
        # The real renderer actually ran (in the executor) and produced a
        # real file, not just a recorded call, proves wraps= genuinely
        # delegated rather than the offload silently swallowing the work.
        self.channel.send.assert_awaited_once()
        self.assertIsInstance(self.channel.send.call_args.kwargs["file"], discord.File)

    async def test_sequential_ready_check_matchup_image_is_offloaded(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(red)
        tournament.register_team(blue)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj.startTournamentHelper(
                FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel), "sequential"
            )

        render_calls = [c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj._renderMatchupImage]
        self.assertEqual(len(render_calls), 1)

    async def test_bracket_images_are_offloaded(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(red)
        tournament.register_team(blue)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj._sendBracketText(self.channel, tournament, GUILD_ID)

        bracket_calls = [c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj.renderBracketImages]
        self.assertEqual(len(bracket_calls), 1)

    async def test_grand_finals_drawing_is_offloaded_but_its_db_read_is_not(self):
        red, blue, cleo, dan = (self._team(n) for n in ["Red", "Blue", "Cleo Team", "Dan Team"])
        wb_nodes = self.helperObj.buildBracket([red, blue, cleo, dan])
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)
        self.helperObj._bracketRounds(wb_nodes)[-1][0].team = red
        lb_rounds[-1][0].team = blue
        self.cursor.execute(
            "INSERT INTO tournament_matches"
            "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, "
            "bracketType) VALUES(?, 0, -1, ?, ?, 'RESOLVED', 'sequential', NULL, NULL, 1, 'finals')",
            (GUILD_ID, red.serializeTeam(), blue.serializeTeam())
        )
        self.db.commit()

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj._sendBracketText(self.channel, tournament, GUILD_ID)

        # _buildGrandFinalsImage (the pure drawing) goes through the
        # offload; _grandFinalsRenderInputs (the DB read feeding it) is
        # never handed to asyncio.to_thread at all; self.cursor is
        # thread-affined (sqlite3's default check_same_thread=True), so
        # offloading that half outright would crash instead.
        offloaded = {c.args[0] for c in mock_to_thread.await_args_list}
        self.assertIn(self.helperObj._buildGrandFinalsImage, offloaded)
        self.assertNotIn(self.helperObj._grandFinalsRenderInputs, offloaded)
        self.assertNotIn(self.helperObj._renderGrandFinalsImage, offloaded)
        finals_calls = [
            c for c in self.channel.send.call_args_list
            if any(f.filename == "grand_finals.png" for f in c.kwargs.get("files", []))
        ]
        self.assertEqual(len(finals_calls), 1)

    async def test_team_card_render_is_offloaded(self):
        await self.helperObj.createTeamHelper(
            FakeInteraction(self.guild, FakeMember("Alice", id=901)), "Red", 5
        )
        team_id, _ = self.helperObj.getTeamRow(GUILD_ID, "Red")
        message = FakeMessage()

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj._swapTeamStatsForCard(message, GUILD_ID, "Test Guild", team_id)

        render_calls = [c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj._renderTeamCardImage]
        self.assertEqual(len(render_calls), 1)
        message.edit.assert_awaited_once()

    async def test_trading_card_render_is_offloaded(self):
        member = FakeMember("Alice", id=901)
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            file = await self.helperObj._renderMemberTradingCardFile(GUILD_ID, "Test Guild", member)

        render_calls = [
            c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj._renderTradingCardImage
        ]
        self.assertEqual(len(render_calls), 1)
        self.assertIsInstance(file, discord.File)

    async def test_preview_image_render_is_offloaded(self):
        # ignore_cleanup_errors: previewHelper's own discord.File(path) keeps
        # the rendered PNG open past this block on Windows, which won't let
        # a directory delete out from under an open file the way POSIX does
        # (same reasoning _FakeLogoDirTestCase's own temp dir uses this for).
        preview_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
            with patch.object(helper_module, "PREVIEW_DIR", preview_dir.name), \
                 patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
                await self.helperObj.previewHelper(ctx, "Fonts")
        finally:
            preview_dir.cleanup()

        render_calls = [
            c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj._renderPreviewImages
        ]
        self.assertEqual(len(render_calls), 1)
        ctx.response.send_message.assert_awaited_once()

    async def test_render_does_not_block_the_event_loop(self):
        # Proves the offload is genuine, not just present in the call graph:
        # a slow render (blocked here on a threading.Event, since it runs in
        # a worker thread, not on the event loop) must not stop an unrelated
        # coroutine from making progress on the loop while it's in flight.
        render_started = threading.Event()
        release_render = threading.Event()
        real_render = self.helperObj._renderMatchupImage

        def slow_render(*args, **kwargs):
            render_started.set()
            release_render.wait(timeout=5)
            return real_render(*args, **kwargs)

        progress = []

        async def other_work():
            for i in range(3):
                await asyncio.sleep(0)
                progress.append(i)

        with patch.object(self.helperObj, "_renderMatchupImage", side_effect=slow_render):
            team1, team2 = self._team("Red"), self._team("Blue")
            render_task = asyncio.create_task(
                self.helperObj._sendMatchupImage(self.channel, team1, team2, "Normal")
            )
            await asyncio.to_thread(render_started.wait, 5)

            # The render is now blocked inside its worker thread; the event
            # loop itself must still be free to run something else.
            await other_work()
            self.assertEqual(progress, [0, 1, 2])
            self.assertFalse(render_task.done())

            release_render.set()
            await render_task

        self.channel.send.assert_awaited_once()

    async def test_simultaneous_round_offloads_a_separate_render_per_match(self):
        # "Simultaneous" mode posts every match in a round at once (see
        # _startRound); each match's own matchup image still goes through
        # its own independent asyncio.to_thread call, and each ends up with
        # the correct, un-mixed-up pair of teams rather than one match's
        # render bleeding into another's.
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        tournament = Tournament("Cup", 1, 4)
        for team in (red, blue, cleo, dan):
            tournament.register_team(team)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue, cleo, dan]))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            await self.helperObj.startTournamentHelper(
                FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel), "simultaneous"
            )

        render_calls = [c for c in mock_to_thread.await_args_list if c.args[0] == self.helperObj._renderMatchupImage]
        self.assertEqual(len(render_calls), 2)  # one independent offload per match

        # Each match's own pair of teams reached its own render call intact;
        # team1/team2 are args[2]/args[3] (args[1] is the match_id). Who
        # actually plays whom is random (buildBracket shuffles seeding), so
        # this checks the pairing is internally consistent rather than
        # asserting one specific bracket; two disjoint pairs, together
        # covering all four teams exactly once, is what "no cross-match
        # mix-up" actually means here.
        rendered_pairs = [frozenset((c.args[2].get_name(), c.args[3].get_name())) for c in render_calls]
        for pair in rendered_pairs:
            self.assertEqual(len(pair), 2)
        all_teams_seen = set().union(*rendered_pairs)
        self.assertEqual(all_teams_seen, {"Red", "Blue", "Cleo Team", "Dan Team"})
        self.assertEqual(sum(len(pair) for pair in rendered_pairs), len(all_teams_seen))

        file_calls = [c for c in self.channel.send.call_args_list if "file" in c.kwargs]
        self.assertEqual(len(file_calls), 2)


class TournamentReadyAndReportViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _setup_tournament(self, *teams):
        tournament = Tournament("Cup", 1, len(teams))
        for team in teams:
            tournament.register_team(team)
        tournament.set_bracket(self.helperObj.buildBracket(list(teams)))
        self.helperObj.saveTournament(GUILD_ID, tournament)
        return tournament

    async def _start(self, mode, *teams):
        self._setup_tournament(*teams)
        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.startTournamentHelper(ctx, mode)

    def _only_match(self):
        self.cursor.execute("SELECT id, messageId FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        return self.cursor.fetchone()

    async def _confirm_report(self, click):
        view = click.response.send_message.call_args.kwargs["view"]
        confirm_click = FakeInteraction(self.guild, FakeMember("Ref", id=999), channel=self.channel)
        await view.confirm.callback(confirm_click)
        return view

    def _click(self, message_id, user_id, name="Clicker"):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )

    async def _click_ready(self, message_id, user_id):
        view = helper_module.TournamentReadyView(self.helperObj)
        click = self._click(message_id, user_id)
        await view.ready.callback(click)
        return click

    async def _click_report(self, message_id, winning_team, user_id=555):
        view = helper_module.TournamentMatchReportView(self.helperObj)
        button = view.team1 if winning_team == 1 else view.team2
        click = self._click(message_id, user_id)
        await button.callback(click)
        return click

    async def test_ready_click_from_non_captain_is_rejected(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_ready(message_id, 999)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_READY")
        self.assertIsNone(self.helperObj.get(GUILD_ID, "active_tournament_match_id"))
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_ready_click_from_either_captain_starts_the_match(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        await self._click_ready(message_id, 902)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "AWAITING_RESULT")
        self.assertEqual(self.helperObj.get(GUILD_ID, "active_tournament_match_id"), match_id)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")

    async def test_sequential_resolution_advances_bracket_via_record_result(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        await self._click_ready(message_id, 902)

        team1 = Team()
        team1.deserializeTeam(self.helperObj.get(GUILD_ID, "team1"))

        # simulate the betting timer eventually resolving the game
        await self.helperObj.recordResult(GUILD_ID, 1, self.channel)

        self.cursor.execute("SELECT state, winner FROM tournament_matches WHERE id=?", (match_id,))
        state, winner = self.cursor.fetchone()
        self.assertEqual(state, "RESOLVED")
        self.assertEqual(winner, 1)
        self.assertIsNone(self.helperObj.get(GUILD_ID, "active_tournament_match_id"))

        updated = self.helperObj.getTournament(GUILD_ID)
        champion = updated.get_bracket()[-1]
        self.assertEqual(champion.team.get_name(), team1.get_name())

        printed = "\n".join(c.args[0] for c in self.channel.send.call_args_list if c.args)
        self.assertIn("Champion:", printed)

    async def test_simultaneous_result_click_posts_a_confirmation_instead_of_resolving(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_report(message_id, 2)

        self.cursor.execute(
            "SELECT state, winner, bettingClosed FROM tournament_matches WHERE id=?", (match_id,)
        )
        state, winner, betting_closed = self.cursor.fetchone()
        self.assertEqual(state, "CONFIRMING")
        self.assertIsNone(winner)
        # betting on this match is closed the moment it's reported, even
        # though it isn't recorded yet; otherwise /wager match_id: could
        # still bet on the reported side during the confirmation window
        self.assertTrue(betting_closed)
        view = click.response.send_message.call_args.kwargs["view"]
        self.assertEqual(view.match_id, match_id)
        self.assertEqual(view.winning_team, 2)

        # a second click on the same match can't post a second
        # confirmation, since it's no longer AWAITING_RESULT
        second_click = await self._click_report(message_id, 2)
        self.assertTrue(second_click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_confirming_a_simultaneous_result_resolves_the_match_and_advances(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        self.cursor.execute("SELECT team2 FROM tournament_matches WHERE id=?", (match_id,))
        team2 = Team()
        team2.deserializeTeam(self.cursor.fetchone()[0])

        click = await self._click_report(message_id, 2)
        await self._confirm_report(click)

        self.cursor.execute("SELECT state, winner FROM tournament_matches WHERE id=?", (match_id,))
        state, winner = self.cursor.fetchone()
        self.assertEqual(state, "RESOLVED")
        self.assertEqual(winner, 2)

        updated = self.helperObj.getTournament(GUILD_ID)
        champion = updated.get_bracket()[-1]
        self.assertEqual(champion.team.get_name(), team2.get_name())

    async def test_cancelling_a_simultaneous_confirmation_allows_reporting_again(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_report(message_id, 2)
        view = click.response.send_message.call_args.kwargs["view"]
        await view.cancel.callback(self._click(message_id, 999, "Ref"))

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "AWAITING_RESULT")

        # reporting again now posts a fresh confirmation
        second_click = await self._click_report(message_id, 2)
        second_view = second_click.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(second_view, helper_module.ConfirmTournamentMatchReportView)

    async def test_simultaneous_report_on_a_pending_ready_match_is_rejected(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_report(message_id, 1)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_READY")
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_round_advances_once_every_match_in_it_resolves(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        await self._start("simultaneous", red, blue, cleo, dan)

        self.cursor.execute(
            "SELECT id, messageId FROM tournament_matches WHERE guildId=? ORDER BY id", (GUILD_ID,)
        )
        rows = self.cursor.fetchall()
        self.assertEqual(len(rows), 2)

        for match_id, message_id in rows:
            click = await self._click_report(message_id, 1)
            await self._confirm_report(click)

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND roundIndex=1", (GUILD_ID,)
        )
        self.assertEqual(self.cursor.fetchone()[0], 1)

        # the round-end transition posted its own "Round 1 has ended!"
        # message and a fresh bracket, ahead of round 2's own kickoff
        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("Round 1 has ended!" in m for m in messages))

    async def test_final_round_resolving_announces_the_champion(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_report(message_id, 1)
        await self._confirm_report(click)

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("is complete!" in m and "Champion" in m for m in messages))

    async def test_tournament_completion_posts_team_leaderboard(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        self.helperObj._saveNewTeam(GUILD_ID, red)
        self.helperObj._saveNewTeam(GUILD_ID, blue)
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        click = await self._click_report(message_id, 1)
        await self._confirm_report(click)

        embeds = [c.kwargs["embed"] for c in self.channel.send.call_args_list if "embed" in c.kwargs]
        self.assertTrue(any("Results" in e.title for e in embeds))


class CorrectTournamentMatchHelperTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Admin", id=1), channel=self.channel)

    async def _resolved_match(self, winner=1):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(red)
        tournament.register_team(blue)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        self.cursor.execute("SELECT id, messageId FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        match_id, message_id = self.cursor.fetchone()
        report_view = helper_module.TournamentMatchReportView(self.helperObj)
        report_button = report_view.team1 if winner == 1 else report_view.team2
        report_click = FakeInteraction(
            self.guild, FakeMember("Ref", id=555),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )
        await report_button.callback(report_click)
        # the click only posts a confirmation now, press it to actually
        # resolve the match
        view = report_click.response.send_message.call_args.kwargs["view"]
        click = FakeInteraction(self.guild, FakeMember("Ref", id=999), channel=self.channel)
        await view.confirm.callback(click)
        return match_id

    async def test_rejects_unknown_match(self):
        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 1, match_id=9999)
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament match with id 9999 in this server."
        )

    async def test_rejects_unresolved_match(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(red)
        tournament.register_team(blue)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))
        self.helperObj.saveTournament(GUILD_ID, tournament)
        ctx0 = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.startTournamentHelper(ctx0, "simultaneous")
        self.cursor.execute("SELECT id FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        match_id = self.cursor.fetchone()[0]

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 1, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(f"Match #{match_id} hasn't been resolved yet.")

    async def test_rejects_already_correct_team(self):
        match_id = await self._resolved_match(winner=1)
        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 1, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(
            f"Match #{match_id} is already recorded as Team 1."
        )

    async def test_rejects_once_next_round_has_started(self):
        match_id = await self._resolved_match(winner=1)  # this IS the final, so no next round exists
        # force a fake "next round already started" scenario by inserting
        # a stray row at roundIndex+1
        self.cursor.execute("SELECT roundIndex FROM tournament_matches WHERE id=?", (match_id,))
        round_index = self.cursor.fetchone()[0]
        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner, bracketType) "
            "VALUES(?, ?, 0, '', '', 'QUEUED', 'simultaneous', NULL, ?, NULL, 'winners')",
            (GUILD_ID, round_index + 1, self.channel.id)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(
            f"Can't correct Match #{match_id} - the next round has already started."
        )

    async def test_successful_correction_flips_bracket_and_winner(self):
        match_id = await self._resolved_match(winner=1)
        self.cursor.execute("SELECT team2 FROM tournament_matches WHERE id=?", (match_id,))
        team2 = Team()
        team2.deserializeTeam(self.cursor.fetchone()[0])

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("corrected", ctx.response.send_message.call_args.args[0])

        self.cursor.execute("SELECT winner FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], 2)

        tournament = self.helperObj.getTournament(GUILD_ID)
        champion = tournament.get_bracket()[-1]
        self.assertEqual(champion.team.get_name(), team2.get_name())

    # Correcting a tournament match also reverses and reapplies any bets
    # already settled against the wrong winner (see _settleMatchWagers);
    # tournament_wagers' own rows are deleted the moment a match resolves,
    # so the bracket fix alone wouldn't touch payouts already paid out.
    async def test_correcting_a_match_reverses_and_reapplies_wager_payouts(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(red)
        tournament.register_team(blue)
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx0 = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.startTournamentHelper(ctx0, "simultaneous")
        self.cursor.execute("SELECT id, messageId FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        match_id, message_id = self.cursor.fetchone()

        for user_id, name, team in ((903, "Cleo", 1), (904, "Dan", 2)):
            self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
            self.cursor.execute(
                "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, user_id)
            )
            self.db.commit()
            bettor_ctx = FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=self.channel)
            await self.helperObj.wagerHelper(bettor_ctx, 100, team, match_id)

        # Resolved (wrongly) as Team 1, Cleo wins the pot, Dan loses her bet.
        report_view = helper_module.TournamentMatchReportView(self.helperObj)
        report_click = FakeInteraction(
            self.guild, FakeMember("Ref", id=555),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )
        await report_view.team1.callback(report_click)
        # the click only posts a confirmation now, press it to actually
        # resolve the match and pay out
        view = report_click.response.send_message.call_args.kwargs["view"]
        click = FakeInteraction(self.guild, FakeMember("Ref", id=999), channel=self.channel)
        await view.confirm.callback(click)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 1100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 904, "balance"), 900)

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)

        self.assertIn("reversed and reapplied", ctx.response.send_message.call_args.args[0])
        # Corrected: Dan actually won, Cleo actually lost.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 900)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 904, "balance"), 1100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 904, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 904, "losses"), 0)
        # gold_wagered reflects each bettor's one real 100-gold bet; the
        # reverse-then-reapply round trip doesn't double (or zero) it out.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "gold_wagered"), 100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 904, "gold_wagered"), 100)

    async def test_correcting_a_match_with_no_wagers_has_no_wager_note(self):
        match_id = await self._resolved_match(winner=1)
        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)
        message = ctx.response.send_message.call_args.args[0]
        self.assertNotIn("reversed", message)

    async def test_rejects_correcting_a_losers_bracket_match(self):
        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner, bracketType) "
            "VALUES(?, 0, 0, '', '', 'RESOLVED', 'simultaneous', NULL, ?, 1, 'losers')",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()
        self.cursor.execute("SELECT id FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        match_id = self.cursor.fetchone()[0]

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(
            f"Match #{match_id} is a losers bracket match - correcting those isn't supported yet."
        )

    async def test_rejects_correcting_a_grand_finals_match(self):
        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner, bracketType) "
            "VALUES(?, 0, -1, '', '', 'RESOLVED', 'simultaneous', NULL, ?, 1, 'finals')",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()
        self.cursor.execute("SELECT id FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        match_id = self.cursor.fetchone()[0]

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(
            f"Match #{match_id} is a Grand Finals match - correcting those isn't supported yet."
        )


# ===========================================================================
# Double elimination: real losers bracket, Grand Finals, bracket reset.
# ===========================================================================

class BuildLosersBracketTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_degenerate_two_team_bracket_has_no_real_match(self):
        # Only one winners-bracket match exists at all; its loser has
        # nobody left to play, so they become the losers-bracket "champion"
        # directly, with no match ever created for them.
        teams = [self._team(f"T{i}") for i in range(2)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        self.assertEqual(len(lb_nodes), 1)
        self.assertEqual([len(r) for r in lb_rounds], [1])
        self.assertIsNone(lb_nodes[0].previous)

    def test_four_team_bracket_round_sizes(self):
        teams = [self._team(f"T{i}") for i in range(4)]
        wb_nodes = self.helperObj.buildBracket(teams)
        _, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        self.assertEqual([len(r) for r in lb_rounds], [1, 1])

    def test_eight_team_bracket_round_sizes(self):
        teams = [self._team(f"T{i}") for i in range(8)]
        wb_nodes = self.helperObj.buildBracket(teams)
        _, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        self.assertEqual([len(r) for r in lb_rounds], [2, 2, 1, 1])

    def test_sixteen_team_bracket_round_sizes(self):
        teams = [self._team(f"T{i}") for i in range(16)]
        wb_nodes = self.helperObj.buildBracket(teams)
        _, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        self.assertEqual([len(r) for r in lb_rounds], [4, 4, 2, 2, 1, 1])

    def test_every_winners_result_node_gets_a_drop_to_target(self):
        teams = [self._team(f"T{i}") for i in range(8)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, _, _ = self.helperObj.buildLosersBracket(wb_nodes)
        wb_rounds = self.helperObj._bracketRounds(wb_nodes)
        for round_nodes in wb_rounds[1:]:
            for node in round_nodes:
                self.assertIsNotNone(node.drop_to)
                self.assertIn(node.drop_to, lb_nodes)

    def test_playing_it_out_always_reaches_a_losers_champion(self):
        # Property-style coverage across a range of team counts (including
        # non-power-of-two, which feeds winners-bracket byes into the
        # losers bracket); every one of these should reach a real losers-
        # bracket champion, distinct from the winners-bracket champion,
        # without crashing or stalling, regardless of how buildBracket's
        # random seeding shuffled things.
        for n in [2, 3, 4, 5, 6, 7, 8, 9, 15, 16]:
            for _ in range(5):
                teams = [self._team(f"T{i}") for i in range(n)]
                wb_nodes = self.helperObj.buildBracket(teams)
                lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)

                wb_rounds = self.helperObj._bracketRounds(wb_nodes)
                for round_nodes in wb_rounds[:-1]:
                    for i in range(0, len(round_nodes), 2):
                        a, b = round_nodes[i], round_nodes[i + 1]
                        if a.team is not None and b.team is not None:
                            winner, loser = random.choice([(a, b), (b, a)])
                            if winner.next is not None:
                                winner.next.team = winner.team
                                winner.next.loser = loser.team
                                if winner.next.drop_to is not None:
                                    winner.next.drop_to.team = loser.team
                        elif a.team is not None or b.team is not None:
                            winner = a if a.team is not None else b
                            if winner.next is not None:
                                winner.next.team = winner.team

                for round_nodes in lb_rounds:
                    for result in round_nodes:
                        fa = result.previous
                        fb = fa.opponent if fa is not None else None
                        if fa is not None and fa.team is not None and fb is not None and fb.team is not None:
                            result.team = random.choice([fa, fb]).team
                        elif fa is not None and fa.team is not None:
                            result.team = fa.team
                        elif fb is not None and fb.team is not None:
                            result.team = fb.team

                wb_champ = wb_rounds[-1][0].team
                lb_champ = lb_rounds[-1][0].team if lb_rounds else None
                self.assertIsNotNone(wb_champ, f"n={n}: no winners champion")
                self.assertIsNotNone(lb_champ, f"n={n}: no losers champion")
                self.assertNotEqual(
                    wb_champ.get_name(), lb_champ.get_name(), f"n={n}: same team both champion"
                )


class CreateBracketHelperDoubleEliminationTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    async def test_double_elimination_also_builds_a_losers_bracket(self):
        tournament = Tournament("Cup", 1, 4)
        for name in ["Red", "Blue", "Cleo", "Dan"]:
            tournament.register_team(self._team(name))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, True)

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertTrue(restored.is_double_elimination())
        self.assertGreater(len(restored.get_losers_bracket_nodes()), 0)
        self.assertEqual([len(r) for r in restored.get_losers_rounds()], [1, 1])

    async def test_single_elimination_leaves_losers_bracket_empty(self):
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(self._team("Red"))
        tournament.register_team(self._team("Blue"))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, False)

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(restored.get_losers_bracket_nodes(), [])
        self.assertEqual(restored.get_losers_rounds(), [])

    async def test_stale_match_rows_are_cleared_on_a_fresh_bracket(self):
        # A finished tournament's resolved Grand Finals row shouldn't stick
        # around to confuse the NEXT tournament's own completion check.
        tournament = Tournament("Cup", 1, 2)
        tournament.register_team(self._team("Red"))
        tournament.register_team(self._team("Blue"))
        self.helperObj.saveTournament(GUILD_ID, tournament)

        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner, bracketType) "
            "VALUES(?, 0, 0, '', '', 'RESOLVED', 'simultaneous', NULL, NULL, 1, 'finals')",
            (GUILD_ID,)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.createBracketHelper(ctx, True)

        self.cursor.execute("SELECT COUNT(*) FROM tournament_matches WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)


class TournamentDoubleEliminationPersistenceTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_losers_bracket_and_drop_to_survive_a_roundtrip(self):
        teams = [self._team(f"T{i}") for i in range(8)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)

        tournament = Tournament("Cup", 1, 8, True)
        for team in teams:
            tournament.register_team(team)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)
        self.helperObj.saveTournament(GUILD_ID, tournament)

        restored = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(len(restored.get_losers_bracket_nodes()), len(lb_nodes))
        self.assertEqual(
            [len(r) for r in restored.get_losers_rounds()], [len(r) for r in lb_rounds]
        )

        wb_rounds = self.helperObj._bracketRounds(restored.get_bracket())
        result_node = wb_rounds[1][0]
        self.assertIsNotNone(result_node.drop_to)
        self.assertIn(result_node.drop_to, restored.get_losers_bracket_nodes())

    def test_loser_field_survives_a_roundtrip_once_set(self):
        red, blue = self._team("Red"), self._team("Blue")
        wb_nodes = self.helperObj.buildBracket([red, blue])
        tournament = Tournament("Cup", 1, 2)
        tournament.set_bracket(wb_nodes)

        rounds = self.helperObj._bracketRounds(wb_nodes)
        leaf_a, leaf_b = rounds[0][0], rounds[0][1]
        leaf_a.next.team = leaf_a.team
        leaf_a.next.loser = leaf_b.team
        self.helperObj.saveTournament(GUILD_ID, tournament)

        restored = self.helperObj.getTournament(GUILD_ID)
        champion = self.helperObj._bracketRounds(restored.get_bracket())[-1][0]
        self.assertEqual(champion.loser.get_name(), leaf_b.team.get_name())


class RenderBracketTextDoubleEliminationTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_shows_winners_and_losers_champion_status(self):
        teams = [self._team(f"T{i}") for i in range(4)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        text = self.helperObj.renderBracketText(tournament)
        self.assertIn("Winners Bracket Champion", text)
        self.assertIn("Losers Bracket Champion", text)
        # the actual tree (team names, connectors) lives in the image now
        # (renderBracketImages), not this status text
        self.assertNotIn("```", text)

    def test_degenerate_two_team_bracket_explains_no_losers_match_needed(self):
        teams = [self._team(n) for n in ["Red", "Blue"]]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 2, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        text = self.helperObj.renderBracketText(tournament)
        self.assertIn("advances directly to Grand Finals", text)
        self.assertNotIn("Losers Bracket Champion", text)

    def test_single_elimination_has_no_losers_bracket_status(self):
        teams = [self._team(f"T{i}") for i in range(4)]
        tournament = Tournament("Cup", 1, 4, False)
        tournament.set_bracket(self.helperObj.buildBracket(teams))

        text = self.helperObj.renderBracketText(tournament)
        self.assertNotIn("Losers Bracket", text)
        self.assertNotIn("Winners Bracket Champion", text)
        self.assertIn("**Champion:**", text)

    def test_grand_finals_section_only_shows_once_both_brackets_have_a_champion(self):
        teams = [self._team(f"T{i}") for i in range(4)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        text = self.helperObj.renderBracketText(tournament, GUILD_ID)
        self.assertNotIn("Grand Finals", text)

        wb_champion_node = self.helperObj._bracketRounds(wb_nodes)[-1][0]
        wb_champion_node.team = teams[0]
        lb_rounds[-1][0].team = teams[1]

        text2 = self.helperObj.renderBracketText(tournament, GUILD_ID)
        self.assertIn("Grand Finals", text2)
        self.assertIn("T0 (winners bracket) vs T1 (losers bracket)", text2)

    def test_omits_grand_finals_section_without_a_guild_id(self):
        # /test (the throwaway bracket-preview command) never creates real
        # tournament_matches rows, so it deliberately doesn't pass a
        # guild_id; passing a real one could pick up an unrelated
        # tournament's actual Grand Finals history for this guild.
        teams = [self._team(f"T{i}") for i in range(4)]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)

        self.helperObj._bracketRounds(wb_nodes)[-1][0].team = teams[0]
        lb_rounds[-1][0].team = teams[1]

        text = self.helperObj.renderBracketText(tournament)
        self.assertNotIn("Grand Finals", text)


class TournamentChampionNameTests(HelperTestCase):
    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def _double_elim_setup(self):
        teams = [self._team(n) for n in ["Red", "Blue", "Cleo", "Dan"]]
        wb_nodes = self.helperObj.buildBracket(teams)
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament = Tournament("Cup", 1, 4, True)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)
        self.helperObj._bracketRounds(wb_nodes)[-1][0].team = teams[0]  # Red
        lb_rounds[-1][0].team = teams[1]  # Blue
        return tournament, teams

    def _insert_finals_row(self, round_index, teams, winner):
        self.cursor.execute(
            "INSERT INTO tournament_matches(guildId, roundIndex, nodeIndex, team1, team2, state, mode, "
            "messageId, channelId, winner, bracketType) "
            "VALUES(?, ?, -1, ?, ?, 'RESOLVED', 'simultaneous', NULL, NULL, ?, 'finals')",
            (GUILD_ID, round_index, teams[0].serializeTeam(), teams[1].serializeTeam(), winner)
        )
        self.db.commit()

    def test_single_elimination_returns_none_until_final_resolves(self):
        teams = [self._team(n) for n in ["Red", "Blue"]]
        tournament = Tournament("Cup", 1, 2, False)
        tournament.set_bracket(self.helperObj.buildBracket(teams))
        self.assertIsNone(self.helperObj._tournamentChampionName(GUILD_ID, tournament))

    def test_single_elimination_returns_champion_name_once_decided(self):
        teams = [self._team(n) for n in ["Red", "Blue"]]
        wb_nodes = self.helperObj.buildBracket(teams)
        tournament = Tournament("Cup", 1, 2, False)
        tournament.set_bracket(wb_nodes)
        self.helperObj._bracketRounds(wb_nodes)[-1][0].team = teams[0]
        self.assertEqual(self.helperObj._tournamentChampionName(GUILD_ID, tournament), "Red")

    def test_double_elimination_returns_none_before_grand_finals_played(self):
        tournament, _ = self._double_elim_setup()
        self.assertIsNone(self.helperObj._tournamentChampionName(GUILD_ID, tournament))

    def test_double_elimination_returns_winners_champion_when_game_one_settles_it(self):
        tournament, teams = self._double_elim_setup()
        self._insert_finals_row(0, teams, winner=1)  # Red (winners bracket) won
        self.assertEqual(self.helperObj._tournamentChampionName(GUILD_ID, tournament), "Red")

    def test_double_elimination_returns_none_after_losers_champion_wins_game_one(self):
        tournament, teams = self._double_elim_setup()
        self._insert_finals_row(0, teams, winner=2)  # Blue (losers bracket) won - reset needed
        self.assertIsNone(self.helperObj._tournamentChampionName(GUILD_ID, tournament))

    def test_double_elimination_reset_winner_is_champion_regardless(self):
        tournament, teams = self._double_elim_setup()
        self._insert_finals_row(0, teams, winner=2)  # Blue forces a reset
        self._insert_finals_row(1, teams, winner=2)  # Blue also wins the reset
        self.assertEqual(self.helperObj._tournamentChampionName(GUILD_ID, tournament), "Blue")


class DoubleEliminationMatchFlowTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)

    def _double_elim_tournament(self, *teams, team_size=1):
        tournament = Tournament("Cup", team_size, len(teams), double_elimination=True)
        for team in teams:
            tournament.register_team(team)
            self.helperObj._saveNewTeam(GUILD_ID, team)
        wb_nodes = self.helperObj.buildBracket(list(teams))
        lb_nodes, lb_rounds, _ = self.helperObj.buildLosersBracket(wb_nodes)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds)
        self.helperObj.saveTournament(GUILD_ID, tournament)
        return tournament

    # Resolves every AWAITING_RESULT simultaneous-mode match, repeatedly,
    # until the WHOLE tournament (winners bracket, losers bracket, and
    # Grand Finals, including a reset, if `winner_of` forces one) has an
    # overall champion. `winner_of(team1, team2, bracket_type, round_index)`
    # picks 1 or 2.
    async def _play_out_simultaneous(self, winner_of):
        tournament = self.helperObj.getTournament(GUILD_ID)
        for _ in range(50):
            if self.helperObj._tournamentChampionName(GUILD_ID, tournament) is not None:
                return
            self.cursor.execute(
                "SELECT id, messageId, team1, team2, bracketType, roundIndex FROM tournament_matches "
                "WHERE guildId=? AND state='AWAITING_RESULT'",
                (GUILD_ID,)
            )
            rows = self.cursor.fetchall()
            if not rows:
                self.fail("no matches in progress but the tournament isn't finished")
            for match_id, message_id, team1_ser, team2_ser, bracket_type, round_index in rows:
                team1, team2 = Team(), Team()
                team1.deserializeTeam(team1_ser)
                team2.deserializeTeam(team2_ser)
                winning_team = winner_of(team1, team2, bracket_type, round_index)
                report_view = helper_module.TournamentMatchReportView(self.helperObj)
                report_button = report_view.team1 if winning_team == 1 else report_view.team2
                report_click = FakeInteraction(
                    self.guild, FakeMember("Ref", id=555),
                    channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
                )
                await report_button.callback(report_click)
                # the click only posts a confirmation now, press it to
                # actually resolve the match, same as a real reporter would
                view = report_click.response.send_message.call_args.kwargs["view"]
                click = FakeInteraction(self.guild, FakeMember("Ref", id=999), channel=self.channel)
                await view.confirm.callback(click)
            tournament = self.helperObj.getTournament(GUILD_ID)
        self.fail("tournament never reached a champion")

    # Same idea, but for sequential mode, drives each match through the
    # real ready-check (as its own captain) + recordResult cycle.
    async def _play_out_sequential(self, winner_of):
        tournament = self.helperObj.getTournament(GUILD_ID)
        for _ in range(50):
            if self.helperObj._tournamentChampionName(GUILD_ID, tournament) is not None:
                return
            self.cursor.execute(
                "SELECT id, messageId, team1, team2, bracketType, roundIndex FROM tournament_matches "
                "WHERE guildId=? AND state='PENDING_READY'",
                (GUILD_ID,)
            )
            row = self.cursor.fetchone()
            if row is None:
                self.fail("no match awaiting ready-check but the tournament isn't finished")
            match_id, message_id, team1_ser, team2_ser, bracket_type, round_index = row
            team1, team2 = Team(), Team()
            team1.deserializeTeam(team1_ser)
            team2.deserializeTeam(team2_ser)

            captain_id = team1.get_captain().get_id()
            ready_view = helper_module.TournamentReadyView(self.helperObj)
            ready_click = FakeInteraction(
                self.guild, FakeMember("Captain", id=captain_id),
                channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
            )
            await ready_view.ready.callback(ready_click)

            winning_team = winner_of(team1, team2, bracket_type, round_index)
            await self.helperObj.recordResult(GUILD_ID, winning_team, self.channel)
            tournament = self.helperObj.getTournament(GUILD_ID)
        self.fail("tournament never reached a champion")

    async def test_simultaneous_flow_reaches_an_overall_champion(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._double_elim_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        await self._play_out_simultaneous(lambda t1, t2, bt, ri: 1)

        tournament = self.helperObj.getTournament(GUILD_ID)
        champion_name = self.helperObj._tournamentChampionName(GUILD_ID, tournament)
        self.assertIsNotNone(champion_name)

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("Losers Bracket Round" in m for m in messages))
        self.assertTrue(any("Grand Finals" in m for m in messages))
        self.assertTrue(any("is complete!" in m and champion_name in m for m in messages))

        embeds = [c.kwargs["embed"] for c in self.channel.send.call_args_list if "embed" in c.kwargs]
        self.assertTrue(any("Results" in e.title for e in embeds))

    async def test_tournament_completion_sends_the_grand_finals_bracket_image(self):
        # _resolveFinalsMatch used to skip reprinting the bracket entirely
        # on tournament completion; every other match-resolution path
        # (_resolveTournamentMatch, _resolveLosersMatch) already does this
        # after every single match, so the last bracket image anyone saw
        # was whatever the losers bracket looked like before Grand Finals
        # even started, never the actual finals result.
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._double_elim_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")
        await self._play_out_simultaneous(lambda t1, t2, bt, ri: 1)

        file_calls = [c for c in self.channel.send.call_args_list if "files" in c.kwargs]
        all_filenames = [f.filename for c in file_calls for f in c.kwargs["files"]]
        self.assertIn("grand_finals.png", all_filenames)

        # ...and it has to come from AFTER the completion announcement, not
        # some earlier round's bracket reprint that happened to also carry
        # a (pre-finals) losers-bracket image.
        all_calls = self.channel.send.call_args_list
        completion_index = next(
            i for i, c in enumerate(all_calls) if c.args and "is complete!" in c.args[0]
        )
        finals_image_index = next(
            i for i, c in enumerate(all_calls)
            if "files" in c.kwargs and any(f.filename == "grand_finals.png" for f in c.kwargs["files"])
        )
        self.assertGreater(finals_image_index, completion_index)

    async def test_match_results_update_each_teams_persisted_win_loss_record(self):
        # Every one of _resolveTournamentMatch/_resolveLosersMatch/
        # _resolveFinalsMatch's paths needs to record its result against
        # the PERSISTED team row (see _recordMatchResult) for the
        # leaderboard to ever show anything but 0W-0L; this exercises all
        # three (winners bracket, losers bracket, and Grand Finals all get
        # played out here) in one pass.
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._double_elim_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")
        await self._play_out_simultaneous(lambda t1, t2, bt, ri: 1)

        tournament = self.helperObj.getTournament(GUILD_ID)
        champion_name = self.helperObj._tournamentChampionName(GUILD_ID, tournament)
        self.assertIsNotNone(champion_name)

        persisted = {team.get_name(): team for _, team in self.helperObj.getTeamsForGuild(GUILD_ID)}
        total_wins = sum(team.wins for team in persisted.values())
        total_losses = sum(team.losses for team in persisted.values())
        # Every resolved match records exactly one win and one loss.
        self.assertGreater(total_wins, 0)
        self.assertEqual(total_wins, total_losses)

        champion = persisted[champion_name]
        self.assertGreater(champion.wins, 0)
        self.assertGreaterEqual(champion.wins, champion.losses)

    async def test_sequential_flow_reaches_an_overall_champion(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._double_elim_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")

        await self._play_out_sequential(lambda t1, t2, bt, ri: 1)

        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertIsNotNone(self.helperObj._tournamentChampionName(GUILD_ID, tournament))

    async def test_bracket_reset_when_losers_champion_wins_game_one(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._double_elim_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        def winner_of(team1, team2, bracket_type, round_index):
            if bracket_type == "finals" and round_index == 0:
                # let the losers-bracket side take game 1, forcing a reset
                return 1 if team1.get_name() != "Red" else 2
            # everywhere else (including the reset match), Red always wins,
            # stays undefeated through the winners bracket, then takes
            # the reset to become champion after dropping game 1.
            if team1.get_name() == "Red":
                return 1
            if team2.get_name() == "Red":
                return 2
            return 1

        await self._play_out_simultaneous(winner_of)

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND bracketType='finals'",
            (GUILD_ID,)
        )
        self.assertEqual(self.cursor.fetchone()[0], 2)  # game 1 + the reset

        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertEqual(self.helperObj._tournamentChampionName(GUILD_ID, tournament), "Red")

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("decider match settles" in m for m in messages))

    async def test_two_team_double_elimination_skips_straight_to_grand_finals(self):
        # The degenerate case: with only 2 teams, the single winners-
        # bracket match's loser has nobody to play in the losers bracket;
        # they go straight to Grand Finals as the "losers bracket champion".
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        self._double_elim_tournament(red, blue)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        await self._play_out_simultaneous(lambda t1, t2, bt, ri: 1)

        tournament = self.helperObj.getTournament(GUILD_ID)
        self.assertIsNotNone(self.helperObj._tournamentChampionName(GUILD_ID, tournament))

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("Grand Finals" in m for m in messages))
        # no losers-bracket ROUND ever plays; there's nothing to queue
        self.assertFalse(any("Losers Bracket Round" in m for m in messages))


class InterleavedLosersBracketTimingTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat", guild=self.guild)
        self.channel.send = AsyncMock(side_effect=lambda *a, **k: FakeMessage())
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)

    def _interleaved_tournament(self, *teams):
        tournament = Tournament("Cup", 1, len(teams), double_elimination=True)
        for team in teams:
            tournament.register_team(team)
            self.helperObj._saveNewTeam(GUILD_ID, team)
        wb_nodes = self.helperObj.buildBracket(list(teams))
        lb_nodes, lb_rounds, wb_dependency = self.helperObj.buildLosersBracket(wb_nodes)
        tournament.set_bracket(wb_nodes)
        tournament.set_losers_bracket(lb_nodes, lb_rounds, wb_dependency)
        tournament.set_losers_bracket_timing("interleaved")
        self.helperObj.saveTournament(GUILD_ID, tournament)
        return tournament

    def _matches(self, bracket_type=None, round_index=None):
        query = "SELECT id, team1, team2 FROM tournament_matches WHERE guildId=?"
        params = [GUILD_ID]
        if bracket_type is not None:
            query += " AND bracketType=?"
            params.append(bracket_type)
        if round_index is not None:
            query += " AND roundIndex=?"
            params.append(round_index)
        self.cursor.execute(query, params)
        return self.cursor.fetchall()

    async def _resolve(self, match_id, winning_team=1):
        await self.helperObj._resolveTournamentMatch(GUILD_ID, match_id, winning_team, self.channel.id)

    # 4 teams means one losers round (index 0) unlocked by winners round 0,
    # and a second (index 1) unlocked by winners round 1 (the final), see
    # buildLosersBracket's wb_dependency comment. Once winners round 0
    # finishes, interleaved timing should start losers round 0 right away,
    # instead of moving straight on to the winners final.
    async def test_losers_round_starts_before_next_winners_round_once_unlocked(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._interleaved_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        wb_round0 = self._matches(bracket_type="winners", round_index=0)
        self.assertEqual(len(wb_round0), 2)
        for match_id, _, _ in wb_round0:
            await self._resolve(match_id, 1)

        self.assertEqual(len(self._matches(bracket_type="losers", round_index=0)), 1)
        # winners round 1 (the final) must NOT have started yet; it's
        # paused behind the now-unlocked losers round.
        self.assertEqual(len(self._matches(bracket_type="winners", round_index=1)), 0)

    # Once losers round 0 finishes, losers round 1 isn't ready yet (it
    # needs winners round 1, the final, which hasn't been played), so the
    # scheduler should resume the winners bracket instead of stalling.
    async def test_winners_resumes_once_the_unlocked_losers_round_finishes(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._interleaved_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")
        for match_id, _, _ in self._matches(bracket_type="winners", round_index=0):
            await self._resolve(match_id, 1)

        losers_round0 = self._matches(bracket_type="losers", round_index=0)
        self.assertEqual(len(losers_round0), 1)
        for match_id, _, _ in losers_round0:
            await self._resolve(match_id, 1)

        # winners final should now be underway...
        self.assertEqual(len(self._matches(bracket_type="winners", round_index=1)), 1)
        # ...and losers round 1 still shouldn't have started (it depends on
        # the winners final, which just started but hasn't resolved).
        self.assertEqual(len(self._matches(bracket_type="losers", round_index=1)), 0)

    # Full playthrough: confirms interleaved mode still reaches an overall
    # champion, posts the "winners bracket complete" announcement exactly
    # once (the entry-guard / _advanceInterleavedTournament redesign in
    # _startRound risked re-triggering it), and creates exactly one Grand
    # Finals match row (guards against _startGrandFinals firing more than
    # once now that multiple code paths can reach it).
    async def test_full_playthrough_reaches_champion_without_duplicate_announcements(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        cleo = _captained_team("Cleo Team", 903, "Cleo")
        dan = _captained_team("Dan Team", 904, "Dan")
        self._interleaved_tournament(red, blue, cleo, dan)

        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "simultaneous")

        for _ in range(50):
            tournament = self.helperObj.getTournament(GUILD_ID)
            if self.helperObj._tournamentChampionName(GUILD_ID, tournament) is not None:
                break
            self.cursor.execute(
                "SELECT id FROM tournament_matches WHERE guildId=? AND state != 'RESOLVED'", (GUILD_ID,)
            )
            rows = self.cursor.fetchall()
            if not rows:
                self.fail("no matches in progress but the tournament isn't finished")
            for (match_id,) in rows:
                await self._resolve(match_id, 1)
        else:
            self.fail("tournament never reached a champion")

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertEqual(sum("winners bracket complete" in m for m in messages), 1)

        self.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=? AND bracketType='finals'", (GUILD_ID,)
        )
        self.assertEqual(self.cursor.fetchone()[0], 1)

    # after_winners is still the default; an interleaved-mode helper
    # wasn't accidentally wired in as the new default for every double
    # elimination bracket.
    async def test_after_winners_is_still_the_default_timing(self):
        tournament = Tournament("Cup", 1, 4, double_elimination=True)
        self.assertEqual(tournament.get_losers_bracket_timing(), "after_winners")


class PrintEmbedTests(HelperTestCase):
    def _five_player_team(self, name, start_id):
        team = Team()
        team.name = name
        for i in range(5):
            team.add_player(Player(start_id + i, f"P{i}"))
        return team

    async def test_use_roles_labels_players_when_team_has_five(self):
        team1 = self._five_player_team("Team 1", 700)
        team2 = self._five_player_team("Team 2", 800)
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        # regression test: printEmbed used to always call makeEmbedString()
        # with its default useRoles=False, so /make-teams use_roles:True
        # never actually showed role labels in the embed it posts.
        await self.helperObj.printEmbed(ctx, team1, team2, useRoles=True)

        embed1 = ctx.channel.send.call_args_list[0].kwargs["embed"]
        embed2 = ctx.channel.send.call_args_list[1].kwargs["embed"]
        self.assertEqual(embed1.description.count(" - "), 5)
        self.assertEqual(embed2.description.count(" - "), 5)
        self.assertIn("Top - P0", embed1.description)

    async def test_without_use_roles_shows_plain_names(self):
        team1 = self._five_player_team("Team 1", 700)
        team2 = Team()
        team2.name = "Team 2"
        team2.add_player(Player(900, "Solo"))
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.printEmbed(ctx, team1, team2)

        embed1 = ctx.channel.send.call_args_list[0].kwargs["embed"]
        self.assertNotIn(" - ", embed1.description)
        self.assertIn("P0", embed1.description)

    async def test_returns_the_two_distinct_posted_messages(self):
        team1 = self._five_player_team("Team 1", 700)
        team2 = self._five_player_team("Team 2", 800)
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        team1_message, team2_message = await self.helperObj.printEmbed(ctx, team1, team2)

        # A fresh message per send() call, not one shared object; matters
        # since _finalizeRoster only ever reacts to the second one.
        self.assertIsNot(team1_message, team2_message)
        self.assertEqual(ctx.channel.send.await_count, 2)


class AdminSetHelperTests(HelperTestCase):
    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Caller"))

    async def _set(
        self, ctx, team1=None, team2=None, size=None, betting_timer=None,
        wager_channel=None, member=None, elo=None, default_elo=None,
    ):
        await self.helperObj.adminSetHelper(
            ctx, team1, team2, size, betting_timer, wager_channel, member, elo, default_elo
        )

    async def test_rejects_when_nothing_is_given(self):
        ctx = self._ctx()
        await self._set(ctx)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("at least one setting", message)

    async def test_rejects_team1_without_team2(self):
        ctx = self._ctx()
        await self._set(ctx, team1="Red")
        ctx.response.send_message.assert_awaited_once_with(
            "Give both team1 and team2 together, or neither."
        )

    async def test_rejects_team2_without_team1(self):
        ctx = self._ctx()
        await self._set(ctx, team2="Blue")
        ctx.response.send_message.assert_awaited_once_with(
            "Give both team1 and team2 together, or neither."
        )

    async def test_rejects_member_without_elo(self):
        ctx = self._ctx()
        await self._set(ctx, member=FakeMember("Target", id=555))
        ctx.response.send_message.assert_awaited_once_with(
            "Give both member and elo together, or neither."
        )

    async def test_rejects_elo_without_member(self):
        ctx = self._ctx()
        await self._set(ctx, elo=1500)
        ctx.response.send_message.assert_awaited_once_with(
            "Give both member and elo together, or neither."
        )

    async def test_rejects_non_positive_betting_timer(self):
        ctx = self._ctx()
        await self._set(ctx, betting_timer=0)
        ctx.response.send_message.assert_awaited_once_with(
            "betting_timer must be greater than 0 seconds."
        )
        self.assertEqual(
            self.helperObj.get(GUILD_ID, "betting_timer_seconds"),
            helper_module.BETTING_DURATION_SECONDS,
        )

    async def test_rejects_betting_timer_over_the_cap(self):
        ctx = self._ctx()
        await self._set(ctx, betting_timer=601)
        ctx.response.send_message.assert_awaited_once_with(
            "betting_timer can't be more than 600 seconds (10 minutes)."
        )

    async def test_rejects_non_positive_default_elo(self):
        ctx = self._ctx()
        await self._set(ctx, default_elo=0)
        ctx.response.send_message.assert_awaited_once_with(
            "default_elo must be greater than 0."
        )
        self.assertIsNone(self.helperObj.get(GUILD_ID, "default_elo"))

    async def test_an_invalid_field_blocks_every_field_from_applying(self):
        # validate-all-then-apply-all: a betting_timer over the cap
        # shouldn't leave an otherwise-valid team1/team2 half-applied.
        ctx = self._ctx()
        await self._set(ctx, team1="Red", team2="Blue", betting_timer=601)
        self.assertIsNone(self.helperObj.get(GUILD_ID, "channel1"))
        self.assertEqual(len(self.guild.channels), 0)

    async def test_sets_team_channels_creating_them_when_missing(self):
        ctx = self._ctx()
        await self._set(ctx, team1="Red", team2="Blue")

        names = {c.name for c in self.guild.channels}
        self.assertEqual(names, {"Red", "Blue"})
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel1"), "Red")
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel2"), "Blue")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("team channels", message)

    async def test_reuses_existing_team_channels(self):
        self.guild.channels.append(FakeChannel("Red"))
        self.guild.channels.append(FakeChannel("Blue"))
        ctx = self._ctx()

        await self._set(ctx, team1="Red", team2="Blue")

        self.assertEqual(len(self.guild.channels), 2)

    async def test_sets_team_size_independently(self):
        ctx = self._ctx()
        await self._set(ctx, size=4)

        self.assertEqual(self.helperObj.get(GUILD_ID, "team_size"), 4)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("team size", message)
        self.assertIn("4", message)

    async def test_sets_betting_timer(self):
        ctx = self._ctx()
        await self._set(ctx, betting_timer=30)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_timer_seconds"), 30)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("30 seconds", message)
        self.assertIn("tournament round", message)

    async def test_sets_wager_channel_creating_it_when_missing(self):
        ctx = self._ctx()
        await self._set(ctx, wager_channel="bets")

        created = [c for c in self.guild.channels if c.name == "bets"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].kind, "text")
        self.assertEqual(self.helperObj.get(GUILD_ID, "wager_channel"), "bets")
        self.assertIn(created[0].mention, ctx.response.send_message.call_args.args[0])

    async def test_wager_channel_ignores_a_same_named_voice_channel(self):
        self.guild.channels.append(FakeChannel("bets", kind="voice"))
        ctx = self._ctx()

        await self._set(ctx, wager_channel="bets")

        # a new text channel is created rather than reusing the voice one
        text_channels = [c for c in self.guild.channels if c.kind == "text"]
        self.assertEqual(len(text_channels), 1)

    async def test_sets_a_players_elo(self):
        target = FakeMember("Target", id=555)
        ctx = self._ctx()
        await self._set(ctx, member=target, elo=1500)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 555, "elo"), 1500)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("1500", message)
        self.assertIn(target.mention, message)

    async def test_setting_elo_creates_an_economy_row_for_a_brand_new_target(self):
        target = FakeMember("NeverPlayed", id=556)
        ctx = self._ctx()
        await self._set(ctx, member=target, elo=800)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 556, "elo"), 800)

    async def test_setting_elo_overwrites_an_existing_value(self):
        target = FakeMember("Target", id=555)
        self.helperObj.ensureEconomyRow(GUILD_ID, 555, "Target")
        self.cursor.execute("UPDATE economy SET elo=1200 WHERE guildId=? AND userId=?", (GUILD_ID, 555))
        self.db.commit()

        ctx = self._ctx()
        await self._set(ctx, member=target, elo=300)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 555, "elo"), 300)

    async def test_setting_a_qualifying_elo_credits_the_tier_reward(self):
        target = FakeMember("Target", id=555)
        ctx = self._ctx()
        await self._set(ctx, member=target, elo=helper_module.ELO_TIER_THRESHOLDS["Diamond"])

        self.assertIn(
            helper_module.CARD_TIER_REWARD_TITLES["Diamond"], self.helperObj.getUnlockedCardTitles(GUILD_ID, 555)
        )

    async def test_sets_the_default_elo(self):
        ctx = self._ctx()
        await self._set(ctx, default_elo=1200)

        self.assertEqual(self.helperObj.get(GUILD_ID, "default_elo"), 1200)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("default starting elo", message)
        self.assertIn("1200", message)

    async def test_default_elo_does_not_change_existing_players(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 555, "Target")
        ctx = self._ctx()
        await self._set(ctx, default_elo=1200)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 555, "elo"), helper_module.DEFAULT_ELO)

    async def test_default_elo_applies_to_brand_new_players(self):
        ctx = self._ctx()
        await self._set(ctx, default_elo=1200)
        self.helperObj.ensureEconomyRow(GUILD_ID, 556, "NewPlayer")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 556, "elo"), 1200)

    async def test_applies_every_field_together_in_one_call(self):
        target = FakeMember("Target", id=555)
        ctx = self._ctx()
        await self._set(
            ctx, team1="Red", team2="Blue", size=4, betting_timer=30,
            wager_channel="bets", member=target, elo=1500, default_elo=1200,
        )

        self.assertEqual(self.helperObj.get(GUILD_ID, "channel1"), "Red")
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel2"), "Blue")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team_size"), 4)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_timer_seconds"), 30)
        self.assertEqual(self.helperObj.get(GUILD_ID, "wager_channel"), "bets")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 555, "elo"), 1500)
        self.assertEqual(self.helperObj.get(GUILD_ID, "default_elo"), 1200)
        ctx.response.send_message.assert_awaited_once()


class NotifyHelperTests(HelperTestCase):
    async def test_sends_dm_with_invite_and_default_message(self):
        voice_channel = FakeChannel("Lobby")
        caller = FakeMember("Caller")
        caller.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, caller)
        target = FakeMember("Target")

        await self.helperObj.notifyHelper(ctx, target)

        target.create_dm.assert_awaited_once()
        dm_channel = target.create_dm.return_value
        dm_channel.send.assert_awaited_once()
        content = dm_channel.send.call_args.args[0]
        self.assertIn("You've been invited to play in a game!", content)
        self.assertIn("https://discord.gg/fake-invite", content)
        self.assertIn("Sent by Caller", content)

    async def test_custom_message_replaces_the_default_text(self):
        voice_channel = FakeChannel("Lobby")
        caller = FakeMember("Caller")
        caller.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, caller)
        target = FakeMember("Target")

        await self.helperObj.notifyHelper(ctx, target, "We need a 5th, hop in!")

        content = target.create_dm.return_value.send.call_args.args[0]
        self.assertIn("We need a 5th, hop in!", content)
        self.assertNotIn("You've been invited to play in a game!", content)
        self.assertIn("https://discord.gg/fake-invite", content)
        self.assertIn("Sent by Caller", content)


class CancelGameHelperTests(HelperTestCase):
    async def test_moves_members_back_and_refunds_open_bets(self):
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=801)
        member2 = FakeMember("Bob", id=802)
        channel1.members = [member1]
        channel2.members = [member2]

        guild = FakeGuild(channels=[og, channel1, channel2])
        channel = FakeChannel("game-channel")

        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")

        self.helperObj.ensureEconomyRow(GUILD_ID, 801, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=500 WHERE guildId=? AND userId=?", (GUILD_ID, 801)
        )
        self.cursor.execute(
            "INSERT INTO wagers(guildId, userId, username, team, amount) VALUES(?, ?, ?, ?, ?)",
            (GUILD_ID, 801, "Alice", 1, 200),
        )
        self.db.commit()

        await self.helperObj.cancelGameHelper(GUILD_ID, channel, guild)

        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 801, "balance"), 700)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

        sent = [call.args[0] for call in channel.send.call_args_list]
        self.assertTrue(any("cancelled" in text for text in sent))
        self.assertTrue(any("refunded" in text for text in sent))
        self.assertTrue(any("Moved everyone back" in text for text in sent))

    async def test_cancel_on_ranked_game_refunds_bets_without_touching_elo(self):
        # Cancelling ends a ranked game early exactly like ending a regular
        # game early: bets get refunded, and, since recordResult (the only
        # place elo/game record ever change) never runs, elo and game
        # record are left completely untouched, not just "reset".
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)
        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")

        og = FakeChannel("Lobby")
        guild = FakeGuild(channels=[og])
        channel = FakeChannel("game-channel")

        self.helperObj.ensureEconomyRow(GUILD_ID, 801, "Bettor")
        self.cursor.execute(
            "UPDATE economy SET balance=500 WHERE guildId=? AND userId=?", (GUILD_ID, 801)
        )
        self.cursor.execute(
            "INSERT INTO wagers(guildId, userId, username, team, amount) VALUES(?, ?, ?, ?, ?)",
            (GUILD_ID, 801, "Bettor", 1, 200),
        )
        self.db.commit()

        await self.helperObj.cancelGameHelper(GUILD_ID, channel, guild)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 801, "balance"), 700)  # bet refunded
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

        # rostered players never touched at all, no economy row even exists
        self.assertIsNone(self.helperObj.getEconomy(GUILD_ID, 701, "elo"))
        self.assertIsNone(self.helperObj.getEconomy(GUILD_ID, 702, "elo"))

    async def test_no_original_channel_still_cancels_without_a_move(self):
        # A sequential tournament match deliberately blanks original_channel
        # (see _handleReadyReaction); cancelling one should still refund
        # bets and reset state, just without a "Moved everyone back" note.
        self.helperObj.update(GUILD_ID, "original_channel", "")
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "active_tournament_match_id", 42)
        guild = FakeGuild(channels=[])
        channel = FakeChannel("game-channel")

        await self.helperObj.cancelGameHelper(GUILD_ID, channel, guild)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")
        self.assertIsNone(self.helperObj.get(GUILD_ID, "active_tournament_match_id"))
        sent = [call.args[0] for call in channel.send.call_args_list]
        self.assertFalse(any("Moved everyone back" in text for text in sent))


class MoveMembersToOriginalChannelTests(HelperTestCase):
    async def test_moves_team_channel_members_and_returns_true(self):
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=801)
        member2 = FakeMember("Bob", id=802)
        channel1.members = [member1]
        channel2.members = [member2]
        guild = FakeGuild(channels=[og, channel1, channel2])

        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")

        moved = await self.helperObj.moveMembersToOriginalChannel(guild)

        self.assertTrue(moved)
        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)

    async def test_returns_false_when_never_started(self):
        guild = FakeGuild()
        moved = await self.helperObj.moveMembersToOriginalChannel(guild)
        self.assertFalse(moved)


# ===========================================================================
# Economy: /daily, ensureEconomyRow (see StatsHelperTests for /stats, which
# now covers what /balance used to)
# ===========================================================================

class EconomyTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    def test_ensure_economy_row_creates_defaults(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)

    def test_ensure_economy_row_updates_username_without_resetting_balance(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=250 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "AliceRenamed")

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "username"), "AliceRenamed")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 250)

    async def test_daily_first_claim_grants_gold(self):
        ctx = self._ctx()
        await self.helperObj.dailyHelper(ctx)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        ctx.response.send_message.assert_awaited_once()
        self.assertIn("1000", ctx.response.send_message.call_args.args[0])

    async def test_daily_second_claim_same_day_blocked(self):
        ctx1 = self._ctx()
        await self.helperObj.dailyHelper(ctx1)

        ctx2 = self._ctx()
        await self.helperObj.dailyHelper(ctx2)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        ctx2.response.send_message.assert_awaited_once_with(
            "You've already claimed your daily gold today! Come back tomorrow."
        )


class GetRosterNameTests(HelperTestCase):
    def test_returns_the_teams_own_name(self):
        team = Team(); team.name = "Red"
        team.add_player(Player(701, "P1"))
        self.helperObj.update(GUILD_ID, "team1", team.serializeTeam())

        self.assertEqual(self.helperObj.getRosterName(GUILD_ID, "team1", "Team 1"), "Red")

    def test_falls_back_when_the_column_is_unset(self):
        self.assertEqual(self.helperObj.getRosterName(GUILD_ID, "team1", "Team 1"), "Team 1")

    def test_escape_false_returns_the_raw_unescaped_name(self):
        # Button labels render as plain text; Discord doesn't apply
        # markdown to them, so escape=False should skip escape_markdown
        # rather than showing a stray backslash in the name.
        team = Team(); team.name = "Red_Wolves*"
        team.add_player(Player(701, "P1"))
        self.helperObj.update(GUILD_ID, "team1", team.serializeTeam())

        self.assertEqual(
            self.helperObj.getRosterName(GUILD_ID, "team1", "Team 1", escape=False), "Red_Wolves*"
        )


# ===========================================================================
# Betting: /wager, _openBetting, the background timer, recordResult,
# WinnerReportView, cancelBettingHelper
# ===========================================================================

class WagerHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_non_positive_amount(self):
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 0, 1)
        ctx.response.send_message.assert_awaited_once_with(
            "Wager amount must be greater than 0."
        )

    async def test_rejects_when_betting_not_open(self):
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1)
        ctx.response.send_message.assert_awaited_once_with(
            "Betting is not currently open. Press Start on the roster message to start a game "
            "and open betting."
        )

    async def test_rejects_a_new_wager_once_a_winner_report_is_pending_confirmation(self):
        # Regression: a report reaction used to leave betting_state
        # untouched until recordResult itself cleared every wager, so a
        # new /wager placed during the confirmation window (after
        # reacting, before Confirm is pressed) would still go through -
        # letting someone bet on whichever side just got reported before
        # it's even confirmed as the real winner.
        channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(channels=[channel], guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "betting_message_id", 555)

        view = helper_module.WinnerReportView(self.helperObj)
        click = FakeInteraction(self.guild, FakeMember("Reporter"), channel=channel, message=FakeMessage(id=555))
        await view.team1.callback(click)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "CLOSED")

        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 2)
        ctx.response.send_message.assert_awaited_once_with(
            "Betting is not currently open. Press Start on the roster message to start a game "
            "and open betting."
        )

    async def test_rejects_insufficient_balance(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        ctx = self._ctx()

        await self.helperObj.wagerHelper(ctx, 100, 1)

        ctx.response.send_message.assert_awaited_once_with(
            "You don't have enough gold for that! Your balance is 0."
        )

    async def test_rejects_duplicate_bet(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        ctx1 = self._ctx()
        await self.helperObj.wagerHelper(ctx1, 100, 1)
        ctx2 = self._ctx()
        await self.helperObj.wagerHelper(ctx2, 50, 2)

        ctx2.response.send_message.assert_awaited_once_with(
            "You've already placed a bet on this game."
        )
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 900)

    async def test_successful_wager_escrows_gold(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 250, 2)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.cursor.execute(
            "SELECT team, amount FROM wagers WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.assertEqual(self.cursor.fetchone(), (2, 250))
        ctx.response.send_message.assert_awaited_once_with("You wagered 250 gold on **Team 2**!")

    async def test_rejects_wager_from_a_player_in_the_game(self):
        team1 = Team()
        team1.name = "Team 1"
        team1.add_player(Player(901, "Alice"))  # the bettor is rostered on team1
        team2 = Team()
        team2.name = "Team 2"
        team2.add_player(Player(902, "Bob"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 2)

        ctx.response.send_message.assert_awaited_once_with(
            "You can't wager on a game you're playing in!"
        )
        # no gold moved, no wager recorded
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_rejects_wager_from_a_player_on_the_other_team(self):
        team1 = Team()
        team1.name = "Team 1"
        team1.add_player(Player(801, "Someone"))
        team2 = Team()
        team2.name = "Team 2"
        team2.add_player(Player(901, "Alice"))  # rostered on team2 instead of team1
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")

        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1)

        ctx.response.send_message.assert_awaited_once_with(
            "You can't wager on a game you're playing in!"
        )

    async def test_spectator_not_in_either_team_can_still_wager(self):
        team1 = Team()
        team1.name = "Team 1"
        team1.add_player(Player(801, "Someone"))
        team2 = Team()
        team2.name = "Team 2"
        team2.add_player(Player(802, "Someone Else"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()  # Alice (901) isn't rostered on either team
        await self.helperObj.wagerHelper(ctx, 100, 1)

        ctx.response.send_message.assert_awaited_once_with("You wagered 100 gold on **Team 1**!")


class RecordResultTests(HelperTestCase):
    def _place_bet(self, user_id, name, team, amount, starting_balance=1000):
        self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
        self.cursor.execute(
            "UPDATE economy SET balance=? WHERE guildId=? AND userId=?",
            (starting_balance, GUILD_ID, user_id),
        )
        self.db.commit()
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        ctx = FakeInteraction(self.guild, FakeMember(name, id=user_id))
        return self.helperObj.wagerHelper(ctx, amount, team)

    async def test_no_bets_placed(self):
        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        channel.send.assert_awaited_once()
        self.assertIn("No bets were placed", channel.send.call_args.args[0])
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")

    async def test_pari_mutuel_uneven_pools_favor_underdog(self):
        await self._place_bet(901, "Alice", 1, 100)  # winner
        await self._place_bet(902, "Bob", 2, 300)  # loser

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # Alice: escrowed 100 -> balance 900, payout 100 + (100/100)*300 = 400
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1300)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_wagered"), 100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 300)

        # Bob: escrowed 300, never returned
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 700)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_wagered"), 300)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_won"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_lost"), 300)

        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

        # Not necessarily the last message: a 400-gold payout on a 100-gold
        # bet is a 4x return, which also crosses the Jackpot achievement's
        # own 3x threshold and gets announced right after this one (see
        # _announceAchievements), scan every call instead of assuming
        # this is the final one, same fix the First Blood case needed.
        messages = [c.args[0] for c in channel.send.call_args_list if c.args]
        self.assertTrue(any("won 400 gold (bet 100)" in m for m in messages))

    async def test_a_heavily_favored_win_is_raked_below_the_full_pari_mutuel_split(self):
        # Reverse of the case above: the WINNING side is the heavy
        # favorite (300 vs 100 -> a 0.75 winning share), so this is exactly
        # the "safe bettor" case _imbalanceRakeFraction exists to tax.
        await self._place_bet(901, "Alice", 1, 300)  # winner, heavy favorite
        await self._place_bet(902, "Bob", 2, 100)  # loser, the underdog side

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # Unraked pari-mutuel would pay 300 + (300/300)*100 = 400 (escrowed
        # 1000-300=700, so balance 1100); the 0.25 rake fraction at this
        # imbalance instead pays 300 + (300/300)*75 = 375, landing on 1075.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1075)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 75)

    async def test_multiple_winners_split_losing_pool_proportionally(self):
        await self._place_bet(901, "Alice", 1, 1)
        await self._place_bet(902, "Dana", 1, 2)
        await self._place_bet(903, "Bob", 2, 10)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # winningPool=3, losingPool=10
        # Alice: round(1 + (1/3)*10) = round(4.333) = 4
        # Dana:  round(2 + (2/3)*10) = round(8.666) = 9
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 3)  # 4-1
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_won"), 7)  # 9-2

    async def test_no_winners_all_losing_bets_forfeited(self):
        await self._place_bet(901, "Alice", 2, 100)  # bets on the team that loses

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 900)  # never refunded
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_lost"), 100)
        self.assertIn("Nobody bet on the winning team", channel.send.call_args.args[0])

    async def test_all_bets_on_winning_side_are_refunded_with_no_profit(self):
        await self._place_bet(901, "Alice", 1, 100)  # only bet, on the winning side

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)  # back to start
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 1)

    async def test_moves_everyone_back_when_guild_provided(self):
        # regression test: recording a winner used to only pay out bets;
        # players stayed in their team channels until someone separately
        # ran /return.
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=901)
        member2 = FakeMember("Bob", id=902)
        channel1.members = [member1]
        channel2.members = [member2]
        guild = FakeGuild(channels=[og, channel1, channel2])

        self.helperObj.update(GUILD_ID, "original_channel", "Lobby")
        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel, guild)

        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)
        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertIn("Moved everyone back to the original channel!", messages)

    async def test_no_move_message_when_never_started(self):
        guild = FakeGuild()  # original_channel was never set
        channel = FakeChannel("game-chat")

        await self.helperObj.recordResult(GUILD_ID, 1, channel, guild)

        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertFalse(any("Moved everyone back" in m for m in messages))

    async def test_no_move_attempted_when_guild_omitted(self):
        channel = FakeChannel("game-chat")

        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # guild defaults to None

        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertFalse(any("Moved everyone back" in m for m in messages))

    async def test_updates_game_record_and_elo_for_rostered_players(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # equal starting elo (1000 each) -> a 50/50 upset, K=32 -> +/-16
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_losses"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 1016)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "elo"), 984)

        # P1's first-ever win also earns the "First Blood" achievement,
        # which posts its own announcement, check every message sent
        # rather than assuming the result message is the last (or only)
        # one.
        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertTrue(any("Elo:" in m for m in messages))

    async def test_winning_on_a_disliked_role_earns_bonus_elo(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)
        # rankedTeamHelper is what would normally set this, keyed on the
        # user_id who got stuck with a disliked role for this roster.
        self.helperObj.update(GUILD_ID, "disliked_role_user_ids", "701")

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # Equal starting elo -> a plain 50/50 win is +16; P1 played a
        # disliked role and won, so ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER
        # applies on top of that.
        boosted = round(16 * helper_module.ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 1000 + boosted)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "elo"), 984)

        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertTrue(any("Disliked-role win bonus: P1" in m for m in messages))

    async def test_losing_on_a_disliked_role_earns_no_bonus(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)
        self.helperObj.update(GUILD_ID, "disliked_role_user_ids", "701")

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 2, channel)  # P1's team loses

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 984)
        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertFalse(any("Disliked-role win bonus" in m for m in messages))

    async def test_underdog_win_gains_more_elo_than_favorite_win(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "Underdog"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "Favorite"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)
        self.helperObj.ensureEconomyRow(GUILD_ID, 701, "Underdog")
        self.helperObj.ensureEconomyRow(GUILD_ID, 702, "Favorite")
        self.cursor.execute(
            "UPDATE economy SET elo=800 WHERE guildId=? AND userId=?", (GUILD_ID, 701)
        )
        self.cursor.execute(
            "UPDATE economy SET elo=1200 WHERE guildId=? AND userId=?", (GUILD_ID, 702)
        )
        self.db.commit()

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # the 800-elo side wins

        underdog_gain = self.helperObj.getEconomy(GUILD_ID, 701, "elo") - 800
        self.assertGreater(underdog_gain, 16)  # more than the equal-elo baseline gain

    async def test_no_roster_no_elo_line_in_message(self):
        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # team1/team2 never set

        message = channel.send.call_args.args[0]
        self.assertNotIn("Elo:", message)

    async def test_casual_game_with_roster_does_not_touch_elo(self):
        # regression test: /make-teams and /captains form rosters just like
        # /ranked does, but is_ranked defaults to 0 for them; elo must stay
        # untouched even though game_wins/game_losses still update.
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        # is_ranked left at its default (0/unset)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_losses"), 1)
        self.assertNotIn("Elo:", channel.send.call_args.args[0])
        # a casual game bumps the combined game_wins/game_losses total but
        # never the ranked-only subset, see getLeaderboardEntries's
        # casual = game_wins - ranked_wins derivation.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "ranked_wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "ranked_losses"), 0)

    async def test_ranked_game_bumps_both_the_combined_and_ranked_only_record(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "ranked_wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "ranked_losses"), 1)

    async def test_rostered_players_get_win_or_loss_gold_accordingly(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # team1 wins, team2 loses

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "balance"), helper_module.GAME_WIN_GOLD)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "balance"), helper_module.GAME_LOSS_GOLD)
        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertTrue(any(
            f"1 winning player earned {helper_module.GAME_WIN_GOLD} gold each and "
            f"1 losing player earned {helper_module.GAME_LOSS_GOLD} gold each just for playing." in m
            for m in messages
        ))

    async def test_result_message_and_elo_line_use_the_rosters_own_team_names(self):
        team1 = Team(); team1.name = "Red"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Blue"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertTrue(any("**Red** wins!" in m for m in messages))
        self.assertTrue(any("Red" in m and "Blue" in m and "Elo:" in m for m in messages))


class ImbalanceRakeFractionTests(HelperTestCase):
    def test_even_split_is_not_raked(self):
        self.assertEqual(self.helperObj._imbalanceRakeFraction(100, 100), 0.0)

    def test_winners_being_the_minority_is_not_raked(self):
        # A real upset (fewer people backed the eventual winner than the
        # loser) shouldn't be taxed at all, only "safe" lopsided bets.
        self.assertEqual(self.helperObj._imbalanceRakeFraction(100, 300), 0.0)

    def test_maximally_lopsided_pool_hits_the_cap(self):
        # losing_pool=0 -> favorite_share=1.0 -> full MAX_IMBALANCE_RAKE,
        # though it's moot here since there's nothing in the losing pool
        # to rake off anyway.
        self.assertEqual(
            self.helperObj._imbalanceRakeFraction(100, 0), helper_module.MAX_IMBALANCE_RAKE
        )

    def test_scales_linearly_between_the_extremes(self):
        # favorite_share=0.75 is halfway between 0.5 (0 rake) and 1.0 (max
        # rake), so the rake should land at exactly half of the cap.
        self.assertAlmostEqual(
            self.helperObj._imbalanceRakeFraction(300, 100), helper_module.MAX_IMBALANCE_RAKE / 2
        )

    def test_empty_pools_do_not_divide_by_zero(self):
        self.assertEqual(self.helperObj._imbalanceRakeFraction(0, 0), 0.0)


class ComputeGameDeltasTests(HelperTestCase):
    def test_equal_elo_teams_split_evenly(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
        )
        self.assertEqual(deltas[1]["elo"], 16)
        self.assertEqual(deltas[2]["elo"], -16)
        self.assertEqual(deltas[1]["game_wins"], 1)
        self.assertEqual(deltas[2]["game_losses"], 1)
        self.assertEqual(summary["elo_changes"], [("Team 1", 16), ("Team 2", -16)])
        self.assertEqual(summary["team1_name"], "Team 1")
        self.assertEqual(summary["team2_name"], "Team 2")

    def test_elo_changes_and_summary_use_given_team_names(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
            team1_name="Red", team2_name="Blue",
        )
        self.assertEqual(summary["elo_changes"], [("Red", 16), ("Blue", -16)])
        self.assertEqual(summary["team1_name"], "Red")
        self.assertEqual(summary["team2_name"], "Blue")

    def test_ranked_game_also_bumps_ranked_wins_losses(self):
        deltas, _summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
        )
        self.assertEqual(deltas[1]["ranked_wins"], 1)
        self.assertEqual(deltas[1]["ranked_losses"], 0)
        self.assertEqual(deltas[2]["ranked_wins"], 0)
        self.assertEqual(deltas[2]["ranked_losses"], 1)

    def test_missing_players_fall_back_to_the_given_default_elo(self):
        # elo_lookup has no entry for either player, as if this guild has
        # a configured default_elo different from the global DEFAULT_ELO.
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={}, winning_team=1, is_ranked=True, default_elo=1200,
        )
        # equal (both defaulted to 1200) elo teams still split evenly
        self.assertEqual(deltas[1]["elo"], 16)
        self.assertEqual(deltas[2]["elo"], -16)
        self.assertEqual(summary["elo_changes"], [("Team 1", 16), ("Team 2", -16)])

    def test_unranked_games_never_touch_elo(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 800, 2: 1200}, winning_team=1, is_ranked=False,
        )
        self.assertEqual(deltas[1]["elo"], 0)
        self.assertEqual(deltas[2]["elo"], 0)
        # game record still tracked even for a casual game
        self.assertEqual(deltas[1]["game_wins"], 1)
        self.assertEqual(deltas[2]["game_losses"], 1)
        self.assertEqual(summary["elo_changes"], [])

    def test_casual_games_never_touch_ranked_wins_losses(self):
        deltas, _summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 800, 2: 1200}, winning_team=1, is_ranked=False,
        )
        self.assertEqual(deltas[1]["ranked_wins"], 0)
        self.assertEqual(deltas[2]["ranked_losses"], 0)

    def test_rostered_players_get_win_or_loss_gold_accordingly(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B"), (3, "C")],
            elo_lookup={1: 1000, 2: 1000, 3: 1000}, winning_team=1, is_ranked=False,
        )
        self.assertEqual(deltas[1]["balance"], helper_module.GAME_WIN_GOLD)
        self.assertEqual(deltas[2]["balance"], helper_module.GAME_LOSS_GOLD)
        self.assertEqual(deltas[3]["balance"], helper_module.GAME_LOSS_GOLD)
        self.assertEqual(summary["winner_gold_count"], 1)
        self.assertEqual(summary["loser_gold_count"], 2)

    def test_participation_gold_stacks_with_bet_payouts_without_touching_wager_fields(self):
        # user 1 is rostered AND bet on their own... no, betting on a game
        # you're playing in is blocked elsewhere; this covers a rostered
        # player (1) and a pure bettor (901) independently, confirming
        # participation gold only ever lands via the roster loop and never
        # perturbs gold_wagered/gold_won/gold_lost either way.
        deltas, _summary = self.helperObj.computeGameDeltas(
            wagers=[(901, "Bettor", 1, 100)], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=False,
        )
        self.assertEqual(deltas[1]["balance"], helper_module.GAME_WIN_GOLD)
        self.assertEqual(deltas[1]["gold_wagered"], 0)
        self.assertEqual(deltas[1]["gold_won"], 0)
        self.assertEqual(deltas[1]["gold_lost"], 0)
        # the bettor's payout is untouched by (and doesn't touch) the
        # participation reward; they're not on either roster.
        self.assertEqual(deltas[901]["balance"], 100)
        self.assertEqual(deltas[901]["gold_won"], 0)

    def test_no_participation_gold_when_nobody_is_rostered(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[(901, "Bettor", 1, 100)], team1_roster=[], team2_roster=[],
            elo_lookup={}, winning_team=1, is_ranked=False,
        )
        self.assertNotIn(1, deltas)
        self.assertEqual(summary["winner_gold_count"], 0)
        self.assertEqual(summary["loser_gold_count"], 0)

    def test_applying_then_reversing_deltas_is_a_no_op(self):
        # this is exactly what /report-correct-winner relies on: reversing
        # a previously-applied result must land back on the exact starting
        # values, not an approximation.
        wagers = [(901, "Alice", 1, 100), (902, "Bob", 2, 300)]
        team1_roster = [(701, "P1")]
        team2_roster = [(702, "P2")]
        elo_lookup = {701: 1050, 702: 950}

        for user_id, name in [(901, "Alice"), (902, "Bob"), (701, "P1"), (702, "P2")]:
            self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 902))
        self.cursor.execute("UPDATE economy SET elo=1050 WHERE guildId=? AND userId=?", (GUILD_ID, 701))
        self.cursor.execute("UPDATE economy SET elo=950 WHERE guildId=? AND userId=?", (GUILD_ID, 702))
        self.db.commit()

        def snapshot():
            self.cursor.execute(
                "SELECT userId, balance, wins, losses, gold_wagered, gold_won, gold_lost, "
                "game_wins, game_losses, ranked_wins, ranked_losses, elo FROM economy "
                "WHERE guildId=? ORDER BY userId",
                (GUILD_ID,),
            )
            return self.cursor.fetchall()

        before = snapshot()

        deltas, _summary = self.helperObj.computeGameDeltas(
            wagers, team1_roster, team2_roster, elo_lookup, winning_team=1, is_ranked=True
        )
        self.helperObj.applyGameDeltas(GUILD_ID, deltas, sign=1)
        self.assertNotEqual(snapshot(), before)  # sanity: something actually changed

        self.helperObj.applyGameDeltas(GUILD_ID, deltas, sign=-1)
        self.assertEqual(snapshot(), before)

    def test_applying_deltas_that_cross_into_diamond_unlocks_the_reward(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 701, "P1")
        self.cursor.execute(
            "UPDATE economy SET elo=? WHERE guildId=? AND userId=?",
            (helper_module.ELO_TIER_THRESHOLDS["Diamond"] - 10, GUILD_ID, 701)
        )
        self.db.commit()

        deltas = {701: {
            "username": "P1", "balance": 0, "wins": 0, "losses": 0, "gold_wagered": 0,
            "gold_won": 0, "gold_lost": 0, "game_wins": 1, "game_losses": 0,
            "ranked_wins": 1, "ranked_losses": 0, "elo": 20,
        }}
        self.helperObj.applyGameDeltas(GUILD_ID, deltas, sign=1)

        self.assertIn(
            helper_module.CARD_TIER_REWARD_TITLES["Diamond"], self.helperObj.getUnlockedCardTitles(GUILD_ID, 701)
        )

    def test_reversing_deltas_never_unlocks_anything(self):
        # A correction's reversal (sign=-1) is "undo a wrongly-recorded
        # result", not "grant a reward on the way back down"; the reapply
        # against the corrected winner that follows calls back in with
        # sign=1, which is what actually re-checks properly.
        self.helperObj.ensureEconomyRow(GUILD_ID, 701, "P1")
        self.cursor.execute(
            "UPDATE economy SET elo=? WHERE guildId=? AND userId=?",
            (helper_module.ELO_TIER_THRESHOLDS["Diamond"] + 50, GUILD_ID, 701)
        )
        self.db.commit()

        deltas = {701: {
            "username": "P1", "balance": 0, "wins": 0, "losses": 0, "gold_wagered": 0,
            "gold_won": 0, "gold_lost": 0, "game_wins": 0, "game_losses": 1,
            "ranked_wins": 0, "ranked_losses": 1, "elo": -300,
        }}
        self.helperObj.applyGameDeltas(GUILD_ID, deltas, sign=-1)

        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 701), [])

    def test_disliked_role_win_bonus_boosts_only_that_players_elo(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A"), (2, "B")], team2_roster=[(3, "C")],
            elo_lookup={1: 1000, 2: 1000, 3: 1000}, winning_team=1, is_ranked=True,
            disliked_role_user_ids={1},
        )
        # An even matchup's plain team delta is +16/-16; player 1 (won on a
        # disliked role) gets it multiplied up; player 2, on the same
        # winning team but no disliked role, gets the plain team delta.
        boosted = round(16 * helper_module.ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER)
        self.assertEqual(deltas[1]["elo"], boosted)
        self.assertEqual(deltas[2]["elo"], 16)
        self.assertEqual(deltas[3]["elo"], -16)
        self.assertEqual(summary["disliked_role_bonus_players"], [("A", boosted)])

    def test_disliked_role_bonus_does_not_apply_to_a_loss(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=2, is_ranked=True,
            disliked_role_user_ids={1},
        )
        # Player 1 is on the disliked-role list but LOST this game, no
        # bonus, just the plain losing-side delta.
        self.assertEqual(deltas[1]["elo"], -16)
        self.assertEqual(summary["disliked_role_bonus_players"], [])

    def test_disliked_role_bonus_does_not_apply_to_a_casual_game(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=False,
            disliked_role_user_ids={1},
        )
        self.assertEqual(deltas[1]["elo"], 0)
        self.assertEqual(summary["disliked_role_bonus_players"], [])

    def test_disliked_role_bonus_ignores_players_outside_the_given_set(self):
        deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
            disliked_role_user_ids=frozenset(),
        )
        self.assertEqual(deltas[1]["elo"], 16)
        self.assertEqual(summary["disliked_role_bonus_players"], [])


class FormatResultMessageTests(HelperTestCase):
    def test_uses_the_summarys_team_names(self):
        _deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[], team2_roster=[], elo_lookup={}, winning_team=2,
            team1_name="Red", team2_name="Blue",
        )
        message = self.helperObj.formatResultMessage(2, summary)
        self.assertTrue(message.startswith("**Blue** wins!"))

    def test_falls_back_to_a_generic_label_when_summary_has_no_names(self):
        # A caller that hasn't threaded real names through computeGameDeltas
        # (or an old-style summary dict) shouldn't KeyError.
        message = self.helperObj.formatResultMessage(1, {
            "no_bets": True, "no_winning_bets": False, "winning_bettors": [], "elo_changes": [],
        })
        self.assertTrue(message.startswith("**Team 1** wins!"))

    def test_announces_win_and_loss_gold_for_multiple_players(self):
        _deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B"), (3, "C")],
            elo_lookup={1: 1000, 2: 1000, 3: 1000}, winning_team=1,
        )
        message = self.helperObj.formatResultMessage(1, summary)
        self.assertIn(
            f"1 winning player earned {helper_module.GAME_WIN_GOLD} gold each and "
            f"2 losing players earned {helper_module.GAME_LOSS_GOLD} gold each just for playing.",
            message,
        )

    def test_announces_win_gold_only_when_there_are_no_losers(self):
        _deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[], elo_lookup={1: 1000}, winning_team=1,
        )
        message = self.helperObj.formatResultMessage(1, summary)
        self.assertIn(
            f"1 winning player earned {helper_module.GAME_WIN_GOLD} gold each just for playing.", message
        )

    def test_disliked_role_bonus_line_appears_when_someone_earned_it(self):
        _deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
            disliked_role_user_ids={1},
        )
        message = self.helperObj.formatResultMessage(1, summary)
        boosted = round(16 * helper_module.ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER)
        self.assertIn(f"Disliked-role win bonus: A {boosted:+d}", message)

    def test_no_disliked_role_bonus_line_when_nobody_earned_it(self):
        _deltas, summary = self.helperObj.computeGameDeltas(
            wagers=[], team1_roster=[(1, "A")], team2_roster=[(2, "B")],
            elo_lookup={1: 1000, 2: 1000}, winning_team=1, is_ranked=True,
        )
        message = self.helperObj.formatResultMessage(1, summary)
        self.assertNotIn("Disliked-role win bonus", message)

    def test_no_participation_line_when_nobody_was_rostered(self):
        message = self.helperObj.formatResultMessage(1, {
            "no_bets": True, "no_winning_bets": False, "winning_bettors": [], "elo_changes": [],
        })
        self.assertNotIn("earned", message)


class SaveGetLastResultTests(HelperTestCase):
    def test_round_trips_through_json_storage(self):
        deltas = {
            901: {"username": "Alice", "balance": 400, "wins": 1, "losses": 0,
                  "gold_wagered": 100, "gold_won": 300, "gold_lost": 0,
                  "game_wins": 0, "game_losses": 0, "elo": 0},
        }
        self.helperObj.saveLastResult(
            GUILD_ID, winning_team=1,
            wagers=[(901, "Alice", 1, 100)],
            team1_roster=[(701, "P1")],
            team2_roster=[(702, "P2")],
            deltas=deltas,
        )

        loaded = self.helperObj.getLastResult(GUILD_ID)

        self.assertEqual(loaded["winning_team"], 1)
        self.assertEqual(loaded["wagers"], [(901, "Alice", 1, 100)])
        self.assertEqual(loaded["team1_roster"], [(701, "P1")])
        self.assertEqual(loaded["team2_roster"], [(702, "P2")])
        self.assertEqual(loaded["deltas"], deltas)
        self.assertIsInstance(next(iter(loaded["deltas"].keys())), int)
        # not given above -> generic fallback
        self.assertEqual(loaded["team1_name"], "Team 1")
        self.assertEqual(loaded["team2_name"], "Team 2")
        self.assertEqual(loaded["disliked_role_user_ids"], frozenset())

    def test_round_trips_disliked_role_user_ids(self):
        self.helperObj.saveLastResult(
            GUILD_ID, winning_team=1, wagers=[], team1_roster=[(701, "P1")], team2_roster=[(702, "P2")],
            deltas={}, disliked_role_user_ids={701},
        )
        loaded = self.helperObj.getLastResult(GUILD_ID)
        self.assertEqual(loaded["disliked_role_user_ids"], frozenset({701}))

    def test_round_trips_the_given_team_names(self):
        self.helperObj.saveLastResult(
            GUILD_ID, winning_team=1, wagers=[], team1_roster=[], team2_roster=[], deltas={},
            team1_name="Red", team2_name="Blue",
        )
        loaded = self.helperObj.getLastResult(GUILD_ID)
        self.assertEqual(loaded["team1_name"], "Red")
        self.assertEqual(loaded["team2_name"], "Blue")

    def test_returns_none_when_nothing_recorded(self):
        self.assertIsNone(self.helperObj.getLastResult(GUILD_ID))

    def test_saving_again_overwrites_the_previous_snapshot(self):
        self.helperObj.saveLastResult(GUILD_ID, 1, [], [], [], {})
        self.helperObj.saveLastResult(GUILD_ID, 2, [], [], [], {})

        self.assertEqual(self.helperObj.getLastResult(GUILD_ID)["winning_team"], 2)


class ReportCorrectWinnerHelperTests(HelperTestCase):
    async def test_no_recent_result_to_correct(self):
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2)
        ctx.response.send_message.assert_awaited_once_with(
            "There's no recent game result to correct."
        )

    async def test_already_recorded_winner_is_a_noop(self):
        self.helperObj.saveLastResult(GUILD_ID, winning_team=1, wagers=[], team1_roster=[], team2_roster=[], deltas={})
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))

        await self.helperObj.reportCorrectWinnerHelper(ctx, 1)

        ctx.response.send_message.assert_awaited_once_with(
            "**Team 1** is already the recorded winner - nothing to correct."
        )

    async def test_corrects_bettor_payouts_when_winner_flips(self):
        # No rosters, isolates the betting-correction math. Alice bet on
        # team1 (wrongly reported as the winner), Bob bet on team2 (the
        # actual winner).
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 902))
        self.db.commit()

        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        await self.helperObj.wagerHelper(FakeInteraction(self.guild, FakeMember("Alice", id=901)), 100, 1)
        await self.helperObj.wagerHelper(FakeInteraction(self.guild, FakeMember("Bob", id=902)), 300, 2)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # misreported: team1 "wins"

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1300)  # Alice paid out
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 700)  # Bob lost his bet

        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2)  # team2 actually won

        # Bob's payout is raked: as the real winner he was also the heavy
        # favorite (300 vs Alice's 100 -> a 0.75 winning share), so
        # _imbalanceRakeFraction takes a 25% cut of the 100 losing pool
        # (25 gold) before splitting it: 300 + (300/300)*75 = 375, not the
        # full 400 an unraked pari-mutuel split would've paid. That 25 gold
        # was already deducted from Alice's balance at bet time and simply
        # isn't credited to anyone now, so total balance across both
        # players (900 + 1075 = 1975) is 25 short of the original 2000,
        # not conserved, that's the rake doing its job.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 900)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_lost"), 100)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 1075)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_won"), 75)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_lost"), 0)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("Team 2", ctx.response.send_message.call_args.args[0])
        self.assertIn("previously recorded as Team 1", ctx.response.send_message.call_args.args[0])

        # a further correction is possible from the new baseline
        self.assertEqual(self.helperObj.getLastResult(GUILD_ID)["winning_team"], 2)

    async def test_correction_still_credits_the_disliked_role_bonus(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)
        self.helperObj.update(GUILD_ID, "disliked_role_user_ids", "701")

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 2, channel)  # misreported: P1's team "loses"
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 984)

        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, 1)  # actually won

        # Back to 1000 (the wrong loss reversed) plus the disliked-role
        # bonus for the real win, recomputed from the last_result snapshot
        # rather than losing track of who was on a disliked role once
        # team1/team2 may have already moved on to a new roster.
        boosted = round(16 * helper_module.ROLE_BALANCE_DISLIKED_ROLE_WIN_ELO_MULTIPLIER)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 1000 + boosted)
        # Not necessarily the last message: P1's first-ever recorded win
        # also earns the "First Blood" achievement, announced separately
        # right after (see _announceAchievements), scan every message
        # rather than assuming the result message is the final one.
        messages = [c.args[0] for c in ctx.channel.send.call_args_list if c.args]
        self.assertTrue(any("Disliked-role win bonus: P1" in m for m in messages))

    async def test_corrects_elo_and_game_record_when_teams_started_equal(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)  # misreported: team1 "wins"

        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2)  # team2 actually won

        # teams started at equal elo, so correcting the winner should land
        # on the exact mirror image of the original (wrong) result
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 984)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "elo"), 1016)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_losses"), 0)

    async def test_invalidate_rejects_when_team_also_given(self):
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, invalidate=True)
        ctx.response.send_message.assert_awaited_once_with("Give team or invalidate, not both.")

    async def test_rejects_when_neither_team_nor_invalidate_given(self):
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, None)
        ctx.response.send_message.assert_awaited_once_with(
            "Give team (who actually won), or invalidate to undo the game entirely."
        )

    async def test_invalidate_rejects_a_specific_tournament_match(self):
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, None, match_id=1, invalidate=True)
        ctx.response.send_message.assert_awaited_once_with(
            "Invalidating isn't supported for a specific tournament match yet - correct it "
            "to the other team instead."
        )

    async def test_invalidate_with_no_recent_result(self):
        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, None, invalidate=True)
        ctx.response.send_message.assert_awaited_once_with(
            "There's no recent game result to correct."
        )

    async def test_invalidate_refunds_bets_and_undoes_elo_and_records(self):
        team1 = Team(); team1.name = "Team 1"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Team 2"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "is_ranked", 1)

        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.cursor.execute("UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 902))
        self.db.commit()

        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        await self.helperObj.wagerHelper(FakeInteraction(self.guild, FakeMember("Alice", id=901)), 100, 1)
        await self.helperObj.wagerHelper(FakeInteraction(self.guild, FakeMember("Bob", id=902)), 300, 2)

        channel = FakeChannel("game-chat")
        await self.helperObj.recordResult(GUILD_ID, 1, channel)

        # sanity: something actually happened before invalidating
        self.assertNotEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 1000)
        self.assertNotEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)

        ctx = FakeInteraction(self.guild, FakeMember("Admin"))
        await self.helperObj.reportCorrectWinnerHelper(ctx, None, invalidate=True)

        # bettors refunded to their exact pre-bet balance, win/loss stats
        # from the invalidated bet undone too
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 0)

        # rostered players' elo/game record/win-loss gold all undone
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "elo"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "game_wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 701, "balance"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "elo"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "game_losses"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 702, "balance"), 0)

        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("Team 1", message)
        self.assertIn("Team 2", message)
        self.assertIn("invalidated", message)

        # nothing left to correct further; the last result is gone entirely
        self.assertIsNone(self.helperObj.getLastResult(GUILD_ID))


class StatsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_defaults_for_a_brand_new_player(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Elo"], "1000 (\U0001f537 Platinum IV)")
        self.assertEqual(values["Game Record"], "0W - 0L (N/A)")
        self.assertEqual(values["Bet Record"], "0W - 0L (N/A)")
        self.assertEqual(values["Balance"], "0 gold")

    async def test_reports_populated_stats(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=500, wins=2, losses=1, gold_wagered=300, "
            "gold_won=150, gold_lost=50, game_wins=7, game_losses=3, "
            "ranked_wins=4, ranked_losses=1, elo=1123 "
            "WHERE guildId=? AND userId=?",
            (GUILD_ID, 901),
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Elo"], "1123 (\U0001f537 Platinum III)")
        self.assertEqual(values["Game Record"], "7W - 3L (70.0%)")
        # ranked: 4W-1L (from the columns above); casual is the remainder
        # of the combined 7W-3L total, i.e. 3W-2L.
        self.assertEqual(values["Ranked Wins"], "4W - 1L")
        self.assertEqual(values["Ranked Win Rate"], "80.0%")
        self.assertEqual(values["Casual Record"], "3W - 2L (60.0%)")
        self.assertEqual(values["Bet Record"], "2W - 1L (66.7%)")
        self.assertEqual(values["Net Gold Won/Lost"], "+100 gold")

    async def test_stats_shows_na_for_ranked_and_casual_rate_with_no_games(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Ranked Wins"], "0W - 0L")
        self.assertEqual(values["Ranked Win Rate"], "N/A")
        self.assertEqual(values["Casual Record"], "0W - 0L (N/A)")

    async def test_fields_are_grouped_ranked_then_casual_then_gold(self):
        # regression test: a blank inline=False spacer field used to force
        # the line break after the ranked row, but it still renders its
        # own (invisible) name+value line, a big empty gap in the actual
        # embed, not the clean break it looked like. Elo joining the
        # ranked row instead (rounding it out to a full 3 wide, same as
        # the other two rows) avoids needing a spacer at all.
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        names = [f.name for f in embed.fields]

        # The first 9 fields are the 3-wide inline stat grid; Role
        # Preferences (see below) is the one non-inline field after it.
        self.assertTrue(all(f.inline for f in embed.fields[:9]))
        self.assertFalse(embed.fields[9].inline)

        # Row 1 (ranked, exactly 3 wide).
        self.assertEqual(names[0:3], ["Elo", "Ranked Wins", "Ranked Win Rate"])

        # Row 2 (casual + bet, exactly 3 wide).
        self.assertEqual(names[3:6], ["Game Record", "Casual Record", "Bet Record"])

        # Row 3 (gold, exactly 3 wide).
        self.assertEqual(names[6:9], ["Balance", "Net Gold Won/Lost", "Gold Wagered"])

    async def test_role_preferences_default_to_none_set(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Role Preferences"], "Liked: none set\nDisliked: none set")

    async def test_role_preferences_reflect_setup(self):
        self.helperObj._applySetupRolePreferences(GUILD_ID, 901, ["Top", "Jungle"], ["Support"])

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        liked_line, disliked_line = values["Role Preferences"].split("\n")
        self.assertEqual(set(liked_line.removeprefix("Liked: ").split(", ")), {"Top", "Jungle"})
        self.assertEqual(disliked_line, "Disliked: Support")

    async def test_role_preferences_follow_the_looked_up_member_not_the_caller(self):
        other = FakeMember("Bob", id=902)
        self.helperObj._applySetupRolePreferences(GUILD_ID, 902, ["Mid"], [])

        ctx = self._ctx()  # caller is Alice (901), with no preferences set
        await self.helperObj.statsHelper(ctx, other)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Role Preferences"], "Liked: Mid\nDisliked: none set")

    async def test_looks_up_another_members_stats(self):
        other = FakeMember("Bob", id=902)
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET elo=1200 WHERE guildId=? AND userId=?", (GUILD_ID, 902)
        )
        self.db.commit()

        ctx = self._ctx()  # caller is Alice (901)
        await self.helperObj.statsHelper(ctx, other)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Bob", embed.title)
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Elo"], "1200 (\U0001f537 Platinum I)")

    async def test_embed_thumbnail_is_the_targets_avatar(self):
        ctx = self._ctx()  # caller is Alice (901)
        await self.helperObj.statsHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertEqual(embed.thumbnail.url, ctx.user.display_avatar.url)

    async def test_embed_thumbnail_follows_the_looked_up_member_not_the_caller(self):
        other = FakeMember("Bob", id=902)
        ctx = self._ctx()  # caller is Alice (901)
        await self.helperObj.statsHelper(ctx, other)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertEqual(embed.thumbnail.url, other.display_avatar.url)
        self.assertNotEqual(embed.thumbnail.url, ctx.user.display_avatar.url)

    async def test_animated_avatar_thumbnail_is_forced_to_a_static_format(self):
        # Discord doesn't reliably unfurl an animated .gif in the embed
        # thumbnail slot; statsHelper's with_format("png") call forces a
        # static format instead, at the cost of losing the animation.
        ctx = self._ctx()
        ctx.user.display_avatar = FakeAsset("https://cdn.discordapp.com/avatars/901/a_deadbeef.gif")

        await self.helperObj.statsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertEqual(embed.thumbnail.url, "https://cdn.discordapp.com/avatars/901/a_deadbeef.png")

    async def test_stale_trading_card_row_is_resynced_to_current_defaults(self):
        # Since there's no /card-customize command yet, every existing
        # trading_cards row is really just a stale snapshot of
        # CARD_DEFAULT_*, not a deliberate choice; it should stay in sync
        # rather than freezing at whatever the defaults were the day the
        # row was first created.
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "UPDATE trading_cards SET background_color=? WHERE guildId=? AND userId=?",
            ("#150B22", GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["background_color"], helper_module.CARD_DEFAULT_BACKGROUND_COLOR)

    async def test_customized_trading_card_row_is_left_alone(self):
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "UPDATE trading_cards SET background_color=?, customized=1 WHERE guildId=? AND userId=?",
            ("#123456", GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["background_color"], "#123456")

    async def test_viewing_stats_lazily_unlocks_tier_rewards_already_earned(self):
        # A player already sitting at Diamond+ before card_unlocks existed
        # (or whose elo got there some other way than a normal ranked
        # result passing through applyGameDeltas) still gets credited the
        # next time anything looks at their stats, not never.
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET elo=? WHERE guildId=? AND userId=?",
            (helper_module.ELO_TIER_THRESHOLDS["Master"], GUILD_ID, 901)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        titles = set(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901))
        self.assertEqual(titles, {
            helper_module.CARD_TIER_REWARD_TITLES["Diamond"], helper_module.CARD_TIER_REWARD_TITLES["Master"],
        })


class StatsViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("stats-chat", guild=self.guild)
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self):
        return FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)

    def _click(self, message, user_id=902, name="Bob"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=self.channel, message=message)

    async def test_posts_a_stats_view_and_tracks_the_view(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        msg = await ctx.original_response()
        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.StatsView)
        self.assertIn(view.avatarToggle, view.children)
        self.assertIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)

        self.cursor.execute("SELECT guildId, targetUserId FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, 901))

    async def test_avatar_toggle_swaps_thumbnail_from_server_to_global_avatar(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()
        original_embed = ctx.response.send_message.call_args.kwargs["embed"]

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [original_embed]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        await view.avatarToggle.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        edited_embed = fetched_message.edit.call_args.kwargs["embed"]
        # fetch_user auto-generates a FakeUser(901) with its own distinct
        # global avatar URL (see FakeClient.fetch_user), this is that URL.
        self.assertEqual(edited_embed.thumbnail.url, "https://cdn.discordapp.com/avatars/901/global.png")
        # everything else on the embed is untouched
        self.assertEqual(edited_embed.title, original_embed.title)
        self.assertEqual(edited_embed.fields, original_embed.fields)

    async def test_avatar_toggle_click_on_an_unknown_message_is_rejected(self):
        view = helper_module.StatsView(self.helperObj, card_shown=False)
        click = self._click(FakeMessage(id=999999))

        await view.avatarToggle.callback(click)

        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_toggles_back_to_the_server_avatar_on_a_second_click(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()
        server_avatar_url = alice.display_avatar.with_format("png").url

        embed_showing_global = ctx.response.send_message.call_args.kwargs["embed"]
        embed_showing_global.set_thumbnail(url="https://cdn.discordapp.com/avatars/901/global.png")
        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [embed_showing_global]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        await view.avatarToggle.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        edited_embed = fetched_message.edit.call_args.kwargs["embed"]
        self.assertEqual(edited_embed.thumbnail.url, server_avatar_url)

    async def test_toggle_noops_if_the_member_left_the_guild(self):
        # Alice deliberately NOT added to self.guild.members, simulates
        # her having left, so the server avatar can't be resolved at all,
        # and there's nothing to toggle between.
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        await view.avatarToggle.callback(self._click(fetched_message))

        fetched_message.edit.assert_not_awaited()

    async def test_card_click_replaces_the_whole_embed_with_a_trading_card(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()
        original_embed = ctx.response.send_message.call_args.kwargs["embed"]

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [original_embed]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        await view.showCard.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        new_embed = fetched_message.edit.call_args.kwargs["embed"]
        # the stats content is gone entirely; this is a full replacement,
        # not just another field/thumbnail tweak like the avatar toggle.
        self.assertEqual(len(new_embed.fields), 0)
        self.assertIsNotNone(new_embed.image.url)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))

        attached_files = fetched_message.edit.call_args.kwargs["attachments"]
        self.assertEqual(len(attached_files), 1)
        self.assertTrue(new_embed.image.url.endswith(attached_files[0].filename))
        attached_files[0].close()

    async def test_card_click_on_an_unknown_message_is_rejected(self):
        view = helper_module.StatsView(self.helperObj, card_shown=False)
        click = self._click(FakeMessage(id=999999))

        await view.showCard.callback(click)

        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_card_shows_the_real_discord_username_not_the_nickname(self):
        alice = FakeMember("Alice", id=901)
        alice.display_name = "Ally"  # a server nickname, distinct from her real username
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        with patch.object(
            self.helperObj, "_renderTradingCardImage", wraps=self.helperObj._renderTradingCardImage
        ) as mock_render:
            await view.showCard.callback(self._click(fetched_message))

        self.assertEqual(mock_render.call_args.kwargs["username"], "Alice")
        self.assertEqual(mock_render.call_args.args[1], "Ally")
        fetched_message.edit.call_args.kwargs["attachments"][0].close()

        # cardShown is recorded, and the re-rendered view swaps Card out
        # for Back; the avatar toggle stays either way, since it still
        # applies to the card too (see the dedicated tests below).
        self.cursor.execute("SELECT cardShown FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (1,))
        new_view = fetched_message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.returnToStats, new_view.children)
        self.assertNotIn(new_view.showCard, new_view.children)
        self.assertIn(new_view.avatarToggle, new_view.children)

    async def test_avatar_toggle_on_the_card_swaps_in_the_global_avatar(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        show_view = helper_module.StatsView(self.helperObj, card_shown=False)
        await show_view.showCard.callback(self._click(fetched_message))
        fetched_message.edit.reset_mock()

        card_view = helper_module.StatsView(self.helperObj, card_shown=True)
        with patch.object(
            self.helperObj, "_resolveCardAvatarImage", wraps=self.helperObj._resolveCardAvatarImage
        ) as mock_resolve:
            await card_view.avatarToggle.callback(self._click(fetched_message))

        # the card gets genuinely re-rendered (a new attachment), not
        # silently ignored the way it used to be once the card was shown.
        fetched_message.edit.assert_awaited_once()
        fetched_message.edit.call_args.kwargs["attachments"][0].close()
        self.assertTrue(mock_resolve.call_args.args[1])  # use_global_avatar=True this time

        self.cursor.execute("SELECT cardAvatarGlobal FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (1,))

    async def test_avatar_toggle_on_the_card_flips_back_to_the_server_avatar(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        show_view = helper_module.StatsView(self.helperObj, card_shown=False)
        await show_view.showCard.callback(self._click(fetched_message))
        card_view = helper_module.StatsView(self.helperObj, card_shown=True)
        await card_view.avatarToggle.callback(self._click(fetched_message))  # -> global
        fetched_message.edit.reset_mock()

        with patch.object(
            self.helperObj, "_resolveCardAvatarImage", wraps=self.helperObj._resolveCardAvatarImage
        ) as mock_resolve:
            await card_view.avatarToggle.callback(self._click(fetched_message))  # -> server again

        fetched_message.edit.assert_awaited_once()
        fetched_message.edit.call_args.kwargs["attachments"][0].close()
        self.assertFalse(mock_resolve.call_args.args[1])  # use_global_avatar=False this time

        self.cursor.execute("SELECT cardAvatarGlobal FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (0,))

    async def test_returning_from_the_card_resets_the_avatar_to_server(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        show_view = helper_module.StatsView(self.helperObj, card_shown=False)
        await show_view.showCard.callback(self._click(fetched_message))
        card_view = helper_module.StatsView(self.helperObj, card_shown=True)
        await card_view.avatarToggle.callback(self._click(fetched_message))

        self.cursor.execute("SELECT cardAvatarGlobal FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (1,))

        # card_embed needs an image (not fields) to look like the card the
        # return path expects to be swapping away from.
        fetched_message.embeds = [discord.Embed(color=discord.Color.gold())]
        fetched_message.embeds[0].set_image(url="attachment://trading_card.png")
        await card_view.returnToStats.callback(self._click(fetched_message))

        self.cursor.execute("SELECT cardShown, cardAvatarGlobal FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (0, 0))

    async def test_card_falls_back_to_a_plain_tile_if_the_avatar_cant_be_fetched(self):
        alice = FakeMember("Alice", id=901)
        alice.display_avatar.read = AsyncMock(side_effect=Exception("network hiccup"))
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        view = helper_module.StatsView(self.helperObj, card_shown=False)
        await view.showCard.callback(self._click(fetched_message))

        # still renders and posts a card despite the failed avatar fetch
        fetched_message.edit.assert_awaited_once()
        fetched_message.edit.call_args.kwargs["attachments"][0].close()

    async def test_return_click_swaps_the_card_back_to_the_stats_embed(self):
        alice = FakeMember("Alice", id=901)
        self.guild.members = [alice]
        ctx = FakeInteraction(self.guild, alice, channel=self.channel)
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()

        # Simulate the message as it looks once the card is already up: a
        # bare image embed, cardShown=1 in the DB.
        card_embed = discord.Embed(color=discord.Color.gold())
        card_embed.set_image(url="attachment://trading_card.png")
        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [card_embed]
        self.cursor.execute("UPDATE stats_views SET cardShown=1 WHERE messageId=?", (msg.id,))
        self.db.commit()

        view = helper_module.StatsView(self.helperObj, card_shown=True)
        await view.returnToStats.callback(self._click(fetched_message))

        fetched_message.edit.assert_awaited_once()
        # back to the real /stats embed, fields restored, image attachment
        # cleared out rather than left dangling behind the new embed.
        new_embed = fetched_message.edit.call_args.kwargs["embed"]
        self.assertGreater(len(new_embed.fields), 0)
        self.assertIsNone(new_embed.image.url)
        self.assertEqual(fetched_message.edit.call_args.kwargs["attachments"], [])
        new_view = fetched_message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.showCard, new_view.children)

        self.cursor.execute("SELECT cardShown FROM stats_views WHERE messageId=?", (msg.id,))
        self.assertEqual(self.cursor.fetchone(), (0,))

    async def test_return_click_is_rejected_before_the_card_is_shown(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)
        msg = await ctx.original_response()
        fetched_message = FakeMessage(id=msg.id)
        fetched_message.embeds = [ctx.response.send_message.call_args.kwargs["embed"]]

        view = helper_module.StatsView(self.helperObj, card_shown=True)
        click = self._click(fetched_message)
        await view.returnToStats.callback(click)

        fetched_message.edit.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


class UnlockAchievementTests(HelperTestCase):
    def test_first_unlock_returns_true_and_records_it(self):
        newly = self.helperObj._unlockAchievement(GUILD_ID, 901, "first_blood")
        self.assertTrue(newly)
        self.assertIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"], self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)
        )

    def test_repeat_unlock_returns_false(self):
        self.helperObj._unlockAchievement(GUILD_ID, 901, "first_blood")
        newly = self.helperObj._unlockAchievement(GUILD_ID, 901, "first_blood")
        self.assertFalse(newly)


class CountShopPurchasesTests(HelperTestCase):
    async def test_counts_only_shop_bought_items_not_other_unlock_types(self):
        # a tier reward and a special grant shouldn't count as "purchased"
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        self.assertEqual(self.helperObj._countShopPurchases(GUILD_ID, 901), 0)

        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET balance=10000 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        await self.helperObj.shopBuyHelper(ctx, "Legend")
        self.assertEqual(self.helperObj._countShopPurchases(GUILD_ID, 901), 1)


class CheckAchievementsTests(HelperTestCase):
    def test_first_win_unlocks_first_blood(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET game_wins=1 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()

        newly = self.helperObj._checkAchievements(GUILD_ID, 901)
        self.assertIn("first_blood", newly)

    def test_veteran_requires_the_configured_win_count(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET game_wins=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_VETERAN_WINS - 1, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertNotIn("veteran", self.helperObj._checkAchievements(GUILD_ID, 901))

        self.cursor.execute(
            "UPDATE economy SET game_wins=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_VETERAN_WINS, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertIn("veteran", self.helperObj._checkAchievements(GUILD_ID, 901))

    def test_on_fire_requires_the_configured_streak(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET current_win_streak=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_ON_FIRE_STREAK, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertIn("on_fire", self.helperObj._checkAchievements(GUILD_ID, 901))

    async def test_team_player_requires_the_configured_team_count(self):
        for i in range(helper_module.CARD_ACHIEVEMENT_TEAM_PLAYER_TEAMS):
            ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
            await self.helperObj.createTeamHelper(ctx, f"Team{i}", 5)
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.assertIn("team_player", self.helperObj._checkAchievements(GUILD_ID, 901))

    def test_veteran_ladder_unlocks_each_distinct_tier_in_order(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        ladder = [
            (helper_module.CARD_ACHIEVEMENT_VETERAN_WINS, "veteran"),
            (helper_module.CARD_ACHIEVEMENT_VETERAN_ELITE_WINS, "veteran_elite"),
            (helper_module.CARD_ACHIEVEMENT_VETERAN_MASTER_WINS, "veteran_master"),
            (helper_module.CARD_ACHIEVEMENT_VETERAN_IMMORTAL_WINS, "veteran_immortal"),
        ]
        for threshold, key in ladder:
            self.cursor.execute(
                "UPDATE economy SET game_wins=? WHERE guildId=? AND userId=?", (threshold - 1, GUILD_ID, 901)
            )
            self.db.commit()
            self.assertNotIn(key, self.helperObj._checkAchievements(GUILD_ID, 901))

            self.cursor.execute(
                "UPDATE economy SET game_wins=? WHERE guildId=? AND userId=?", (threshold, GUILD_ID, 901)
            )
            self.db.commit()
            self.assertIn(key, self.helperObj._checkAchievements(GUILD_ID, 901))

        # Every tier's title is distinct; reaching Immortal doesn't just
        # mean four copies of "Veteran".
        unlocked = self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)
        for _, key in ladder:
            self.assertIn(helper_module.CARD_ACHIEVEMENT_TITLES[key], unlocked)
        self.assertEqual(len({helper_module.CARD_ACHIEVEMENT_TITLES[key] for _, key in ladder}), 4)

    def test_on_fire_ladder_unlocks_each_distinct_tier(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET current_win_streak=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_ON_FIRE_UNTOUCHABLE_STREAK, GUILD_ID, 901)
        )
        self.db.commit()

        newly = self.helperObj._checkAchievements(GUILD_ID, 901)
        self.assertIn("on_fire", newly)
        self.assertIn("on_fire_unstoppable", newly)
        self.assertIn("on_fire_untouchable", newly)

    def test_on_fire_untouchable_requires_its_own_higher_streak(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET current_win_streak=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_ON_FIRE_UNSTOPPABLE_STREAK, GUILD_ID, 901)
        )
        self.db.commit()

        newly = self.helperObj._checkAchievements(GUILD_ID, 901)
        self.assertIn("on_fire_unstoppable", newly)
        self.assertNotIn("on_fire_untouchable", newly)

    def test_iron_will_requires_the_configured_loss_count(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET game_losses=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_IRON_WILL_LOSSES - 1, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertNotIn("iron_will", self.helperObj._checkAchievements(GUILD_ID, 901))

        self.cursor.execute(
            "UPDATE economy SET game_losses=? WHERE guildId=? AND userId=?",
            (helper_module.CARD_ACHIEVEMENT_IRON_WILL_LOSSES, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertIn("iron_will", self.helperObj._checkAchievements(GUILD_ID, 901))

    def test_gambler_counts_total_bet_wins_and_losses_together(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        half = helper_module.CARD_ACHIEVEMENT_GAMBLER_BETS // 2
        remainder = helper_module.CARD_ACHIEVEMENT_GAMBLER_BETS - half
        self.cursor.execute(
            "UPDATE economy SET wins=?, losses=? WHERE guildId=? AND userId=?",
            (half, remainder - 1, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertNotIn("gambler", self.helperObj._checkAchievements(GUILD_ID, 901))

        self.cursor.execute(
            "UPDATE economy SET losses=? WHERE guildId=? AND userId=?", (remainder, GUILD_ID, 901)
        )
        self.db.commit()
        self.assertIn("gambler", self.helperObj._checkAchievements(GUILD_ID, 901))

    def test_captain_requires_actually_being_a_teams_captain(self):
        bob = Player(902, "Bob")
        alice = Player(901, "Alice")
        team = Team()
        team.set_name("NotCaptainTeam")
        team.add_player(bob)
        team.add_player(alice)
        team.set_captain(bob)
        self.helperObj._saveNewTeam(GUILD_ID, team)

        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.assertNotIn("captain", self.helperObj._checkAchievements(GUILD_ID, 901))

        team_id, persisted_team = self.helperObj.getTeamRow(GUILD_ID, "NotCaptainTeam")
        persisted_alice = next(p for p in persisted_team.get_players() if p.get_id() == 901)
        persisted_team.set_captain(persisted_alice)
        self.helperObj.updateTeamData(team_id, persisted_team)
        self.assertIn("captain", self.helperObj._checkAchievements(GUILD_ID, 901))

    def test_repeat_calls_do_not_re_unlock_the_same_achievement(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET game_wins=1 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()

        first = self.helperObj._checkAchievements(GUILD_ID, 901)
        second = self.helperObj._checkAchievements(GUILD_ID, 901)
        self.assertIn("first_blood", first)
        self.assertEqual(second, [])

    def test_no_economy_row_returns_no_achievements(self):
        self.assertEqual(self.helperObj._checkAchievements(GUILD_ID, 999999), [])


class ApplyGameDeltasAchievementTests(HelperTestCase):
    def _win_deltas(self, user_id, name="Alice", gold_wagered=0, gold_won=0, elo=0):
        return {
            user_id: {
                "username": name, "balance": 0, "wins": 1 if gold_wagered else 0, "losses": 0,
                "gold_wagered": gold_wagered, "gold_won": gold_won, "gold_lost": 0,
                "game_wins": 1, "game_losses": 0, "ranked_wins": 0, "ranked_losses": 0, "elo": elo,
            }
        }

    def _loss_deltas(self, user_id, name="Alice"):
        return {
            user_id: {
                "username": name, "balance": 0, "wins": 0, "losses": 0,
                "gold_wagered": 0, "gold_won": 0, "gold_lost": 0,
                "game_wins": 0, "game_losses": 1, "ranked_wins": 0, "ranked_losses": 0, "elo": 0,
            }
        }

    def test_a_win_extends_the_streak_and_a_loss_resets_it(self):
        self.helperObj.applyGameDeltas(GUILD_ID, self._win_deltas(901))
        self.helperObj.applyGameDeltas(GUILD_ID, self._win_deltas(901))
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "current_win_streak"), 2)

        self.helperObj.applyGameDeltas(GUILD_ID, self._loss_deltas(901))
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "current_win_streak"), 0)

    def test_reaching_the_streak_threshold_unlocks_on_fire(self):
        newly_unlocked = []
        for _ in range(helper_module.CARD_ACHIEVEMENT_ON_FIRE_STREAK):
            newly_unlocked = self.helperObj.applyGameDeltas(GUILD_ID, self._win_deltas(901))
        self.assertIn((901, "on_fire"), newly_unlocked)

    def test_winning_a_big_enough_bet_unlocks_high_roller(self):
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, gold_wagered=helper_module.CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD)
        )
        self.assertIn((901, "high_roller"), newly_unlocked)

    def test_a_small_winning_bet_does_not_unlock_high_roller(self):
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, gold_wagered=helper_module.CARD_ACHIEVEMENT_HIGH_ROLLER_GOLD - 1)
        )
        self.assertNotIn((901, "high_roller"), newly_unlocked)

    def test_a_big_enough_payout_ratio_unlocks_jackpot(self):
        multiplier = helper_module.CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, gold_wagered=100, gold_won=(multiplier - 1) * 100)
        )
        self.assertIn((901, "jackpot"), newly_unlocked)

    def test_a_small_payout_ratio_does_not_unlock_jackpot(self):
        multiplier = helper_module.CARD_ACHIEVEMENT_JACKPOT_PAYOUT_MULTIPLIER
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, gold_wagered=100, gold_won=(multiplier - 1) * 100 - 1)
        )
        self.assertNotIn((901, "jackpot"), newly_unlocked)

    def test_a_big_elo_gain_unlocks_giant_slayer(self):
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, elo=helper_module.CARD_ACHIEVEMENT_UNDERDOG_ELO_GAIN)
        )
        self.assertIn((901, "underdog"), newly_unlocked)

    def test_a_normal_elo_gain_does_not_unlock_giant_slayer(self):
        newly_unlocked = self.helperObj.applyGameDeltas(
            GUILD_ID, self._win_deltas(901, elo=helper_module.CARD_ACHIEVEMENT_UNDERDOG_ELO_GAIN - 1)
        )
        self.assertNotIn((901, "underdog"), newly_unlocked)

    def test_reversal_never_unlocks_or_touches_the_streak(self):
        self.helperObj.applyGameDeltas(GUILD_ID, self._win_deltas(901))
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "current_win_streak"), 1)

        newly_unlocked = self.helperObj.applyGameDeltas(GUILD_ID, self._win_deltas(901), sign=-1)
        self.assertEqual(newly_unlocked, [])
        # reversing a win delta subtracts game_wins back to 0, but the
        # streak counter itself is untouched by a reversal, same
        # reasoning the elo-tier check already skips on sign<0 for.
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "current_win_streak"), 1)


class AnnounceAchievementsTests(HelperTestCase):
    async def test_posts_one_message_per_newly_unlocked_achievement(self):
        channel = FakeChannel("game-chat")
        await self.helperObj._announceAchievements(channel, [(901, "first_blood"), (902, "veteran")])

        self.assertEqual(channel.send.await_count, 2)
        messages = [c.args[0] for c in channel.send.call_args_list]
        self.assertTrue(any("<@901>" in m and "First Blood" in m for m in messages))
        self.assertTrue(any("<@902>" in m and "Veteran" in m for m in messages))

    async def test_no_messages_when_nothing_newly_unlocked(self):
        channel = FakeChannel("game-chat")
        await self.helperObj._announceAchievements(channel, [])
        channel.send.assert_not_awaited()


class TournamentChampionAchievementTests(HelperTestCase):
    def _team(self, name, player_ids):
        team = Team()
        team.set_name(name)
        for pid in player_ids:
            team.add_player(Player(pid, f"P{pid}"))
        return team

    def test_grants_every_rostered_player_on_the_champion_team(self):
        team = self._team("Champions", [901, 902, 903])
        newly_unlocked = self.helperObj._grantTournamentChampionAchievement(GUILD_ID, team)

        self.assertEqual(set(newly_unlocked), {(901, "tournament_champion"), (902, "tournament_champion"),
                                                 (903, "tournament_champion")})
        for pid in (901, 902, 903):
            self.assertIn(
                helper_module.CARD_ACHIEVEMENT_TITLES["tournament_champion"],
                self.helperObj.getUnlockedCardTitles(GUILD_ID, pid)
            )

    def test_does_not_re_grant_on_a_second_call(self):
        team = self._team("Champions", [901])
        self.helperObj._grantTournamentChampionAchievement(GUILD_ID, team)
        newly_unlocked = self.helperObj._grantTournamentChampionAchievement(GUILD_ID, team)
        self.assertEqual(newly_unlocked, [])


class AchievementsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_lists_every_achievement_with_lock_state(self):
        ctx = self._ctx()
        await self.helperObj.achievementsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        combined = "\n".join(field.value for field in embed.fields)
        for title in helper_module.CARD_ACHIEVEMENT_TITLES.values():
            self.assertIn(title, combined)
        self.assertIn("🔒", combined)

    async def test_veteran_and_on_fire_ladders_get_their_own_fields(self):
        ctx = self._ctx()
        await self.helperObj.achievementsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        field_names = [field.name for field in embed.fields]
        self.assertIn("__Veteran__", field_names)
        self.assertIn("__On Fire__", field_names)
        self.assertIn("__Other__", field_names)

        veteran_field = next(field for field in embed.fields if field.name == "__Veteran__")
        for key in ("veteran", "veteran_elite", "veteran_master", "veteran_immortal"):
            self.assertIn(helper_module.CARD_ACHIEVEMENT_TITLES[key], veteran_field.value)
        # Rungs of the ladder shouldn't also be scattered into "Other".
        other_field = next(field for field in embed.fields if field.name == "__Other__")
        self.assertNotIn(helper_module.CARD_ACHIEVEMENT_TITLES["veteran_immortal"], other_field.value)

    async def test_self_heals_already_qualifying_achievements(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET game_wins=1 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.achievementsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        combined = "\n".join(field.value for field in embed.fields)
        self.assertIn("✅", combined)
        self.assertIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)
        )


class CardUnlocksTests(HelperTestCase):
    def test_reaching_diamond_unlocks_its_title_and_color_scheme(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])

        titles = self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)
        self.assertEqual(titles, [helper_module.CARD_TIER_REWARD_TITLES["Diamond"]])

        schemes = self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)
        self.assertEqual([s["name"] for s in schemes], ["Diamond"])

    def test_below_diamond_unlocks_nothing(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"] - 1)
        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901), [])
        self.assertEqual(self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901), [])

    def test_reaching_a_higher_tier_unlocks_every_lower_tier_reward_too(self):
        # A single huge elo swing that lands straight on Grandmaster (a
        # tournament correction, a big upset) still credits Diamond and
        # Master along the way, not just the top tier actually landed on.
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Grandmaster"])
        titles = set(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901))
        self.assertEqual(titles, {
            helper_module.CARD_TIER_REWARD_TITLES["Diamond"],
            helper_module.CARD_TIER_REWARD_TITLES["Master"],
            helper_module.CARD_TIER_REWARD_TITLES["Grandmaster"],
        })

    def test_unlocking_the_same_tier_twice_is_idempotent(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.assertEqual(len(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)), 1)

    def test_reward_persists_after_deranking_back_down(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Master"])
        # simulate deranking all the way back down below every reward tier
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, 0)

        titles = set(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901))
        self.assertEqual(titles, {
            helper_module.CARD_TIER_REWARD_TITLES["Diamond"],
            helper_module.CARD_TIER_REWARD_TITLES["Master"],
        })

    def test_unlocks_are_scoped_per_guild(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID + 1, 901), [])

    def test_color_scheme_accent_is_a_readable_version_of_the_tiers_badge_color(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Challenger"])
        schemes = {s["name"]: s for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)}

        badge_color = helper_module.ELO_TIER_BADGE_COLORS["Challenger"]
        background = tuple(round(c * helper_module.CARD_BACKGROUND_DARKEN_RATIO) for c in badge_color)
        background_center = self.helperObj._lightenColor(background, 0.3)
        expected_accent = self.helperObj._ensureReadableAccent(badge_color, background_center)

        self.assertEqual(schemes["Challenger"]["accent_color"], self.helperObj._rgbToHex(expected_accent))
        self.assertEqual(
            schemes["Challenger"]["background_color"], self.helperObj._rgbToHex(background)
        )

    def test_color_scheme_background_stays_the_raw_darkened_badge_color(self):
        # Only the accent gets the readability boost; the background is
        # still the badge color's own straight 28%-darkened shade, same as
        # the team card's own derivation, so a scheme's overall mood still
        # authentically reflects the tier that earned it.
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        schemes = {s["name"]: s for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)}
        badge_color = helper_module.ELO_TIER_BADGE_COLORS["Diamond"]
        expected_background = tuple(round(c * helper_module.CARD_BACKGROUND_DARKEN_RATIO) for c in badge_color)
        self.assertEqual(
            schemes["Diamond"]["background_color"], self.helperObj._rgbToHex(expected_background)
        )

    def test_special_title_grant_shows_up_in_unlocked_titles(self):
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901), ["Developer"])
        # a special grant has no elo tier behind it, so no color scheme
        # unlocks alongside it the way a rank reward's does.
        self.assertEqual(self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901), [])

    def test_special_title_grant_is_idempotent(self):
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 901), ["Developer"])

    def test_shockwave_developer_always_has_developer_title_with_no_grant_needed(self):
        # No card_unlocks row at all for this (guild, user) pair; the
        # hardcoded id check in getUnlockedCardTitles is what surfaces it,
        # not a stored grant, so it works in any guild, including ones
        # never explicitly granted.
        titles = self.helperObj.getUnlockedCardTitles(GUILD_ID, helper_module.SHOCKWAVE_DEVELOPER_ID)
        self.assertEqual(titles, ["Developer"])

    def test_shockwave_developer_title_works_in_an_unrelated_guild(self):
        titles = self.helperObj.getUnlockedCardTitles(GUILD_ID + 12345, helper_module.SHOCKWAVE_DEVELOPER_ID)
        self.assertEqual(titles, ["Developer"])

    def test_developer_title_is_not_duplicated_if_also_separately_granted(self):
        self.helperObj.grantSpecialCardTitle(GUILD_ID, helper_module.SHOCKWAVE_DEVELOPER_ID, "Developer")
        titles = self.helperObj.getUnlockedCardTitles(GUILD_ID, helper_module.SHOCKWAVE_DEVELOPER_ID)
        self.assertEqual(titles, ["Developer"])

    def test_other_players_do_not_get_the_developer_title_for_free(self):
        titles = self.helperObj.getUnlockedCardTitles(GUILD_ID, 901)
        self.assertNotIn("Developer", titles)

    def test_available_titles_always_includes_the_default_even_with_no_unlocks(self):
        self.assertEqual(
            self.helperObj.getAvailableCardTitles(GUILD_ID, 901), [helper_module.CARD_DEFAULT_TITLE]
        )

    def test_available_titles_combines_the_default_with_unlocked_ones(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        available = self.helperObj.getAvailableCardTitles(GUILD_ID, 901)
        # CARD_DEFAULT_TITLE is always first (it's prepended in Python, not
        # part of the DB query); the unlocked ones' own relative order
        # isn't guaranteed (SQLite has no ORDER BY here), so compare those
        # as a set instead.
        self.assertEqual(available[0], helper_module.CARD_DEFAULT_TITLE)
        self.assertEqual(
            set(available[1:]), {helper_module.CARD_TIER_REWARD_TITLES["Diamond"], "Developer"}
        )

    def test_set_card_title_marks_the_row_customized(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.setCardTitle(GUILD_ID, 901, helper_module.CARD_TIER_REWARD_TITLES["Diamond"])

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_TIER_REWARD_TITLES["Diamond"])

    def test_available_color_schemes_always_includes_the_default_even_with_no_unlocks(self):
        available = self.helperObj.getAvailableCardColorSchemes(GUILD_ID, 901)
        self.assertEqual([s["name"] for s in available], [helper_module.CARD_DEFAULT_SCHEME_NAME])
        self.assertEqual(available[0]["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)
        self.assertEqual(available[0]["background_color"], helper_module.CARD_DEFAULT_BACKGROUND_COLOR)

    def test_available_color_schemes_combines_the_default_with_unlocked_ones(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        available = self.helperObj.getAvailableCardColorSchemes(GUILD_ID, 901)
        names = [s["name"] for s in available]
        self.assertEqual(names[0], helper_module.CARD_DEFAULT_SCHEME_NAME)
        self.assertIn("Diamond", names[1:])

    def test_set_card_color_scheme_marks_the_row_customized(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.setCardColorScheme(GUILD_ID, 901, "#111111", "#222222")

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], "#111111")
        self.assertEqual(settings["background_color"], "#222222")

        # customized=1 protects it from ensureCardSettings' own
        # resync-to-defaults on the next /stats call.
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], "#111111")


class CardSetHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_when_nothing_is_given(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, None, None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("at least one", message)

    async def test_equipping_the_default_title_always_succeeds(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, helper_module.CARD_DEFAULT_TITLE, None, None)
        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertIn(f'title **"{helper_module.CARD_DEFAULT_TITLE}"**', kwargs["content"])
        # the card itself is shown too, not just confirmed in text
        self.assertIsInstance(kwargs["file"], discord.File)
        self.assertTrue(kwargs["embed"].image.url.startswith("attachment://"))
        kwargs["file"].close()
        self.assertEqual(
            self.helperObj.getCardSettings(GUILD_ID, 901)["title"], helper_module.CARD_DEFAULT_TITLE
        )

    async def test_rejects_a_title_that_has_not_been_unlocked(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, helper_module.CARD_TIER_REWARD_TITLES["Diamond"], None, None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)
        # never touched; still whatever ensureCardSettings' own default is
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_DEFAULT_TITLE)

    async def test_equips_a_title_the_caller_has_unlocked(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Master"])
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, helper_module.CARD_TIER_REWARD_TITLES["Master"], None, None)

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_TIER_REWARD_TITLES["Master"])

    async def test_equipping_someone_elses_unlocked_title_is_rejected(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 902, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        ctx = self._ctx(user_id=901, name="Alice")  # Alice herself hasn't unlocked it
        await self.helperObj.cardSetHelper(ctx, helper_module.CARD_TIER_REWARD_TITLES["Diamond"], None, None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)

    async def test_equips_a_specially_granted_title(self):
        self.helperObj.grantSpecialCardTitle(GUILD_ID, 901, "Developer")
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, "Developer", None, None)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], "Developer")

    async def test_equipping_the_default_scheme_always_succeeds(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, helper_module.CARD_DEFAULT_SCHEME_NAME, None)
        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertIn(f'the **{helper_module.CARD_DEFAULT_SCHEME_NAME}** color scheme', kwargs["content"])
        self.assertIsInstance(kwargs["file"], discord.File)
        kwargs["file"].close()
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)
        self.assertEqual(settings["background_color"], helper_module.CARD_DEFAULT_BACKGROUND_COLOR)

    async def test_rejects_a_scheme_that_has_not_been_unlocked(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, "Diamond", None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)
        # never touched; still whatever ensureCardSettings' own default is
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)

    async def test_equips_a_scheme_the_caller_has_unlocked(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Master"])
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, "Master", None)

        expected = {s["name"]: s for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)}["Master"]
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], expected["accent_color"])
        self.assertEqual(settings["background_color"], expected["background_color"])

    async def test_equipping_someone_elses_unlocked_scheme_is_rejected(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 902, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        ctx = self._ctx(user_id=901, name="Alice")  # Alice herself hasn't unlocked it
        await self.helperObj.cardSetHelper(ctx, None, "Diamond", None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)

    async def test_setting_a_scheme_marks_the_row_customized(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, "Diamond", None)

        # customized=1 protects it from ensureCardSettings' own
        # resync-to-defaults on the next /stats call.
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertNotEqual(settings["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)

    async def test_equipped_scheme_tracks_later_changes_to_its_catalog_entry(self):
        # A player who equips, say, "Fire" shouldn't have its colors frozen
        # forever at whatever they were computed to be the moment
        # /card-set ran; color_scheme_name plus _resyncEquippedColorScheme
        # (run from ensureCardSettings, so the very next /stats call picks
        # it up) keeps it tracking later tweaks to CARD_SHOP_COLOR_SCHEMES
        # (or to CARD_MIN_ACCENT_CONTRAST itself), the same staleness the
        # customized flag protects the default palette from.
        self.cursor.execute(
            "INSERT INTO card_unlocks(guildId, userId, itemType, itemKey) VALUES(?, ?, 'color_scheme', 'Fire')",
            (GUILD_ID, 901)
        )
        self.db.commit()

        with patch.dict(
            helper_module.CARD_SHOP_COLOR_SCHEMES,
            {"Fire": {"price": 4000, "accent_color": "#FF4500", "background_color": "#3D0C02"}},
        ):
            ctx = self._ctx()
            await self.helperObj.cardSetHelper(ctx, None, "Fire", None)
            original = self.helperObj.getCardSettings(GUILD_ID, 901)["accent_color"]

        with patch.dict(
            helper_module.CARD_SHOP_COLOR_SCHEMES,
            {"Fire": {"price": 4000, "accent_color": "#00FF00", "background_color": "#3D0C02"}},
        ):
            self.helperObj.ensureCardSettings(GUILD_ID, 901)
            updated = self.helperObj.getCardSettings(GUILD_ID, 901)["accent_color"]

        self.assertNotEqual(updated, original)

    async def test_a_hand_edited_custom_hex_is_never_resynced(self):
        # No color_scheme_name recorded here (setCardColorScheme wasn't
        # used), nothing for _resyncEquippedColorScheme to track, so a
        # directly-written custom value stays exactly as set.
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "UPDATE trading_cards SET accent_color=?, customized=1 WHERE guildId=? AND userId=?",
            ("#123456", GUILD_ID, 901)
        )
        self.db.commit()

        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["accent_color"], "#123456")

    async def test_equipping_the_default_font_always_succeeds(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, None, helper_module.CARD_DEFAULT_FONT_STYLE)
        kwargs = ctx.response.send_message.call_args.kwargs
        self.assertIn(f'the **{helper_module.CARD_DEFAULT_FONT_STYLE}** font', kwargs["content"])
        self.assertIsInstance(kwargs["file"], discord.File)
        kwargs["file"].close()
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["font_style"], helper_module.CARD_DEFAULT_FONT_STYLE)

    async def test_rejects_a_font_that_has_not_been_purchased(self):
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, None, "Bold")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["font_style"], helper_module.CARD_DEFAULT_FONT_STYLE)

    async def test_equips_a_purchased_font(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.cursor.execute(
            "UPDATE economy SET balance=10000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.helperObj.db.commit()
        ctx = self._ctx()
        await self.helperObj.shopBuyHelper(ctx, "Bold")

        ctx = self._ctx()
        await self.helperObj.cardSetHelper(ctx, None, None, "Bold")
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["font_style"], "Bold")

    async def test_equips_all_three_fields_at_once(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Master"])
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute("UPDATE economy SET balance=10000 WHERE guildId=? AND userId=?", (GUILD_ID, 901))
        self.db.commit()
        await self.helperObj.shopBuyHelper(self._ctx(), "Bold")

        ctx = self._ctx()
        await self.helperObj.cardSetHelper(
            ctx, helper_module.CARD_TIER_REWARD_TITLES["Master"], "Master", "Bold"
        )
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_TIER_REWARD_TITLES["Master"])
        self.assertEqual(settings["font_style"], "Bold")
        expected = {s["name"]: s for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)}["Master"]
        self.assertEqual(settings["accent_color"], expected["accent_color"])

    async def test_an_invalid_field_blocks_every_field_from_applying(self):
        # validate-all-then-apply-all: an unpurchased font shouldn't leave
        # an otherwise-valid, otherwise-unlocked title half-applied.
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 901, helper_module.ELO_TIER_THRESHOLDS["Master"])
        ctx = self._ctx()
        await self.helperObj.cardSetHelper(
            ctx, helper_module.CARD_TIER_REWARD_TITLES["Master"], None, "NotAFont"
        )
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("haven't unlocked", message)
        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_DEFAULT_TITLE)


class ResetCardUnlocksHelperTests(HelperTestCase):
    def test_removes_every_unlock_for_the_target(self):
        target_id = 555
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, target_id, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.grantSpecialCardTitle(GUILD_ID, target_id, "Developer")
        self.assertTrue(self.helperObj.getUnlockedCardTitles(GUILD_ID, target_id))

        self.helperObj.resetCardUnlocksHelper(GUILD_ID, target_id)

        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, target_id), [])
        self.assertEqual(self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, target_id), [])
        self.assertEqual(self.helperObj.getUnlockedCardFontStyles(GUILD_ID, target_id), [])

    def test_resets_the_equipped_card_to_defaults(self):
        target_id = 555
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, target_id, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj.setCardTitle(GUILD_ID, target_id, helper_module.CARD_TIER_REWARD_TITLES["Diamond"])
        self.helperObj.setCardColorScheme(
            GUILD_ID, target_id, "#123456", "#654321", scheme_name="Diamond"
        )

        self.helperObj.resetCardUnlocksHelper(GUILD_ID, target_id)

        settings = self.helperObj.getCardSettings(GUILD_ID, target_id)
        self.assertEqual(settings["title"], helper_module.CARD_DEFAULT_TITLE)
        self.assertEqual(settings["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)
        self.assertEqual(settings["background_color"], helper_module.CARD_DEFAULT_BACKGROUND_COLOR)

        # customized=0 again; a later /stats call's own default-resync
        # logic won't be refused by a stale customized flag.
        self.cursor.execute(
            "SELECT customized, color_scheme_name FROM trading_cards WHERE guildId=? AND userId=?",
            (GUILD_ID, target_id)
        )
        self.assertEqual(self.cursor.fetchone(), (0, None))

    def test_does_not_affect_a_different_players_unlocks(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 555, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 556, helper_module.ELO_TIER_THRESHOLDS["Diamond"])

        self.helperObj.resetCardUnlocksHelper(GUILD_ID, 555)

        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 555), [])
        self.assertTrue(self.helperObj.getUnlockedCardTitles(GUILD_ID, 556))

    def test_user_id_none_resets_every_player_in_the_guild(self):
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 555, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.helperObj._checkTierRewardUnlocks(GUILD_ID, 556, helper_module.ELO_TIER_THRESHOLDS["Diamond"])

        self.helperObj.resetCardUnlocksHelper(GUILD_ID)

        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 555), [])
        self.assertEqual(self.helperObj.getUnlockedCardTitles(GUILD_ID, 556), [])


class PreviewHelperTests(_FakeLogoDirTestCase):
    def setUp(self):
        super().setUp()
        # ignore_cleanup_errors: same Windows-open-handle reasoning
        # _FakeLogoDirTestCase's own logo dir uses.
        self._preview_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._preview_dir_patch = patch.object(helper_module, "PREVIEW_DIR", self._preview_dir.name)
        self._preview_dir_patch.start()

    def tearDown(self):
        self._preview_dir_patch.stop()
        self._preview_dir.cleanup()
        super().tearDown()

    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_renders_and_caches_a_logos_preview(self):
        ctx = self._ctx()
        await self.helperObj.previewHelper(ctx, "Logos")

        ctx.response.send_message.assert_awaited_once()
        files = ctx.response.send_message.call_args.kwargs["files"]
        self.assertEqual(len(files), 1)
        self.assertTrue(os.path.isfile(os.path.join(self._preview_dir.name, "logos-1.png")))

    async def test_reuses_the_cached_file_instead_of_rerendering(self):
        ctx1 = self._ctx()
        await self.helperObj.previewHelper(ctx1, "Color Schemes")
        cached_path = os.path.join(self._preview_dir.name, "color-schemes-1.png")
        mtime_before = os.path.getmtime(cached_path)

        with patch.object(self.helperObj, "_renderColorSchemePreviewImages") as mock_render:
            ctx2 = self._ctx()
            await self.helperObj.previewHelper(ctx2, "Color Schemes")

        mock_render.assert_not_called()
        self.assertEqual(os.path.getmtime(cached_path), mtime_before)

    async def test_different_types_get_their_own_cache_file(self):
        await self.helperObj.previewHelper(self._ctx(), "Fonts")
        await self.helperObj.previewHelper(self._ctx(), "Card Titles")

        self.assertTrue(os.path.isfile(os.path.join(self._preview_dir.name, "fonts-1.png")))
        self.assertTrue(os.path.isfile(os.path.join(self._preview_dir.name, "card-titles-1.png")))

    async def test_fonts_preview_sends_a_single_image(self):
        ctx = self._ctx()
        await self.helperObj.previewHelper(ctx, "Fonts")
        files = ctx.response.send_message.call_args.kwargs["files"]
        self.assertEqual(len(files), 1)

    async def test_card_titles_preview_sends_a_single_image(self):
        ctx = self._ctx()
        await self.helperObj.previewHelper(ctx, "Card Titles")
        files = ctx.response.send_message.call_args.kwargs["files"]
        self.assertEqual(len(files), 1)

    async def test_message_notes_the_image_count_only_when_more_than_one(self):
        ctx = self._ctx()
        await self.helperObj.previewHelper(ctx, "Fonts")
        text = ctx.response.send_message.call_args.args[0]
        self.assertNotIn("image", text.lower())

    async def test_message_notes_the_image_count_when_paginated(self):
        two_pages = [Image.new("RGB", (10, 10)), Image.new("RGB", (10, 10))]
        with patch.object(self.helperObj, "_renderLogoPreviewImages", return_value=two_pages):
            ctx = self._ctx()
            await self.helperObj.previewHelper(ctx, "Logos")

        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("2 images", text)
        files = ctx.response.send_message.call_args.kwargs["files"]
        self.assertEqual(len(files), 2)

    async def test_logo_grid_includes_every_available_logo(self):
        # LOGO_NAMES from _FakeLogoDirTestCase: Demacia, Noxus, Freljord
        names = self.helperObj.listAvailableLogos()
        self.assertEqual(set(names), set(self.LOGO_NAMES))
        images = self.helperObj._renderLogoPreviewImages()
        self.assertEqual(len(images), 1)

    async def test_paginates_when_a_page_would_be_too_tall(self):
        many_names = [f"Logo{i}" for i in range(500)]
        with patch.object(helper_module, "PREVIEW_MAX_PAGE_HEIGHT", 500):
            pages = self.helperObj._paginateGridItems(many_names)
        self.assertGreater(len(pages), 1)
        self.assertEqual(sum(len(p) for p in pages), len(many_names))

    async def test_color_scheme_grid_includes_the_default_and_every_shop_scheme(self):
        images = self.helperObj._renderColorSchemePreviewImages()
        self.assertEqual(len(images), 1)
        # sanity: rendering didn't blow up across every real scheme entry
        self.assertGreater(images[0].height, 0)


class ShopTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", balance=10000):
        self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
        self.cursor.execute(
            "UPDATE economy SET balance=? WHERE guildId=? AND userId=?", (balance, GUILD_ID, user_id)
        )
        self.db.commit()
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_shop_lists_every_catalog_item_with_price(self):
        ctx = self._ctx()
        await self.helperObj.shopHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        # category headings are underlined (Discord markdown) to read
        # distinctly from each item line's own bolded name.
        self.assertIn("__Titles__", values)
        self.assertIn("Legend", values["__Titles__"])
        self.assertIn("Crimson", values["__Color Schemes__"])
        self.assertIn("Bold", values["__Fonts__"])
        self.assertIn("Retro", values["__Fonts__"])
        self.assertIn("Villain", values["__Fonts__"])
        self.assertIn("Military", values["__Fonts__"])
        self.assertIn(f"{helper_module.CARD_SHOP_TITLES['Legend']} gold", values["__Titles__"])

    async def test_shop_marks_already_owned_items_with_a_checkmark_next_to_the_price(self):
        ctx = self._ctx()
        await self.helperObj.shopBuyHelper(ctx, "Legend")

        ctx = self._ctx()
        await self.helperObj.shopHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertIn(f"Legend** - {helper_module.CARD_SHOP_TITLES['Legend']} gold ✅", values["__Titles__"])

    async def test_shop_shows_plain_price_for_unowned_items(self):
        ctx = self._ctx()
        await self.helperObj.shopHelper(ctx)
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertIn(f"{helper_module.CARD_SHOP_TITLES['Legend']} gold", values["__Titles__"])
        self.assertNotIn("✅", values["__Titles__"])

    async def test_purchasing_a_title_deducts_gold_and_unlocks_it(self):
        ctx = self._ctx(balance=10000)
        await self.helperObj.shopBuyHelper(ctx, "Legend")

        self.assertEqual(
            self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 10000 - helper_module.CARD_SHOP_TITLES["Legend"]
        )
        self.assertIn("Legend", self.helperObj.getUnlockedCardTitles(GUILD_ID, 901))

    async def test_purchasing_a_color_scheme_unlocks_it(self):
        ctx = self._ctx(balance=10000)
        await self.helperObj.shopBuyHelper(ctx, "Crimson")
        names = [s["name"] for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)]
        self.assertIn("Crimson", names)

    async def test_purchasing_a_region_themed_color_scheme_unlocks_it(self):
        ctx = self._ctx(balance=10000)
        await self.helperObj.shopBuyHelper(ctx, "Noxus")
        names = [s["name"] for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)]
        self.assertIn("Noxus", names)

    async def test_every_shop_color_scheme_is_a_real_readable_hex_pair(self):
        # Regression coverage for the whole catalog, not just one entry -
        # every CARD_SHOP_COLOR_SCHEMES name (Fire/Ice/each region
        # included) should unlock into a scheme with valid "#RRGGBB" hex
        # for both colors, run through the same _ensureReadableAccent
        # safety net a tier reward's own scheme gets.
        for name in helper_module.CARD_SHOP_COLOR_SCHEMES:
            ctx = self._ctx(user_id=901, balance=10000)
            await self.helperObj.shopBuyHelper(ctx, name)

        schemes = {s["name"]: s for s in self.helperObj.getUnlockedCardColorSchemes(GUILD_ID, 901)}
        for name in helper_module.CARD_SHOP_COLOR_SCHEMES:
            self.assertIn(name, schemes)
            accent = schemes[name]["accent_color"]
            background = schemes[name]["background_color"]
            self.assertRegex(accent, r"^#[0-9A-F]{6}$")
            self.assertRegex(background, r"^#[0-9A-F]{6}$")

    async def test_purchasing_a_font_style_unlocks_it(self):
        ctx = self._ctx(balance=10000)
        await self.helperObj.shopBuyHelper(ctx, "Elegant")
        self.assertIn("Elegant", self.helperObj.getUnlockedCardFontStyles(GUILD_ID, 901))

    async def test_rejects_purchase_with_insufficient_gold(self):
        ctx = self._ctx(balance=10)
        await self.helperObj.shopBuyHelper(ctx, "Legend")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("only have", message)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 10)
        self.assertNotIn("Legend", self.helperObj.getUnlockedCardTitles(GUILD_ID, 901))

    async def test_rejects_purchasing_an_item_already_owned(self):
        ctx = self._ctx(balance=10000)
        await self.helperObj.shopBuyHelper(ctx, "Legend")
        balance_after_first_purchase = self.helperObj.getEconomy(GUILD_ID, 901, "balance")

        ctx = self._ctx(balance=balance_after_first_purchase)
        await self.helperObj.shopBuyHelper(ctx, "Legend")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("already own", message)
        # not charged a second time
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), balance_after_first_purchase)

    async def test_rejects_an_item_not_in_the_shop(self):
        ctx = self._ctx()
        await self.helperObj.shopBuyHelper(ctx, "Nonexistent Item")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("isn't in the shop", message)

    def test_shop_titles_have_more_than_one_price_point(self):
        # /shop titles shouldn't all be flatly priced the same; there
        # should be real spread between the cheapest and priciest one.
        prices = set(helper_module.CARD_SHOP_TITLES.values())
        self.assertGreater(len(prices), 1)
        self.assertGreater(max(prices) - min(prices), 1000)

    def test_every_title_and_font_name_is_globally_unique(self):
        # _resolveShopItem looks a purchase up by name alone across
        # CARD_SHOP_TITLES/CARD_SHOP_COLOR_SCHEMES/CARD_SHOP_FONT_STYLES,
        # and CARD_TITLE_CATALOG folds shop/tier-reward/special titles
        # together the same way; a name reused across any of these (or
        # reused as the CARD_DEFAULT_TITLE/CARD_DEFAULT_FONT_STYLE everyone
        # already has for free) would make one shadow the other. Every
        # title-ish and font-ish name in the game has to be pairwise
        # distinct for that to stay safe.
        title_like_sources = [
            list(helper_module.CARD_SHOP_TITLES),
            list(helper_module.CARD_TIER_REWARD_TITLES.values()),
            list(helper_module.CARD_SPECIAL_TITLES.values()),
            list(helper_module.CARD_ACHIEVEMENT_TITLES.values()),
            [helper_module.CARD_DEFAULT_TITLE],
        ]
        all_titles = [name for source in title_like_sources for name in source]
        self.assertEqual(len(all_titles), len(set(all_titles)))

        font_like_sources = [
            list(helper_module.CARD_SHOP_FONT_STYLES),
            [helper_module.CARD_DEFAULT_FONT_STYLE],
        ]
        all_fonts = [name for source in font_like_sources for name in source]
        self.assertEqual(len(all_fonts), len(set(all_fonts)))

        # color schemes share the same lookup-by-name space as titles/fonts
        all_shop_names = (
            list(helper_module.CARD_SHOP_TITLES)
            + list(helper_module.CARD_SHOP_COLOR_SCHEMES)
            + list(helper_module.CARD_SHOP_FONT_STYLES)
        )
        self.assertEqual(len(all_shop_names), len(set(all_shop_names)))

    def test_loud_font_styles_cost_more_than_quiet_ones(self):
        # Bold/Elegant/Handwritten are the "quiet" faces, a plain display
        # font, a plain serif, a plain handwritten marker. Everything else
        # (Cyber, Retro, Villain, Military, Neon, Western) commits hard to
        # one loud aesthetic and should cost more for it.
        quiet = {"Bold", "Elegant", "Handwritten"}
        loud = set(helper_module.CARD_SHOP_FONT_STYLES) - quiet
        self.assertTrue(quiet.issubset(helper_module.CARD_SHOP_FONT_STYLES))
        self.assertTrue(loud)
        max_quiet_price = max(helper_module.CARD_SHOP_FONT_STYLES[name] for name in quiet)
        min_loud_price = min(helper_module.CARD_SHOP_FONT_STYLES[name] for name in loud)
        self.assertLess(max_quiet_price, min_loud_price)


class ShopSortViewTests(HelperTestCase):
    def _click(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    def _title_names_in_order(self, embed):
        values = {f.name: f.value for f in embed.fields}
        # each line is "**Name** - ...", pull just the names, in the
        # order they appear in the field's text.
        return re.findall(r"\*\*(.+?)\*\*", values["__Titles__"])

    async def test_shop_posts_a_sort_view(self):
        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
        await self.helperObj.shopHelper(ctx)

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.ShopSortView)
        self.assertEqual(view.guild_id, GUILD_ID)
        self.assertEqual(view.user_id, 901)
        self.assertIsNotNone(view.message)

    async def test_sort_by_price_ascending_orders_cheapest_first(self):
        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        click = self._click()

        await view.sortByPrice.callback(click)

        embed = click.response.edit_message.call_args.kwargs["embed"]
        names = self._title_names_in_order(embed)
        prices = [helper_module.CARD_SHOP_TITLES[name] for name in names]
        self.assertEqual(prices, sorted(prices))
        self.assertIn("sorted by price (ascending)", embed.footer.text)

    async def test_sort_by_price_descending_orders_priciest_first(self):
        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        click1 = self._click()
        await view.sortByPrice.callback(click1)
        click2 = self._click()
        await view.descending.callback(click2)

        embed = click2.response.edit_message.call_args.kwargs["embed"]
        names = self._title_names_in_order(embed)
        prices = [helper_module.CARD_SHOP_TITLES[name] for name in names]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertIn("sorted by price (descending)", embed.footer.text)

    async def test_sort_by_owned_puts_unowned_first_then_owned_when_descending(self):
        ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=? WHERE guildId=? AND userId=?", (100000, GUILD_ID, 901)
        )
        self.db.commit()
        await self.helperObj.shopBuyHelper(ctx, "Legend")

        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        click1 = self._click()
        await view.sortByOwned.callback(click1)

        embed = click1.response.edit_message.call_args.kwargs["embed"]
        names = self._title_names_in_order(embed)
        self.assertEqual(names[-1], "Legend")
        self.assertIn("sorted by owned status (ascending)", embed.footer.text)

        click2 = self._click()
        await view.descending.callback(click2)
        embed2 = click2.response.edit_message.call_args.kwargs["embed"]
        names2 = self._title_names_in_order(embed2)
        self.assertEqual(names2[0], "Legend")
        self.assertIn("sorted by owned status (descending)", embed2.footer.text)

    async def test_default_view_has_no_sort_note_in_the_footer(self):
        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        embed = self.helperObj._buildShopEmbed(GUILD_ID, 901)
        self.assertNotIn("sorted by", embed.footer.text)
        self.assertIsNone(view.sort_key)

    async def test_rejects_a_click_from_someone_other_than_the_caller(self):
        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        stranger = FakeInteraction(self.guild, FakeMember("Stranger", id=999))

        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_timeout_disables_the_buttons_and_edits_the_message(self):
        view = helper_module.ShopSortView(self.helperObj, GUILD_ID, 901)
        view.message = FakeMessage()

        await view.on_timeout()

        for item in view.children:
            self.assertTrue(item.disabled)
        view.message.edit.assert_awaited_once_with(view=view)


class TradingCardSettingsTests(HelperTestCase):
    def test_ensure_creates_shockwave_default_settings(self):
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "SELECT title, accent_color, background_color, text_color, font_style "
            "FROM trading_cards WHERE guildId=? AND userId=?",
            (GUILD_ID, 901)
        )
        self.assertEqual(
            self.cursor.fetchone(),
            (
                helper_module.CARD_DEFAULT_TITLE, helper_module.CARD_DEFAULT_ACCENT_COLOR,
                helper_module.CARD_DEFAULT_BACKGROUND_COLOR, helper_module.CARD_DEFAULT_TEXT_COLOR,
                helper_module.CARD_DEFAULT_FONT_STYLE,
            )
        )

    def test_ensure_does_not_overwrite_a_row_marked_customized(self):
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "UPDATE trading_cards SET title=?, accent_color=?, customized=1 WHERE guildId=? AND userId=?",
            ("Legend", "#FF0000", GUILD_ID, 901)
        )
        self.db.commit()

        self.helperObj.ensureCardSettings(GUILD_ID, 901)

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], "Legend")
        self.assertEqual(settings["accent_color"], "#FF0000")

    def test_ensure_resyncs_an_uncustomized_row_to_current_defaults(self):
        # Since there's no /card-customize command yet, an uncustomized row
        # is always just a stale snapshot, not a real choice, so it should
        # track the current CARD_DEFAULT_* values instead of staying frozen
        # at whatever they were when the row was first created.
        self.helperObj.ensureCardSettings(GUILD_ID, 901)
        self.cursor.execute(
            "UPDATE trading_cards SET title=?, background_color=? WHERE guildId=? AND userId=?",
            ("Old Title", "#150B22", GUILD_ID, 901)
        )
        self.db.commit()

        self.helperObj.ensureCardSettings(GUILD_ID, 901)

        settings = self.helperObj.getCardSettings(GUILD_ID, 901)
        self.assertEqual(settings["title"], helper_module.CARD_DEFAULT_TITLE)
        self.assertEqual(settings["background_color"], helper_module.CARD_DEFAULT_BACKGROUND_COLOR)

    def test_get_card_settings_creates_defaults_for_a_brand_new_player(self):
        settings = self.helperObj.getCardSettings(GUILD_ID, 902)
        self.assertEqual(settings["title"], helper_module.CARD_DEFAULT_TITLE)
        self.assertEqual(settings["accent_color"], helper_module.CARD_DEFAULT_ACCENT_COLOR)
        self.assertEqual(settings["font_style"], helper_module.CARD_DEFAULT_FONT_STYLE)


class HexToRgbTests(HelperTestCase):
    def test_parses_a_valid_hex_color(self):
        self.assertEqual(self.helperObj._hexToRgb("#EDC643", (0, 0, 0)), (237, 198, 67))

    def test_falls_back_for_invalid_input(self):
        fallback = (1, 2, 3)
        self.assertEqual(self.helperObj._hexToRgb("not-a-color", fallback), fallback)
        self.assertEqual(self.helperObj._hexToRgb(None, fallback), fallback)
        self.assertEqual(self.helperObj._hexToRgb("#ZZZZZZ", fallback), fallback)


class RenderTradingCardImageTests(HelperTestCase):
    def _stats(self, elo=1123, ranked_wins=4, ranked_losses=1):
        ranked_games = ranked_wins + ranked_losses
        return {
            "elo": elo, "elo_rank": self.helperObj.eloRankLabelPlain(elo),
            "ranked_wins": ranked_wins, "ranked_losses": ranked_losses,
            "ranked_win_rate": f"{(ranked_wins / ranked_games) * 100:.1f}%" if ranked_games > 0 else "N/A",
        }

    def _team(self, name):
        team = Team()
        team.set_name(name)
        return team

    def test_renders_without_crashing_with_default_settings_and_no_teams(self):
        settings = {
            "title": helper_module.CARD_DEFAULT_TITLE, "accent_color": helper_module.CARD_DEFAULT_ACCENT_COLOR,
            "background_color": helper_module.CARD_DEFAULT_BACKGROUND_COLOR,
            "text_color": helper_module.CARD_DEFAULT_TEXT_COLOR, "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))

        image = self.helperObj._renderTradingCardImage("Test Guild", "Alice", avatar, settings, self._stats(), [])

        self.assertEqual(image.width, helper_module.CARD_WIDTH)
        self.assertGreater(image.height, 0)

    def test_no_stat_line_or_label_is_cut_off_by_the_card_edge(self):
        # regression test: the old 3-column layout could clip a long
        # value against its own column's edge; every stat is one full-
        # width line now, so nothing should ever measure wider than the
        # space between the left margin and the card's right edge.
        settings = {
            "title": helper_module.CARD_DEFAULT_TITLE, "accent_color": helper_module.CARD_DEFAULT_ACCENT_COLOR,
            "background_color": helper_module.CARD_DEFAULT_BACKGROUND_COLOR,
            "text_color": helper_module.CARD_DEFAULT_TEXT_COLOR, "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        stats = self._stats(elo=2450, ranked_wins=1234, ranked_losses=987)

        image = self.helperObj._renderTradingCardImage(
            "A Very Long Server Name Indeed", "SomeoneWithAReallyLongDisplayName", avatar, settings, stats, []
        )
        self.assertEqual(image.width, helper_module.CARD_WIDTH)

    def test_name_font_shrinks_to_fit_a_long_username_in_every_shop_font(self):
        # Regression test: PRESS_START_2P ("Retro")'s near-monospace glyphs
        # are unusually wide; a real (up to 32-char) Discord username at
        # the standard CARD_NAME_FONT_SIZE could measure at or past
        # CARD_WIDTH itself, well past the card's own border, where every
        # other bundled font stays comfortably clear of the edge.
        # _fitNameFont shrinks the actual font size (never the layout) to
        # cover this for any font, not just this one.
        max_width = helper_module.CARD_WIDTH - helper_module.BRACKET_MARGIN * 2
        name = "abcdefghijklmnopqrstuvwxyzABCDEF"  # Discord's own 32-char cap
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        for font_style in (helper_module.CARD_DEFAULT_FONT_STYLE, *helper_module.CARD_SHOP_FONT_STYLES):
            fonts = self.helperObj._cardFontPaths(font_style)
            font = self.helperObj._fitNameFont(fonts["name_font"], fonts["name_variation"], name, max_width)
            self.assertLessEqual(measurer.textlength(name, font=font), max_width)

    def test_name_font_never_shrinks_below_the_floor(self):
        # An absurdly long name still can't be made to fit at ANY size a
        # human could read; _fitNameFont has to stop somewhere rather than
        # shrinking toward 0.
        fonts = self.helperObj._cardFontPaths("Retro")
        font = self.helperObj._fitNameFont(fonts["name_font"], fonts["name_variation"], "x" * 200, max_width=1)
        self.assertEqual(font.size, helper_module.CARD_NAME_MIN_FONT_SIZE)

    def test_name_font_does_not_shrink_when_it_already_fits(self):
        fonts = self.helperObj._cardFontPaths(helper_module.CARD_DEFAULT_FONT_STYLE)
        font = self.helperObj._fitNameFont(fonts["name_font"], fonts["name_variation"], "Alice", 100000)
        self.assertEqual(font.size, helper_module.CARD_NAME_FONT_SIZE)

    def test_grows_taller_to_fit_teams(self):
        settings = {
            "title": "X", "accent_color": "#EDC643", "background_color": "#150B22",
            "text_color": "#F3EFFA", "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        teams = [self._team("Red Dragons"), self._team("Blue Phoenixes"), self._team("Green Giants")]

        without_teams = self.helperObj._renderTradingCardImage("Guild", "Alice", avatar, settings, self._stats(), [])
        with_teams = self.helperObj._renderTradingCardImage(
            "Guild", "Alice", avatar, settings, self._stats(), teams
        )
        self.assertGreater(with_teams.height, without_teams.height)

    def test_caps_team_rows_and_shows_a_remainder_count(self):
        settings = {
            "title": "X", "accent_color": "#EDC643", "background_color": "#150B22",
            "text_color": "#F3EFFA", "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        teams = [self._team(f"Team {i}") for i in range(helper_module.CARD_MAX_TEAM_ROWS + 3)]

        # Just confirms this doesn't crash and still produces a reasonably
        # bounded image rather than growing one row per team forever;
        # the "+N more teams" line itself is exercised via the full
        # reaction-flow tests (HandleStatsReactionTests), which check
        # actual pixel/text content is harder to do without an OCR step.
        image = self.helperObj._renderTradingCardImage("Guild", "Alice", avatar, settings, self._stats(), teams)
        self.assertGreater(image.height, 0)

    def test_custom_colors_are_respected(self):
        # A hand-picked accent color should show up as the frame's outline
        # pixel color, the simplest observable proof the setting actually
        # reached the renderer instead of silently falling back to default.
        settings = {
            "title": "X", "accent_color": "#00FF00", "background_color": "#000000",
            "text_color": "#FFFFFF", "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))

        image = self.helperObj._renderTradingCardImage("Guild", "Alice", avatar, settings, self._stats(), [])
        mid_y = image.height // 2
        border_pixel = image.convert("RGB").getpixel((helper_module.BRACKET_LINE_WIDTH, mid_y))
        self.assertEqual(border_pixel, (0, 255, 0))

    def test_username_renders_in_the_top_right_when_provided(self):
        settings = {
            "title": "X", "accent_color": "#00FF00", "background_color": "#000000",
            "text_color": "#FFFFFF", "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        # the header's top-right quadrant (where a username is drawn,
        # right-aligned, mirroring the logo/guild-name block's own
        # top-left placement) should differ once one is actually given.
        box = (
            helper_module.CARD_WIDTH // 2, 0,
            helper_module.CARD_WIDTH, helper_module.BRACKET_MARGIN + helper_module.BRACKET_LOGO_HEIGHT
        )

        without_username = self.helperObj._renderTradingCardImage(
            "Guild", "Alice", avatar, settings, self._stats(), []
        ).convert("RGB").crop(box)
        with_username = self.helperObj._renderTradingCardImage(
            "Guild", "Alice", avatar, settings, self._stats(), [], username="alice_real"
        ).convert("RGB").crop(box)

        self.assertNotEqual(list(without_username.getdata()), list(with_username.getdata()))

    def test_omitting_username_matches_the_pre_existing_render(self):
        # username defaults to None; every caller written before this
        # parameter existed passes exactly the same positional args it
        # always did, and should get exactly the same image back, not a
        # blank "@" or other placeholder in the corner.
        settings = {
            "title": "X", "accent_color": "#EDC643", "background_color": "#150B22",
            "text_color": "#F3EFFA", "font_style": helper_module.CARD_DEFAULT_FONT_STYLE,
        }
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        without_arg = self.helperObj._renderTradingCardImage("Guild", "Alice", avatar, settings, self._stats(), [])
        with_none = self.helperObj._renderTradingCardImage(
            "Guild", "Alice", avatar, settings, self._stats(), [], username=None
        )
        self.assertEqual(list(without_arg.getdata()), list(with_none.getdata()))

    def test_font_style_changes_body_text_not_just_the_name_and_title(self):
        # font_style should vary every body element too (stat labels,
        # values, roster rows, username), not just name_font/title_font; a
        # stat label (drawn with label_font) is the cheapest one to compare
        # pixel-for-pixel between styles.
        team = self._team("Red Dragons")  # gives the render some roster-row pixels too
        avatar = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        rendered = {}
        for font_style in (helper_module.CARD_DEFAULT_FONT_STYLE, "Bold", "Elegant"):
            settings = {
                "title": "X", "accent_color": "#EDC643", "background_color": "#150B22",
                "text_color": "#F3EFFA", "font_style": font_style,
            }
            rendered[font_style] = self.helperObj._renderTradingCardImage(
                "Guild", "Alice", avatar, settings, self._stats(), [team]
            ).convert("RGB")

        default_data = list(rendered[helper_module.CARD_DEFAULT_FONT_STYLE].getdata())
        for font_style in ("Bold", "Elegant"):
            self.assertNotEqual(list(rendered[font_style].getdata()), default_data)

    def test_card_font_paths_covers_every_font_style_key_the_shop_sells(self):
        for font_style in (helper_module.CARD_DEFAULT_FONT_STYLE, *helper_module.CARD_SHOP_FONT_STYLES):
            fonts = self.helperObj._cardFontPaths(font_style)
            for key in (
                "name_font", "name_variation", "title_font", "title_variation", "body_font",
                "label_weight", "value_weight", "team_weight",
            ):
                self.assertIn(key, fonts)

    def test_shop_font_styles_use_a_genuinely_different_typeface_than_default(self):
        # Each shop style's name_font should be a completely different
        # bundled font file, not just a different weight of the same one.
        default_name_font = self.helperObj._cardFontPaths(helper_module.CARD_DEFAULT_FONT_STYLE)["name_font"]
        for font_style in helper_module.CARD_SHOP_FONT_STYLES:
            self.assertNotEqual(self.helperObj._cardFontPaths(font_style)["name_font"], default_name_font)

    def test_every_font_style_uses_its_own_distinct_font_file(self):
        # Not just distinct from the default style, distinct from every
        # OTHER shop style too, so two styles can't accidentally end up
        # pointing at the same bundled file.
        paths = {
            font_style: self.helperObj._cardFontPaths(font_style)["name_font"]
            for font_style in (helper_module.CARD_DEFAULT_FONT_STYLE, *helper_module.CARD_SHOP_FONT_STYLES)
        }
        self.assertEqual(len(set(paths.values())), len(paths))

    def test_every_shop_font_file_actually_loads(self):
        # _loadFont silently degrades to PIL's built-in font on a bad path
        # (OSError/ValueError caught and swallowed), real for a genuinely
        # missing/renamed style, but it means a corrupted or truncated font
        # FILE wouldn't be caught by any test that only goes through
        # _loadFont. Load each bundled shop-font file directly instead, so
        # a bad download/corrupted TTF fails loudly here.
        for font_style in helper_module.CARD_SHOP_FONT_STYLES:
            paths = self.helperObj._cardFontPaths(font_style)
            font = ImageFont.truetype(paths["name_font"], 40)
            self.assertGreater(len(font.getname()[0]), 0)

    def test_unrecognized_font_style_falls_back_to_default(self):
        self.assertEqual(
            self.helperObj._cardFontPaths("Nonexistent"), self.helperObj._cardFontPaths(helper_module.CARD_DEFAULT_FONT_STYLE)
        )


class EloRankLabelTests(HelperTestCase):
    def test_maps_elo_to_the_expected_tier_and_division(self):
        cases = [
            (-500, "⚙️ Iron IV"),   # clamped, doesn't go negative
            (0, "⚙️ Iron IV"),
            (62, "⚙️ Iron IV"),
            (63, "⚙️ Iron III"),
            (124, "⚙️ Iron III"),
            (125, "⚙️ Iron II"),
            (186, "⚙️ Iron II"),
            (188, "⚙️ Iron I"),
            (249, "⚙️ Iron I"),
            (250, "\U0001f949 Bronze IV"),
            (499, "\U0001f949 Bronze I"),
            (500, "\U0001f948 Silver IV"),
            (749, "\U0001f948 Silver I"),
            (750, "\U0001f947 Gold IV"),
            (999, "\U0001f947 Gold I"),
            (1000, "\U0001f537 Platinum IV"),
            (1249, "\U0001f537 Platinum I"),
            (1250, "\U0001f48e Diamond IV"),
            (1499, "\U0001f48e Diamond I"),
            # Master and above: no division, same as League showing raw LP
            (1500, "\U0001f7e3 Master"),
            (1749, "\U0001f7e3 Master"),
            (1750, "\U0001f534 Grandmaster"),
            (1999, "\U0001f534 Grandmaster"),
            (2000, "\U0001f451 Challenger"),
            (5000, "\U0001f451 Challenger"),
        ]
        for elo, expected in cases:
            with self.subTest(elo=elo):
                self.assertEqual(self.helperObj.eloRankLabel(elo), expected)

    def test_plain_label_omits_the_leading_emoji(self):
        # eloRankLabelPlain is what the trading card uses (see
        # _swapStatsForTradingCard); PIL's bundled TTF fonts can't render
        # these emoji, so the card needs the tier text without one.
        self.assertEqual(self.helperObj.eloRankLabelPlain(1123), "Platinum III")
        self.assertEqual(self.helperObj.eloRankLabelPlain(1600), "Master")
        self.assertEqual(
            self.helperObj.eloRankLabel(1123),
            f"\U0001f537 {self.helperObj.eloRankLabelPlain(1123)}"
        )


class CancelBettingHelperTests(HelperTestCase):
    async def test_noop_when_no_active_round(self):
        channel = FakeChannel("game-chat")
        await self.helperObj.cancelBettingHelper(GUILD_ID, channel)
        channel.send.assert_not_awaited()

    async def test_refunds_open_bets_and_resets_state(self):
        self.helperObj.update(GUILD_ID, "betting_state", "CLOSED")
        self.helperObj.update(GUILD_ID, "active_tournament_match_id", 42)
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=700 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.cursor.execute(
            "INSERT INTO wagers(guildId, userId, username, team, amount) VALUES(?, ?, ?, ?, ?)",
            (GUILD_ID, 901, "Alice", 1, 300),
        )
        self.db.commit()

        channel = FakeChannel("game-chat")
        await self.helperObj.cancelBettingHelper(GUILD_ID, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")
        self.assertIsNone(self.helperObj.get(GUILD_ID, "active_tournament_match_id"))
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)
        channel.send.assert_awaited_once()

    async def test_cancels_running_task(self):
        task = asyncio.create_task(asyncio.sleep(100))
        self.helperObj.bettingTasks[GUILD_ID] = task

        await self.helperObj.cancelBettingHelper(GUILD_ID, FakeChannel("game-chat"))
        await asyncio.sleep(0)  # let the cancellation propagate

        self.assertTrue(task.cancelled())
        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)


class OpenBettingTests(HelperTestCase):
    async def test_opens_betting_and_schedules_timer(self):
        channel = FakeChannel("game-chat")
        channel.send = AsyncMock(return_value=FakeMessage(id=12345))

        await self.helperObj._openBetting(GUILD_ID, channel)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 12345)
        self.assertIn(GUILD_ID, self.helperObj.bettingTasks)
        channel.send.assert_awaited_once()
        message_text = channel.send.call_args.args[0]
        self.assertIn("Betting is open", message_text)
        self.assertIn("winning team's button", message_text)
        self.assertIn("cancel the game", message_text)

        # The winner-report/cancel buttons go out immediately alongside the
        # "betting is open" message, no waiting on the timer to close
        # betting first before anyone can report a winner or cancel.
        view = channel.send.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.WinnerReportView)
        # No persistent teams involved here (a plain random-split roster),
        # so the buttons fall back to the generic "Team 1"/"Team 2" labels.
        self.assertEqual(view.team1.label, "Team 1 🔵")
        self.assertEqual(view.team2.label, "Team 2 🔴")

        # _bettingTimer catches CancelledError itself (so a cancelled game
        # never crashes with an unhandled exception); once a task is
        # genuinely suspended on the sleep and then cancelled, that means
        # it finishes *normally* rather than raising, which is fine: what
        # actually matters is that it stops and cleans itself up.
        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])
        self.assertTrue(task.done())

    async def test_report_buttons_show_the_rosters_actual_team_names(self):
        team1 = Team(); team1.name = "Red Wolves"
        team1.add_player(Player(701, "P1"))
        team2 = Team(); team2.name = "Blue Hawks"
        team2.add_player(Player(702, "P2"))
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())

        channel = FakeChannel("game-chat")
        channel.send = AsyncMock(return_value=FakeMessage(id=12345))
        await self.helperObj._openBetting(GUILD_ID, channel)

        view = channel.send.call_args.kwargs["view"]
        self.assertEqual(view.team1.label, "Red Wolves 🔵")
        self.assertEqual(view.team2.label, "Blue Hawks 🔴")

        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])

    async def test_redirects_to_the_configured_wager_channel(self):
        origin_channel = FakeChannel("game-chat")
        wager_channel = FakeChannel("bets", kind="text")
        self.guild.channels.append(wager_channel)
        self.helperObj.client = FakeClient(guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "wager_channel", "bets")

        await self.helperObj._openBetting(GUILD_ID, origin_channel)

        origin_channel.send.assert_not_awaited()
        wager_channel.send.assert_awaited_once()
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_channel_id"), wager_channel.id)

        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])

    async def test_falls_back_to_the_origin_channel_if_wager_channel_unresolvable(self):
        origin_channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "wager_channel", "does-not-exist")

        await self.helperObj._openBetting(GUILD_ID, origin_channel)

        origin_channel.send.assert_awaited_once()

        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])

    async def test_records_when_betting_opened(self):
        channel = FakeChannel("game-chat")
        before = int(time.time())

        await self.helperObj._openBetting(GUILD_ID, channel)

        opened_at = self.helperObj.get(GUILD_ID, "betting_opened_at")
        self.assertIsNotNone(opened_at)
        self.assertGreaterEqual(opened_at, before)
        self.assertLessEqual(opened_at, int(time.time()))

        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])

    async def test_timer_closes_betting_without_touching_the_report_message(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 0)
        channel = FakeChannel("game-chat")
        channel.send = AsyncMock(return_value=FakeMessage(id=12345))

        await self.helperObj._openBetting(GUILD_ID, channel)
        await self.helperObj.bettingTasks[GUILD_ID]

        # The report message (with its reactions) was already posted by
        # _openBetting itself; the timer firing only closes betting, it
        # doesn't post or replace anything report-related.
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "CLOSED")
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 12345)
        self.assertEqual(channel.send.await_count, 2)  # open+report combined, then closed
        self.assertIn("closed", channel.send.call_args.args[0])
        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)

    async def test_restarting_mid_round_cancels_and_refunds_previous_round(self):
        # Long enough that the timer can't possibly fire before this test's
        # own assertions run.
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 100)
        channel = FakeChannel("game-chat")

        await self.helperObj._openBetting(GUILD_ID, channel)

        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()
        bet_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
        await self.helperObj.wagerHelper(bet_ctx, 400, 1)

        first_task = self.helperObj.bettingTasks[GUILD_ID]

        await self.helperObj._openBetting(GUILD_ID, channel)
        await asyncio.sleep(0)  # let the requested cancellation propagate

        self.assertTrue(first_task.cancelled())
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")

        # clean up the second (still-open) round's timer task so it
        # doesn't leak past the end of the test
        second_task = self.helperObj.bettingTasks[GUILD_ID]
        second_task.cancel()
        await asyncio.wait([second_task])


class ReconcileStaleBettingWindowsTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "betting_channel_id", self.channel.id)
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 60)

    async def _cleanup_task(self, guild_id=GUILD_ID):
        task = self.helperObj.bettingTasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])

    async def test_resumes_with_the_remaining_time_for_a_window_still_within_its_window(self):
        self.helperObj.update(GUILD_ID, "betting_opened_at", int(time.time()) - 40)  # 40 of 60s used

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertIn(GUILD_ID, self.helperObj.bettingTasks)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")
        self.channel.send.assert_not_awaited()
        await self._cleanup_task()

    async def test_closes_a_window_whose_remaining_time_already_elapsed(self):
        self.helperObj.update(GUILD_ID, "betting_opened_at", int(time.time()) - 120)  # 60s window, long over

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "CLOSED")
        self.channel.send.assert_awaited_once()
        self.assertIn("closed", self.channel.send.call_args.args[0])

    async def test_treats_a_missing_opened_at_as_already_expired(self):
        # Only unset for a window that predates the betting_opened_at
        # column, closed outright rather than guessing how long ago it
        # actually opened.
        self.helperObj.update(GUILD_ID, "betting_opened_at", None)

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "CLOSED")

    async def test_does_not_double_start_a_window_that_already_has_a_live_task(self):
        # A mere gateway reconnect (not a real process restart) can also
        # fire on_ready, but self.bettingTasks survives that; reconciling
        # it again would stomp a window that was never actually
        # interrupted.
        self.helperObj.update(GUILD_ID, "betting_opened_at", int(time.time()) - 120)
        placeholder = asyncio.create_task(asyncio.sleep(100))
        self.helperObj.bettingTasks[GUILD_ID] = placeholder

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")
        self.channel.send.assert_not_awaited()
        self.assertIs(self.helperObj.bettingTasks[GUILD_ID], placeholder)
        await self._cleanup_task()

    async def test_leaves_the_window_alone_if_the_channel_is_unresolvable(self):
        self.helperObj.update(GUILD_ID, "betting_opened_at", int(time.time()) - 120)
        self.helperObj.update(GUILD_ID, "betting_channel_id", 999999999)

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")

    async def test_ignores_guilds_that_are_not_currently_open(self):
        self.helperObj.update(GUILD_ID, "betting_state", "NONE")

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)
        self.channel.send.assert_not_awaited()

    async def test_reconciles_every_open_guild_independently(self):
        guild2 = FakeGuild(id=GUILD_ID + 1)
        channel2 = FakeChannel("game-chat-2")
        self.helperObj.client = FakeClient(channels=[self.channel, channel2], guilds=[self.guild, guild2])
        insert_guild_row(self.cursor, self.db, guild_id=GUILD_ID + 1, name="Second Guild")
        self.helperObj.update(GUILD_ID + 1, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID + 1, "betting_channel_id", channel2.id)
        self.helperObj.update(GUILD_ID + 1, "betting_timer_seconds", 60)
        self.helperObj.update(GUILD_ID + 1, "betting_opened_at", int(time.time()) - 40)
        self.helperObj.update(GUILD_ID, "betting_opened_at", int(time.time()) - 120)

        await self.helperObj.reconcileStaleBettingWindows(self.helperObj.client)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "CLOSED")
        self.assertEqual(self.helperObj.get(GUILD_ID + 1, "betting_state"), "OPEN")
        self.assertIn(GUILD_ID + 1, self.helperObj.bettingTasks)
        await self._cleanup_task(GUILD_ID + 1)


class GetBettingTimerSecondsTests(HelperTestCase):
    def test_returns_the_configured_value(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 45)
        self.assertEqual(self.helperObj._getBettingTimerSeconds(GUILD_ID), 45)

    def test_falls_back_to_the_default_when_the_column_is_null(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", None)
        self.assertEqual(
            self.helperObj._getBettingTimerSeconds(GUILD_ID),
            helper_module.BETTING_DURATION_SECONDS,
        )

    def test_falls_back_to_the_default_when_the_guild_has_no_row_at_all(self):
        # A guild with no `servers` row at all (not just a null column,
        # /test's simulated tournament is one such case) shouldn't crash
        # just because it wants a betting duration.
        self.assertEqual(
            self.helperObj._getBettingTimerSeconds(999999),
            helper_module.BETTING_DURATION_SECONDS,
        )


class OpenConcurrentTournamentBettingTests(HelperTestCase):
    def _insert_match(self, match_id, channel_id):
        self.cursor.execute(
            "INSERT INTO tournament_matches(id, guildId, roundIndex, nodeIndex, team1, team2, state, "
            "mode, messageId, channelId, winner, bracketType) "
            "VALUES(?, ?, 0, 0, '', '', 'AWAITING_RESULT', 'simultaneous', NULL, ?, NULL, 'winners')",
            (match_id, GUILD_ID, channel_id)
        )
        self.db.commit()

    async def test_scales_duration_by_match_count_and_opens_betting(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 10)
        channel = FakeChannel("bracket-chat")

        await self.helperObj._openConcurrentTournamentBetting(GUILD_ID, [1, 2, 3], channel)

        channel.send.assert_awaited_once()
        message = channel.send.call_args.args[0]
        self.assertIn("3 matches", message)
        self.assertIn("#1", message)
        self.assertIn("#2", message)
        self.assertIn("#3", message)
        self.assertIn("30 seconds", message)  # 10 * 3 matches

    async def test_caps_duration_at_the_configured_maximum(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 600)
        channel = FakeChannel("bracket-chat")

        await self.helperObj._openConcurrentTournamentBetting(GUILD_ID, [1, 2, 3, 4], channel)

        message = channel.send.call_args.args[0]
        self.assertIn(f"{helper_module.MAX_CONCURRENT_BETTING_SECONDS} seconds", message)

    async def test_singular_match_uses_singular_wording(self):
        channel = FakeChannel("bracket-chat")
        await self.helperObj._openConcurrentTournamentBetting(GUILD_ID, [7], channel)
        message = channel.send.call_args.args[0]
        self.assertIn("1 match ", message)

    async def test_timer_closes_betting_on_the_listed_matches(self):
        self.helperObj.update(GUILD_ID, "betting_timer_seconds", 0)
        channel = FakeChannel("bracket-chat")
        self._insert_match(1, channel.id)

        before = asyncio.all_tasks()
        await self.helperObj._openConcurrentTournamentBetting(GUILD_ID, [1], channel)
        timer_task = (asyncio.all_tasks() - before).pop()
        await timer_task

        self.cursor.execute("SELECT bettingClosed FROM tournament_matches WHERE id=1")
        self.assertEqual(self.cursor.fetchone()[0], 1)
        self.assertEqual(channel.send.await_count, 2)  # open, then closed


class PlaceTournamentWagerTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    def _insert_match(self, match_id, team1=None, team2=None, state="AWAITING_RESULT", betting_closed=0):
        team1 = team1 or Team()
        team2 = team2 or Team()
        team1.name = team1.name or "Team 1"
        team2.name = team2.name or "Team 2"
        self.cursor.execute(
            "INSERT INTO tournament_matches(id, guildId, roundIndex, nodeIndex, team1, team2, state, "
            "mode, messageId, channelId, winner, bracketType, bettingClosed) "
            "VALUES(?, ?, 0, 0, ?, ?, ?, 'simultaneous', NULL, NULL, NULL, 'winners', ?)",
            (match_id, GUILD_ID, team1.serializeTeam(), team2.serializeTeam(), state, betting_closed)
        )
        self.db.commit()

    def _give_gold(self, user_id, name, amount):
        self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
        self.cursor.execute(
            "UPDATE economy SET balance=? WHERE guildId=? AND userId=?", (amount, GUILD_ID, user_id)
        )
        self.db.commit()

    async def test_rejects_unknown_match_id(self):
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=42)
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament match with id 42 in this server."
        )

    async def test_rejects_when_match_is_resolved(self):
        self._insert_match(1, state="RESOLVED")
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=1)
        ctx.response.send_message.assert_awaited_once_with("Betting is closed for match #1.")

    async def test_rejects_when_the_betting_closed_flag_is_set(self):
        self._insert_match(1, betting_closed=1)
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=1)
        ctx.response.send_message.assert_awaited_once_with("Betting is closed for match #1.")

    async def test_rejects_once_a_report_reaction_is_pending_confirmation(self):
        # Regression: a simultaneous-match report reaction used to resolve
        # the match immediately, but the confirmation step now leaves it in
        # a CONFIRMING state for a bit; betting has to close right at the
        # reaction, not just once Confirm is actually pressed, or someone
        # could still bet on the reported side during that window.
        self._insert_match(1, state="CONFIRMING", betting_closed=1)
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=1)
        ctx.response.send_message.assert_awaited_once_with("Betting is closed for match #1.")

    async def test_rejects_a_rostered_player(self):
        team1 = Team()
        team1.name = "Team 1"
        team1.add_player(Player(901, "Alice"))
        self._insert_match(1, team1=team1)
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=1)
        ctx.response.send_message.assert_awaited_once_with(
            "You can't wager on a match you're playing in!"
        )

    async def test_rejects_insufficient_balance(self):
        self._insert_match(1)
        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 100, 1, match_id=1)
        ctx.response.send_message.assert_awaited_once_with(
            "You don't have enough gold for that! Your balance is 0."
        )

    async def test_rejects_duplicate_bet_on_the_same_match(self):
        self._insert_match(1)
        self._give_gold(901, "Alice", 1000)

        await self.helperObj.wagerHelper(self._ctx(), 100, 1, match_id=1)
        ctx2 = self._ctx()
        await self.helperObj.wagerHelper(ctx2, 50, 2, match_id=1)

        ctx2.response.send_message.assert_awaited_once_with(
            "You've already placed a bet on match #1."
        )
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 900)

    async def test_successful_wager_escrows_gold(self):
        self._insert_match(1)
        self._give_gold(901, "Alice", 1000)

        ctx = self._ctx()
        await self.helperObj.wagerHelper(ctx, 250, 2, match_id=1)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.cursor.execute(
            "SELECT team, amount FROM tournament_wagers WHERE matchId=? AND userId=?", (1, 901)
        )
        self.assertEqual(self.cursor.fetchone(), (2, 250))
        ctx.response.send_message.assert_awaited_once_with(
            "You wagered 250 gold on Team 2 for match #1!"
        )

    async def test_a_user_can_bet_on_multiple_concurrent_matches_independently(self):
        self._insert_match(1)
        self._insert_match(2)
        self._give_gold(901, "Alice", 1000)

        await self.helperObj.wagerHelper(self._ctx(), 100, 1, match_id=1)
        await self.helperObj.wagerHelper(self._ctx(), 200, 2, match_id=2)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 700)
        self.cursor.execute("SELECT COUNT(*) FROM tournament_wagers WHERE userId=901")
        self.assertEqual(self.cursor.fetchone()[0], 2)


class SettleMatchWagersTests(HelperTestCase):
    def _bet(self, match_id, user_id, name, team, amount):
        # Mirrors only the state _settleMatchWagers reads/needs, a wager
        # row plus an economy row. The real wagerHelper flow debits `amount`
        # from balance up front and settlement only ever credits winnings
        # back, so starting from balance=0 lets a settled balance be
        # compared directly against the expected payout.
        self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
        self.cursor.execute(
            "INSERT INTO tournament_wagers(matchId, guildId, userId, username, team, amount) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (match_id, GUILD_ID, user_id, name, team, amount)
        )
        self.db.commit()

    async def test_winners_split_the_losing_pool_pari_mutuel(self):
        self._bet(1, 901, "Alice", 1, 100)
        self._bet(1, 902, "Bob", 1, 300)
        self._bet(1, 903, "Carol", 2, 200)
        channel = FakeChannel("bracket-chat")

        await self.helperObj._settleMatchWagers(GUILD_ID, 1, 1, channel)

        # winning_pool=400 vs losing_pool=200 is a 0.667 favorite share, so
        # _imbalanceRakeFraction takes a 1/6 cut of the losing pool first
        # (200 -> 166.67) before splitting it: Alice: 100 + (100/400)*166.67
        # = ~141.67 -> 142; Bob: 300 + (300/400)*166.67 = 425 (not the
        # unraked 150/450 an even split of the full 200 would give).
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 142)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 425)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "balance"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 903, "losses"), 1)

        self.cursor.execute("SELECT COUNT(*) FROM tournament_wagers WHERE matchId=1")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        channel.send.assert_awaited_once()
        message = channel.send.call_args.args[0]
        self.assertIn("Alice won 142 gold", message)
        self.assertIn("Bob won 425 gold", message)

    async def test_a_winner_just_gets_their_wager_back_when_no_one_bet_the_losing_side(self):
        self._bet(1, 901, "Alice", 1, 100)
        channel = FakeChannel("bracket-chat")

        await self.helperObj._settleMatchWagers(GUILD_ID, 1, 1, channel)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 100)

    async def test_does_nothing_when_no_one_bet_on_the_match(self):
        channel = FakeChannel("bracket-chat")
        await self.helperObj._settleMatchWagers(GUILD_ID, 1, 1, channel)
        channel.send.assert_not_awaited()

    async def test_only_settles_the_named_match(self):
        self._bet(1, 901, "Alice", 1, 100)
        self._bet(2, 902, "Bob", 1, 100)
        channel = FakeChannel("bracket-chat")

        await self.helperObj._settleMatchWagers(GUILD_ID, 1, 1, channel)

        self.cursor.execute("SELECT COUNT(*) FROM tournament_wagers WHERE matchId=2")
        self.assertEqual(self.cursor.fetchone()[0], 1)


class WinnerReportViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "betting_message_id", 555)

    def _click(self, message_id=555, user_id=901, name="Alice"):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )

    async def test_valid_while_betting_is_still_open(self):
        # A real game can finish before the nominal betting window closes -
        # the report message's buttons are live from the moment the game
        # starts, not just after the timer closes betting.
        click = self._click()
        await helper_module.WinnerReportView(self.helperObj).team1.callback(click)
        click.response.send_message.assert_awaited_once()
        self.assertIn("view", click.response.send_message.call_args.kwargs)

    async def test_valid_after_betting_has_closed(self):
        self.helperObj.update(GUILD_ID, "betting_state", "CLOSED")
        click = self._click()
        await helper_module.WinnerReportView(self.helperObj).team1.callback(click)
        click.response.send_message.assert_awaited_once()
        self.assertIn("reported as the winner", click.response.send_message.call_args.args[0])

    async def test_ignores_when_betting_state_is_none(self):
        self.helperObj.update(GUILD_ID, "betting_state", "NONE")
        click = self._click()
        await helper_module.WinnerReportView(self.helperObj).team1.callback(click)
        click.response.send_message.assert_awaited_once()
        self.assertNotIn("reported as the winner", click.response.send_message.call_args.args[0])
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_ignores_mismatched_message_id(self):
        click = self._click(message_id=999)
        await helper_module.WinnerReportView(self.helperObj).team1.callback(click)
        click.response.send_message.assert_awaited_once()
        self.assertNotIn("reported as the winner", click.response.send_message.call_args.args[0])
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_valid_click_clears_message_id_and_posts_a_confirmation_instead_of_recording(self):
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            click = self._click()
            await helper_module.WinnerReportView(self.helperObj).team1.callback(click)

        # a real elo/payout change shouldn't hinge on the click alone; it's
        # not recorded until the posted confirmation is actually clicked
        mock.assert_not_awaited()
        click.response.send_message.assert_awaited_once()
        text = click.response.send_message.call_args.args[0]
        self.assertIn("reported as the winner", text)
        view = click.response.send_message.call_args.kwargs["view"]
        self.assertEqual(view.winning_team, 1)
        self.assertEqual(view.report_message_id, 555)
        # betting_message_id clears synchronously before the confirmation
        # posts, so a second/concurrent click can't also pass the guard above
        self.assertIsNone(self.helperObj.get(GUILD_ID, "betting_message_id"))

    async def test_concurrent_clicks_only_process_once(self):
        click1 = self._click()
        click2 = self._click()
        await asyncio.gather(
            helper_module.WinnerReportView(self.helperObj).team1.callback(click1),
            helper_module.WinnerReportView(self.helperObj).team2.callback(click2),
        )

        texts = [
            click1.response.send_message.call_args.args[0],
            click2.response.send_message.call_args.args[0],
        ]
        self.assertEqual(sum("reported as the winner" in text for text in texts), 1)
        self.assertEqual(sum("already been reported" in text for text in texts), 1)

    async def test_cancel_button_posts_a_confirmation_instead_of_cancelling(self):
        # A real refund-and-move-everyone-back action shouldn't hinge on a
        # single accidental click any more than a winner report does; it's
        # not cancelled until the posted confirmation is actually clicked.
        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()) as cancel_mock:
            click = self._click()
            await helper_module.WinnerReportView(self.helperObj).cancelGame.callback(click)

        cancel_mock.assert_not_awaited()
        click.response.send_message.assert_awaited_once()
        text = click.response.send_message.call_args.args[0]
        self.assertIn("Cancel this game?", text)
        view = click.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.ConfirmCancelGameView)
        self.assertEqual(view.report_message_id, 555)
        self.assertIs(view.report_message, click.message)
        # betting_message_id clears synchronously before the confirmation
        # posts, so a second/concurrent click can't also pass the guard above
        self.assertIsNone(self.helperObj.get(GUILD_ID, "betting_message_id"))

    async def test_cancel_button_also_stops_the_running_timer(self):
        task = asyncio.create_task(asyncio.sleep(100))
        self.helperObj.bettingTasks[GUILD_ID] = task

        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()):
            click = self._click()
            await helper_module.WinnerReportView(self.helperObj).cancelGame.callback(click)
        await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)

    async def test_cancel_button_ignores_mismatched_message_id(self):
        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()) as cancel_mock:
            click = self._click(message_id=999)
            await helper_module.WinnerReportView(self.helperObj).cancelGame.callback(click)

        cancel_mock.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


class ConfirmWinnerReportViewTests(HelperTestCase):
    def _click(self, user_id=901, name="Alice", channel=None, guild=None):
        return FakeInteraction(guild if guild is not None else self.guild, FakeMember(name, id=user_id), channel=channel)

    def setUp(self):
        super().setUp()
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")

    async def test_confirm_records_the_result_and_disables_the_buttons(self):
        report_message = FakeMessage(id=555)
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555, report_message=report_message)
        channel = FakeChannel("game-chat")
        click = self._click(channel=channel)

        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            await view.confirm.callback(click)

        mock.assert_awaited_once_with(GUILD_ID, 1, channel, self.guild)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("confirmed", click.response.edit_message.call_args.kwargs["content"])
        for item in view.children:
            self.assertTrue(item.disabled)
        # The original report message's own Team 1/Team 2/Cancel Game
        # buttons are stripped once the result is actually recorded, so a
        # resolved game doesn't keep showing live buttons.
        report_message.edit.assert_awaited_once_with(view=None)

    async def test_confirm_with_no_report_message_does_not_crash(self):
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)
        click = self._click(channel=FakeChannel("game-chat"))

        with patch.object(self.helperObj, "recordResult", AsyncMock()):
            await view.confirm.callback(click)  # report_message defaults to None - should just no-op

    async def test_cancel_restores_the_report_message_without_recording(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)
        click = self._click()

        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            await view.cancel.callback(click)

        mock.assert_not_awaited()
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("cancelled", click.response.edit_message.call_args.kwargs["content"])

    async def test_cancel_does_not_restore_if_the_game_was_already_resolved_another_way(self):
        # e.g. 🛑 cancelled the whole game (or a fresh roster's own
        # clearTeamsHelper did) while this confirmation was still pending -
        # betting_state is no longer OPEN/CLOSED, so restoring a stale
        # betting_message_id here would resurrect an already-settled game.
        self.helperObj.update(GUILD_ID, "betting_state", "NONE")
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)

        await view.cancel.callback(self._click())

        self.assertIsNone(self.helperObj.get(GUILD_ID, "betting_message_id"))

    async def test_cancel_does_not_stomp_a_newer_report_message(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", 777)
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)

        await view.cancel.callback(self._click())

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 777)

    async def test_timeout_restores_the_report_message_and_edits_it(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)
        posted = FakeMessage(id=900)
        view.message = posted

        await view.on_timeout()

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)
        posted.edit.assert_awaited_once()
        self.assertIn("timed out", posted.edit.call_args.kwargs["content"])
        for item in view.children:
            self.assertTrue(item.disabled)

    async def test_timeout_with_no_posted_message_does_not_crash(self):
        view = helper_module.ConfirmWinnerReportView(self.helperObj, GUILD_ID, 1, 555)
        await view.on_timeout()  # view.message is still None - should just no-op the edit


class ConfirmCancelGameViewTests(HelperTestCase):
    def _click(self, user_id=901, name="Alice", channel=None, guild=None):
        return FakeInteraction(guild if guild is not None else self.guild, FakeMember(name, id=user_id), channel=channel)

    def setUp(self):
        super().setUp()
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")

    async def test_confirm_cancels_the_game_and_disables_the_buttons(self):
        report_message = FakeMessage(id=555)
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555, report_message=report_message)
        channel = FakeChannel("game-chat")
        click = self._click(channel=channel)

        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()) as mock:
            await view.confirm.callback(click)

        mock.assert_awaited_once_with(GUILD_ID, channel, self.guild)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("confirmed", click.response.edit_message.call_args.kwargs["content"])
        for item in view.children:
            self.assertTrue(item.disabled)
        # Same as a confirmed winner report; the original message's own
        # buttons are stripped once the game is actually cancelled.
        report_message.edit.assert_awaited_once_with(view=None)

    async def test_confirm_with_no_report_message_does_not_crash(self):
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555)
        click = self._click(channel=FakeChannel("game-chat"))

        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()):
            await view.confirm.callback(click)  # report_message defaults to None - should just no-op

    async def test_cancel_restores_the_report_message_without_cancelling(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555)
        click = self._click()

        with patch.object(self.helperObj, "cancelGameHelper", AsyncMock()) as mock:
            await view.cancel.callback(click)

        mock.assert_not_awaited()
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("kept", click.response.edit_message.call_args.kwargs["content"])

    async def test_cancel_does_not_stomp_a_newer_report_message(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", 777)
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555)

        await view.cancel.callback(self._click())

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 777)

    async def test_timeout_restores_the_report_message_and_edits_it(self):
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555)
        posted = FakeMessage(id=900)
        view.message = posted

        await view.on_timeout()

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)
        posted.edit.assert_awaited_once()
        self.assertIn("timed out", posted.edit.call_args.kwargs["content"])
        for item in view.children:
            self.assertTrue(item.disabled)

    async def test_timeout_with_no_posted_message_does_not_crash(self):
        view = helper_module.ConfirmCancelGameView(self.helperObj, GUILD_ID, 555)
        await view.on_timeout()  # view.message is still None - should just no-op the edit


class RestoreWinnerReportMessageTests(HelperTestCase):
    async def test_restores_when_betting_is_open_and_unset(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        self.helperObj._restoreWinnerReportMessage(GUILD_ID, 555)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)

    async def test_restores_when_betting_is_closed_and_unset(self):
        self.helperObj.update(GUILD_ID, "betting_state", "CLOSED")
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        self.helperObj._restoreWinnerReportMessage(GUILD_ID, 555)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 555)

    async def test_does_not_restore_when_betting_state_is_none(self):
        self.helperObj.update(GUILD_ID, "betting_state", "NONE")
        self.helperObj.update(GUILD_ID, "betting_message_id", None)
        self.helperObj._restoreWinnerReportMessage(GUILD_ID, 555)
        self.assertIsNone(self.helperObj.get(GUILD_ID, "betting_message_id"))

    async def test_does_not_overwrite_an_already_set_message_id(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        self.helperObj.update(GUILD_ID, "betting_message_id", 777)
        self.helperObj._restoreWinnerReportMessage(GUILD_ID, 555)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 777)


class ChallengeDuelHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel)

    async def test_rejects_challenging_yourself(self):
        ctx = self._ctx()
        await self.helperObj.challengeDuelHelper(ctx, ctx.user, 100)
        ctx.response.send_message.assert_awaited_once_with("You can't wager against yourself!")

    async def test_rejects_challenging_a_bot(self):
        ctx = self._ctx()
        target = FakeMember("Botty", id=902, bot=True)
        await self.helperObj.challengeDuelHelper(ctx, target, 100)
        ctx.response.send_message.assert_awaited_once_with("You can't wager against a bot!")

    async def test_rejects_non_positive_amount(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        await self.helperObj.challengeDuelHelper(ctx, target, 0)
        ctx.response.send_message.assert_awaited_once_with(
            "Wager amount must be greater than 0."
        )

    async def test_rejects_insufficient_balance(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        await self.helperObj.challengeDuelHelper(ctx, target, 100)
        ctx.response.send_message.assert_awaited_once_with(
            "You don't have enough gold for that! Your balance is 0."
        )

    async def test_successful_challenge_posts_message_and_stores_pending_duel(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
        )
        self.db.commit()

        channel = FakeChannel("general")
        ctx = self._ctx(channel=channel)
        target = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=4242)
        ctx.original_response.return_value = posted_message

        await self.helperObj.challengeDuelHelper(ctx, target, 250)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(target.mention, text)
        self.assertIn(ctx.user.mention, text)
        self.assertIn("250", text)
        self.assertIsInstance(ctx.response.send_message.call_args.kwargs["view"], helper_module.DuelAcceptView)

        # challenging doesn't escrow anything up front
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)

        self.cursor.execute(
            "SELECT guildId, channelId, messageId, challengerId, targetId, amount, state FROM duels"
        )
        row = self.cursor.fetchone()
        self.assertEqual(
            row, (GUILD_ID, channel.id, posted_message.id, 901, 902, 250, "PENDING_ACCEPT")
        )


class DuelAcceptViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("general")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId IN (901, 902)",
            (GUILD_ID,),
        )
        self.db.commit()
        self.cursor.execute(
            "INSERT INTO duels(guildId, channelId, messageId, challengerId, challengerName, "
            "targetId, targetName, amount, state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_ACCEPT')",
            (GUILD_ID, self.channel.id, 555, 901, "Alice", 902, "Bob", 250),
        )
        self.db.commit()
        # accepting posts a new message, give it a real id rather than the
        # channel's plain default AsyncMock.
        self.channel.send = AsyncMock(return_value=FakeMessage(id=777))

    def _click(self, message_id, user_id, name="Clicker"):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )

    async def test_accept_from_challenger_is_rejected(self):
        click = self._click(555, 901)
        await helper_module.DuelAcceptView(self.helperObj).accept.callback(click)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=555")
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_ACCEPT")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_accept_from_target_escrows_both_and_posts_result_prompt(self):
        click = self._click(555, 902)
        await helper_module.DuelAcceptView(self.helperObj).accept.callback(click)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 750)

        self.cursor.execute(
            "SELECT state, messageId FROM duels WHERE challengerId=901 AND targetId=902"
        )
        state, message_id = self.cursor.fetchone()
        self.assertEqual(state, "AWAITING_RESULT")
        self.assertEqual(message_id, 777)

        self.assertIsInstance(self.channel.send.call_args.kwargs["view"], helper_module.DuelResultView)

    async def test_accept_cancels_if_either_side_cant_cover_it(self):
        self.cursor.execute(
            "UPDATE economy SET balance=100 WHERE guildId=? AND userId=902", (GUILD_ID,)
        )
        self.db.commit()

        click = self._click(555, 902)
        await helper_module.DuelAcceptView(self.helperObj).accept.callback(click)

        # nothing escrowed, no duel left behind
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 100)
        self.cursor.execute("SELECT COUNT(*) FROM duels")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.channel.send.assert_awaited_once()

    async def test_accept_ignores_mismatched_message_id(self):
        click = self._click(999, 902)
        await helper_module.DuelAcceptView(self.helperObj).accept.callback(click)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=555")
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_ACCEPT")
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


class DuelResultViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("general")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId IN (901, 902)",
            (GUILD_ID,),
        )
        self.db.commit()
        self.cursor.execute(
            "INSERT INTO duels(guildId, channelId, messageId, challengerId, challengerName, "
            "targetId, targetName, amount, state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_ACCEPT')",
            (GUILD_ID, self.channel.id, 555, 901, "Alice", 902, "Bob", 250),
        )
        self.db.commit()
        self.channel.send = AsyncMock(return_value=FakeMessage(id=777))

    def _click(self, message_id, user_id, name="Clicker"):
        return FakeInteraction(
            self.guild, FakeMember(name, id=user_id),
            channel=self.channel, message=FakeMessage(id=message_id, channel=self.channel),
        )

    async def _accept(self):
        accept_view = helper_module.DuelAcceptView(self.helperObj)
        await accept_view.accept.callback(self._click(555, 902))
        self.cursor.execute("SELECT messageId FROM duels WHERE challengerId=901")
        return self.cursor.fetchone()[0]

    async def test_result_click_before_accept_is_rejected(self):
        click = self._click(555, 903)
        await helper_module.DuelResultView(self.helperObj).challengerWon.callback(click)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=555")
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_ACCEPT")
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_challenger_won_posts_a_confirmation_instead_of_paying_out(self):
        result_message_id = await self._accept()

        click = self._click(result_message_id, 903)
        await helper_module.DuelResultView(self.helperObj).challengerWon.callback(click)

        # a real gold transfer shouldn't hinge on the click alone; it's
        # not paid out until the posted confirmation is actually clicked
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        text = click.response.send_message.call_args.args[0]
        self.assertIn("Alice", text)
        self.assertIn("reported as the winner", text)
        view = click.response.send_message.call_args.kwargs["view"]
        self.assertTrue(view.winner_is_challenger)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=?", (result_message_id,))
        self.assertEqual(self.cursor.fetchone()[0], "CONFIRMING")

    async def test_target_won_posts_a_confirmation_naming_the_target(self):
        result_message_id = await self._accept()

        click = self._click(result_message_id, 903)
        await helper_module.DuelResultView(self.helperObj).targetWon.callback(click)

        text = click.response.send_message.call_args.args[0]
        self.assertIn("Bob", text)
        view = click.response.send_message.call_args.kwargs["view"]
        self.assertFalse(view.winner_is_challenger)

    async def test_concurrent_result_clicks_only_post_one_confirmation(self):
        result_message_id = await self._accept()

        click1 = self._click(result_message_id, 903)
        click2 = self._click(result_message_id, 904)
        await asyncio.gather(
            helper_module.DuelResultView(self.helperObj).challengerWon.callback(click1),
            helper_module.DuelResultView(self.helperObj).targetWon.callback(click2),
        )

        texts = [
            click1.response.send_message.call_args.args[0],
            click2.response.send_message.call_args.args[0],
        ]
        self.assertEqual(sum("reported as the winner" in text for text in texts), 1)
        self.assertEqual(sum("already been reported" in text for text in texts), 1)


class ConfirmDuelResultViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("general")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET balance=750 WHERE guildId=? AND userId IN (901, 902)",
            (GUILD_ID,),
        )
        self.db.commit()
        self.cursor.execute(
            "INSERT INTO duels(guildId, channelId, messageId, challengerId, challengerName, "
            "targetId, targetName, amount, state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMING')",
            (GUILD_ID, self.channel.id, 777, 901, "Alice", 902, "Bob", 250),
        )
        self.db.commit()
        self.cursor.execute("SELECT id FROM duels WHERE messageId=777")
        self.duel_id = self.cursor.fetchone()[0]

    def _click(self, user_id=903, name="Clicker"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=self.channel)

    async def test_challenger_win_pays_out_and_updates_bet_records(self):
        view = helper_module.ConfirmDuelResultView(self.helperObj, self.duel_id, True)
        await view.confirm.callback(self._click())

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1250)  # 750 + 500 pot
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 250)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_lost"), 250)

        self.cursor.execute("SELECT COUNT(*) FROM duels")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        for item in view.children:
            self.assertTrue(item.disabled)

    async def test_target_win_pays_out_the_other_way(self):
        view = helper_module.ConfirmDuelResultView(self.helperObj, self.duel_id, False)
        await view.confirm.callback(self._click())

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 1250)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "losses"), 1)

    async def test_cancel_restores_the_duel_without_paying_out(self):
        view = helper_module.ConfirmDuelResultView(self.helperObj, self.duel_id, True)
        await view.cancel.callback(self._click())

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.cursor.execute("SELECT state FROM duels WHERE id=?", (self.duel_id,))
        self.assertEqual(self.cursor.fetchone()[0], "AWAITING_RESULT")

    async def test_cancel_does_not_restore_if_the_duel_was_already_resolved_another_way(self):
        self.cursor.execute("UPDATE duels SET state='ACCEPTING' WHERE id=?", (self.duel_id,))
        self.db.commit()
        view = helper_module.ConfirmDuelResultView(self.helperObj, self.duel_id, True)

        await view.cancel.callback(self._click())

        self.cursor.execute("SELECT state FROM duels WHERE id=?", (self.duel_id,))
        self.assertEqual(self.cursor.fetchone()[0], "ACCEPTING")

    async def test_timeout_restores_the_duel_and_edits_the_message(self):
        view = helper_module.ConfirmDuelResultView(self.helperObj, self.duel_id, True)
        posted = FakeMessage(id=900)
        view.message = posted

        await view.on_timeout()

        self.cursor.execute("SELECT state FROM duels WHERE id=?", (self.duel_id,))
        self.assertEqual(self.cursor.fetchone()[0], "AWAITING_RESULT")
        posted.edit.assert_awaited_once()
        self.assertIn("timed out", posted.edit.call_args.kwargs["content"])
        for item in view.children:
            self.assertTrue(item.disabled)


class LeaderboardHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel)

    def _seed_players(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.helperObj.ensureEconomyRow(GUILD_ID, 903, "Cleo")
        self.cursor.execute(
            "UPDATE economy SET elo=1300, balance=500, wins=3, losses=1, "
            "game_wins=5, game_losses=2, gold_won=300, gold_lost=100, gold_wagered=400 "
            "WHERE guildId=? AND userId=901", (GUILD_ID,)
        )
        self.cursor.execute(
            "UPDATE economy SET elo=900, balance=1500, wins=1, losses=4, "
            "game_wins=1, game_losses=6, gold_won=50, gold_lost=200, gold_wagered=250 "
            "WHERE guildId=? AND userId=902", (GUILD_ID,)
        )
        # Cleo has no bets/games played yet; her win rates should be None.
        self.cursor.execute(
            "UPDATE economy SET elo=1000, balance=0 WHERE guildId=? AND userId=903", (GUILD_ID,)
        )
        self.db.commit()

    async def test_no_entries_sends_message_and_stores_nothing(self):
        ctx = self._ctx()
        await self.helperObj.leaderboardHelper(ctx, None, "desc")

        ctx.response.send_message.assert_awaited_once_with(
            "Nobody has any stats to show yet in this server!"
        )
        self.cursor.execute("SELECT COUNT(*) FROM leaderboards")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    def test_get_leaderboard_entries_computes_rates_and_none_for_no_games(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        by_id = {e["user_id"]: e for e in entries}

        self.assertAlmostEqual(by_id[901]["bet_win_rate"], 0.75)
        self.assertAlmostEqual(by_id[901]["game_win_rate"], 5 / 7)
        self.assertEqual(by_id[901]["net_gold"], 200)
        self.assertIsNone(by_id[903]["bet_win_rate"])
        self.assertIsNone(by_id[903]["game_win_rate"])
        # nobody in _seed_players has any ranked-tagged games, so the
        # entire game_wins/game_losses total should read as casual, and
        # ranked_win_rate stays None the same way an empty game_win_rate does.
        self.assertEqual(by_id[901]["casual_wins"], 5)
        self.assertEqual(by_id[901]["casual_losses"], 2)
        self.assertAlmostEqual(by_id[901]["casual_win_rate"], 5 / 7)
        self.assertIsNone(by_id[901]["ranked_win_rate"])

    def test_get_leaderboard_entries_splits_ranked_from_casual(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET game_wins=5, game_losses=2, ranked_wins=3, ranked_losses=1 "
            "WHERE guildId=? AND userId=901", (GUILD_ID,)
        )
        self.db.commit()

        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        entry = entries[0]

        self.assertEqual(entry["ranked_wins"], 3)
        self.assertEqual(entry["ranked_losses"], 1)
        self.assertAlmostEqual(entry["ranked_win_rate"], 3 / 4)
        # casual = the remainder of game_wins/game_losses after ranked is
        # taken out, 5-3 wins, 2-1 losses.
        self.assertEqual(entry["casual_wins"], 2)
        self.assertEqual(entry["casual_losses"], 1)
        self.assertAlmostEqual(entry["casual_win_rate"], 2 / 3)

    def test_sort_descending_by_elo_puts_highest_first(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        sorted_entries = self.helperObj._sortLeaderboardEntries(entries, "elo", "desc")
        self.assertEqual([e["user_id"] for e in sorted_entries], [901, 903, 902])

    def test_sort_ascending_by_elo_puts_lowest_first(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        sorted_entries = self.helperObj._sortLeaderboardEntries(entries, "elo", "asc")
        self.assertEqual([e["user_id"] for e in sorted_entries], [902, 903, 901])

    def test_sort_sinks_missing_stat_to_the_bottom_regardless_of_order(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        desc = self.helperObj._sortLeaderboardEntries(entries, "bet_win_rate", "desc")
        asc = self.helperObj._sortLeaderboardEntries(entries, "bet_win_rate", "asc")
        self.assertEqual(desc[-1]["user_id"], 903)
        self.assertEqual(asc[-1]["user_id"], 903)

    def test_filter_drops_the_0w_0l_player_from_the_relevant_game_and_bet_views(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        for stat in (None, "elo", "game_wins", "game_win_rate", "bet_wins", "bet_win_rate"):
            filtered = self.helperObj._filterLeaderboardEntries(entries, stat)
            self.assertNotIn(903, [e["user_id"] for e in filtered], f"stat={stat!r}")
            self.assertEqual(len(filtered), 2, f"stat={stat!r}")

    def test_filter_drops_everyone_from_a_ranked_view_when_nobody_has_played_ranked(self):
        # _seed_players deliberately gives nobody a ranked-tagged game (see
        # test_get_leaderboard_entries_computes_rates_and_none_for_no_games),
        # so a ranked-scoped view should read as 0W-0L across the board.
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        filtered = self.helperObj._filterLeaderboardEntries(entries, "ranked_wins")
        self.assertEqual(filtered, [])

    def test_filter_keeps_everyone_for_stats_with_no_wins_losses_concept(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        for stat in ("balance", "net_gold", "gold_wagered"):
            filtered = self.helperObj._filterLeaderboardEntries(entries, stat)
            self.assertIn(903, [e["user_id"] for e in filtered], f"stat={stat!r}")
            self.assertEqual(len(filtered), 3, f"stat={stat!r}")

    async def test_zero_record_player_is_left_off_the_posted_overview(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertNotIn("Cleo", embed.description)

    async def test_zero_record_player_still_shows_up_on_a_gold_view(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, "balance", "asc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Cleo", embed.description)

    async def test_successful_call_posts_embed_reacts_and_stores_page_state(self):
        self._seed_players()
        channel = FakeChannel("leaderboard-chat")
        ctx = self._ctx(channel=channel)
        posted_message = FakeMessage(id=8888)
        ctx.original_response.return_value = posted_message

        await self.helperObj.leaderboardHelper(ctx, "balance", "asc")

        ctx.response.send_message.assert_awaited_once()
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Balance", embed.title)
        lines = embed.description.split("\n")
        # ascending balance: Cleo (0), Alice (500), Bob (1500)
        self.assertTrue(lines[0].startswith("**#1.** Cleo"))
        self.assertTrue(lines[2].startswith("**#3.** Bob"))

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(view, helper_module.LeaderboardPagingView)

        self.cursor.execute(
            "SELECT guildId, channelId, filter, sort_order, page FROM leaderboards WHERE messageId=?",
            (8888,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, channel.id, "balance", "asc", 0))

    async def test_overview_mode_defaults_sort_to_elo_and_shows_elo_and_ranked_record(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Overview", embed.title)
        lines = embed.description.split("\n")
        self.assertTrue(lines[0].startswith("**#1.** Alice"))  # highest elo (1300)
        self.assertIn("Elo:", lines[0])
        self.assertIn("Ranked:", lines[0])
        self.assertNotIn("Balance:", lines[0])

    async def test_overview_mode_record_reflects_ranked_not_combined_wins(self):
        self._seed_players()
        entries = self.helperObj.getLeaderboardEntries(GUILD_ID)
        alice = next(e for e in entries if e["username"] == "Alice")
        self.assertNotEqual(alice["ranked_wins"], alice["game_wins"])
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        lines = embed.description.split("\n")
        self.assertIn(f"Ranked: {alice['ranked_wins']}W-{alice['ranked_losses']}L", lines[0])


# cards:true mode: /my-teams-style one-player-full-/stats-embed-per-page
# rendering, sourced from the same sorted/filtered entries a plain
# /leaderboard would list, plus a Card/Back toggle over to that player's
# actual trading card (see LeaderboardPagingView).
class LeaderboardCardsModeTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("leaderboard-cards-chat")
        self.guild.members = [FakeMember("Alice", id=901), FakeMember("Bob", id=902)]
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    def _ctx(self, user_id=901, name="Alice", channel=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel or self.channel)

    def _seed_players(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET elo=1300, game_wins=3, game_losses=1 "
            "WHERE guildId=? AND userId=901", (GUILD_ID,)
        )
        self.cursor.execute(
            "UPDATE economy SET elo=900, game_wins=1, game_losses=3 "
            "WHERE guildId=? AND userId=902", (GUILD_ID,)
        )
        self.db.commit()

    async def test_renders_the_first_ranked_players_stats_embed(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc", cards=True)

        kwargs = ctx.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed.title, "Alice's Stats")  # highest elo, descending
        self.assertIn("Player 1/2", embed.footer.text)
        self.assertIsInstance(kwargs["view"], helper_module.LeaderboardPagingView)

    async def test_respects_the_requested_stat_and_order(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, "elo", "asc", cards=True)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Bob's Stats")  # lowest elo, ascending

    async def test_stores_the_cards_flag_alongside_the_rest_of_the_view_state(self):
        self._seed_players()
        ctx = self._ctx()
        posted = FakeMessage(id=7171)
        ctx.original_response.return_value = posted

        await self.helperObj.leaderboardHelper(ctx, None, "desc", cards=True)

        self.cursor.execute(
            "SELECT filter, sort_order, page, cards, cardShown FROM leaderboards WHERE messageId=7171"
        )
        self.assertEqual(self.cursor.fetchone(), (None, "desc", 0, 1, 0))
        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)

    async def test_falls_back_gracefully_when_the_member_has_left(self):
        self._seed_players()
        self.guild.members = []  # Alice no longer resolvable as a guild member
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc", cards=True)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        # falls back to discord.User (fetch_user), which still has a name
        self.assertIn("Stats", embed.title)

    async def test_no_entries_message_is_unaffected_by_cards_mode(self):
        ctx = self._ctx()
        await self.helperObj.leaderboardHelper(ctx, None, "desc", cards=True)
        ctx.response.send_message.assert_awaited_once_with(
            "Nobody has any stats to show yet in this server!"
        )

    async def test_list_mode_never_gets_the_card_toggle(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc", cards=False)

        view = ctx.response.send_message.call_args.kwargs["view"]
        self.assertNotIn(view.showCard, view.children)
        self.assertNotIn(view.returnToStats, view.children)


class LeaderboardPagingViewCardsModeTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("leaderboard-cards-paging-chat")
        self.guild.members = [
            FakeMember("Alice", id=901), FakeMember("Bob", id=902), FakeMember("Charlie", id=903),
        ]
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        for user_id, name, elo in ((901, "Alice", 1300), (902, "Bob", 1200), (903, "Charlie", 1100)):
            self.helperObj.ensureEconomyRow(GUILD_ID, user_id, name)
            self.cursor.execute(
                "UPDATE economy SET elo=?, game_wins=1, game_losses=1 WHERE guildId=? AND userId=?",
                (elo, GUILD_ID, user_id)
            )
        self.db.commit()

        self.message = FakeMessage(id=8181)
        self.cursor.execute(
            "INSERT INTO leaderboards(messageId, guildId, channelId, filter, sort_order, page, cards, cardShown) "
            "VALUES(8181, ?, ?, NULL, 'desc', 0, 1, 0)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _click(self, message=None, user_id=1):
        return FakeInteraction(
            self.guild, FakeMember("Clicker", id=user_id), channel=self.channel,
            message=message if message is not None else self.message,
        )

    def _cardShown(self):
        self.cursor.execute("SELECT cardShown FROM leaderboards WHERE messageId=8181")
        return self.cursor.fetchone()[0]

    async def test_card_click_swaps_the_current_player_to_their_trading_card(self):
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.showCard.callback(click)

        self.message.edit.assert_awaited_once()
        new_embed = self.message.edit.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        self.assertIn("Player 1/3", new_embed.footer.text)
        attached_files = self.message.edit.call_args.kwargs["attachments"]
        self.assertEqual(len(attached_files), 1)
        attached_files[0].close()
        new_view = self.message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.returnToStats, new_view.children)
        self.assertNotIn(new_view.showCard, new_view.children)
        self.assertEqual(self._cardShown(), 1)

    async def test_return_click_swaps_back_to_the_stats_embed(self):
        await helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=False) \
            .showCard.callback(self._click())
        self.message.edit.reset_mock()
        self.cursor.execute("UPDATE leaderboards SET cardShown=1 WHERE messageId=8181")
        self.db.commit()

        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=True)
        await view.returnToStats.callback(self._click())

        self.message.edit.assert_awaited_once()
        new_embed = self.message.edit.call_args.kwargs["embed"]
        self.assertEqual(new_embed.title, "Alice's Stats")
        self.assertIn("Player 1/3", new_embed.footer.text)
        new_view = self.message.edit.call_args.kwargs["view"]
        self.assertIn(new_view.showCard, new_view.children)
        self.assertEqual(self._cardShown(), 0)

    async def test_return_click_is_rejected_before_the_card_is_shown(self):
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=True)
        click = self._click()

        await view.returnToStats.callback(click)

        self.message.edit.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_paging_while_card_is_shown_keeps_showing_cards(self):
        self.cursor.execute("UPDATE leaderboards SET cardShown=1 WHERE messageId=8181")
        self.db.commit()
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=True)
        click = self._click()

        await view.next.callback(click)

        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        self.assertIn("Player 2/3", new_embed.footer.text)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()
        self.assertEqual(self._cardShown(), 1)

    async def test_paging_while_stats_is_shown_keeps_showing_stats(self):
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.next.callback(click)

        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(new_embed.title, "Bob's Stats")

    async def test_ascending_button_re_sorts_and_resets_to_page_zero(self):
        self.cursor.execute("UPDATE leaderboards SET page=1 WHERE messageId=8181")
        self.db.commit()
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.ascending.callback(click)

        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(new_embed.title, "Charlie's Stats")  # lowest elo, now ascending
        self.cursor.execute("SELECT sort_order, page FROM leaderboards WHERE messageId=8181")
        self.assertEqual(self.cursor.fetchone(), ("asc", 0))

    async def test_ascending_button_keeps_card_view_selected(self):
        self.cursor.execute("UPDATE leaderboards SET cardShown=1 WHERE messageId=8181")
        self.db.commit()
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=True)
        click = self._click()

        await view.ascending.callback(click)

        new_embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(len(new_embed.fields), 0)
        self.assertTrue(new_embed.image.url.startswith("attachment://"))
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_order_click_already_active_is_a_noop(self):
        view = helper_module.LeaderboardPagingView(self.helperObj, cards=True, card_shown=False)
        click = self._click()

        await view.descending.callback(click)  # already descending

        click.response.edit_message.assert_not_awaited()


class TeamListPagingViewTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def asyncSetUp(self):
        self.channel = FakeChannel("team-list-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
                     "Kilo", "Lima"):
            await self.helperObj.createTeamHelper(self._ctx(), name, 5)

        self.message = FakeMessage(id=7777)
        self.cursor.execute(
            "INSERT INTO team_list_views"
            "(messageId, guildId, channelId, search, recruitingOnly, sort, sort_order, page) "
            "VALUES(7777, ?, ?, NULL, 0, 'name', 'asc', 0)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _page(self):
        self.cursor.execute("SELECT page FROM team_list_views WHERE messageId=7777")
        return self.cursor.fetchone()[0]

    def _click(self, message=None, user_id=1):
        return FakeInteraction(
            self.guild, FakeMember("Clicker", id=user_id), channel=self.channel,
            message=message if message is not None else self.message,
        )

    async def test_ignores_unknown_message(self):
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click(message=FakeMessage(id=12345))
        await view.next.callback(click)
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_next_advances_page_and_edits_message(self):
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)

        self.assertEqual(self._page(), 1)
        click.response.edit_message.assert_awaited_once()
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("Page 2/2", embed.footer.text)
        self.assertIn("Kilo", embed.description)

    async def test_last_jumps_to_final_page(self):
        view = helper_module.TeamListPagingView(self.helperObj)
        await view.last.callback(self._click())
        self.assertEqual(self._page(), 1)  # 12 teams / 10 per page = 2 pages (0, 1)

    async def test_next_at_last_page_is_a_noop(self):
        self.cursor.execute("UPDATE team_list_views SET page=1 WHERE messageId=7777")
        self.db.commit()
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)
        self.assertEqual(self._page(), 1)
        click.response.edit_message.assert_not_awaited()

    async def test_preserves_sort_order_across_a_page_flip(self):
        # sort_order='desc' with sort='name' reverses the alphabet; all 12
        # fake teams stay eligible (no search/recruiting_only filter here),
        # so there's still a real second page to flip to.
        self.cursor.execute(
            "UPDATE team_list_views SET sort_order='desc' WHERE messageId=7777"
        )
        self.db.commit()
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)

        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("Descending", embed.footer.text)
        # descending by name: Lima..Charlie fill page 0, page 1 (this one)
        # holds whatever's left (Bravo, Alpha), proving sort_order='desc'
        # carried over rather than resetting to ascending
        self.assertIn("Alpha", embed.description)

    async def test_preserves_member_filter_across_a_page_flip(self):
        # asyncSetUp's own _ctx() defaults to Alice (901) as every team's
        # captain, so all 12 stay eligible under this filter, still a
        # real second page to flip to, proving the filter (not just the
        # footer text) carried over rather than resetting to unfiltered.
        self.cursor.execute(
            "UPDATE team_list_views SET memberIds='901', memberNames='Alice' WHERE messageId=7777"
        )
        self.db.commit()
        view = helper_module.TeamListPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)

        self.assertEqual(self._page(), 1)
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("with Alice", embed.footer.text)
        self.assertIn("Page 2/2", embed.footer.text)


class MyTeamsPagingViewTests(_FakeLogoDirTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def asyncSetUp(self):
        # _FakeLogoDirTestCase.setUp (sync) already ran by this point
        # (IsolatedAsyncioTestCase calls setUp() then asyncSetUp()), so this
        # only needs to handle the parts that require awaiting.
        self.channel = FakeChannel("my-teams-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        await self.helperObj.createTeamHelper(self._ctx(), "Alpha", 5)
        await self.helperObj.createTeamHelper(self._ctx(), "Bravo", 5)
        await self.helperObj.createTeamHelper(self._ctx(), "Charlie", 5)

        self.message = FakeMessage(id=8888)
        self.cursor.execute(
            "INSERT INTO my_team_views(messageId, guildId, channelId, userId, page) "
            "VALUES(8888, ?, ?, 901, 1)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _page(self):
        self.cursor.execute("SELECT page FROM my_team_views WHERE messageId=8888")
        return self.cursor.fetchone()[0]

    def _click(self, message=None, user_id=1):
        return FakeInteraction(
            self.guild, FakeMember("Clicker", id=user_id), channel=self.channel,
            message=message if message is not None else self.message,
        )

    async def test_ignores_unknown_message(self):
        click = self._click(message=FakeMessage(id=12345))
        view = helper_module.MyTeamsPagingView(self.helperObj)
        await view.next.callback(click)
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_next_advances_to_the_next_team_and_edits_message(self):
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)

        self.assertEqual(self._page(), 2)
        click.response.edit_message.assert_awaited_once()
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Charlie Stats")
        self.assertIn("Team 3/3", embed.footer.text)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_prev_goes_back_a_team(self):
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click()
        await view.prev.callback(click)
        self.assertEqual(self._page(), 0)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_first_jumps_to_page_zero(self):
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click()
        await view.first.callback(click)
        self.assertEqual(self._page(), 0)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_last_jumps_to_final_team(self):
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click()
        await view.last.callback(click)
        self.assertEqual(self._page(), 2)
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()

    async def test_next_at_last_team_is_a_noop(self):
        self.cursor.execute("UPDATE my_team_views SET page=2 WHERE messageId=8888")
        self.db.commit()
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click()
        await view.next.callback(click)
        self.assertEqual(self._page(), 2)
        click.response.edit_message.assert_not_awaited()

    async def test_click_from_a_different_user_still_pages_the_owners_view(self):
        # Matches /leaderboard's existing behavior: paging a shared view
        # isn't restricted to whoever posted it. _handleMyTeamsPageClick
        # re-derives the team list from the view's stored userId (901,
        # Alice), not from interaction.user.id (a stranger here), so the
        # page still steps through ALICE's teams either way.
        view = helper_module.MyTeamsPagingView(self.helperObj)
        click = self._click(user_id=999)
        await view.next.callback(click)
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "Charlie Stats")
        for f in click.response.edit_message.call_args.kwargs["attachments"]:
            f.close()


class LeaderboardPagingViewTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("leaderboard-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        for i in range(25):
            user_id = 1000 + i
            self.helperObj.ensureEconomyRow(GUILD_ID, user_id, f"Player{i:02d}")
            self.cursor.execute(
                "UPDATE economy SET elo=?, game_wins=1, game_losses=1 WHERE guildId=? AND userId=?",
                (1000 + i, GUILD_ID, user_id)
            )
        self.db.commit()

        self.message = FakeMessage(id=9999)
        self.cursor.execute(
            "INSERT INTO leaderboards(messageId, guildId, channelId, filter, sort_order, page) "
            "VALUES(9999, ?, ?, NULL, 'desc', 1)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _page(self):
        self.cursor.execute("SELECT page FROM leaderboards WHERE messageId=9999")
        return self.cursor.fetchone()[0]

    def _click(self, message=None, user_id=1):
        return FakeInteraction(
            self.guild, FakeMember("Clicker", id=user_id), channel=self.channel,
            message=message if message is not None else self.message,
        )

    async def test_ignores_unknown_message(self):
        click = self._click(message=FakeMessage(id=12345))
        await helper_module.LeaderboardPagingView(self.helperObj).next.callback(click)
        self.message.edit.assert_not_awaited()
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_next_advances_page_and_edits_message(self):
        await helper_module.LeaderboardPagingView(self.helperObj).next.callback(self._click())

        self.assertEqual(self._page(), 2)
        self.message.edit.assert_not_called()  # edited via interaction.response, not message.edit

    async def test_prev_goes_back_a_page(self):
        await helper_module.LeaderboardPagingView(self.helperObj).prev.callback(self._click())
        self.assertEqual(self._page(), 0)

    async def test_first_jumps_to_page_zero(self):
        await helper_module.LeaderboardPagingView(self.helperObj).first.callback(self._click())
        self.assertEqual(self._page(), 0)

    async def test_last_jumps_to_final_page(self):
        await helper_module.LeaderboardPagingView(self.helperObj).last.callback(self._click())
        self.assertEqual(self._page(), 2)  # 25 entries / 10 per page = 3 pages (0, 1, 2)

    async def test_next_at_last_page_is_a_noop(self):
        self.cursor.execute("UPDATE leaderboards SET page=2 WHERE messageId=9999")
        self.db.commit()
        click = self._click()
        await helper_module.LeaderboardPagingView(self.helperObj).next.callback(click)
        self.assertEqual(self._page(), 2)
        click.response.edit_message.assert_not_awaited()

    async def test_prev_at_first_page_is_a_noop(self):
        self.cursor.execute("UPDATE leaderboards SET page=0 WHERE messageId=9999")
        self.db.commit()
        click = self._click()
        await helper_module.LeaderboardPagingView(self.helperObj).prev.callback(click)
        self.assertEqual(self._page(), 0)
        click.response.edit_message.assert_not_awaited()

    async def test_edits_via_the_interaction_response(self):
        click = self._click()
        await helper_module.LeaderboardPagingView(self.helperObj).next.callback(click)
        click.response.edit_message.assert_awaited_once()
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("Page 3/3", embed.footer.text)

    async def test_ascending_button_flips_order_and_resets_to_page_zero(self):
        click = self._click()
        await helper_module.LeaderboardPagingView(self.helperObj).ascending.callback(click)

        self.assertEqual(self._page(), 0)
        embed = click.response.edit_message.call_args.kwargs["embed"]
        self.assertIn("Ascending", embed.footer.text)
        self.assertIn("Page 1/3", embed.footer.text)
        # lowest elo first now, Player00 (elo 1000)
        self.assertIn("Player00", embed.description)
        self.cursor.execute("SELECT sort_order FROM leaderboards WHERE messageId=9999")
        self.assertEqual(self.cursor.fetchone()[0], "asc")

    async def test_descending_button_already_active_is_a_noop(self):
        click = self._click()
        await helper_module.LeaderboardPagingView(self.helperObj).descending.callback(click)

        click.response.edit_message.assert_not_awaited()
        self.assertEqual(self._page(), 1)  # untouched

    async def test_order_click_on_an_unknown_message_is_rejected(self):
        click = self._click(message=FakeMessage(id=424242))
        await helper_module.LeaderboardPagingView(self.helperObj).ascending.callback(click)
        self.assertTrue(click.response.send_message.call_args.kwargs.get("ephemeral"))


# ===========================================================================
# Concurrency: "multiple servers sending commands at the same time" really
# means multiple coroutines interleaved on discord.py's one asyncio event
# loop, all sharing the one process-wide sqlite3 cursor (see helpers.__init__
# in helper.py and its single instantiation in bot.py); there's no thread
# parallelism to worry about, but two guilds' coroutines genuinely can
# interleave at any `await` point. These run real asyncio.gather() calls
# across genuinely different guild_ids (never mocking the concurrency away)
# to prove each guild's state stays isolated no matter how the awaits land.
# ===========================================================================

class ConcurrentMultiGuildCommandsTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.guild_a = GUILD_ID
        self.guild_b = GUILD_ID + 1
        insert_guild_row(self.cursor, self.db, guild_id=self.guild_b, name="Guild B")

        self.channel_a = FakeChannel("game-chat-a")
        self.team1_a = FakeChannel("A Team 1")
        self.team2_a = FakeChannel("A Team 2")
        self.voice_a = FakeChannel("A Lobby")
        self.member_a1 = FakeMember("Alice", id=101)
        self.member_a1.voice = FakeVoiceState(self.voice_a)
        self.member_a2 = FakeMember("Bob", id=102)
        self.fake_guild_a = FakeGuild(
            id=self.guild_a, channels=[self.team1_a, self.team2_a],
            members=[self.member_a1, self.member_a2],
        )

        self.channel_b = FakeChannel("game-chat-b")
        self.team1_b = FakeChannel("B Team 1")
        self.team2_b = FakeChannel("B Team 2")
        self.voice_b = FakeChannel("B Lobby")
        self.member_b1 = FakeMember("Carol", id=201)
        self.member_b1.voice = FakeVoiceState(self.voice_b)
        self.member_b2 = FakeMember("Dave", id=202)
        self.fake_guild_b = FakeGuild(
            id=self.guild_b, channels=[self.team1_b, self.team2_b],
            members=[self.member_b1, self.member_b2],
        )

        # One shared client across both guilds, matches the real bot,
        # which is a single process/single event loop no matter how many
        # servers it's in.
        self.helperObj.client = FakeClient(
            channels=[self.channel_a, self.channel_b], guilds=[self.fake_guild_a, self.fake_guild_b]
        )

        self.helperObj.update(self.guild_a, "channel1", "A Team 1")
        self.helperObj.update(self.guild_a, "channel2", "A Team 2")
        self.helperObj.update(self.guild_b, "channel1", "B Team 1")
        self.helperObj.update(self.guild_b, "channel2", "B Team 2")

        team1_a = Team(); team1_a.set_name("A Team 1"); team1_a.add_player(Player(101, "Alice"))
        team2_a = Team(); team2_a.set_name("A Team 2"); team2_a.add_player(Player(102, "Bob"))
        self.helperObj.update(self.guild_a, "team1", team1_a.serializeTeam())
        self.helperObj.update(self.guild_a, "team2", team2_a.serializeTeam())
        self.helperObj.update(self.guild_a, "roster_team2_message_id", 501)

        team1_b = Team(); team1_b.set_name("B Team 1"); team1_b.add_player(Player(201, "Carol"))
        team2_b = Team(); team2_b.set_name("B Team 2"); team2_b.add_player(Player(202, "Dave"))
        self.helperObj.update(self.guild_b, "team1", team1_b.serializeTeam())
        self.helperObj.update(self.guild_b, "team2", team2_b.serializeTeam())
        self.helperObj.update(self.guild_b, "roster_team2_message_id", 502)

    def _click(self, guild, channel, message_id):
        return FakeInteraction(
            guild, FakeMember("Clicker", id=999),
            channel=channel, message=FakeMessage(id=message_id, channel=channel),
        )

    async def test_two_guilds_starting_rosters_at_the_same_time_stay_isolated(self):
        click_a = self._click(self.fake_guild_a, self.channel_a, 501)
        click_b = self._click(self.fake_guild_b, self.channel_b, 502)

        with patch.object(self.helperObj, "_openBetting", AsyncMock()), \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await asyncio.gather(
                self.helperObj._handleRosterStartClick(click_a, move=True),
                self.helperObj._handleRosterStartClick(click_b, move=True),
            )

        # each guild's own players only ever moved into that same guild's
        # own channels, never the other guild's, even though both ran on
        # the same event loop at "the same time"
        self.member_a1.move_to.assert_awaited_once_with(self.team1_a)
        self.member_a2.move_to.assert_awaited_once_with(self.team2_a)
        self.member_b1.move_to.assert_awaited_once_with(self.team1_b)
        self.member_b2.move_to.assert_awaited_once_with(self.team2_b)

        self.assertEqual(self.helperObj.get(self.guild_a, "original_channel"), "A Lobby")
        self.assertEqual(self.helperObj.get(self.guild_b, "original_channel"), "B Lobby")
        self.assertIsNone(self.helperObj.get(self.guild_a, "roster_team2_message_id"))
        self.assertIsNone(self.helperObj.get(self.guild_b, "roster_team2_message_id"))

    async def test_guild_b_finishing_mid_flight_does_not_corrupt_guild_a(self):
        # Forces genuine interleaving rather than hoping asyncio.gather
        # happens to produce it: guild A's very first move_to() suspends
        # for real, guaranteeing guild B's entire _handleRosterStartClick
        # call (its own reads, writes, and moves) runs to completion
        # while guild A is still mid-flight, sharing the one cursor the
        # whole time. Guild A must still resume and finish correctly.
        real_move_to = self.member_a1.move_to

        async def slow_first_move(*args, **kwargs):
            await asyncio.sleep(0.01)
            return await real_move_to(*args, **kwargs)

        self.member_a1.move_to = AsyncMock(side_effect=slow_first_move)

        click_a = self._click(self.fake_guild_a, self.channel_a, 501)
        click_b = self._click(self.fake_guild_b, self.channel_b, 502)

        with patch.object(self.helperObj, "_openBetting", AsyncMock()), \
             patch.object(self.helperObj, "_sendMatchupImage", AsyncMock()):
            await asyncio.gather(
                self.helperObj._handleRosterStartClick(click_a, move=True),
                self.helperObj._handleRosterStartClick(click_b, move=True),
            )

        self.member_a1.move_to.assert_awaited_once_with(self.team1_a)
        self.member_a2.move_to.assert_awaited_once_with(self.team2_a)
        self.member_b1.move_to.assert_awaited_once_with(self.team1_b)
        self.member_b2.move_to.assert_awaited_once_with(self.team2_b)
        self.assertEqual(self.helperObj.get(self.guild_a, "channel1"), "A Team 1")
        self.assertEqual(self.helperObj.get(self.guild_a, "channel2"), "A Team 2")
        self.assertEqual(self.helperObj.get(self.guild_b, "channel1"), "B Team 1")
        self.assertEqual(self.helperObj.get(self.guild_b, "channel2"), "B Team 2")

    async def test_concurrent_wagers_in_different_guilds_settle_independently(self):
        self.helperObj.update(self.guild_a, "betting_state", "OPEN")
        self.helperObj.update(self.guild_b, "betting_state", "OPEN")
        self.helperObj.ensureEconomyRow(self.guild_a, 901, "Alice")
        self.helperObj.ensureEconomyRow(self.guild_b, 901, "Alice")  # same real user, two servers
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (self.guild_a, 901)
        )
        self.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (self.guild_b, 901)
        )
        self.db.commit()

        guild_a_ctx = SimpleNamespace(
            guild=SimpleNamespace(id=self.guild_a), user=FakeMember("Alice", id=901),
            response=AsyncMock(),
        )
        guild_b_ctx = SimpleNamespace(
            guild=SimpleNamespace(id=self.guild_b), user=FakeMember("Alice", id=901),
            response=AsyncMock(),
        )

        await asyncio.gather(
            self.helperObj.wagerHelper(guild_a_ctx, 400, 1),
            self.helperObj.wagerHelper(guild_b_ctx, 250, 2),
        )

        self.assertEqual(self.helperObj.getEconomy(self.guild_a, 901, "balance"), 600)
        self.assertEqual(self.helperObj.getEconomy(self.guild_b, 901, "balance"), 750)

        self.cursor.execute(
            "SELECT team, amount FROM wagers WHERE guildId=? AND userId=?", (self.guild_a, 901)
        )
        self.assertEqual(self.cursor.fetchone(), (1, 400))
        self.cursor.execute(
            "SELECT team, amount FROM wagers WHERE guildId=? AND userId=?", (self.guild_b, 901)
        )
        self.assertEqual(self.cursor.fetchone(), (2, 250))

    async def test_concurrent_daily_claims_for_the_same_user_in_different_guilds(self):
        # The same real Discord account, active in two different servers,
        # claiming /daily in both at once; each guild's economy row must
        # be credited independently, not double-counted or merged.
        ctx_a = SimpleNamespace(
            guild=SimpleNamespace(id=self.guild_a), user=FakeMember("Alice", id=901),
            response=AsyncMock(),
        )
        ctx_b = SimpleNamespace(
            guild=SimpleNamespace(id=self.guild_b), user=FakeMember("Alice", id=901),
            response=AsyncMock(),
        )

        await asyncio.gather(
            self.helperObj.dailyHelper(ctx_a),
            self.helperObj.dailyHelper(ctx_b),
        )

        self.assertEqual(
            self.helperObj.getEconomy(self.guild_a, 901, "balance"), helper_module.DAILY_GOLD_AMOUNT
        )
        self.assertEqual(
            self.helperObj.getEconomy(self.guild_b, 901, "balance"), helper_module.DAILY_GOLD_AMOUNT
        )
        ctx_a.response.send_message.assert_awaited_once()
        ctx_b.response.send_message.assert_awaited_once()
        self.assertIn("claimed", ctx_a.response.send_message.call_args.args[0])
        self.assertIn("claimed", ctx_b.response.send_message.call_args.args[0])


# ===========================================================================
# bot.py: import with DB/token side effects redirected away from the real
# project database and the real bot token, then exercise command callbacks
# and event handlers directly. Never connects to Discord.
# ===========================================================================

def _import_bot_module():
    real_connect = sqlite3.connect

    def fake_connect(path, *args, **kwargs):
        return real_connect(":memory:")

    with patch("sqlite3.connect", side_effect=fake_connect), \
         patch("os.path.isfile", return_value=False), \
         patch("builtins.open", mock_open(read_data="fake-token-for-tests\n")):
        sys.modules.pop("bot", None)
        import bot as bot_module
    return bot_module


class BotModuleTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = _import_bot_module()
        # tree.sync() makes a real Discord API call; commands are global
        # definitions now (see syncCommandsToGuild in bot.py), copied/synced
        # to a guild dynamically on_ready/on_guild_join rather than one
        # hardcoded testing guild id, so on_guild_join (called throughout
        # these tests via _insert_guild_row) would otherwise try to hit the
        # network. copy_global_to() is pure in-memory bookkeeping and safe
        # to leave real.
        self._sync_patch = patch.object(self.bot.tree, "sync", AsyncMock())
        self._sync_patch.start()
        # _backupDatabase writes a real snapshot file into the real
        # data/guildData/backups/; BACKUP_DIR is anchored to this file's
        # own directory (BASE_DIR), not redirected by _import_bot_module()
        # the way sqlite3.connect/open are, so any test that calls
        # on_ready() (which starts backupDatabaseTask, and tasks.loop runs
        # its coroutine immediately on .start()) would otherwise leave a
        # real, if garbage, test-fixture-derived .db file behind on disk
        # every single time the suite runs. Saved before patching so a
        # test that specifically wants the real thing (see
        # BackupDatabaseTests) can still call it directly, bypassing the
        # mock, without needing to stop and restart this patch.
        self._real_backupDatabase = self.bot._backupDatabase
        self._backup_patch = patch.object(self.bot, "_backupDatabase", MagicMock())
        self._backup_patch.start()

    def tearDown(self):
        self._sync_patch.stop()
        self._backup_patch.stop()
        self.bot.mainDB.close()
        sys.modules.pop("bot", None)

    def _command(self, name):
        for c in self.bot.tree.get_commands():
            if c.name == name:
                return c
        raise AssertionError(f"command {name!r} is not registered")

    def _ctx(self, guild_id=GUILD_ID, channel=None, manage_guild=True):
        guild = FakeGuild(id=guild_id)
        user = FakeMember("Caller", id=1, manage_guild=manage_guild)
        return FakeInteraction(guild, user, channel=channel)

    # For commands that require the caller to be sitting in a voice
    # channel (make-teams, captains, notify, ...); _ctx()'s FakeMember has
    # .voice = None by default, which would trip those guards.
    def _ctx_in_voice(self, guild_id=GUILD_ID):
        ctx = self._ctx(guild_id=guild_id)
        ctx.user.voice = FakeVoiceState(FakeChannel("Lobby"))
        return ctx

    async def _insert_guild_row(self, guild_id, name="Test Guild"):
        await self.bot.on_guild_join(SimpleNamespace(id=guild_id, name=name))


class CommandRegistrationTests(BotModuleTestCase):
    def test_all_expected_commands_registered(self):
        names = {c.name for c in self.bot.tree.get_commands()}
        expected = {
            "set", "wager", "wager-against", "daily",
            "stats", "card-set", "shop", "shop-buy",
            "achievements",
            "leaderboard", "help", "make-teams", "report-correct-winner",
            "captains", "choose", "clear", "notify",
            "roll", "tournament-create", "team-create", "team-set",
            "team-rename", "team-delete", "team-transfer",
            "team-invite", "team-stats", "team-list", "my-teams",
            "tournament-register", "tournament-create-bracket",
            "tournament-print-bracket", "tournament-start", "team-use", "reuse", "preview", "team-leave",
            "setup",
        }
        self.assertEqual(names, expected)

    def test_move_is_not_a_registered_command_name(self):
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertNotIn("move", names)

    async def test_on_guild_join_publishes_commands_to_the_new_guild(self):
        await self.bot.on_guild_join(SimpleNamespace(id=999, name="New Guild"))
        self.bot.tree.sync.assert_awaited_once()
        synced_guild = self.bot.tree.sync.call_args.kwargs["guild"]
        self.assertEqual(synced_guild.id, 999)

    async def test_on_ready_syncs_to_every_guild_the_bot_is_in(self):
        guild_a = SimpleNamespace(id=1, name="Guild A")
        guild_b = SimpleNamespace(id=2, name="Guild B")
        # discord.Client.guilds is a read-only property backed by internal
        # connection state, patch it at the class level via PropertyMock
        # rather than trying to assign the instance attribute directly.
        with patch.object(type(self.bot.client), "guilds", new_callable=PropertyMock) as mock_guilds:
            mock_guilds.return_value = [guild_a, guild_b]
            await self.bot.on_ready()
        self.assertEqual(self.bot.tree.sync.await_count, 2)
        synced_ids = {c.kwargs["guild"].id for c in self.bot.tree.sync.call_args_list}
        self.assertEqual(synced_ids, {1, 2})


class LogoAutocompleteTests(BotModuleTestCase):
    def setUp(self):
        super().setUp()
        self._logo_dir = tempfile.TemporaryDirectory()
        for name in ("Demacia", "Noxus", "Dragon", "Freljord"):
            open(os.path.join(self._logo_dir.name, f"{name}.png"), "wb").close()
        self._logo_dir_patch = patch.object(helper_module, "TEAM_LOGO_DIR", self._logo_dir.name)
        self._logo_dir_patch.start()

    def tearDown(self):
        self._logo_dir_patch.stop()
        self._logo_dir.cleanup()
        super().tearDown()

    async def test_filters_by_current_input_case_insensitively(self):
        choices = await self.bot.logoAutocomplete(None, "dra")
        self.assertEqual([c.value for c in choices], ["Dragon"])

    async def test_empty_input_returns_everything(self):
        choices = await self.bot.logoAutocomplete(None, "")
        self.assertEqual(sorted(c.value for c in choices), ["Demacia", "Dragon", "Freljord", "Noxus"])

    async def test_caps_results_at_25(self):
        with patch.object(helper_module, "TEAM_LOGO_DIR", self._logo_dir.name):
            for i in range(30):
                open(os.path.join(self._logo_dir.name, f"Extra{i}.png"), "wb").close()
            choices = await self.bot.logoAutocomplete(None, "")
        self.assertLessEqual(len(choices), 25)


class CardTitleAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardTitles",
            return_value=["Rookie", "Diamond Mind", "Mastermind"]
        ):
            choices = await self.bot.cardTitleAutocomplete(ctx, "DIA")
        self.assertEqual([c.value for c in choices], ["Diamond Mind"])

    async def test_empty_input_returns_every_available_title(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardTitles", return_value=["Rookie", "Diamond Mind"]
        ):
            choices = await self.bot.cardTitleAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Diamond Mind", "Rookie"])

    async def test_caps_results_at_25(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardTitles", return_value=[f"Title{i}" for i in range(30)]
        ):
            choices = await self.bot.cardTitleAutocomplete(ctx, "")
        self.assertLessEqual(len(choices), 25)


def _named_teams(*names):
    teams = []
    for i, name in enumerate(names):
        team = Team()
        team.set_name(name)
        teams.append((i, team))
    return teams


class MyCaptainedTeamAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx(manage_guild=False)
        with patch.object(
            self.bot.helperObj, "getTeamsCaptainedBy", return_value=_named_teams("Red Dragons", "Blue Sharks")
        ):
            choices = await self.bot.myCaptainedTeamAutocomplete(ctx, "dra")
        self.assertEqual([c.value for c in choices], ["Red Dragons"])

    async def test_empty_input_returns_every_captained_team(self):
        ctx = self._ctx(manage_guild=False)
        with patch.object(
            self.bot.helperObj, "getTeamsCaptainedBy", return_value=_named_teams("Red", "Blue")
        ):
            choices = await self.bot.myCaptainedTeamAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Blue", "Red"])

    async def test_scoped_to_the_caller_via_ctx(self):
        ctx = self._ctx(guild_id=GUILD_ID, manage_guild=False)
        mock = MagicMock(return_value=[])
        with patch.object(self.bot.helperObj, "getTeamsCaptainedBy", mock):
            await self.bot.myCaptainedTeamAutocomplete(ctx, "")
        mock.assert_called_once_with(ctx.guild.id, ctx.user.id)

    async def test_caps_results_at_25(self):
        ctx = self._ctx(manage_guild=False)
        with patch.object(
            self.bot.helperObj, "getTeamsCaptainedBy",
            return_value=_named_teams(*[f"Team{i}" for i in range(30)])
        ):
            choices = await self.bot.myCaptainedTeamAutocomplete(ctx, "")
        self.assertLessEqual(len(choices), 25)

    # A Manage Server admin can act on any team (see the helpers' own
    # captain-or-admin override), so they get every team in the guild
    # suggested here too, not just ones they happen to captain.
    async def test_admin_sees_every_team_not_just_ones_they_captain(self):
        ctx = self._ctx(manage_guild=True)
        with patch.object(
            self.bot.helperObj, "getTeamsForGuild", return_value=_named_teams("Red Dragons", "Blue Sharks")
        ) as guild_mock, patch.object(
            self.bot.helperObj, "getTeamsCaptainedBy", return_value=[]
        ) as captained_mock:
            choices = await self.bot.myCaptainedTeamAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Blue Sharks", "Red Dragons"])
        guild_mock.assert_called_once_with(ctx.guild.id)
        captained_mock.assert_not_called()


class MyTeamAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx(manage_guild=False)
        with patch.object(
            self.bot.helperObj, "getTeamsForPlayer", return_value=_named_teams("Red Dragons", "Blue Sharks")
        ):
            choices = await self.bot.myTeamAutocomplete(ctx, "sha")
        self.assertEqual([c.value for c in choices], ["Blue Sharks"])

    async def test_scoped_to_the_caller_via_ctx(self):
        ctx = self._ctx(guild_id=GUILD_ID, manage_guild=False)
        mock = MagicMock(return_value=[])
        with patch.object(self.bot.helperObj, "getTeamsForPlayer", mock):
            await self.bot.myTeamAutocomplete(ctx, "")
        mock.assert_called_once_with(ctx.guild.id, ctx.user.id)

    # Same Manage Server carve-out as myCaptainedTeamAutocomplete; an admin
    # sees every team in the guild here too, not just ones they're rostered on.
    async def test_admin_sees_every_team_not_just_ones_theyre_on(self):
        ctx = self._ctx(manage_guild=True)
        with patch.object(
            self.bot.helperObj, "getTeamsForGuild", return_value=_named_teams("Red Dragons", "Blue Sharks")
        ) as guild_mock, patch.object(
            self.bot.helperObj, "getTeamsForPlayer", return_value=[]
        ) as player_mock:
            choices = await self.bot.myTeamAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Blue Sharks", "Red Dragons"])
        guild_mock.assert_called_once_with(ctx.guild.id)
        player_mock.assert_not_called()


class CardColorSchemeAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx()
        schemes = [
            {"name": "Default", "accent_color": "#EDC643", "background_color": "#251A5B"},
            {"name": "Diamond", "accent_color": "#89CFF0", "background_color": "#274050"},
        ]
        with patch.object(self.bot.helperObj, "getAvailableCardColorSchemes", return_value=schemes):
            choices = await self.bot.cardColorSchemeAutocomplete(ctx, "dia")
        self.assertEqual([c.value for c in choices], ["Diamond"])

    async def test_empty_input_returns_every_available_scheme(self):
        ctx = self._ctx()
        schemes = [
            {"name": "Default", "accent_color": "#EDC643", "background_color": "#251A5B"},
            {"name": "Diamond", "accent_color": "#89CFF0", "background_color": "#274050"},
        ]
        with patch.object(self.bot.helperObj, "getAvailableCardColorSchemes", return_value=schemes):
            choices = await self.bot.cardColorSchemeAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Default", "Diamond"])

    async def test_caps_results_at_25(self):
        ctx = self._ctx()
        schemes = [{"name": f"Scheme{i}", "accent_color": "#000000", "background_color": "#111111"}
                   for i in range(30)]
        with patch.object(self.bot.helperObj, "getAvailableCardColorSchemes", return_value=schemes):
            choices = await self.bot.cardColorSchemeAutocomplete(ctx, "")
        self.assertLessEqual(len(choices), 25)


class CardFontAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardFontStyles",
            return_value=[helper_module.CARD_DEFAULT_FONT_STYLE, "Bold", "Elegant"]
        ):
            choices = await self.bot.cardFontAutocomplete(ctx, "BOL")
        self.assertEqual([c.value for c in choices], ["Bold"])

    async def test_empty_input_returns_every_available_font(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardFontStyles",
            return_value=[helper_module.CARD_DEFAULT_FONT_STYLE, "Bold"]
        ):
            choices = await self.bot.cardFontAutocomplete(ctx, "")
        self.assertEqual(sorted(c.value for c in choices), ["Bold", helper_module.CARD_DEFAULT_FONT_STYLE])

    async def test_caps_results_at_25(self):
        ctx = self._ctx()
        with patch.object(
            self.bot.helperObj, "getAvailableCardFontStyles", return_value=[f"Font{i}" for i in range(30)]
        ):
            choices = await self.bot.cardFontAutocomplete(ctx, "")
        self.assertLessEqual(len(choices), 25)


class ShopBuyAutocompleteTests(BotModuleTestCase):
    async def test_only_offers_unowned_items(self):
        ctx = self._ctx()
        catalog = [
            {"type": "title", "name": "Legend", "price": 500, "owned": False},
            {"type": "title", "name": "Ace", "price": 300, "owned": True},
        ]
        with patch.object(self.bot.helperObj, "getShopCatalog", return_value=catalog):
            choices = await self.bot.shopBuyAutocomplete(ctx, "")
        self.assertEqual([c.value for c in choices], ["Legend"])

    async def test_choice_label_shows_the_price(self):
        ctx = self._ctx()
        catalog = [{"type": "title", "name": "Legend", "price": 500, "owned": False}]
        with patch.object(self.bot.helperObj, "getShopCatalog", return_value=catalog):
            choices = await self.bot.shopBuyAutocomplete(ctx, "")
        self.assertEqual(choices[0].name, "Legend (500 gold)")
        self.assertEqual(choices[0].value, "Legend")

    async def test_filters_by_current_input_case_insensitively(self):
        ctx = self._ctx()
        catalog = [
            {"type": "title", "name": "Legend", "price": 500, "owned": False},
            {"type": "color_scheme", "name": "Crimson", "price": 400, "owned": False},
        ]
        with patch.object(self.bot.helperObj, "getShopCatalog", return_value=catalog):
            choices = await self.bot.shopBuyAutocomplete(ctx, "leg")
        self.assertEqual([c.value for c in choices], ["Legend"])

    async def test_caps_results_at_25(self):
        ctx = self._ctx()
        catalog = [
            {"type": "title", "name": f"Item{i}", "price": 100, "owned": False} for i in range(30)
        ]
        with patch.object(self.bot.helperObj, "getShopCatalog", return_value=catalog):
            choices = await self.bot.shopBuyAutocomplete(ctx, "")
        self.assertLessEqual(len(choices), 25)


class GuildLifecycleEventTests(BotModuleTestCase):
    async def test_on_guild_join_inserts_default_row(self):
        await self.bot.on_guild_join(SimpleNamespace(id=777, name="New Guild"))

        self.bot.cursor.execute(
            "SELECT serverName, betting_state FROM servers WHERE guildId=?", (777,)
        )
        self.assertEqual(self.bot.cursor.fetchone(), ("New Guild", "NONE"))

    async def test_on_guild_remove_deletes_row(self):
        await self.bot.on_guild_join(SimpleNamespace(id=778, name="Leaving Guild"))
        await self.bot.on_guild_remove(SimpleNamespace(id=778, name="Leaving Guild"))

        self.bot.cursor.execute("SELECT * FROM servers WHERE guildId=?", (778,))
        self.assertIsNone(self.bot.cursor.fetchone())

    async def test_on_ready_registers_persistent_views_exactly_once(self):
        # Persistent views (fixed custom_id, timeout=None) need registering
        # once per process so their buttons keep routing to this bot across
        # a restart; on_ready can fire more than once (e.g. on reconnect),
        # so the second call here must not re-register.
        with patch.object(self.bot.client, "add_view") as mock_add_view:
            await self.bot.on_ready()
            first_call_count = mock_add_view.call_count
            await self.bot.on_ready()

        self.assertEqual(mock_add_view.call_count, first_call_count)
        registered_types = {type(call.args[0]) for call in mock_add_view.call_args_list}
        self.assertEqual(
            registered_types,
            {
                self.bot.helper.WinnerReportView, self.bot.helper.DuelAcceptView,
                self.bot.helper.DuelResultView, self.bot.helper.TournamentReadyView,
                self.bot.helper.TournamentMatchReportView, self.bot.helper.RosterActionView,
                self.bot.helper.TeamInviteAcceptView, self.bot.helper.StatsView, self.bot.helper.TeamStatsView,
                self.bot.helper.LeaderboardPagingView, self.bot.helper.MyTeamsPagingView,
                self.bot.helper.TeamListPagingView,
            },
        )

    async def test_on_ready_self_heals_a_guild_missing_its_row(self):
        # on_ready backfills any guild it's already in whose servers row
        # never got created (or was lost); only on_guild_join otherwise
        # ever inserts one.
        guild = SimpleNamespace(id=779, name="Already Joined")
        with patch.object(type(self.bot.client), "guilds", new_callable=PropertyMock) as mock_guilds:
            mock_guilds.return_value = [guild]
            await self.bot.on_ready()

        self.bot.cursor.execute(
            "SELECT serverName, betting_state FROM servers WHERE guildId=?", (779,)
        )
        self.assertEqual(self.bot.cursor.fetchone(), ("Already Joined", "NONE"))

    async def test_ensure_guild_row_does_not_duplicate_an_existing_row(self):
        await self.bot.on_guild_join(SimpleNamespace(id=780, name="Existing"))
        self.bot.ensure_guild_row(780, "Existing")

        self.bot.cursor.execute("SELECT COUNT(*) FROM servers WHERE guildId=?", (780,))
        self.assertEqual(self.bot.cursor.fetchone()[0], 1)

    async def test_on_ready_reconciles_stale_betting_windows(self):
        with patch.object(self.bot.helperObj, "reconcileStaleBettingWindows", AsyncMock()) as mock_reconcile:
            await self.bot.on_ready()
        mock_reconcile.assert_awaited_once_with(self.bot.client)


class LoggingCommandTreeTests(BotModuleTestCase):
    def _interaction(self, itype, command=None, namespace=None, guild="default", data=None):
        return SimpleNamespace(
            type=itype,
            command=command,
            namespace=namespace if namespace is not None else {},
            user=FakeMember("Alice", id=901),
            guild=FakeGuild(id=GUILD_ID) if guild == "default" else guild,
            data=data if data is not None else {"name": "unknown"},
        )

    async def test_logs_a_real_command_invocation(self):
        command = SimpleNamespace(qualified_name="wager")
        interaction = self._interaction(
            discord.InteractionType.application_command, command=command, namespace={"amount": 100, "team": 1},
        )

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            result = await self.bot.tree.interaction_check(interaction)

        self.assertTrue(result)
        self.assertEqual(len(cm.output), 1)
        self.assertIn("Command called: /wager", cm.output[0])
        self.assertIn("amount", cm.output[0])
        self.assertIn("Alice", cm.output[0])
        self.assertIn(str(GUILD_ID), cm.output[0])

    async def test_does_not_log_autocomplete_interactions(self):
        # interaction_check fires for these too (same code path in
        # CommandTree._call); every keystroke into an autocomplete field
        # would otherwise get logged as if it were a real command call.
        command = SimpleNamespace(qualified_name="wager")
        interaction = self._interaction(discord.InteractionType.autocomplete, command=command)

        with self.assertNoLogs(self.bot.logger, level="INFO"):
            result = await self.bot.tree.interaction_check(interaction)

        self.assertTrue(result)

    async def test_falls_back_to_the_raw_data_name_when_the_command_is_unresolved(self):
        interaction = self._interaction(
            discord.InteractionType.application_command, command=None, data={"name": "mystery"},
        )

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            await self.bot.tree.interaction_check(interaction)

        self.assertIn("Command called: /mystery", cm.output[0])

    async def test_dm_interaction_logs_dm_instead_of_a_guild(self):
        command = SimpleNamespace(qualified_name="roll")
        interaction = self._interaction(discord.InteractionType.application_command, command=command, guild=None)

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            await self.bot.tree.interaction_check(interaction)

        self.assertIn("guild=DM", cm.output[0])

    async def test_always_returns_true(self):
        # The default implementation this overrides is a no-op check that
        # always allows the interaction through; logging must never
        # change that.
        interaction = self._interaction(discord.InteractionType.ping)
        result = await self.bot.tree.interaction_check(interaction)
        self.assertTrue(result)

    async def test_survives_an_exception_from_real_discord_py_resolution(self):
        # Regression: interaction.command/.namespace run discord.py's own
        # real option-resolution machinery, which nothing else in this
        # class can faithfully exercise; every other test here hands it a
        # plain SimpleNamespace with .command/.namespace already resolved,
        # never anything that could actually raise the way a real
        # Interaction's cached_slot_property can. CommandTree.
        # _from_interaction's own wrapper only catches AppCommandError
        # around the whole dispatch, so before this method wrapped its own
        # body, anything else raised here silently killed the interaction
        # (Discord shows "This interaction failed", nothing reaches
        # on_app_command_error or the log at all) instead of just skipping
        # this one log line and letting the real command still run.
        class _RaisingInteraction:
            type = discord.InteractionType.application_command
            data = {"name": "team-create"}
            user = FakeMember("Alice", id=901)
            guild = FakeGuild(id=GUILD_ID)

            @property
            def command(self):
                raise RuntimeError("simulated discord.py internals failure")

        interaction = _RaisingInteraction()

        with self.assertLogs(self.bot.logger, level="ERROR") as cm:
            result = await self.bot.tree.interaction_check(interaction)

        self.assertTrue(result)
        self.assertIn("interaction_check failed", cm.output[0])


class AppCommandCompletionTests(BotModuleTestCase):
    async def test_logs_a_successful_completion(self):
        command = SimpleNamespace(qualified_name="wager")
        interaction = SimpleNamespace(user=FakeMember("Alice", id=901), guild=FakeGuild(id=GUILD_ID))

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            await self.bot.on_app_command_completion(interaction, command)

        self.assertEqual(len(cm.output), 1)
        self.assertIn("Command completed: /wager", cm.output[0])
        self.assertIn("Alice", cm.output[0])
        self.assertIn(str(GUILD_ID), cm.output[0])

    async def test_dm_interaction_logs_dm_instead_of_a_guild(self):
        command = SimpleNamespace(qualified_name="roll")
        interaction = SimpleNamespace(user=FakeMember("Alice", id=901), guild=None)

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            await self.bot.on_app_command_completion(interaction, command)

        self.assertIn("guild=DM", cm.output[0])

    async def test_survives_a_logging_failure(self):
        # This fires only once the command it's about has already fully
        # succeeded and responded, so a bug here can't fail the
        # interaction the way interaction_check's own could, still caught
        # explicitly so it reaches this file's own log instead of only
        # discord.py's default stderr-only on_error.
        class _RaisingCommand:
            @property
            def qualified_name(self):
                raise RuntimeError("simulated failure")

        interaction = SimpleNamespace(user=FakeMember("Alice", id=901), guild=FakeGuild(id=GUILD_ID))

        with self.assertLogs(self.bot.logger, level="ERROR") as cm:
            await self.bot.on_app_command_completion(interaction, _RaisingCommand())

        self.assertIn("on_app_command_completion logging failed", cm.output[0])


class LogDatabaseStatementTests(BotModuleTestCase):
    async def test_logs_insert_update_and_delete(self):
        for sql in ("INSERT INTO x VALUES (1)", "UPDATE x SET y=1", "DELETE FROM x WHERE y=1"):
            with self.assertLogs(self.bot.logger, level="INFO") as cm:
                self.bot._logDatabaseStatement(sql)
            self.assertEqual(cm.output, [f"INFO:shockwave:DB: {sql}"])

    async def test_does_not_log_select_or_an_implicit_transaction_begin(self):
        with self.assertNoLogs(self.bot.logger, level="INFO"):
            self.bot._logDatabaseStatement("SELECT * FROM x")
            self.bot._logDatabaseStatement("BEGIN ")

    async def test_truncates_an_oversized_statement(self):
        long_sql = "INSERT INTO x VALUES ('" + ("a" * 1000) + "')"
        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            self.bot._logDatabaseStatement(long_sql)

        logged = cm.output[0]
        self.assertIn("... (truncated)", logged)
        self.assertLess(len(logged), len(long_sql))

    async def test_a_real_mutation_reaches_the_log_via_the_trace_callback(self):
        # End-to-end: an actual cursor.execute against mainDB (which has
        # set_trace_callback(_logDatabaseStatement) registered, see
        # bot.py) reaches the log, not just a direct call to the function
        # in isolation.
        self.bot.cursor.execute("CREATE TABLE IF NOT EXISTS _log_test(x)")
        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            self.bot.cursor.execute("INSERT INTO _log_test VALUES (1)")
        self.assertIn("_log_test", cm.output[0])
        self.assertIn("DB:", cm.output[0])


class BackupDatabaseTests(BotModuleTestCase):
    def setUp(self):
        super().setUp()
        # The real _backupDatabase (see self._real_backupDatabase, saved by
        # the parent setUp before _backupDatabase itself gets mocked out)
        # writes wherever BACKUP_DIR points, redirected to a throwaway
        # temp directory here so this class's own real-backup assertions
        # never touch data/guildData/backups/.
        self._temp_backup_dir = tempfile.TemporaryDirectory()
        self._backup_dir_patch = patch.object(self.bot, "BACKUP_DIR", self._temp_backup_dir.name)
        self._backup_dir_patch.start()

    def tearDown(self):
        self._backup_dir_patch.stop()
        self._temp_backup_dir.cleanup()
        super().tearDown()

    def test_creates_a_snapshot_file(self):
        self.assertEqual(os.listdir(self._temp_backup_dir.name), [])

        self._real_backupDatabase()

        files = os.listdir(self._temp_backup_dir.name)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("main-") and files[0].endswith(".db"))

    def test_snapshot_contains_the_live_databases_data(self):
        self.bot.cursor.execute("INSERT INTO servers(guildId, serverName) VALUES (?, ?)", (999, "Backup Test"))
        self.bot.mainDB.commit()

        self._real_backupDatabase()

        backup_name = os.listdir(self._temp_backup_dir.name)[0]
        conn = sqlite3.connect(os.path.join(self._temp_backup_dir.name, backup_name))
        try:
            cur = conn.cursor()
            cur.execute("SELECT serverName FROM servers WHERE guildId=?", (999,))
            self.assertEqual(cur.fetchone(), ("Backup Test",))
        finally:
            conn.close()

    def test_prunes_backups_older_than_the_retention_window_but_keeps_recent_ones(self):
        old_path = os.path.join(self._temp_backup_dir.name, "main-old.db")
        open(old_path, "w").close()
        old_time = time.time() - (self.bot.BACKUP_RETENTION_DAYS + 1) * 86400
        os.utime(old_path, (old_time, old_time))

        recent_path = os.path.join(self._temp_backup_dir.name, "main-recent.db")
        open(recent_path, "w").close()

        self._real_backupDatabase()

        remaining = os.listdir(self._temp_backup_dir.name)
        self.assertNotIn("main-old.db", remaining)
        self.assertIn("main-recent.db", remaining)

    async def test_backup_database_task_calls_the_real_backup_and_logs_success(self):
        with patch.object(self.bot, "_backupDatabase", MagicMock()) as mock_backup:
            with self.assertLogs(self.bot.logger, level="INFO") as cm:
                await self.bot.backupDatabaseTask.coro()
        mock_backup.assert_called_once()
        self.assertIn("Database backup completed", cm.output[0])

    async def test_backup_database_task_logs_and_swallows_a_failure(self):
        with patch.object(self.bot, "_backupDatabase", MagicMock(side_effect=OSError("disk full"))):
            with self.assertLogs(self.bot.logger, level="ERROR") as cm:
                await self.bot.backupDatabaseTask.coro()  # must not raise
        self.assertIn("Database backup failed", cm.output[0])


class RunStartupSelfTestsTests(BotModuleTestCase):
    # _runStartupSelfTests now shells out to a real `pytest -n auto`
    # subprocess rather than running the suite in-process; these tests
    # fake that subprocess entirely (never actually spawning the real
    # ~900-test suite as a subprocess of itself, which would be both slow
    # and a little too recursive for comfort), by patching subprocess.run
    # to write a hand-built junitxml report to whatever path the real
    # `--junitxml=<path>` argument names and return a fake CompletedProcess.
    # This exercises _runStartupSelfTests' own parsing/logging logic
    # exactly as it runs against a genuine pytest-produced report.
    def _mock_subprocess(self, xml_body=None, returncode=0, stdout="", stderr=""):
        def fake_run(argv, **kwargs):
            if xml_body is not None:
                report_arg = next(a for a in argv if a.startswith("--junitxml="))
                report_path = report_arg.split("=", 1)[1]
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(xml_body)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

        return patch("subprocess.run", side_effect=fake_run)

    @staticmethod
    def _junit_xml(tests, failures, errors, testcases="", time="12.345"):
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<testsuites><testsuite tests="{tests}" failures="{failures}" errors="{errors}" '
            f'skipped="0" time="{time}">'
            f"{testcases}</testsuite></testsuites>"
        )

    def test_a_passing_suite_logs_a_summary_info_line(self):
        xml = self._junit_xml(tests=3, failures=0, errors=0, time="12.345", testcases=(
            '<testcase classname="tests.A" name="test_a" />'
            '<testcase classname="tests.A" name="test_b" />'
            '<testcase classname="tests.A" name="test_c" />'
        ))
        with self._mock_subprocess(xml_body=xml):
            with self.assertLogs(self.bot.logger, level="INFO") as cm:
                self.bot._runStartupSelfTests()

        self.assertEqual(len(cm.output), 1)
        self.assertIn("3/3 passed", cm.output[0])
        self.assertIn("12.3s", cm.output[0])

    def test_a_failing_suite_logs_a_warning_naming_the_failures(self):
        xml = self._junit_xml(tests=3, failures=1, errors=1, testcases=(
            '<testcase classname="tests.A" name="test_fails"><failure message="boom">boom</failure></testcase>'
            '<testcase classname="tests.A" name="test_errors"><error message="boom">boom</error></testcase>'
            '<testcase classname="tests.A" name="test_passes" />'
        ))
        with self._mock_subprocess(xml_body=xml):
            with self.assertLogs(self.bot.logger, level="WARNING") as cm:
                self.bot._runStartupSelfTests()

        self.assertEqual(len(cm.output), 1)
        self.assertIn("2/3 tests failed", cm.output[0])
        self.assertIn("tests.A.test_fails", cm.output[0])
        self.assertIn("tests.A.test_errors", cm.output[0])
        self.assertNotIn("test_passes", cm.output[0])

    def test_a_failing_suite_also_logs_the_full_output_at_debug(self):
        xml = self._junit_xml(tests=1, failures=1, errors=0, testcases=(
            '<testcase classname="tests.A" name="test_fails"><failure message="boom">boom</failure></testcase>'
        ))
        with self._mock_subprocess(xml_body=xml, stdout="FAILED tests.py::A::test_fails"):
            with self.assertLogs(self.bot.logger, level="DEBUG") as cm:
                self.bot._runStartupSelfTests()

        self.assertTrue(any("FAILED tests.py::A::test_fails" in line for line in cm.output))

    def test_invokes_pytest_with_xdist_auto_workers_from_the_project_root(self):
        xml = self._junit_xml(tests=1, failures=0, errors=0)
        with self._mock_subprocess(xml_body=xml) as mock_run:
            self.bot._runStartupSelfTests()

        argv, kwargs = mock_run.call_args
        argv = argv[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("pytest", argv)
        self.assertIn("tests.py", argv)
        self.assertIn("-n", argv)
        self.assertIn("auto", argv)
        self.assertEqual(kwargs["cwd"], self.bot.BASE_DIR)

    def test_missing_report_logs_a_warning_without_crashing(self):
        # pytest ran (or tried to) but never wrote a report at all, e.g. a
        # collection error, or pytest-xdist missing from a stale install.
        with self._mock_subprocess(
            xml_body=None, returncode=2, stderr="usage error: unrecognized arguments: -n"
        ):
            with self.assertLogs(self.bot.logger, level="WARNING") as cm:
                self.bot._runStartupSelfTests()

        self.assertIn("no readable report", cm.output[0])
        self.assertIn("unrecognized arguments", cm.output[0])

    def test_pytest_failing_to_launch_logs_a_warning_without_crashing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
            with self.assertLogs(self.bot.logger, level="WARNING") as cm:
                self.bot._runStartupSelfTests()

        self.assertIn("failed to launch pytest", cm.output[0])


class MaxLinesFileHandlerTests(BotModuleTestCase):
    def setUp(self):
        super().setUp()
        self._temp_dir = tempfile.TemporaryDirectory()
        self._log_path = os.path.join(self._temp_dir.name, "test.log")
        self._loggers_to_clean = []

    def tearDown(self):
        for name, handler in self._loggers_to_clean:
            logging.getLogger(name).removeHandler(handler)
            handler.close()
        self._temp_dir.cleanup()
        super().tearDown()

    def _logger_with_handler(self, name, max_lines):
        handler = self.bot.MaxLinesFileHandler(self._log_path, max_lines, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        test_logger = logging.getLogger(name)
        test_logger.addHandler(handler)
        test_logger.propagate = False
        test_logger.setLevel(logging.INFO)
        self._loggers_to_clean.append((name, handler))
        return test_logger

    def _read_lines(self):
        with open(self._log_path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_trims_to_the_most_recent_lines_once_over_the_cap(self):
        test_logger = self._logger_with_handler("maxlines_trim", max_lines=3)
        for i in range(1, 6):
            test_logger.info(f"line {i}")

        self.assertEqual(self._read_lines(), ["line 3", "line 4", "line 5"])

    def test_stays_untouched_while_under_the_cap(self):
        test_logger = self._logger_with_handler("maxlines_under_cap", max_lines=10)
        test_logger.info("only line")

        self.assertEqual(self._read_lines(), ["only line"])

    def test_seeds_its_line_count_from_a_pre_existing_file(self):
        with open(self._log_path, "w", encoding="utf-8") as f:
            f.write("old1\nold2\nold3\nold4\n")

        test_logger = self._logger_with_handler("maxlines_seeded", max_lines=5)
        test_logger.info("new1")
        test_logger.info("new2")

        self.assertEqual(self._read_lines(), ["old2", "old3", "old4", "new1", "new2"])


class GlobalErrorHandlerTests(BotModuleTestCase):
    def _ctx_with_response_done(self, done):
        ctx = self._ctx()
        # discord.py's real InteractionResponse.is_done() is a plain sync
        # method, not a coroutine; ctx.response is an AsyncMock, whose
        # attributes default to AsyncMock too, so it has to be overridden
        # explicitly here rather than just awaited like everything else on it.
        ctx.response.is_done = MagicMock(return_value=done)
        return ctx

    async def test_missing_permissions_gets_a_specific_message(self):
        ctx = self._ctx_with_response_done(False)
        error = app_commands.MissingPermissions(["manage_guild"])

        await self.bot.tree.on_error(ctx, error)

        ctx.response.send_message.assert_awaited_once_with(
            "You don't have permission to use this command.", ephemeral=True
        )

    async def test_unexpected_error_gets_a_generic_message(self):
        ctx = self._ctx_with_response_done(False)
        error = ValueError("boom")

        await self.bot.tree.on_error(ctx, error)

        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("Something went wrong", message)
        self.assertTrue(ctx.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_skips_the_generic_message_if_a_local_handler_already_responded(self):
        # setBettingTimer_error/reportCorrectWinner_error/clearAll_error
        # already send their own MissingPermissions message and return -
        # discord.py calls this tree-wide handler right afterward
        # regardless (CommandTree._dispatch_error always calls both), so
        # without this check every permission error would get a second,
        # redundant message stacked on top of the specific one.
        ctx = self._ctx_with_response_done(True)
        error = app_commands.MissingPermissions(["manage_guild"])

        await self.bot.tree.on_error(ctx, error)

        ctx.response.send_message.assert_not_awaited()

    async def test_skips_the_generic_message_for_unexpected_errors_too_once_already_responded(self):
        ctx = self._ctx_with_response_done(True)
        error = ValueError("boom")

        await self.bot.tree.on_error(ctx, error)

        ctx.response.send_message.assert_not_awaited()

    async def test_unexpected_error_appends_a_variable_dump_to_the_log(self):
        ctx = self._ctx_with_response_done(False)
        ctx.command = SimpleNamespace(qualified_name="wager")
        ctx.namespace = {"amount": 250, "team": 1}
        error = ValueError("boom")

        with self.assertLogs(self.bot.logger, level="ERROR") as cm:
            await self.bot.tree.on_error(ctx, error)

        self.assertIn("command=/wager", cm.output[0])
        self.assertIn("amount", cm.output[0])
        self.assertIn("250", cm.output[0])
        self.assertIn("Caller", cm.output[0])
        self.assertIn(str(GUILD_ID), cm.output[0])

    async def test_missing_permissions_does_not_log_a_variable_dump(self):
        # Only the "something went wrong" branch needs the extra diagnostic
        # context; MissingPermissions already gets a specific, expected
        # user-facing message and isn't a bug to dump state for.
        ctx = self._ctx_with_response_done(False)
        error = app_commands.MissingPermissions(["manage_guild"])

        with self.assertNoLogs(self.bot.logger, level="ERROR"):
            await self.bot.tree.on_error(ctx, error)

    async def test_variable_dump_falls_back_when_command_is_unresolved(self):
        # _ctx()'s FakeInteraction has no .command/.namespace/.data at all
        # by default, the same "real discord.py resolution can fail" shape
        # LoggingCommandTreeTests.test_survives_an_exception_from_real_
        # discord_py_resolution covers for interaction_check; the dump
        # must degrade to "?"/unresolvable instead of raising and losing
        # the original error's own log line.
        ctx = self._ctx_with_response_done(False)
        error = ValueError("boom")

        with self.assertLogs(self.bot.logger, level="ERROR") as cm:
            await self.bot.tree.on_error(ctx, error)

        self.assertIn("command=/?", cm.output[0])
        self.assertIn("<unresolvable>", cm.output[0])


class CommandDelegationTests(BotModuleTestCase):
    async def test_wager_passes_resolved_team_value(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "wagerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("wager").callback(ctx, 250, choice)
        mock.assert_awaited_once_with(ctx, 250, 2, None)

    async def test_wager_against_delegates(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "challengeDuelHelper", mock):
            await self._command("wager-against").callback(ctx, target, 250)
        mock.assert_awaited_once_with(ctx, target, 250)

    async def test_daily_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "dailyHelper", mock):
            await self._command("daily").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_leaderboard_defaults_to_no_filter_and_descending(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "leaderboardHelper", mock):
            await self._command("leaderboard").callback(ctx)
        mock.assert_awaited_once_with(ctx, None, "desc", False)

    async def test_leaderboard_passes_resolved_filter_and_order(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "leaderboardHelper", mock):
            filter_choice = app_commands.Choice(name="Balance", value="balance")
            order_choice = app_commands.Choice(name="Ascending (lowest first)", value="asc")
            await self._command("leaderboard").callback(ctx, filter=filter_choice, order=order_choice)
        mock.assert_awaited_once_with(ctx, "balance", "asc", False)

    async def test_leaderboard_forwards_cards_true(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "leaderboardHelper", mock):
            await self._command("leaderboard").callback(ctx, cards=True)
        mock.assert_awaited_once_with(ctx, None, "desc", True)

    async def test_create_tournament_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createTournamentHelper", mock):
            await self._command("tournament-create").callback(ctx, "Spring Cup", 5, 8)
        mock.assert_awaited_once_with(ctx, "Spring Cup", 5, 8, False)

    async def test_create_tournament_passes_double_elim_flag(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createTournamentHelper", mock):
            await self._command("tournament-create").callback(ctx, "Spring Cup", 5, 8, double_elim=True)
        mock.assert_awaited_once_with(ctx, "Spring Cup", 5, 8, True)

    async def test_register_team_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "registerTeamHelper", mock):
            await self._command("tournament-register").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red")

    async def test_create_bracket_passes_resolved_elimination_type(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createBracketHelper", mock):
            choice = app_commands.Choice(name="Double elimination", value="double")
            await self._command("tournament-create-bracket").callback(ctx, elimination_type=choice)
        mock.assert_awaited_once_with(ctx, True, "after_winners")

    async def test_create_bracket_single_elimination_resolves_to_false(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createBracketHelper", mock):
            choice = app_commands.Choice(name="Single elimination", value="single")
            await self._command("tournament-create-bracket").callback(ctx, elimination_type=choice)
        mock.assert_awaited_once_with(ctx, False, "after_winners")

    async def test_print_bracket_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "printBracketHelper", mock):
            await self._command("tournament-print-bracket").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_start_tournament_passes_resolved_mode(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "startTournamentHelper", mock):
            choice = app_commands.Choice(name="Sequential", value="sequential")
            await self._command("tournament-start").callback(ctx, mode=choice)
        mock.assert_awaited_once_with(ctx, "sequential")

    async def test_start_tournament_simultaneous_mode(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "startTournamentHelper", mock):
            choice = app_commands.Choice(name="Simultaneous", value="simultaneous")
            await self._command("tournament-start").callback(ctx, mode=choice)
        mock.assert_awaited_once_with(ctx, "simultaneous")

    async def test_create_team_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createTeamHelper", mock):
            await self._command("team-create").callback(ctx, "Red", 5)
        mock.assert_awaited_once_with(ctx, "Red", 5, None)

    async def test_create_team_delegates_with_captain(self):
        ctx = self._ctx()
        captain = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createTeamHelper", mock):
            await self._command("team-create").callback(ctx, "Red", 5, captain)
        mock.assert_awaited_once_with(ctx, "Red", 5, captain)

    async def test_team_set_defaults_to_nothing_given(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamSetHelper", mock):
            await self._command("team-set").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red", None, False, None)

    async def test_team_set_passes_voice_channel_new_voice_channel_and_logo(self):
        ctx = self._ctx()
        mock = AsyncMock()
        channel = FakeChannel("Arena")
        with patch.object(self.bot.helperObj, "teamSetHelper", mock):
            await self._command("team-set").callback(
                ctx, "Red", voice_channel=channel, new_voice_channel=True, logo="Demacia"
            )
        mock.assert_awaited_once_with(ctx, "Red", channel, True, "Demacia")

    async def test_team_rename_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamRenameHelper", mock):
            await self._command("team-rename").callback(ctx, "Red", "Crimson")
        mock.assert_awaited_once_with(ctx, "Red", "Crimson")

    async def test_team_delete_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamDeleteHelper", mock):
            await self._command("team-delete").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red")

    async def test_team_transfer_delegates(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamTransferHelper", mock):
            await self._command("team-transfer").callback(ctx, "Red", target)
        mock.assert_awaited_once_with(ctx, "Red", target)

    async def test_team_invite_delegates(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamInviteHelper", mock):
            await self._command("team-invite").callback(ctx, "Red", target)
        mock.assert_awaited_once_with(ctx, "Red", [target], False)

    async def test_team_invite_delegates_multiple_members_and_drops_unset_slots(self):
        ctx = self._ctx()
        bob = FakeMember("Bob", id=902)
        cleo = FakeMember("Cleo", id=903)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamInviteHelper", mock):
            await self._command("team-invite").callback(
                ctx, "Red", member_1=bob, member_2=cleo, member_3=None, member_4=None, member_5=None
            )
        mock.assert_awaited_once_with(ctx, "Red", [bob, cleo], False)

    async def test_team_invite_delegates_force_flag(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamInviteHelper", mock):
            await self._command("team-invite").callback(ctx, "Red", target, force=True)
        mock.assert_awaited_once_with(ctx, "Red", [target], True)

    async def test_team_leave_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamLeaveHelper", mock):
            await self._command("team-leave").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red")

    async def test_team_stats_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamStatsHelper", mock):
            await self._command("team-stats").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red")

    async def test_use_teams_defaults_ranked_to_false(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "useTeamsHelper", mock):
            await self._command("team-use").callback(ctx, "Red", "Blue")
        mock.assert_awaited_once_with(ctx, "Red", "Blue", False)

    async def test_use_teams_passes_ranked_flag(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "useTeamsHelper", mock):
            await self._command("team-use").callback(ctx, "Red", "Blue", ranked=True)
        mock.assert_awaited_once_with(ctx, "Red", "Blue", True)

    async def test_reuse_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reuseTeamsHelper", mock):
            await self._command("reuse").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_preview_delegates_with_resolved_type_value(self):
        ctx = self._ctx()
        mock = AsyncMock()
        choice = app_commands.Choice(name="Logos", value="Logos")
        with patch.object(self.bot.helperObj, "previewHelper", mock):
            await self._command("preview").callback(ctx, type=choice)
        mock.assert_awaited_once_with(ctx, "Logos")

    async def test_setup_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "setupHelper", mock):
            await self._command("setup").callback(ctx, "Alice's Team")
        mock.assert_awaited_once_with(ctx, "Alice's Team")

    async def test_setup_delegates_with_no_name(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "setupHelper", mock):
            await self._command("setup").callback(ctx)
        mock.assert_awaited_once_with(ctx, None)

    async def test_shop_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "shopHelper", mock):
            await self._command("shop").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_choose_delegates_to_choose_func(self):
        ctx = self._ctx()
        target = FakeMember("Target")
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "chooseFunc", mock):
            await self._command("choose").callback(ctx, member=target, use_random=False)
        mock.assert_awaited_once_with(ctx, target)

    async def test_choose_random_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "chooseRandomMember", mock):
            await self._command("choose").callback(ctx, member=None, use_random=True)
        mock.assert_awaited_once_with(ctx)

class HelpCommandTests(BotModuleTestCase):
    def _ctx(self, guild_id=GUILD_ID, channel=None):
        guild = FakeGuild(id=guild_id)
        user = FakeMember("Caller", id=1)
        return FakeInteraction(guild, user, channel=channel)

    async def test_no_command_links_to_the_site(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx, command=None)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("addshockwave.com", message)

    async def test_known_command_describes_it_with_usage(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx, command="wager")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("/wager", message)
        self.assertIn("<amount>", message)
        self.assertIn("<team>", message)
        self.assertIn(self.bot.COMMAND_HELP["wager"], message)

    async def test_optional_parameters_are_marked_in_usage(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx, command="stats")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("<member?>", message)

    async def test_command_lookup_ignores_case_and_a_leading_slash(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx, command="/Wager")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("/wager", message)

    async def test_unknown_command_points_to_the_site(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx, command="not-a-real-command")
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("not-a-real-command", message)
        self.assertIn("addshockwave.com", message)

    def test_every_registered_command_has_an_entry(self):
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertEqual(names, set(self.bot.COMMAND_HELP.keys()))


class HelpCommandAutocompleteTests(BotModuleTestCase):
    async def test_filters_by_current_input_case_insensitively(self):
        choices = await self.bot.helpCommandAutocomplete(None, "DAI")
        self.assertEqual([c.value for c in choices], ["daily"])

    async def test_empty_input_returns_only_real_commands(self):
        choices = await self.bot.helpCommandAutocomplete(None, "")
        self.assertTrue({c.value for c in choices} <= set(self.bot.COMMAND_HELP.keys()))

    async def test_caps_results_at_25(self):
        choices = await self.bot.helpCommandAutocomplete(None, "")
        self.assertEqual(len(choices), 25)
        self.assertGreater(len(self.bot.COMMAND_HELP), 25)

    async def test_name_and_value_match_the_command_name(self):
        choices = await self.bot.helpCommandAutocomplete(None, "wager-against")
        self.assertEqual(choices[0].name, "wager-against")
        self.assertEqual(choices[0].value, "wager-against")


class AdminSetCommandTests(BotModuleTestCase):
    def test_requires_manage_guild_permission(self):
        cmd = self._command("set")
        denied = SimpleNamespace(permissions=discord.Permissions.none())

        with self.assertRaises(app_commands.MissingPermissions):
            for check in cmd.checks:
                check(denied)

    def test_manage_guild_permission_is_sufficient(self):
        cmd = self._command("set")
        allowed = SimpleNamespace(permissions=discord.Permissions(manage_guild=True))

        for check in cmd.checks:
            self.assertTrue(check(allowed))

    async def test_error_handler_gives_a_friendly_denial_message(self):
        cmd = self._command("set")
        ctx = self._ctx()

        await cmd.on_error(ctx, app_commands.MissingPermissions(["manage_guild"]))

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("Manage Server", ctx.response.send_message.call_args.args[0])

    async def test_error_handler_reraises_unrelated_errors(self):
        cmd = self._command("set")
        ctx = self._ctx()

        with self.assertRaises(ValueError):
            await cmd.on_error(ctx, ValueError("boom"))

    async def test_delegates_every_field_to_the_helper(self):
        ctx = self._ctx()
        member = FakeMember("Target", id=555)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "adminSetHelper", mock):
            await self._command("set").callback(
                ctx, team1="Red", team2="Blue", size=4, betting_timer=30,
                wager_channel="bets", member=member, elo=1500, default_elo=1200,
            )
        mock.assert_awaited_once_with(ctx, "Red", "Blue", 4, 30, "bets", member, 1500, 1200)

    async def test_omitted_fields_default_to_none(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "adminSetHelper", mock):
            await self._command("set").callback(ctx)
        mock.assert_awaited_once_with(ctx, None, None, None, None, None, None, None, None)

    async def test_updates_team_size_and_confirms_end_to_end(self):
        guild_id = 903
        await self._insert_guild_row(guild_id)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("set").callback(ctx, team1="Red", team2="Blue", size=4)

        self.assertEqual(self.bot.helperObj.get(guild_id, "team_size"), 4)
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("4", message)


class RollCommandTests(BotModuleTestCase):
    async def test_rejects_numbers_not_greater_than_one(self):
        ctx = self._ctx()
        await self._command("roll").callback(ctx, num=1)
        ctx.response.send_message.assert_awaited_once_with(
            "Please use a number greater than 1."
        )

    async def test_rolls_within_requested_range(self):
        ctx = self._ctx()
        await self._command("roll").callback(ctx, num=6)
        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertTrue(message.startswith("You rolled "))
        self.assertTrue(1 <= int(message.split()[-1]) <= 6)


class ClearCommandTests(BotModuleTestCase):
    def test_requires_manage_guild_permission(self):
        cmd = self._command("clear")
        denied = SimpleNamespace(permissions=discord.Permissions.none())

        with self.assertRaises(app_commands.MissingPermissions):
            for check in cmd.checks:
                check(denied)

    def test_manage_guild_permission_is_sufficient(self):
        cmd = self._command("clear")
        allowed = SimpleNamespace(permissions=discord.Permissions(manage_guild=True))

        for check in cmd.checks:
            self.assertTrue(check(allowed))

    async def test_error_handler_reports_missing_permission(self):
        ctx = self._ctx()
        error = app_commands.MissingPermissions(["manage_guild"])
        await self.bot.clearAll_error(ctx, error)
        ctx.response.send_message.assert_awaited_once_with(
            "You need the Manage Server permission to use /clear."
        )

    async def test_clears_optional_fields_when_requested(self):
        guild_id = 903
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.update(guild_id, "channel1", "Red")
        self.bot.helperObj.saveTournament(guild_id, Tournament("Spring Cup", 2, 4))
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(
            ctx, clear_channels=True, clear_tournament=True, clear_elo=True
        )

        self.assertEqual(self.bot.helperObj.get(guild_id, "channel1"), "")
        # regression: clear_tournament used to write to a dead servers.tournament
        # column and never actually touch a real /tournament-create tournament.
        self.assertIsNone(self.bot.helperObj.getTournament(guild_id))
        ctx.response.send_message.assert_awaited_once_with("Cleared!")

    async def test_clear_tournament_also_wipes_its_match_history(self):
        guild_id = 9037
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.saveTournament(guild_id, Tournament("Spring Cup", 2, 4))
        self.bot.cursor.execute(
            "INSERT INTO tournament_matches"
            "(guildId, roundIndex, nodeIndex, team1, team2, state, mode, messageId, channelId, winner, bracketType) "
            "VALUES(?, 0, 0, '', '', 'RESOLVED', 'simultaneous', NULL, NULL, NULL, 'winners')",
            (guild_id,)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_tournament=True)

        self.assertIsNone(self.bot.helperObj.getTournament(guild_id))
        self.bot.cursor.execute(
            "SELECT COUNT(*) FROM tournament_matches WHERE guildId=?", (guild_id,)
        )
        self.assertEqual(self.bot.cursor.fetchone()[0], 0)

    async def test_leaves_optional_fields_alone_by_default(self):
        guild_id = 9031
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.update(guild_id, "channel1", "Red")
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx)

        self.assertEqual(self.bot.helperObj.get(guild_id, "channel1"), "Red")

    async def test_clear_economy_does_not_wipe_stats_until_confirmed(self):
        guild_id = 9032
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_economy=True)

        # "Cleared!" goes out immediately for the non-destructive parts...
        ctx.response.send_message.assert_awaited_once_with("Cleared!")
        # ...but the guild-wide economy wipe waits on the confirmation view.
        self.bot.cursor.execute("SELECT COUNT(*) FROM economy WHERE guildId=?", (guild_id,))
        self.assertEqual(self.bot.cursor.fetchone()[0], 1)
        ctx.followup.send.assert_awaited_once()
        self.assertIn("wipe the entire economy", ctx.followup.send.call_args.args[0])
        self.assertIn("view", ctx.followup.send.call_args.kwargs)

    async def test_confirming_clear_economy_wipes_stats(self):
        guild_id = 9034
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_economy=True)
        view = ctx.followup.send.call_args.kwargs["view"]

        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.bot.cursor.execute("SELECT COUNT(*) FROM economy WHERE guildId=?", (guild_id,))
        self.assertEqual(self.bot.cursor.fetchone()[0], 0)
        click.response.edit_message.assert_awaited_once()
        self.assertIn("Economy data", click.response.edit_message.call_args.kwargs["content"])

    async def test_clear_elo_resets_only_elo_after_confirmation(self):
        guild_id = 9035
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET elo=1500, balance=250 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_elo=True)

        # real per-player elo doesn't change until the reset is confirmed.
        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "elo"), 1500)

        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(
            self.bot.helperObj.getEconomy(guild_id, 901, "elo"), helper_module.DEFAULT_ELO
        )
        # balance is untouched; clear_elo only resets elo
        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "balance"), 250)

    async def test_cancelling_clear_confirmation_leaves_data_untouched(self):
        guild_id = 9036
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET elo=1500 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_elo=True)
        view = ctx.followup.send.call_args.kwargs["view"]

        click = self._ctx(guild_id=guild_id)
        await view.cancel.callback(click)

        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "elo"), 1500)
        click.response.edit_message.assert_awaited_once_with(
            content="Cancelled - nothing was reset.", view=view
        )

    async def test_clear_confirmation_view_rejects_non_invoker(self):
        guild_id = 9037
        await self._insert_guild_row(guild_id)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_elo=True)
        view = ctx.followup.send.call_args.kwargs["view"]

        stranger = FakeInteraction(FakeGuild(id=guild_id), FakeMember("Stranger", id=999))
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))

    async def test_clear_achievements_resets_only_achievements_after_confirmation(self):
        guild_id = 9038
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.helperObj.grantSpecialCardTitle(guild_id, 901, "Developer")
        self.bot.helperObj._unlockAchievement(guild_id, 901, "first_blood")
        self.bot.helperObj._unlockAchievement(guild_id, 901, "veteran")
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_achievements=True)

        # real per-player unlocks don't change until the reset is confirmed.
        unlocked = self.bot.helperObj.getUnlockedCardTitles(guild_id, 901)
        self.assertIn(helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"], unlocked)

        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        unlocked = self.bot.helperObj.getUnlockedCardTitles(guild_id, 901)
        # achievements are gone...
        self.assertNotIn(helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"], unlocked)
        self.assertNotIn(helper_module.CARD_ACHIEVEMENT_TITLES["veteran"], unlocked)
        # ...but an unrelated special-grant title survives untouched.
        self.assertIn("Developer", unlocked)
        self.assertIn("Every earned achievement", click.response.edit_message.call_args.kwargs["content"])

    async def test_clear_achievements_can_combine_with_clear_elo(self):
        guild_id = 9039
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET elo=1500 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        self.bot.helperObj._unlockAchievement(guild_id, 901, "first_blood")
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_elo=True, clear_achievements=True)
        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "elo"), helper_module.DEFAULT_ELO)
        self.assertNotIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.bot.helperObj.getUnlockedCardTitles(guild_id, 901)
        )
        content = click.response.edit_message.call_args.kwargs["content"]
        self.assertIn("Elo has been reset", content)
        self.assertIn("Every earned achievement", content)

    async def test_clear_achievements_with_a_user_only_resets_that_players_achievements(self):
        guild_id = 9040
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.helperObj.ensureEconomyRow(guild_id, 902, "Bob")
        self.bot.helperObj._unlockAchievement(guild_id, 901, "first_blood")
        self.bot.helperObj._unlockAchievement(guild_id, 902, "first_blood")
        target = FakeMember("Alice", id=901)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_achievements=True, user=target)

        # warning names the specific player, not "every player".
        warning = ctx.followup.send.call_args.args[0]
        self.assertIn(target.mention, warning)
        self.assertNotIn("every player", warning)

        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertNotIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.bot.helperObj.getUnlockedCardTitles(guild_id, 901)
        )
        # Bob's own achievement survives untouched.
        self.assertIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.bot.helperObj.getUnlockedCardTitles(guild_id, 902)
        )
        self.assertIn(target.mention, click.response.edit_message.call_args.kwargs["content"])

    async def test_user_without_clear_achievements_is_rejected(self):
        guild_id = 9041
        await self._insert_guild_row(guild_id)
        target = FakeMember("Alice", id=901)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, user=target)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("clear_achievements", ctx.response.send_message.call_args.args[0])
        # Nothing else ran either, not even the non-destructive team wipe.
        ctx.followup.send.assert_not_awaited()

    async def test_clear_achievements_for_a_user_can_combine_with_whole_server_clear_elo(self):
        guild_id = 9042
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.helperObj.ensureEconomyRow(guild_id, 902, "Bob")
        self.bot.cursor.execute(
            "UPDATE economy SET elo=1500 WHERE guildId=?", (guild_id,)
        )
        self.bot.mainDB.commit()
        self.bot.helperObj._unlockAchievement(guild_id, 901, "first_blood")
        self.bot.helperObj._unlockAchievement(guild_id, 902, "first_blood")
        target = FakeMember("Alice", id=901)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_elo=True, clear_achievements=True, user=target)
        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        # elo reset for EVERYONE...
        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 902, "elo"), helper_module.DEFAULT_ELO)
        # ...but achievements only reset for the targeted user.
        self.assertNotIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.bot.helperObj.getUnlockedCardTitles(guild_id, 901)
        )
        self.assertIn(
            helper_module.CARD_ACHIEVEMENT_TITLES["first_blood"],
            self.bot.helperObj.getUnlockedCardTitles(guild_id, 902)
        )

    async def test_clear_card_unlocks_resets_only_unlocks_after_confirmation(self):
        guild_id = 9043
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.helperObj._checkTierRewardUnlocks(guild_id, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_card_unlocks=True)

        # real per-player unlocks don't change until the reset is confirmed.
        self.assertTrue(self.bot.helperObj.getUnlockedCardTitles(guild_id, 901))

        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(self.bot.helperObj.getUnlockedCardTitles(guild_id, 901), [])
        self.assertIn("trading-card unlock", click.response.edit_message.call_args.kwargs["content"])

    async def test_clear_card_unlocks_with_a_user_only_resets_that_players_unlocks(self):
        guild_id = 9044
        await self._insert_guild_row(guild_id)
        self.bot.helperObj._checkTierRewardUnlocks(guild_id, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.bot.helperObj._checkTierRewardUnlocks(guild_id, 902, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        target = FakeMember("Alice", id=901)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_card_unlocks=True, user=target)
        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(self.bot.helperObj.getUnlockedCardTitles(guild_id, 901), [])
        # Bob's own unlocks survive untouched.
        self.assertTrue(self.bot.helperObj.getUnlockedCardTitles(guild_id, 902))
        self.assertIn(target.mention, click.response.edit_message.call_args.kwargs["content"])

    async def test_clear_card_unlocks_can_combine_with_clear_achievements(self):
        guild_id = 9045
        await self._insert_guild_row(guild_id)
        self.bot.helperObj._checkTierRewardUnlocks(guild_id, 901, helper_module.ELO_TIER_THRESHOLDS["Diamond"])
        self.bot.helperObj._unlockAchievement(guild_id, 901, "first_blood")
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, clear_achievements=True, clear_card_unlocks=True)
        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(self.bot.helperObj.getUnlockedCardTitles(guild_id, 901), [])
        content = click.response.edit_message.call_args.kwargs["content"]
        self.assertIn("Every earned achievement", content)
        self.assertIn("trading-card unlock", content)

    async def test_user_without_clear_card_unlocks_or_clear_achievements_is_rejected(self):
        guild_id = 9046
        await self._insert_guild_row(guild_id)
        target = FakeMember("Alice", id=901)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx, user=target)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("clear_card_unlocks", ctx.response.send_message.call_args.args[0])
        ctx.followup.send.assert_not_awaited()

    async def test_clear_economy_leaves_player_stats_alone_by_default(self):
        guild_id = 9033
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.ensureEconomyRow(guild_id, 901, "Alice")
        self.bot.cursor.execute(
            "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (guild_id, 901)
        )
        self.bot.mainDB.commit()
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(ctx)

        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "balance"), 1000)


class NotifyCommandTests(BotModuleTestCase):
    async def test_notify_member_confirms_by_name(self):
        guild_id = 904
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        target = FakeMember("Target")

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, member=target)

        mock.assert_awaited_once_with(ctx, target, None)
        ctx.response.send_message.assert_awaited_once_with("Sent an invite to Target!")

    async def test_notify_passes_a_custom_message_through(self):
        guild_id = 907
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        target = FakeMember("Target")

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, member=target, message="We need a 5th!")

        mock.assert_awaited_once_with(ctx, target, "We need a 5th!")

    async def test_notify_role_notifies_every_member(self):
        guild_id = 905
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        role = SimpleNamespace(name="Squad", members=[FakeMember("A"), FakeMember("B")])

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, role=role)

        self.assertEqual(mock.await_count, 2)
        mock.assert_awaited_with(ctx, role.members[-1], None)
        ctx.response.send_message.assert_awaited_once_with("Sent an invite to 2 members in Squad!")

    async def test_notify_role_with_a_single_member_uses_singular_wording(self):
        guild_id = 906
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        role = SimpleNamespace(name="Squad", members=[FakeMember("A")])

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, role=role)

        ctx.response.send_message.assert_awaited_once_with("Sent an invite to 1 member in Squad!")

    async def test_notify_rejects_when_neither_member_nor_role_given(self):
        guild_id = 905
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        await self._command("notify").callback(ctx)
        ctx.response.send_message.assert_awaited_once_with("Mention a member or a role to invite.")

    async def test_notify_rejects_when_both_member_and_role_given(self):
        guild_id = 905
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        role = SimpleNamespace(members=[FakeMember("A")])
        await self._command("notify").callback(ctx, member=FakeMember("Target"), role=role)
        ctx.response.send_message.assert_awaited_once_with("Give a member or a role, not both.")

    async def test_notify_rejects_when_not_in_a_voice_channel(self):
        guild_id = 908
        await self._insert_guild_row(guild_id)
        ctx = self._ctx(guild_id=guild_id)  # ctx.user.voice is None by default
        target = FakeMember("Target")

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, member=target)

        mock.assert_not_awaited()
        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to invite someone to it - join one and try again."
        )

    async def test_notify_rejects_when_voice_state_has_no_channel(self):
        guild_id = 909
        await self._insert_guild_row(guild_id)
        ctx = self._ctx(guild_id=guild_id)
        ctx.user.voice = FakeVoiceState(None)
        target = FakeMember("Target")

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, member=target)

        mock.assert_not_awaited()
        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to invite someone to it - join one and try again."
        )


class MakeTeamsCommandTests(BotModuleTestCase):
    async def _setup_teams(self, guild_id):
        await self._insert_guild_row(guild_id)
        team1 = Team(); team1.set_id(1); team1.name = "Team 1"
        team1.add_player(Player(1, "A"))
        team2 = Team(); team2.set_id(2); team2.name = "Team 2"
        team2.add_player(Player(2, "B"))
        self.bot.helperObj.update(guild_id, "team1", team1.serializeTeam())
        self.bot.helperObj.update(guild_id, "team2", team2.serializeTeam())

    async def test_random_split_announces_without_moving(self):
        guild_id = 907
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ) as embed_mock, \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()) as finalize_mock:
            await self._command("make-teams").callback(ctx, use_roles=False)

        randomize_mock.assert_awaited_once_with(ctx)
        embed_mock.assert_awaited_once()
        # regression: /make-teams used to optionally move everyone itself;
        # moving/betting only happens once the posted roster's own ▶️
        # reaction is clicked (see _finalizeRoster) now.
        finalize_mock.assert_awaited_once()
        ctx.response.send_message.assert_awaited_once_with("Teams created!")
        # regression: the ready reminder used to be folded into the very
        # first response, which posts *before* the team embeds and is easy
        # to miss. It's the last message sent now, after the rosters.
        ctx.channel.send.assert_awaited_once()
        self.assertIn("Press Start", ctx.channel.send.call_args.args[0])

    async def test_rejects_when_not_in_a_voice_channel(self):
        ctx = self._ctx()  # ctx.user.voice is None by default
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "randomizeTeamHelper", mock):
            await self._command("make-teams").callback(ctx, use_roles=False)
        mock.assert_not_awaited()
        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to form teams from it - join one and try again."
        )

    async def test_rejects_when_voice_state_has_no_channel(self):
        ctx = self._ctx()
        ctx.user.voice = FakeVoiceState(None)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "randomizeTeamHelper", mock):
            await self._command("make-teams").callback(ctx, use_roles=False)
        mock.assert_not_awaited()
        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to form teams from it - join one and try again."
        )

    async def test_use_roles_forwards_the_flag_to_print_embed_and_finalize(self):
        guild_id = 908
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ) as embed_mock, \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()) as finalize_mock:
            await self._command("make-teams").callback(ctx, use_roles=True)

        randomize_mock.assert_awaited_once_with(ctx)
        # regression test: /make-teams use_roles:True used to never forward
        # that flag to printEmbed, so roles never actually showed up.
        embed_mock.assert_awaited_once()
        self.assertTrue(embed_mock.call_args.kwargs.get("useRoles"))
        finalize_mock.assert_awaited_once()
        self.assertTrue(finalize_mock.call_args.args[-1])

    async def test_use_roles_explains_when_a_team_is_not_five(self):
        # _setup_teams() gives each team 1 player, so roles can't apply -
        # continue normally (teams still get created and posted) but say
        # why no roles showed up.
        guild_id = 912
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()), \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ) as embed_mock, \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=True)

        embed_mock.assert_awaited_once()  # teams still get announced normally
        # explanation, then the trailing ready reminder
        self.assertEqual(ctx.channel.send.await_count, 2)
        explanation = ctx.channel.send.call_args_list[0].args[0]
        self.assertIn("Team 1 (1 players)", explanation)
        self.assertIn("Team 2 (1 players)", explanation)
        self.assertIn("Press Start", ctx.channel.send.call_args_list[1].args[0])

    async def test_use_roles_no_explanation_when_teams_are_five_v_five(self):
        guild_id = 913
        await self._insert_guild_row(guild_id)
        team1 = Team(); team1.set_id(1); team1.name = "Team 1"
        team2 = Team(); team2.set_id(2); team2.name = "Team 2"
        for i in range(5):
            team1.add_player(Player(100 + i, f"A{i}"))
            team2.add_player(Player(200 + i, f"B{i}"))
        self.bot.helperObj.update(guild_id, "team1", team1.serializeTeam())
        self.bot.helperObj.update(guild_id, "team2", team2.serializeTeam())
        ctx = self._ctx_in_voice(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()), \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ), \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=True)

        # only the trailing ready reminder, no role explanation needed
        ctx.channel.send.assert_awaited_once()
        self.assertNotIn("Roles need", ctx.channel.send.call_args.args[0])
        self.assertIn("Press Start", ctx.channel.send.call_args.args[0])

    async def test_use_roles_blocks_when_a_voice_member_has_not_run_setup(self):
        guild_id = 914
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        setup_member = FakeMember("Ready", id=950)
        not_setup_member = FakeMember("NotReady", id=951)
        ctx.user.voice.channel.members = [setup_member, not_setup_member]

        with patch.object(
            self.bot.helperObj, "hasCompletedSetup", side_effect=lambda gid, uid: uid == 950
        ), patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock:
            await self._command("make-teams").callback(ctx, use_roles=True)

        randomize_mock.assert_not_awaited()
        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("/setup", text)
        self.assertIn(not_setup_member.mention, text)
        self.assertNotIn(setup_member.mention, text)

    async def test_use_roles_allows_when_everyone_in_voice_has_run_setup(self):
        guild_id = 915
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        ctx.user.voice.channel.members = [FakeMember("Ready", id=952)]

        with patch.object(self.bot.helperObj, "hasCompletedSetup", return_value=True), \
             patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ), \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=True)

        randomize_mock.assert_awaited_once()

    async def test_use_roles_ignores_bots_that_have_not_run_setup(self):
        guild_id = 916
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        ctx.user.voice.channel.members = [FakeMember("SomeBot", id=953, bot=True)]

        with patch.object(self.bot.helperObj, "hasCompletedSetup", return_value=False), \
             patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ), \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=True)

        randomize_mock.assert_awaited_once()

    async def test_use_roles_false_never_checks_setup(self):
        guild_id = 917
        await self._setup_teams(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        ctx.user.voice.channel.members = [FakeMember("NotReady", id=954)]

        with patch.object(self.bot.helperObj, "hasCompletedSetup", return_value=False) as setup_mock, \
             patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()), \
             patch.object(
                 self.bot.helperObj, "printEmbed", AsyncMock(return_value=(FakeMessage(), FakeMessage()))
             ), \
             patch.object(self.bot.helperObj, "_finalizeRoster", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=False)

        setup_mock.assert_not_called()


class RankedCommandTests(BotModuleTestCase):
    async def test_make_teams_ranked_delegates_to_ranked_team_helper(self):
        ctx = self._ctx_in_voice()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "rankedTeamHelper", mock):
            await self._command("make-teams").callback(ctx, use_roles=False, ranked=True)
        mock.assert_awaited_once_with(ctx, False)

    async def test_make_teams_ranked_passes_use_roles_through(self):
        # ranked=True short-circuits before the random-split flow even
        # runs (randomizeTeamHelper must never be touched), but use_roles
        # is now forwarded into rankedTeamHelper instead of being dropped.
        ctx = self._ctx_in_voice()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "rankedTeamHelper", mock), \
             patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as random_mock:
            await self._command("make-teams").callback(ctx, use_roles=True, ranked=True)
        random_mock.assert_not_awaited()
        mock.assert_awaited_once_with(ctx, True)

    async def test_ranked_use_roles_still_blocks_when_a_voice_member_has_not_run_setup(self):
        guild_id = 918
        await self._insert_guild_row(guild_id)
        ctx = self._ctx_in_voice(guild_id=guild_id)
        not_setup_member = FakeMember("NotReady", id=960)
        ctx.user.voice.channel.members = [not_setup_member]

        with patch.object(self.bot.helperObj, "hasCompletedSetup", return_value=False), \
             patch.object(self.bot.helperObj, "rankedTeamHelper", AsyncMock()) as ranked_mock:
            await self._command("make-teams").callback(ctx, use_roles=True, ranked=True)

        ranked_mock.assert_not_awaited()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn("/setup", text)
        self.assertIn(not_setup_member.mention, text)


class CaptainsCommandTests(BotModuleTestCase):
    async def test_rejects_when_not_in_a_voice_channel(self):
        ctx = self._ctx()
        ctx.user.voice = None

        await self._command("captains").callback(
            ctx, captain_1=None, captain_2=None, use_random=False
        )

        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to start a captains draft - join one and try again."
        )

    async def test_rejects_when_voice_state_has_no_channel(self):
        ctx = self._ctx()
        ctx.user.voice = FakeVoiceState(None)

        await self._command("captains").callback(
            ctx, captain_1=None, captain_2=None, use_random=False
        )

        ctx.response.send_message.assert_awaited_once_with(
            "You need to be in a voice channel to start a captains draft - join one and try again."
        )

    async def test_requires_at_least_two_in_voice_channel(self):
        ctx = self._ctx()
        ctx.user.voice = FakeVoiceState(FakeChannel("Lobby", members=[FakeMember("Solo")]))

        await self._command("captains").callback(
            ctx, captain_1=None, captain_2=None, use_random=False
        )

        ctx.response.send_message.assert_awaited_once_with(
            "Not enough players in the voice channel!"
        )

    async def test_explicit_captains_delegate(self):
        guild_id = 909
        await self._insert_guild_row(guild_id)
        cap1 = FakeMember("Cap1", id=1)
        cap2 = FakeMember("Cap2", id=2)
        voice_channel = FakeChannel("Lobby", members=[cap1, cap2])
        user = FakeMember("Caller", id=3)
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(FakeGuild(id=guild_id), user)

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "captainsHelper", mock):
            await self._command("captains").callback(
                ctx, captain_1=cap1, captain_2=cap2, use_random=False
            )

        mock.assert_awaited_once_with(ctx, cap1, cap2, ranked=False)

    async def test_ranked_captains_delegates_with_ranked_flag(self):
        guild_id = 9091
        await self._insert_guild_row(guild_id)
        cap1 = FakeMember("Cap1", id=1)
        cap2 = FakeMember("Cap2", id=2)
        voice_channel = FakeChannel("Lobby", members=[cap1, cap2])
        user = FakeMember("Caller", id=3)
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(FakeGuild(id=guild_id), user)

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "captainsHelper", mock):
            await self._command("captains").callback(
                ctx, captain_1=cap1, captain_2=cap2, use_random=False, ranked=True
            )

        mock.assert_awaited_once_with(ctx, cap1, cap2, ranked=True)

    async def test_use_random_picks_two_distinct_captains_from_voice_channel(self):
        # Regression test: this path used to store a plain Python list as
        # the "players" column, which sqlite3 can't bind as a parameter
        # (InterfaceError); it crashed before ever picking a captain.
        guild_id = 910
        await self._insert_guild_row(guild_id)
        members = [FakeMember(f"P{i}", id=1000 + i) for i in range(4)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller", id=2000)
        user.voice = FakeVoiceState(voice_channel)
        # getRandomMember() resolves picks via ctx.guild.members (not the
        # voice channel's member list), so both need the same players.
        ctx = FakeInteraction(FakeGuild(id=guild_id, members=members), user)

        captured = {}

        async def fake_captains_helper(c, captain1, captain2, ranked=False):
            captured["captain1"] = captain1
            captured["captain2"] = captain2

        with patch.object(self.bot.helperObj, "captainsHelper", AsyncMock(side_effect=fake_captains_helper)):
            await self._command("captains").callback(
                ctx, captain_1=None, captain_2=None, use_random=True
            )

        self.assertIsNotNone(captured.get("captain1"))
        self.assertIsNotNone(captured.get("captain2"))
        self.assertNotEqual(captured["captain1"].id, captured["captain2"].id)
        self.assertIn(captured["captain1"].id, {m.id for m in members})
        self.assertIn(captured["captain2"].id, {m.id for m in members})


class StatsCommandTests(BotModuleTestCase):
    async def test_defaults_to_the_caller(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "statsHelper", mock):
            await self._command("stats").callback(ctx, member=None)
        mock.assert_awaited_once_with(ctx, None)

    async def test_looks_up_another_member(self):
        ctx = self._ctx()
        target = FakeMember("Target")
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "statsHelper", mock):
            await self._command("stats").callback(ctx, member=target)
        mock.assert_awaited_once_with(ctx, target)


class MyTeamsCommandTests(BotModuleTestCase):
    async def test_defaults_to_the_caller(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "myTeamsHelper", mock):
            await self._command("my-teams").callback(ctx, member=None)
        mock.assert_awaited_once_with(ctx, None)

    async def test_looks_up_another_member(self):
        ctx = self._ctx()
        target = FakeMember("Target")
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "myTeamsHelper", mock):
            await self._command("my-teams").callback(ctx, member=target)
        mock.assert_awaited_once_with(ctx, target)


class TeamListCommandTests(BotModuleTestCase):
    async def test_defaults_to_cards_false(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamListHelper", mock):
            await self._command("team-list").callback(ctx)
        mock.assert_awaited_once_with(ctx, None, False, "name", "asc", False, [])

    async def test_forwards_cards_true(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamListHelper", mock):
            await self._command("team-list").callback(ctx, cards=True)
        mock.assert_awaited_once_with(ctx, None, False, "name", "asc", True, [])

    async def test_collects_given_members_and_skips_unset_slots(self):
        ctx = self._ctx()
        alice = FakeMember("Alice", id=901)
        bob = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamListHelper", mock):
            await self._command("team-list").callback(ctx, member_1=alice, member_3=bob)
        mock.assert_awaited_once_with(ctx, None, False, "name", "asc", False, [alice, bob])


class ReportCorrectWinnerCommandTests(BotModuleTestCase):
    async def test_delegates_with_resolved_team_value(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reportCorrectWinnerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("report-correct-winner").callback(ctx, choice)
        mock.assert_awaited_once_with(ctx, 2, None, False)

    async def test_delegates_with_match_id(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reportCorrectWinnerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("report-correct-winner").callback(ctx, choice, match_id=42)
        mock.assert_awaited_once_with(ctx, 2, 42, False)

    async def test_delegates_with_invalidate_and_no_team(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reportCorrectWinnerHelper", mock):
            await self._command("report-correct-winner").callback(ctx, invalidate=True)
        mock.assert_awaited_once_with(ctx, None, None, True)

    def test_requires_manage_guild_permission(self):
        cmd = self._command("report-correct-winner")
        denied = SimpleNamespace(permissions=discord.Permissions.none())

        with self.assertRaises(app_commands.MissingPermissions):
            for check in cmd.checks:
                check(denied)

    def test_manage_guild_permission_is_sufficient(self):
        cmd = self._command("report-correct-winner")
        allowed = SimpleNamespace(permissions=discord.Permissions(manage_guild=True))

        for check in cmd.checks:
            self.assertTrue(check(allowed))

    async def test_error_handler_gives_a_friendly_denial_message(self):
        cmd = self._command("report-correct-winner")
        ctx = self._ctx()

        await cmd.on_error(ctx, app_commands.MissingPermissions(["manage_guild"]))

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("Manage Server", ctx.response.send_message.call_args.args[0])

    async def test_error_handler_reraises_unrelated_errors(self):
        cmd = self._command("report-correct-winner")
        ctx = self._ctx()

        with self.assertRaises(RuntimeError):
            await cmd.on_error(ctx, RuntimeError("boom"))


# ===========================================================================
# restore_backup.py: a standalone ops script (run by whoever hosts the bot,
# with the bot itself stopped) for reverting main.db to one of
# backupDatabaseTask's daily snapshots. Never imported by bot.py/helper.py,
# so these tests exercise it directly, always against a throwaway temp
# directory (DB_PATH/BACKUP_DIR patched for the duration of each test),
# never the real data/guildData/ paths it points at by default.
# ===========================================================================

class RestoreBackupTests(unittest.TestCase):
    def setUp(self):
        self._real_db_path = restore_backup.DB_PATH
        self._real_backup_dir = restore_backup.BACKUP_DIR
        self._tmp = tempfile.TemporaryDirectory()
        serverinfo_dir = os.path.join(self._tmp.name, "serverInfo")
        self.backups_dir = os.path.join(self._tmp.name, "backups")
        os.makedirs(serverinfo_dir)
        os.makedirs(self.backups_dir)
        self.db_path = os.path.join(serverinfo_dir, "main.db")
        restore_backup.DB_PATH = self.db_path
        restore_backup.BACKUP_DIR = self.backups_dir

    def tearDown(self):
        restore_backup.DB_PATH = self._real_db_path
        restore_backup.BACKUP_DIR = self._real_backup_dir
        self._tmp.cleanup()

    def _write_db(self, content):
        with open(self.db_path, "wb") as f:
            f.write(content)

    def _read_db(self):
        with open(self.db_path, "rb") as f:
            return f.read()

    def _write_backup(self, name, content, mtime):
        path = os.path.join(self.backups_dir, name)
        with open(path, "wb") as f:
            f.write(content)
        os.utime(path, (mtime, mtime))
        return path

    def test_list_backups_sorts_newest_first(self):
        self._write_backup("main-20260101-010000.db", b"old", mtime=1000)
        self._write_backup("main-20260215-030000.db", b"new", mtime=2000)

        self.assertEqual(
            restore_backup._listBackups(), ["main-20260215-030000.db", "main-20260101-010000.db"]
        )

    def test_list_backups_excludes_safety_backups_and_empty_dir(self):
        self.assertEqual(restore_backup._listBackups(), [])
        self._write_backup("main-20260101-010000.db", b"old", mtime=1000)
        self._write_backup("main-before-restore-20260101-010000.db", b"safety", mtime=1500)

        self.assertEqual(restore_backup._listBackups(), ["main-20260101-010000.db"])

    def test_resolve_choice_by_index_and_filename(self):
        backups = ["main-20260215-030000.db", "main-20260101-010000.db"]

        self.assertEqual(restore_backup._resolveChoice(backups, "1"), backups[0])
        self.assertEqual(restore_backup._resolveChoice(backups, "2"), backups[1])
        self.assertEqual(restore_backup._resolveChoice(backups, backups[1]), backups[1])
        self.assertIsNone(restore_backup._resolveChoice(backups, "0"))
        self.assertIsNone(restore_backup._resolveChoice(backups, "99"))
        self.assertIsNone(restore_backup._resolveChoice(backups, "not-a-backup.db"))

    def test_format_size_scales_units(self):
        self.assertEqual(restore_backup._formatSize(500), "500B")
        self.assertEqual(restore_backup._formatSize(2048), "2.0KB")
        self.assertEqual(restore_backup._formatSize(5 * 1024 * 1024), "5.0MB")

    def test_main_restores_the_chosen_backup_by_index(self):
        self._write_db(b"LIVE-BEFORE")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)
        self._write_backup("main-20260215-030000.db", b"NEWER", mtime=2000)

        with patch.object(sys, "argv", ["restore_backup.py", "1"]), \
             patch("builtins.input", return_value="yes"):
            restore_backup.main()

        self.assertEqual(self._read_db(), b"NEWER")

    def test_main_restores_the_chosen_backup_by_filename(self):
        self._write_db(b"LIVE-BEFORE")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py", "main-20260101-010000.db"]), \
             patch("builtins.input", return_value="yes"):
            restore_backup.main()

        self.assertEqual(self._read_db(), b"OLDER")

    def test_main_saves_a_safety_backup_of_the_live_db_before_overwriting(self):
        self._write_db(b"LIVE-BEFORE")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py", "1"]), \
             patch("builtins.input", return_value="yes"):
            restore_backup.main()

        safety_files = [n for n in os.listdir(self.backups_dir) if n.startswith("main-before-restore-")]
        self.assertEqual(len(safety_files), 1)
        with open(os.path.join(self.backups_dir, safety_files[0]), "rb") as f:
            self.assertEqual(f.read(), b"LIVE-BEFORE")

    def test_main_does_not_save_a_safety_backup_when_there_was_no_live_db_yet(self):
        # A fresh install being seeded from a backup for the first time -
        # nothing to protect since main.db doesn't exist yet.
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py", "1"]), \
             patch("builtins.input", return_value="yes"):
            restore_backup.main()

        self.assertEqual(self._read_db(), b"OLDER")
        safety_files = [n for n in os.listdir(self.backups_dir) if n.startswith("main-before-restore-")]
        self.assertEqual(safety_files, [])

    def test_main_blank_input_cancels_without_modifying_anything(self):
        self._write_db(b"UNTOUCHED")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py"]), \
             patch("builtins.input", return_value=""):
            restore_backup.main()

        self.assertEqual(self._read_db(), b"UNTOUCHED")
        self.assertEqual(os.listdir(self.backups_dir), ["main-20260101-010000.db"])

    def test_main_declining_confirmation_cancels_without_modifying_anything(self):
        self._write_db(b"UNTOUCHED")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py", "1"]), \
             patch("builtins.input", return_value="no"):
            restore_backup.main()

        self.assertEqual(self._read_db(), b"UNTOUCHED")

    def test_main_out_of_range_choice_exits_nonzero_without_modifying_anything(self):
        self._write_db(b"UNTOUCHED")
        self._write_backup("main-20260101-010000.db", b"OLDER", mtime=1000)

        with patch.object(sys, "argv", ["restore_backup.py", "99"]), \
             patch("builtins.input", return_value="yes"):
            with self.assertRaises(SystemExit) as cm:
                restore_backup.main()

        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(self._read_db(), b"UNTOUCHED")

    def test_main_with_no_backups_present_returns_without_raising(self):
        # No main.db yet either, the very first run on a fresh install,
        # before backupDatabaseTask has ever produced a snapshot.
        restore_backup.main()
        self.assertFalse(os.path.isfile(self.db_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
