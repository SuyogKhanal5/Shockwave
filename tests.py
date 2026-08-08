"""
All-encompassing test suite for Shockwave.

Runs with the stdlib test runner only (no pytest/pip install required):

    python tests.py
    python tests.py -v
    python -m unittest tests -v

Layout:
  - Fakes: lightweight stand-ins for discord.py objects (real attributes,
    not auto-magic Mocks, so a wrong attribute access fails loudly).
  - TourneyClasses tests: Player/Team, pure logic, no I/O.
  - helper.helpers tests: the bot's actual command logic, run against a
    real (in-memory) sqlite database and the fakes above.
  - bot.py tests: imports bot.py with its module-level DB connection and
    token read redirected away from the real project database and the
    real bot token (see _import_bot_module), then exercises command
    callbacks and event handlers directly — never touches Discord or the
    real data/guildData/serverInfo/main.db.
"""

import asyncio
import contextlib
import itertools
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, mock_open, patch

import discord
from discord import app_commands

from TourneyClasses import (
    Player, Team, Tournament, BracketNode, serialize_bracket, deserialize_bracket,
)
import helper as helper_module
from helper import helpers as Helpers

GUILD_ID = 555000111
COMMAND_GUILD_ID = 526081127643873280  # matches the guild id every @tree.command in bot.py registers under


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
    "active_tournament_match_id, wager_channel)"
)
ECONOMY_SCHEMA = (
    "CREATE TABLE economy(guildId, userId, username, balance, wins, losses, "
    "gold_wagered, gold_won, gold_lost, game_wins, game_losses, elo, last_daily, "
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
    "filter, sort_order, page)"
)
TEAMS_SCHEMA = "CREATE TABLE teams(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, name, data)"
TOURNAMENTS_SCHEMA = (
    "CREATE TABLE tournaments(guildId PRIMARY KEY, name, team_size, num_teams, "
    "double_elimination, teams, bracket)"
)
TEAM_INVITES_SCHEMA = (
    "CREATE TABLE team_invites(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, channelId, "
    "messageId, teamId, teamName, inviterId, targetId, targetName)"
)
TOURNAMENT_MATCHES_SCHEMA = (
    "CREATE TABLE tournament_matches(id INTEGER PRIMARY KEY AUTOINCREMENT, guildId, roundIndex, "
    "nodeIndex, team1, team2, state, mode, messageId, channelId, winner)"
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
    cursor.execute(TEAMS_SCHEMA)
    cursor.execute(TOURNAMENTS_SCHEMA)
    cursor.execute(TEAM_INVITES_SCHEMA)
    cursor.execute(TOURNAMENT_MATCHES_SCHEMA)
    db.commit()
    return db, cursor


def insert_guild_row(cursor, db, guild_id=GUILD_ID, name="Test Guild"):
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL, 0, NULL, NULL)",
        (guild_id, name),
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
        # Defaults to True so existing tests that don't care about
        # permissions aren't affected — pass manage_guild=False to test
        # the insufficient-permission path.
        self.guild_permissions = SimpleNamespace(manage_guild=manage_guild)


class FakeMessage:
    def __init__(self, id=None):
        self.id = id if id is not None else next_id()
        self.add_reaction = AsyncMock()
        self.edit = AsyncMock()


class FakeChannel:
    def __init__(self, name, id=None, members=None, kind="voice"):
        self.name = name
        self.id = id if id is not None else next_id()
        self.members = members if members is not None else []
        self.mention = f"<#{self.id}>"
        self.kind = kind
        self.send = AsyncMock()
        self.create_invite = AsyncMock(return_value="https://discord.gg/fake-invite")
        self.fetch_message = AsyncMock(return_value=FakeMessage())

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


class FakeInteraction:
    def __init__(self, guild, user, channel=None):
        self.guild = guild
        self.user = user
        self.channel = channel if channel is not None else FakeChannel("text-channel")
        self.response = AsyncMock()
        self.followup = AsyncMock()
        self.original_response = AsyncMock(return_value=FakeMessage())


class FakeClient:
    def __init__(self, channels=(), guilds=()):
        self._channels = {c.id: c for c in channels}
        self._guilds = {g.id: g for g in guilds}
        self.fetch_channel = AsyncMock()

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)


class FakePayload:
    def __init__(self, guild_id, message_id, channel_id, emoji, member=None, user_id=None):
        self.guild_id = guild_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.emoji = emoji
        self.member = member
        self.user_id = user_id if user_id is not None else (member.id if member is not None else None)


# ===========================================================================
# TourneyClasses.py — pure logic, no I/O
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
# helper.helpers — real in-memory sqlite db + fake discord objects
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


class MovefuncTests(HelperTestCase):
    def _team_with_player(self, team_id, name, player_id, player_name):
        team = Team()
        team.set_id(team_id)
        team.name = name
        team.add_player(Player(player_id, player_name))
        return team

    async def test_moves_players_when_channels_set(self):
        team1 = self._team_with_player(1, "Team 1", 101, "Alice")
        team2 = self._team_with_player(2, "Team 2", 102, "Bob")

        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        og_channel = FakeChannel("Lobby")
        member1 = FakeMember("Alice", id=101)
        member2 = FakeMember("Bob", id=102)

        guild = FakeGuild(channels=[channel1, channel2], members=[member1, member2])
        user = FakeMember("Caller", id=999)
        user.voice = FakeVoiceState(og_channel)
        ctx = FakeInteraction(guild, user)

        self.helperObj.update(GUILD_ID, "channel1", "Team 1")
        self.helperObj.update(GUILD_ID, "channel2", "Team 2")
        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())

        await self.helperObj.movefunc(ctx)

        member1.move_to.assert_awaited_once_with(channel1)
        member2.move_to.assert_awaited_once_with(channel2)
        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "Lobby")

    async def test_sends_warning_when_channels_not_set(self):
        team1 = self._team_with_player(1, "Team 1", 101, "Alice")
        team2 = self._team_with_player(2, "Team 2", 102, "Bob")

        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())
        self.helperObj.update(GUILD_ID, "channel1", "")
        self.helperObj.update(GUILD_ID, "channel2", "")

        og_channel = FakeChannel("Lobby")
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(og_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.movefunc(ctx)

        ctx.channel.send.assert_awaited_once()
        self.assertIn("Team Channels Not Set", ctx.channel.send.call_args.args[0])


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

        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("avg elo", message)
        self.assertIn("/start", message)

    async def test_creates_economy_rows_at_default_elo_for_new_players(self):
        members = [FakeMember("New1", id=401), FakeMember("New2", id=402)]
        voice_channel = FakeChannel("Lobby", members=members)
        user = FakeMember("Caller")
        user.voice = FakeVoiceState(voice_channel)
        ctx = FakeInteraction(self.guild, user)

        await self.helperObj.rankedTeamHelper(ctx)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 401, "elo"), helper_module.DEFAULT_ELO)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 402, "elo"), helper_module.DEFAULT_ELO)


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

    async def test_same_captain_rejected_before_any_state_change(self):
        captain1 = FakeMember("Cap1", id=301)
        ctx = self._ctx(captain1, captain1, [])

        await self.helperObj.captainsHelper(ctx, captain1, captain1)

        ctx.response.send_message.assert_awaited_once_with("Mention two different people!")
        # nothing should have been written — clearTeamsHelper never ran
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
        self.assertIn("/start", last_message)

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
        # in the pool — the old code never prompted /start in that case.
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
        self.assertIn("/start", last_message)

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

        ctx = FakeInteraction(self.guild, FakeMember("Caller"))
        await self.helperObj.clearTeamsHelper(ctx)

        self.assertEqual(self.helperObj.get(GUILD_ID, "original_channel"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team1"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team2"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "players"), "")
        self.assertEqual(self.helperObj.get(GUILD_ID, "team_size"), 5)
        self.assertEqual(self.helperObj.get(GUILD_ID, "mode"), "Normal")
        self.assertEqual(self.helperObj.get(GUILD_ID, "turn"), 1)


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
        # balance/wins are untouched — only elo resets
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 500)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 3)
        # other guild's elo is untouched
        self.assertEqual(self.helperObj.getEconomy(other_guild_id, 901, "elo"), 1600)


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
        # nothing changed — the old tournament is still there untouched
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

    async def test_team_ids_are_assigned_automatically_and_unique(self):
        ctx1 = self._ctx()
        await self.helperObj.createTeamHelper(ctx1, "Red", 5)
        ctx2 = self._ctx(user_id=902, name="Bob")
        await self.helperObj.createTeamHelper(ctx2, "Blue", 5)

        _, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        _, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        self.assertNotEqual(red.get_id(), blue.get_id())


class SetTeamVoiceChannelHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def _make_team(self, name="Red", captain_id=901, captain_name="Alice"):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, name, 5)
        return self.helperObj.getTeamRow(GUILD_ID, name)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        await self.helperObj.setTeamVoiceChannelHelper(ctx, "Nonexistent", None)
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain(self):
        await self._make_team()
        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.setTeamVoiceChannelHelper(ctx, "Red", None)
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain can set its voice channel."
        )

    async def test_no_channel_creates_a_new_one_named_after_the_team(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.setTeamVoiceChannelHelper(ctx, "Red", None)

        self.assertEqual(len(self.guild.channels), 1)
        created = self.guild.channels[0]
        self.assertEqual(created.name, "Red")
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "Red")
        ctx.response.send_message.assert_awaited_once()
        self.assertIn(created.mention, ctx.response.send_message.call_args.args[0])

    async def test_unused_channel_is_set_directly(self):
        await self._make_team()
        ctx = self._ctx()
        channel = FakeChannel("general-voice")

        await self.helperObj.setTeamVoiceChannelHelper(ctx, "Red", channel)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(team.get_voice_channel(), "general-voice")
        ctx.response.send_message.assert_awaited_once_with(
            f"**Red**'s voice channel is now {channel.mention}."
        )

    async def test_channel_already_used_by_another_team_requires_confirmation(self):
        await self._make_team("Red", 901, "Alice")
        await self._make_team("Blue", 902, "Bob")
        shared_channel = FakeChannel("Arena")

        blue_ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.setTeamVoiceChannelHelper(blue_ctx, "Blue", shared_channel)

        red_ctx = self._ctx()
        posted_message = FakeMessage(id=555)
        red_ctx.original_response.return_value = posted_message
        await self.helperObj.setTeamVoiceChannelHelper(red_ctx, "Red", shared_channel)

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
        await self.helperObj.setTeamVoiceChannelHelper(blue_ctx, "Blue", shared_channel)

        red_ctx = self._ctx()
        await self.helperObj.setTeamVoiceChannelHelper(red_ctx, "Red", shared_channel)
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
        await self.helperObj.setTeamVoiceChannelHelper(blue_ctx, "Blue", shared_channel)

        red_ctx = self._ctx()
        await self.helperObj.setTeamVoiceChannelHelper(red_ctx, "Red", shared_channel)
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
        await self.helperObj.setTeamVoiceChannelHelper(blue_ctx, "Blue", shared_channel)

        red_ctx = self._ctx()
        await self.helperObj.setTeamVoiceChannelHelper(red_ctx, "Red", shared_channel)
        view = red_ctx.response.send_message.call_args.kwargs["view"]

        stranger = FakeInteraction(self.guild, FakeMember("Stranger", id=999))
        allowed = await view.interaction_check(stranger)

        self.assertFalse(allowed)
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(stranger.response.send_message.call_args.kwargs.get("ephemeral"))


class TeamInviteHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice", channel=None):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id), channel=channel)

    async def _make_team(self, name="Red", captain_id=901, captain_name="Alice"):
        ctx = self._ctx(user_id=captain_id, name=captain_name)
        await self.helperObj.createTeamHelper(ctx, name, 5)

    async def test_rejects_unknown_team(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        await self.helperObj.teamInviteHelper(ctx, "Nonexistent", target)
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain(self):
        await self._make_team()
        ctx = self._ctx(user_id=903, name="Cleo")
        target = FakeMember("Bob", id=902)
        await self.helperObj.teamInviteHelper(ctx, "Red", target)
        ctx.response.send_message.assert_awaited_once_with("Only **Red**'s captain can invite players.")

    async def test_rejects_inviting_a_bot(self):
        await self._make_team()
        ctx = self._ctx()
        target = FakeMember("Botty", id=902, bot=True)
        await self.helperObj.teamInviteHelper(ctx, "Red", target)
        ctx.response.send_message.assert_awaited_once_with("You can't invite a bot to a team.")

    async def test_rejects_inviting_someone_already_on_the_team(self):
        await self._make_team()
        ctx = self._ctx()
        await self.helperObj.teamInviteHelper(ctx, "Red", ctx.user)
        ctx.response.send_message.assert_awaited_once_with("Alice is already on **Red**.")

    async def test_successful_invite_posts_message_and_stores_pending_row(self):
        await self._make_team()
        channel = FakeChannel("general")
        ctx = self._ctx(channel=channel)
        target = FakeMember("Bob", id=902)
        posted_message = FakeMessage(id=777)
        ctx.original_response.return_value = posted_message

        await self.helperObj.teamInviteHelper(ctx, "Red", target)

        ctx.response.send_message.assert_awaited_once()
        text = ctx.response.send_message.call_args.args[0]
        self.assertIn(target.mention, text)
        self.assertIn(ctx.user.mention, text)
        posted_message.add_reaction.assert_awaited_once_with(helper_module.TEAM_INVITE_ACCEPT_EMOJI)

        # nobody's added to the roster yet
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 1)

        self.cursor.execute(
            "SELECT guildId, channelId, teamName, inviterId, targetId FROM team_invites WHERE messageId=?",
            (777,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, channel.id, "Red", 901, 902))


class HandleTeamInviteReactionTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("general")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

    async def _make_team_and_invite(self):
        create_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        await self.helperObj.createTeamHelper(create_ctx, "Red", 5)
        target = FakeMember("Bob", id=902)
        invite_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901), channel=self.channel)
        posted_message = FakeMessage(id=888)
        invite_ctx.original_response.return_value = posted_message
        await self.helperObj.teamInviteHelper(invite_ctx, "Red", target)
        return posted_message

    async def test_ignores_unrelated_emoji(self):
        message = await self._make_team_and_invite()
        payload = FakePayload(GUILD_ID, message.id, self.channel.id, "🎉", user_id=902)
        await self.helperObj.handleTeamInviteReaction(payload)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 1)

    async def test_accept_from_someone_other_than_the_invitee_is_ignored(self):
        message = await self._make_team_and_invite()
        payload = FakePayload(
            GUILD_ID, message.id, self.channel.id, helper_module.TEAM_INVITE_ACCEPT_EMOJI, user_id=903
        )
        await self.helperObj.handleTeamInviteReaction(payload)
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 1)

    async def test_accept_from_invitee_adds_them_to_the_roster(self):
        message = await self._make_team_and_invite()
        payload = FakePayload(
            GUILD_ID, message.id, self.channel.id, helper_module.TEAM_INVITE_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleTeamInviteReaction(payload)

        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual({p.get_id() for p in team.get_players()}, {901, 902})
        self.channel.send.assert_awaited_once()
        self.assertIn("Bob", self.channel.send.call_args.args[0])

        self.cursor.execute("SELECT COUNT(*) FROM team_invites")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_concurrent_accepts_only_add_once(self):
        message = await self._make_team_and_invite()
        payload1 = FakePayload(
            GUILD_ID, message.id, self.channel.id, helper_module.TEAM_INVITE_ACCEPT_EMOJI, user_id=902
        )
        payload2 = FakePayload(
            GUILD_ID, message.id, self.channel.id, helper_module.TEAM_INVITE_ACCEPT_EMOJI, user_id=902
        )
        await asyncio.gather(
            self.helperObj.handleTeamInviteReaction(payload1),
            self.helperObj.handleTeamInviteReaction(payload2),
        )
        _, team = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(len(team.get_players()), 2)


class TeamStatsHelperTests(HelperTestCase):
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


class TeamLeaderboardHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_no_teams_sends_a_message(self):
        ctx = self._ctx()
        await self.helperObj.teamLeaderboardHelper(ctx)
        ctx.response.send_message.assert_awaited_once_with(
            "No teams have been created in this server yet!"
        )

    async def test_ranks_teams_by_win_rate_then_wins_with_no_games_last(self):
        await self.helperObj.createTeamHelper(self._ctx(901, "Alice"), "Red", 5)
        await self.helperObj.createTeamHelper(self._ctx(902, "Bob"), "Blue", 5)
        await self.helperObj.createTeamHelper(self._ctx(903, "Cleo"), "Green", 5)

        red_id, red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        red.addWin()
        red.addWin()
        red.addLoss()  # 66.7%
        self.helperObj.updateTeamData(red_id, red)

        blue_id, blue = self.helperObj.getTeamRow(GUILD_ID, "Blue")
        blue.addWin()
        blue.addWin()
        blue.addWin()
        blue.addLoss()  # 75%
        self.helperObj.updateTeamData(blue_id, blue)
        # Green never plays — should sink to the bottom

        ctx = self._ctx()
        await self.helperObj.teamLeaderboardHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        lines = embed.description.split("\n")
        self.assertTrue(lines[0].startswith("**#1.** Blue"))
        self.assertTrue(lines[1].startswith("**#2.** Red"))
        self.assertTrue(lines[2].startswith("**#3.** Green"))


class UseTeamsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_picking_the_same_team_twice(self):
        await self.helperObj.createTeamHelper(self._ctx(), "Red", 5)
        ctx = self._ctx()
        await self.helperObj.useTeamsHelper(ctx, "Red", "Red", False)
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
        self.assertIn('Use "/start"', ctx.response.send_message.call_args.args[0])

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
        # movefunc's sake — the persistent team's row/id in `teams` is
        # untouched, since it never calls updateTeamData.
        after_id, stored_red = self.helperObj.getTeamRow(GUILD_ID, "Red")
        self.assertEqual(after_id, before_id)
        self.assertEqual(stored_red.get_id(), before_id)


class RegisterTeamHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

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
            "No tournament set up for this server — use /tournament-create first."
        )

    async def test_rejects_unknown_team(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        ctx = self._ctx()
        await self.helperObj.registerTeamHelper(ctx, "Nonexistent")
        ctx.response.send_message.assert_awaited_once_with("No team named **Nonexistent** in this server.")

    async def test_rejects_non_captain(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 2, 4))
        await self._make_team("Red", 901, "Alice", 2)
        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.registerTeamHelper(ctx, "Red")
        ctx.response.send_message.assert_awaited_once_with(
            "Only **Red**'s captain can register it for the tournament."
        )

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
            "No tournament set up for this server — use /tournament-create first."
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
            "Bracket created for **Cup** — 2 teams, double elimination."
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
        self.assertEqual(self.helperObj._nodeLabel(node), "Red")

    def test_node_label_describes_the_feeder_pairing_one_level_deep(self):
        red, blue = Team(), Team()
        red.set_name("Red")
        blue.set_name("Blue")
        leaf_a, leaf_b, parent = BracketNode(red), BracketNode(blue), BracketNode()
        leaf_a.opponent = leaf_b
        leaf_b.opponent = leaf_a
        leaf_a.next = parent
        leaf_b.next = parent
        parent.previous = leaf_a

        self.assertEqual(self.helperObj._nodeLabel(parent), "Winner of (Red vs Blue)")

    def test_node_label_is_tbd_with_no_team_and_no_previous(self):
        self.assertEqual(self.helperObj._nodeLabel(BracketNode()), "TBD")


class RenderBracketTextTests(HelperTestCase):
    async def test_no_bracket_yet(self):
        tournament = Tournament("Cup", 2, 4)
        self.assertEqual(self.helperObj.renderBracketText(tournament), "No bracket has been created yet.")

    async def test_renders_round_one_real_matchups_and_champion_line(self):
        tournament = Tournament("Cup", 2, 4)
        red, blue = Team(), Team()
        red.set_name("Red")
        blue.set_name("Blue")
        tournament.set_bracket(self.helperObj.buildBracket([red, blue]))

        text = self.helperObj.renderBracketText(tournament)

        self.assertIn("Cup", text)
        self.assertIn("__Round 1__", text)
        # buildBracket shuffles seeding, so don't assume which side is which
        self.assertIn("Red", text)
        self.assertIn("Blue", text)
        self.assertIn(" vs ", text)
        self.assertIn("Champion:", text)
        # only one real round exists, so the champion resolves one level
        # deep to a known pairing rather than staying a bare "TBD"
        self.assertIn("Winner of (", text)


class PrintBracketHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_rejects_when_no_tournament_exists(self):
        ctx = self._ctx()
        await self.helperObj.printBracketHelper(ctx)
        ctx.response.send_message.assert_awaited_once_with(
            "No tournament set up for this server — use /tournament-create first."
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
        self.assertIn("Red", text)
        self.assertIn("Blue", text)


class StartTournamentHelperTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat")
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
            "No tournament set up for this server — use /tournament-create first."
        )

    async def test_rejects_when_no_bracket_exists(self):
        self.helperObj.saveTournament(GUILD_ID, Tournament("Cup", 1, 2))
        ctx = self._ctx()
        await self.helperObj.startTournamentHelper(ctx, "sequential")
        ctx.response.send_message.assert_awaited_once_with(
            "No bracket has been created yet — use /tournament-create-bracket first."
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
            "**Cup** is already finished — **Red** is the champion!"
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


class HandleTournamentReactionTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat")
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

    async def test_ignores_unrelated_emoji(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        payload = FakePayload(GUILD_ID, message_id, self.channel.id, "🎉", user_id=901)
        await self.helperObj.handleTournamentReaction(payload)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_READY")

    async def test_ready_reaction_from_non_captain_is_ignored(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TOURNAMENT_READY_EMOJI, user_id=999
        )
        await self.helperObj.handleTournamentReaction(payload)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_READY")
        self.assertIsNone(self.helperObj.get(GUILD_ID, "active_tournament_match_id"))

    async def test_ready_reaction_from_either_captain_starts_the_match(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TOURNAMENT_READY_EMOJI, user_id=902
        )
        await self.helperObj.handleTournamentReaction(payload)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "AWAITING_RESULT")
        self.assertEqual(self.helperObj.get(GUILD_ID, "active_tournament_match_id"), match_id)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")

    async def test_sequential_resolution_advances_bracket_via_record_result(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        ready_payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TOURNAMENT_READY_EMOJI, user_id=902
        )
        await self.helperObj.handleTournamentReaction(ready_payload)

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

    async def test_simultaneous_result_reaction_resolves_match_and_advances(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("simultaneous", red, blue)
        match_id, message_id = self._only_match()

        self.cursor.execute("SELECT team2 FROM tournament_matches WHERE id=?", (match_id,))
        team2 = Team()
        team2.deserializeTeam(self.cursor.fetchone()[0])

        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TEAM_EMOJIS[2], user_id=555
        )
        await self.helperObj.handleTournamentReaction(payload)

        self.cursor.execute("SELECT state, winner FROM tournament_matches WHERE id=?", (match_id,))
        state, winner = self.cursor.fetchone()
        self.assertEqual(state, "RESOLVED")
        self.assertEqual(winner, 2)

        updated = self.helperObj.getTournament(GUILD_ID)
        champion = updated.get_bracket()[-1]
        self.assertEqual(champion.team.get_name(), team2.get_name())

    async def test_simultaneous_reaction_on_a_pending_ready_match_is_ignored(self):
        red = _captained_team("Red", 901, "Alice")
        blue = _captained_team("Blue", 902, "Bob")
        await self._start("sequential", red, blue)
        match_id, message_id = self._only_match()

        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TEAM_EMOJIS[1], user_id=555
        )
        await self.helperObj.handleTournamentReaction(payload)

        self.cursor.execute("SELECT state FROM tournament_matches WHERE id=?", (match_id,))
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_READY")

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
            payload = FakePayload(
                GUILD_ID, message_id, self.channel.id, helper_module.TEAM_EMOJIS[1], user_id=555
            )
            await self.helperObj.handleTournamentReaction(payload)

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

        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TEAM_EMOJIS[1], user_id=555
        )
        await self.helperObj.handleTournamentReaction(payload)

        messages = [c.args[0] for c in self.channel.send.call_args_list if c.args]
        self.assertTrue(any("is complete!" in m and "Champion" in m for m in messages))


class CorrectTournamentMatchHelperTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("tourney-chat")
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
        payload = FakePayload(
            GUILD_ID, message_id, self.channel.id, helper_module.TEAM_EMOJIS[winner], user_id=555
        )
        await self.helperObj.handleTournamentReaction(payload)
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
            "messageId, channelId, winner) VALUES(?, ?, 0, '', '', 'QUEUED', 'simultaneous', NULL, ?, NULL)",
            (GUILD_ID, round_index + 1, self.channel.id)
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.reportCorrectWinnerHelper(ctx, 2, match_id=match_id)
        ctx.response.send_message.assert_awaited_once_with(
            f"Can't correct Match #{match_id} — the next round has already started."
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


class RandomRoleHelperTests(HelperTestCase):
    async def test_assigns_a_role_line_per_player(self):
        team1 = Team()
        team1.name = "Team 1"
        for i in range(5):
            team1.add_player(Player(600 + i, f"P{i}"))
        team2 = Team()
        team2.name = "Team 2"
        for i in range(3):
            team2.add_player(Player(700 + i, f"Q{i}"))

        self.helperObj.update(GUILD_ID, "team1", team1.serializeTeam())
        self.helperObj.update(GUILD_ID, "team2", team2.serializeTeam())

        ctx = FakeInteraction(self.guild, FakeMember("Caller"))
        await self.helperObj.randomRoleHelper(ctx)

        result1 = self.helperObj.get(GUILD_ID, "result1")
        result2 = self.helperObj.get(GUILD_ID, "result2")

        for p in team1.get_players():
            self.assertIn(p.get_name(), result1)
        for p in team2.get_players():
            self.assertIn(p.get_name(), result2)
        self.assertEqual(result1.count(" - "), 5)
        self.assertEqual(result2.count(" - "), 3)


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


class SetTeamHelperTests(HelperTestCase):
    async def test_creates_channels_when_missing(self):
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.setTeamHelper(ctx, "Red", "Blue")

        names = {c.name for c in self.guild.channels}
        self.assertEqual(names, {"Red", "Blue"})
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel1"), "Red")
        self.assertEqual(self.helperObj.get(GUILD_ID, "channel2"), "Blue")
        ctx.response.send_message.assert_awaited_once_with("Channels set!")

    async def test_reuses_existing_channels(self):
        self.guild.channels.append(FakeChannel("Red"))
        self.guild.channels.append(FakeChannel("Blue"))
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.setTeamHelper(ctx, "Red", "Blue")

        self.assertEqual(len(self.guild.channels), 2)


class SetWagerChannelHelperTests(HelperTestCase):
    async def test_creates_the_channel_when_missing(self):
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.setWagerChannelHelper(ctx, "bets")

        created = [c for c in self.guild.channels if c.name == "bets"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].kind, "text")
        self.assertEqual(self.helperObj.get(GUILD_ID, "wager_channel"), "bets")
        ctx.response.send_message.assert_awaited_once()
        self.assertIn(created[0].mention, ctx.response.send_message.call_args.args[0])

    async def test_reuses_an_existing_text_channel(self):
        self.guild.channels.append(FakeChannel("bets", kind="text"))
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.setWagerChannelHelper(ctx, "bets")

        self.assertEqual(len(self.guild.channels), 1)
        self.assertEqual(self.helperObj.get(GUILD_ID, "wager_channel"), "bets")

    async def test_ignores_a_same_named_voice_channel(self):
        self.guild.channels.append(FakeChannel("bets", kind="voice"))
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.setWagerChannelHelper(ctx, "bets")

        # a new text channel is created rather than reusing the voice one
        text_channels = [c for c in self.guild.channels if c.kind == "text"]
        self.assertEqual(len(text_channels), 1)


class NotifyHelperTests(HelperTestCase):
    async def test_sends_dm_with_invite(self):
        self.helperObj.update(GUILD_ID, "team_size", 5)
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
        self.assertIn("10 man", content)
        self.assertIn("https://discord.gg/fake-invite", content)


class ReturnHelperTests(HelperTestCase):
    async def test_not_separated_sends_message_and_does_not_defer(self):
        ctx = FakeInteraction(self.guild, FakeMember("Caller"))

        await self.helperObj.returnHelper(ctx)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("/start", ctx.response.send_message.call_args.args[0])
        ctx.response.defer.assert_not_awaited()

    async def test_moves_members_back_and_refunds_open_bets(self):
        og = FakeChannel("Lobby")
        channel1 = FakeChannel("Team 1")
        channel2 = FakeChannel("Team 2")
        member1 = FakeMember("Alice", id=801)
        member2 = FakeMember("Bob", id=802)
        channel1.members = [member1]
        channel2.members = [member2]

        guild = FakeGuild(channels=[og, channel1, channel2])
        ctx = FakeInteraction(guild, FakeMember("Caller"))

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

        await self.helperObj.returnHelper(ctx)

        member1.move_to.assert_awaited_once_with(og)
        member2.move_to.assert_awaited_once_with(og)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 801, "balance"), 700)
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)
        ctx.followup.send.assert_awaited_once_with("Moved!")

    async def test_return_on_ranked_game_refunds_bets_without_touching_elo(self):
        # /return ending a ranked game early behaves exactly like ending a
        # regular game early: bets get refunded, and — since recordResult
        # (the only place elo/game record ever change) never runs — elo
        # and game record are left completely untouched, not just "reset".
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
        ctx = FakeInteraction(guild, FakeMember("Caller"))

        self.helperObj.ensureEconomyRow(GUILD_ID, 801, "Bettor")
        self.cursor.execute(
            "UPDATE economy SET balance=500 WHERE guildId=? AND userId=?", (GUILD_ID, 801)
        )
        self.cursor.execute(
            "INSERT INTO wagers(guildId, userId, username, team, amount) VALUES(?, ?, ?, ?, ?)",
            (GUILD_ID, 801, "Bettor", 1, 200),
        )
        self.db.commit()

        await self.helperObj.returnHelper(ctx)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 801, "balance"), 700)  # bet refunded
        self.cursor.execute("SELECT COUNT(*) FROM wagers WHERE guildId=?", (GUILD_ID,))
        self.assertEqual(self.cursor.fetchone()[0], 0)

        # rostered players never touched at all — no economy row even exists
        self.assertIsNone(self.helperObj.getEconomy(GUILD_ID, 701, "elo"))
        self.assertIsNone(self.helperObj.getEconomy(GUILD_ID, 702, "elo"))


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


# ===========================================================================
# Betting: /wager, startBettingHelper, the background timer, recordResult,
# handleWinnerReaction, cancelBettingHelper
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
            "Betting is not currently open. Use \"/start\" to start a game and open betting."
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
        ctx.response.send_message.assert_awaited_once_with("You wagered 250 gold on Team 2!")

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

        ctx.response.send_message.assert_awaited_once_with("You wagered 100 gold on Team 1!")


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

        message = channel.send.call_args.args[0]
        self.assertIn("won 400 gold (bet 100)", message)

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
        # regression test: recording a winner used to only pay out bets —
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

        message = channel.send.call_args.args[0]
        self.assertIn("Elo:", message)

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
        # /ranked does, but is_ranked defaults to 0 for them — elo must stay
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
                "game_wins, game_losses, elo FROM economy WHERE guildId=? ORDER BY userId",
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
            "Team 1 is already the recorded winner — nothing to correct."
        )

    async def test_corrects_bettor_payouts_when_winner_flips(self):
        # No rosters — isolates the betting-correction math. Alice bet on
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

        # money is conserved (2000 total) and now sits with the real winner
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 900)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_lost"), 100)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 1100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 0)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_won"), 100)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_lost"), 0)

        ctx.response.send_message.assert_awaited_once()
        self.assertIn("Team 2", ctx.response.send_message.call_args.args[0])
        self.assertIn("previously recorded as Team 1", ctx.response.send_message.call_args.args[0])

        # a further correction is possible from the new baseline
        self.assertEqual(self.helperObj.getLastResult(GUILD_ID)["winning_team"], 2)

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


class StatsHelperTests(HelperTestCase):
    def _ctx(self, user_id=901, name="Alice"):
        return FakeInteraction(self.guild, FakeMember(name, id=user_id))

    async def test_defaults_for_a_brand_new_player(self):
        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Elo"], "1000 (\U0001f537 Platinum IV)")
        self.assertEqual(values["Game Record"], "0W - 0L")
        self.assertEqual(values["Game Win Rate"], "N/A")
        self.assertEqual(values["Bet Win Rate"], "N/A")
        self.assertEqual(values["Balance"], "0 gold")

    async def test_reports_populated_stats(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=500, wins=2, losses=1, gold_wagered=300, "
            "gold_won=150, gold_lost=50, game_wins=7, game_losses=3, elo=1123 "
            "WHERE guildId=? AND userId=?",
            (GUILD_ID, 901),
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.statsHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Elo"], "1123 (\U0001f537 Platinum III)")
        self.assertEqual(values["Game Record"], "7W - 3L")
        self.assertEqual(values["Game Win Rate"], "70.0%")
        self.assertEqual(values["Bet Record"], "2W - 1L")
        self.assertEqual(values["Bet Win Rate"], "66.7%")
        self.assertEqual(values["Net Gold Won/Lost"], "+100 gold")

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


class CancelBettingHelperTests(HelperTestCase):
    async def test_noop_when_no_active_round(self):
        channel = FakeChannel("game-chat")
        await self.helperObj.cancelBettingHelper(GUILD_ID, channel)
        channel.send.assert_not_awaited()

    async def test_refunds_open_bets_and_resets_state(self):
        self.helperObj.update(GUILD_ID, "betting_state", "AWAITING_RESULT")
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


class StartBettingHelperTests(HelperTestCase):
    async def test_opens_betting_and_schedules_timer(self):
        channel = FakeChannel("game-chat")
        ctx = FakeInteraction(self.guild, FakeMember("Caller"), channel=channel)

        await self.helperObj.startBettingHelper(ctx)

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")
        self.assertIn(GUILD_ID, self.helperObj.bettingTasks)
        channel.send.assert_awaited_once()
        self.assertIn("Betting is now open", channel.send.call_args.args[0])

        # _bettingTimer catches CancelledError itself (so a cancelled game
        # never crashes with an unhandled exception) — once a task is
        # genuinely suspended on the sleep and then cancelled, that means
        # it finishes *normally* rather than raising, which is fine: what
        # actually matters is that it stops and cleans itself up.
        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])
        self.assertTrue(task.done())

    async def test_redirects_to_the_configured_wager_channel(self):
        origin_channel = FakeChannel("game-chat")
        wager_channel = FakeChannel("bets", kind="text")
        self.guild.channels.append(wager_channel)
        self.helperObj.client = FakeClient(guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "wager_channel", "bets")

        ctx = FakeInteraction(self.guild, FakeMember("Caller"), channel=origin_channel)
        await self.helperObj.startBettingHelper(ctx)

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

        ctx = FakeInteraction(self.guild, FakeMember("Caller"), channel=origin_channel)
        await self.helperObj.startBettingHelper(ctx)

        origin_channel.send.assert_awaited_once()

        task = self.helperObj.bettingTasks[GUILD_ID]
        task.cancel()
        await asyncio.wait([task])

    async def test_full_timer_flow_opens_reports_and_awaits_result(self):
        with patch.object(helper_module, "BETTING_DURATION_SECONDS", 0), \
             patch.object(helper_module, "WINNER_REPORT_DELAY_SECONDS", 0):
            channel = FakeChannel("game-chat")
            channel.send = AsyncMock(return_value=FakeMessage(id=12345))
            ctx = FakeInteraction(self.guild, FakeMember("Caller"), channel=channel)

            await self.helperObj.startBettingHelper(ctx)
            await self.helperObj.bettingTasks[GUILD_ID]

        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "AWAITING_RESULT")
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_message_id"), 12345)
        self.assertEqual(channel.send.await_count, 3)  # open, closed, report-winner
        last_message = channel.send.return_value
        last_message.add_reaction.assert_any_await("1️⃣")
        last_message.add_reaction.assert_any_await("2️⃣")
        self.assertNotIn(GUILD_ID, self.helperObj.bettingTasks)

    async def test_restarting_mid_round_cancels_and_refunds_previous_round(self):
        with patch.object(helper_module, "BETTING_DURATION_SECONDS", 100):
            channel = FakeChannel("game-chat")
            ctx = FakeInteraction(self.guild, FakeMember("Caller"), channel=channel)

            await self.helperObj.startBettingHelper(ctx)

            self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
            self.cursor.execute(
                "UPDATE economy SET balance=1000 WHERE guildId=? AND userId=?", (GUILD_ID, 901)
            )
            self.db.commit()
            bet_ctx = FakeInteraction(self.guild, FakeMember("Alice", id=901))
            await self.helperObj.wagerHelper(bet_ctx, 400, 1)

            first_task = self.helperObj.bettingTasks[GUILD_ID]

            await self.helperObj.startBettingHelper(ctx)
            await asyncio.sleep(0)  # let the requested cancellation propagate

            self.assertTrue(first_task.cancelled())
            self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
            self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "OPEN")

            # clean up the second (still-open) round's timer task so it
            # doesn't leak past the end of the test
            second_task = self.helperObj.bettingTasks[GUILD_ID]
            second_task.cancel()
            await asyncio.wait([second_task])


class HandleWinnerReactionTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("game-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])
        self.helperObj.update(GUILD_ID, "betting_state", "AWAITING_RESULT")
        self.helperObj.update(GUILD_ID, "betting_message_id", 555)

    async def test_ignores_unrelated_emoji(self):
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            payload = FakePayload(GUILD_ID, 555, self.channel.id, "🎉")
            await self.helperObj.handleWinnerReaction(payload)
        mock.assert_not_awaited()

    async def test_ignores_when_not_awaiting_result(self):
        self.helperObj.update(GUILD_ID, "betting_state", "OPEN")
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            payload = FakePayload(GUILD_ID, 555, self.channel.id, "1️⃣")
            await self.helperObj.handleWinnerReaction(payload)
        mock.assert_not_awaited()

    async def test_ignores_mismatched_message_id(self):
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            payload = FakePayload(GUILD_ID, 999, self.channel.id, "1️⃣")
            await self.helperObj.handleWinnerReaction(payload)
        mock.assert_not_awaited()

    async def test_valid_reaction_flips_state_and_calls_record_result(self):
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            payload = FakePayload(GUILD_ID, 555, self.channel.id, "1️⃣")
            await self.helperObj.handleWinnerReaction(payload)

        # the guild gets looked up and forwarded so recordResult can move
        # everyone back to the original channel once the winner is settled
        mock.assert_awaited_once_with(GUILD_ID, 1, self.channel, self.guild)
        # state flips to NONE synchronously before recordResult runs, so a
        # second/concurrent reaction can't also pass the guard above
        self.assertEqual(self.helperObj.get(GUILD_ID, "betting_state"), "NONE")

    async def test_concurrent_reactions_only_process_once(self):
        with patch.object(self.helperObj, "recordResult", AsyncMock()) as mock:
            payload1 = FakePayload(GUILD_ID, 555, self.channel.id, "1️⃣")
            payload2 = FakePayload(GUILD_ID, 555, self.channel.id, "2️⃣")
            await asyncio.gather(
                self.helperObj.handleWinnerReaction(payload1),
                self.helperObj.handleWinnerReaction(payload2),
            )

        mock.assert_awaited_once()


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
        posted_message.add_reaction.assert_awaited_once_with(helper_module.DUEL_ACCEPT_EMOJI)

        # challenging doesn't escrow anything up front
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)

        self.cursor.execute(
            "SELECT guildId, channelId, messageId, challengerId, targetId, amount, state FROM duels"
        )
        row = self.cursor.fetchone()
        self.assertEqual(
            row, (GUILD_ID, channel.id, posted_message.id, 901, 902, 250, "PENDING_ACCEPT")
        )


class HandleDuelReactionTests(HelperTestCase):
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
        # accepting posts a new message and reacts on it — give it a real
        # id/add_reaction rather than the channel's plain default AsyncMock.
        self.channel.send = AsyncMock(return_value=FakeMessage(id=777))

    async def test_ignores_unrelated_emoji(self):
        payload = FakePayload(GUILD_ID, 555, self.channel.id, "🎉", user_id=902)
        await self.helperObj.handleDuelReaction(payload)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)

    async def test_accept_from_challenger_is_ignored(self):
        payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=901
        )
        await self.helperObj.handleDuelReaction(payload)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=555")
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_ACCEPT")
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)

    async def test_accept_from_target_escrows_both_and_posts_result_prompt(self):
        result_message = self.channel.send.return_value

        payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleDuelReaction(payload)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 750)

        self.cursor.execute(
            "SELECT state, messageId FROM duels WHERE challengerId=901 AND targetId=902"
        )
        state, message_id = self.cursor.fetchone()
        self.assertEqual(state, "AWAITING_RESULT")
        self.assertEqual(message_id, 777)

        result_message.add_reaction.assert_any_await(helper_module.DUEL_CHALLENGER_EMOJI)
        result_message.add_reaction.assert_any_await(helper_module.DUEL_TARGET_EMOJI)

    async def test_accept_cancels_if_either_side_cant_cover_it(self):
        self.cursor.execute(
            "UPDATE economy SET balance=100 WHERE guildId=? AND userId=902", (GUILD_ID,)
        )
        self.db.commit()

        payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleDuelReaction(payload)

        # nothing escrowed, no duel left behind
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1000)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 100)
        self.cursor.execute("SELECT COUNT(*) FROM duels")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.channel.send.assert_awaited_once()

    async def test_result_reaction_before_accept_is_ignored(self):
        payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_CHALLENGER_EMOJI, user_id=555
        )
        await self.helperObj.handleDuelReaction(payload)

        self.cursor.execute("SELECT state FROM duels WHERE messageId=555")
        self.assertEqual(self.cursor.fetchone()[0], "PENDING_ACCEPT")

    async def test_challenger_win_pays_out_and_updates_bet_records(self):
        accept_payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleDuelReaction(accept_payload)

        self.cursor.execute("SELECT messageId FROM duels WHERE challengerId=901")
        result_message_id = self.cursor.fetchone()[0]

        win_payload = FakePayload(
            GUILD_ID, result_message_id, self.channel.id, helper_module.DUEL_CHALLENGER_EMOJI, user_id=903
        )
        await self.helperObj.handleDuelReaction(win_payload)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 1250)  # 750 + 500 pot
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "gold_won"), 250)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "losses"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "gold_lost"), 250)

        self.cursor.execute("SELECT COUNT(*) FROM duels")
        self.assertEqual(self.cursor.fetchone()[0], 0)

    async def test_target_win_pays_out_the_other_way(self):
        accept_payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleDuelReaction(accept_payload)

        self.cursor.execute("SELECT messageId FROM duels WHERE challengerId=901")
        result_message_id = self.cursor.fetchone()[0]

        win_payload = FakePayload(
            GUILD_ID, result_message_id, self.channel.id, helper_module.DUEL_TARGET_EMOJI, user_id=903
        )
        await self.helperObj.handleDuelReaction(win_payload)

        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "balance"), 1250)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 902, "wins"), 1)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "balance"), 750)
        self.assertEqual(self.helperObj.getEconomy(GUILD_ID, 901, "losses"), 1)

    async def test_concurrent_result_reactions_only_pay_out_once(self):
        accept_payload = FakePayload(
            GUILD_ID, 555, self.channel.id, helper_module.DUEL_ACCEPT_EMOJI, user_id=902
        )
        await self.helperObj.handleDuelReaction(accept_payload)

        self.cursor.execute("SELECT messageId FROM duels WHERE challengerId=901")
        result_message_id = self.cursor.fetchone()[0]

        payload1 = FakePayload(
            GUILD_ID, result_message_id, self.channel.id, helper_module.DUEL_CHALLENGER_EMOJI, user_id=903
        )
        payload2 = FakePayload(
            GUILD_ID, result_message_id, self.channel.id, helper_module.DUEL_TARGET_EMOJI, user_id=904
        )
        await asyncio.gather(
            self.helperObj.handleDuelReaction(payload1),
            self.helperObj.handleDuelReaction(payload2),
        )

        # exactly one side paid out — total gold in the pot is conserved
        total = (
            self.helperObj.getEconomy(GUILD_ID, 901, "balance")
            + self.helperObj.getEconomy(GUILD_ID, 902, "balance")
        )
        self.assertEqual(total, 2000)


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
        # Cleo has no bets/games played yet — her win rates should be None.
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

        for emoji in helper_module.LEADERBOARD_NAV_EMOJIS:
            posted_message.add_reaction.assert_any_await(emoji)

        self.cursor.execute(
            "SELECT guildId, channelId, filter, sort_order, page FROM leaderboards WHERE messageId=?",
            (8888,)
        )
        self.assertEqual(self.cursor.fetchone(), (GUILD_ID, channel.id, "balance", "asc", 0))

    async def test_overview_mode_defaults_sort_to_elo_and_shows_elo_and_record(self):
        self._seed_players()
        ctx = self._ctx()

        await self.helperObj.leaderboardHelper(ctx, None, "desc")

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        self.assertIn("Overview", embed.title)
        lines = embed.description.split("\n")
        self.assertTrue(lines[0].startswith("**#1.** Alice"))  # highest elo (1300)
        self.assertIn("Elo:", lines[0])
        self.assertIn("Record:", lines[0])
        self.assertNotIn("Balance:", lines[0])


class HandleLeaderboardReactionTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel("leaderboard-chat")
        self.helperObj.client = FakeClient(channels=[self.channel], guilds=[self.guild])

        for i in range(25):
            user_id = 1000 + i
            self.helperObj.ensureEconomyRow(GUILD_ID, user_id, f"Player{i:02d}")
            self.cursor.execute(
                "UPDATE economy SET elo=? WHERE guildId=? AND userId=?",
                (1000 + i, GUILD_ID, user_id)
            )
        self.db.commit()

        self.message = FakeMessage(id=9999)
        self.channel.fetch_message = AsyncMock(return_value=self.message)
        self.cursor.execute(
            "INSERT INTO leaderboards(messageId, guildId, channelId, filter, sort_order, page) "
            "VALUES(9999, ?, ?, NULL, 'desc', 1)",
            (GUILD_ID, self.channel.id)
        )
        self.db.commit()

    def _page(self):
        self.cursor.execute("SELECT page FROM leaderboards WHERE messageId=9999")
        return self.cursor.fetchone()[0]

    async def test_ignores_unrelated_emoji(self):
        payload = FakePayload(GUILD_ID, 9999, self.channel.id, "🎉", user_id=1)
        await self.helperObj.handleLeaderboardReaction(payload)
        self.message.edit.assert_not_awaited()

    async def test_ignores_unknown_message(self):
        payload = FakePayload(
            GUILD_ID, 12345, self.channel.id, helper_module.LEADERBOARD_NEXT_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.message.edit.assert_not_awaited()

    async def test_next_advances_page_and_edits_message(self):
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_NEXT_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)

        self.assertEqual(self._page(), 2)
        self.message.edit.assert_awaited_once()
        embed = self.message.edit.call_args.kwargs["embed"]
        self.assertIn("Page 3/3", embed.footer.text)

    async def test_prev_goes_back_a_page(self):
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_PREV_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.assertEqual(self._page(), 0)

    async def test_first_jumps_to_page_zero(self):
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_FIRST_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.assertEqual(self._page(), 0)

    async def test_last_jumps_to_final_page(self):
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_LAST_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.assertEqual(self._page(), 2)  # 25 entries / 10 per page = 3 pages (0, 1, 2)

    async def test_next_at_last_page_is_a_noop(self):
        self.cursor.execute("UPDATE leaderboards SET page=2 WHERE messageId=9999")
        self.db.commit()
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_NEXT_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.assertEqual(self._page(), 2)
        self.message.edit.assert_not_awaited()

    async def test_prev_at_first_page_is_a_noop(self):
        self.cursor.execute("UPDATE leaderboards SET page=0 WHERE messageId=9999")
        self.db.commit()
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_PREV_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.assertEqual(self._page(), 0)
        self.message.edit.assert_not_awaited()

    async def test_edits_existing_message_rather_than_sending_new_one(self):
        payload = FakePayload(
            GUILD_ID, 9999, self.channel.id, helper_module.LEADERBOARD_NEXT_EMOJI, user_id=1
        )
        await self.helperObj.handleLeaderboardReaction(payload)
        self.message.edit.assert_awaited_once()
        self.channel.send.assert_not_awaited()


# ===========================================================================
# bot.py — import with DB/token side effects redirected away from the real
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

    def tearDown(self):
        self.bot.mainDB.close()
        sys.modules.pop("bot", None)

    def _command(self, name):
        for c in self.bot.tree.get_commands(guild=discord.Object(id=COMMAND_GUILD_ID)):
            if c.name == name:
                return c
        raise AssertionError(f"command {name!r} is not registered")

    def _ctx(self, guild_id=GUILD_ID, channel=None):
        guild = FakeGuild(id=guild_id)
        user = FakeMember("Caller", id=1)
        return FakeInteraction(guild, user, channel=channel)

    async def _insert_guild_row(self, guild_id, name="Test Guild"):
        await self.bot.on_guild_join(SimpleNamespace(id=guild_id, name=name))


class CommandRegistrationTests(BotModuleTestCase):
    def test_all_expected_commands_registered(self):
        names = {c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=COMMAND_GUILD_ID))}
        expected = {
            "team-set-size", "team-set-channels", "start", "wager", "wager-against", "daily",
            "stats", "leaderboard", "help", "make-teams", "ranked", "return", "report-correct-winner",
            "captains", "ranked-captains", "choose", "clear", "notify", "notify-role",
            "roll", "randomize-roles", "tournament-create", "team-create", "team-set-voice-channel",
            "team-invite", "team-stats", "team-leaderboard", "tournament-register", "tournament-create-bracket",
            "tournament-print-bracket", "tournament-start", "wager-set-channel", "team-use",
        }
        self.assertEqual(names, expected)

    def test_move_is_not_a_registered_command_name(self):
        names = {c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=COMMAND_GUILD_ID))}
        self.assertNotIn("move", names)


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


class ReactionEventTests(BotModuleTestCase):
    HANDLER_NAMES = (
        "handleWinnerReaction", "handleDuelReaction", "handleLeaderboardReaction",
        "handleTeamInviteReaction", "handleTournamentReaction",
    )

    def _patch_all_handlers(self, stack):
        return {
            name: stack.enter_context(patch.object(self.bot.helperObj, name, AsyncMock()))
            for name in self.HANDLER_NAMES
        }

    async def test_ignores_bot_reactions(self):
        payload = SimpleNamespace(member=FakeMember("BotUser", bot=True), guild_id=1)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_all_handlers(stack)
            await self.bot.on_raw_reaction_add(payload)
        for mock in mocks.values():
            mock.assert_not_awaited()

    async def test_ignores_reactions_with_no_member(self):
        payload = SimpleNamespace(member=None, guild_id=1)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_all_handlers(stack)
            await self.bot.on_raw_reaction_add(payload)
        for mock in mocks.values():
            mock.assert_not_awaited()

    async def test_ignores_dm_reactions(self):
        payload = SimpleNamespace(member=FakeMember("User"), guild_id=None)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_all_handlers(stack)
            await self.bot.on_raw_reaction_add(payload)
        for mock in mocks.values():
            mock.assert_not_awaited()

    async def test_delegates_valid_guild_member_reaction(self):
        payload = SimpleNamespace(member=FakeMember("User"), guild_id=1)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_all_handlers(stack)
            await self.bot.on_raw_reaction_add(payload)
        for mock in mocks.values():
            mock.assert_awaited_once_with(payload)


class CommandDelegationTests(BotModuleTestCase):
    async def test_start_moves_then_opens_betting(self):
        ctx = self._ctx()
        order = []
        with patch.object(self.bot.helperObj, "movefunc", AsyncMock(side_effect=lambda c: order.append("move"))), \
             patch.object(self.bot.helperObj, "startBettingHelper", AsyncMock(side_effect=lambda c: order.append("bet"))):
            await self._command("start").callback(ctx)

        ctx.response.defer.assert_awaited_once()
        ctx.followup.send.assert_awaited_once_with("Moved!")
        self.assertEqual(order, ["move", "bet"])

    async def test_wager_passes_resolved_team_value(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "wagerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("wager").callback(ctx, 250, choice)
        mock.assert_awaited_once_with(ctx, 250, 2)

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
        mock.assert_awaited_once_with(ctx, None, "desc")

    async def test_leaderboard_passes_resolved_filter_and_order(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "leaderboardHelper", mock):
            filter_choice = app_commands.Choice(name="Balance", value="balance")
            order_choice = app_commands.Choice(name="Ascending (lowest first)", value="asc")
            await self._command("leaderboard").callback(ctx, filter=filter_choice, order=order_choice)
        mock.assert_awaited_once_with(ctx, "balance", "asc")

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
        mock.assert_awaited_once_with(ctx, True)

    async def test_create_bracket_single_elimination_resolves_to_false(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "createBracketHelper", mock):
            choice = app_commands.Choice(name="Single elimination", value="single")
            await self._command("tournament-create-bracket").callback(ctx, elimination_type=choice)
        mock.assert_awaited_once_with(ctx, False)

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
        mock.assert_awaited_once_with(ctx, "Red", 5)

    async def test_set_team_voice_channel_defaults_channel_to_none(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "setTeamVoiceChannelHelper", mock):
            await self._command("team-set-voice-channel").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red", None)

    async def test_set_team_voice_channel_passes_channel(self):
        ctx = self._ctx()
        mock = AsyncMock()
        channel = FakeChannel("Arena")
        with patch.object(self.bot.helperObj, "setTeamVoiceChannelHelper", mock):
            await self._command("team-set-voice-channel").callback(ctx, "Red", channel=channel)
        mock.assert_awaited_once_with(ctx, "Red", channel)

    async def test_team_invite_delegates(self):
        ctx = self._ctx()
        target = FakeMember("Bob", id=902)
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamInviteHelper", mock):
            await self._command("team-invite").callback(ctx, "Red", target)
        mock.assert_awaited_once_with(ctx, "Red", target)

    async def test_team_stats_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamStatsHelper", mock):
            await self._command("team-stats").callback(ctx, "Red")
        mock.assert_awaited_once_with(ctx, "Red")

    async def test_team_leaderboard_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "teamLeaderboardHelper", mock):
            await self._command("team-leaderboard").callback(ctx)
        mock.assert_awaited_once_with(ctx)

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

    async def test_set_wager_channel_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "setWagerChannelHelper", mock):
            await self._command("wager-set-channel").callback(ctx, "bets")
        mock.assert_awaited_once_with(ctx, "bets")

    async def test_return_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "returnHelper", mock):
            await self._command("return").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_set_team_channels_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "setTeamHelper", mock):
            await self._command("team-set-channels").callback(ctx, team1="Red", team2="Blue")
        mock.assert_awaited_once_with(ctx, "Red", "Blue")

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

    async def test_help_sends_message(self):
        ctx = self._ctx()
        await self._command("help").callback(ctx)
        ctx.response.send_message.assert_awaited_once()


class SetTeamSizeCommandTests(BotModuleTestCase):
    async def test_updates_team_size_and_confirms(self):
        guild_id = 902
        await self._insert_guild_row(guild_id)
        ctx = self._ctx(guild_id=guild_id)

        await self._command("team-set-size").callback(ctx, sizechange=4)

        self.assertEqual(self.bot.helperObj.get(guild_id, "team_size"), 4)
        ctx.response.send_message.assert_awaited_once_with("Set team size!")


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
        self.bot.helperObj.update(guild_id, "tournament", "Spring Cup")
        self.bot.helperObj.update(guild_id, "elo", "1200")
        ctx = self._ctx(guild_id=guild_id)

        await self._command("clear").callback(
            ctx, clear_channels=True, clear_tournament=True, clear_elo=True
        )

        self.assertEqual(self.bot.helperObj.get(guild_id, "channel1"), "")
        self.assertEqual(self.bot.helperObj.get(guild_id, "tournament"), "")
        self.assertEqual(self.bot.helperObj.get(guild_id, "elo"), "")
        ctx.response.send_message.assert_awaited_once_with("Cleared!")

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

        # the legacy servers.elo field clears immediately; real per-player
        # elo does not, until the reset is confirmed.
        self.assertEqual(self.bot.helperObj.get(guild_id, "elo"), "")
        self.assertEqual(self.bot.helperObj.getEconomy(guild_id, 901, "elo"), 1500)

        view = ctx.followup.send.call_args.kwargs["view"]
        click = self._ctx(guild_id=guild_id)
        await view.confirm.callback(click)

        self.assertEqual(
            self.bot.helperObj.getEconomy(guild_id, 901, "elo"), helper_module.DEFAULT_ELO
        )
        # balance is untouched — clear_elo only resets elo
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
            content="Cancelled — nothing was reset.", view=view
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
    async def test_notify_reports_team_size(self):
        guild_id = 904
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.update(guild_id, "team_size", 5)
        ctx = self._ctx(guild_id=guild_id)
        target = FakeMember("Target")

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify").callback(ctx, member=target)

        mock.assert_awaited_once_with(ctx, target)
        ctx.response.send_message.assert_awaited_once_with("Sent an invite for the 10 man!")

    async def test_notify_role_notifies_every_member(self):
        guild_id = 905
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.update(guild_id, "team_size", 5)
        ctx = self._ctx(guild_id=guild_id)
        role = SimpleNamespace(members=[FakeMember("A"), FakeMember("B")])

        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "notifyHelper", mock):
            await self._command("notify-role").callback(ctx, role=role)

        self.assertEqual(mock.await_count, 2)
        ctx.response.send_message.assert_awaited_once_with("Sent an invite for the 10 man!")


class RandomizeRolesCommandTests(BotModuleTestCase):
    async def test_reports_both_results(self):
        guild_id = 906
        await self._insert_guild_row(guild_id)
        self.bot.helperObj.update(guild_id, "result1", "Top - A\n")
        self.bot.helperObj.update(guild_id, "result2", "Top - B\n")
        ctx = self._ctx(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomRoleHelper", AsyncMock()) as mock:
            await self._command("randomize-roles").callback(ctx)

        mock.assert_awaited_once_with(ctx)
        ctx.response.send_message.assert_awaited_once()
        message = ctx.response.send_message.call_args.args[0]
        self.assertIn("Top - A", message)
        self.assertIn("Top - B", message)


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
        ctx = self._ctx(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(self.bot.helperObj, "both", AsyncMock()) as both_mock, \
             patch.object(self.bot.helperObj, "movefunc", AsyncMock()) as move_mock, \
             patch.object(self.bot.helperObj, "printEmbed", AsyncMock()) as embed_mock:
            await self._command("make-teams").callback(ctx, use_roles=False)

        randomize_mock.assert_awaited_once_with(ctx)
        both_mock.assert_not_awaited()
        # regression: /make-teams used to optionally move everyone itself;
        # that's exclusively /start's job now.
        move_mock.assert_not_awaited()
        embed_mock.assert_awaited_once()
        ctx.response.send_message.assert_awaited_once_with("Teams created!")
        # regression: the /start reminder used to be folded into the very
        # first response, which posts *before* the team embeds and is easy
        # to miss. It's the last message sent now, after the rosters.
        ctx.channel.send.assert_awaited_once()
        self.assertIn("/start", ctx.channel.send.call_args.args[0])

    async def test_use_roles_calls_both_instead_of_randomize(self):
        guild_id = 908
        await self._setup_teams(guild_id)
        ctx = self._ctx(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "randomizeTeamHelper", AsyncMock()) as randomize_mock, \
             patch.object(self.bot.helperObj, "both", AsyncMock()) as both_mock, \
             patch.object(self.bot.helperObj, "movefunc", AsyncMock()) as move_mock, \
             patch.object(self.bot.helperObj, "printEmbed", AsyncMock()) as embed_mock:
            await self._command("make-teams").callback(ctx, use_roles=True)

        both_mock.assert_awaited_once_with(ctx)
        randomize_mock.assert_not_awaited()
        move_mock.assert_not_awaited()
        # regression test: /make-teams use_roles:True used to never forward
        # that flag to printEmbed, so roles never actually showed up.
        embed_mock.assert_awaited_once()
        self.assertTrue(embed_mock.call_args.kwargs.get("useRoles"))

    async def test_use_roles_explains_when_a_team_is_not_five(self):
        # _setup_teams() gives each team 1 player, so roles can't apply —
        # continue normally (teams still get created and posted) but say
        # why no roles showed up.
        guild_id = 912
        await self._setup_teams(guild_id)
        ctx = self._ctx(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "both", AsyncMock()), \
             patch.object(self.bot.helperObj, "printEmbed", AsyncMock()) as embed_mock:
            await self._command("make-teams").callback(ctx, use_roles=True)

        embed_mock.assert_awaited_once()  # teams still get announced normally
        # explanation, then the trailing /start reminder
        self.assertEqual(ctx.channel.send.await_count, 2)
        explanation = ctx.channel.send.call_args_list[0].args[0]
        self.assertIn("Team 1 (1 players)", explanation)
        self.assertIn("Team 2 (1 players)", explanation)
        self.assertIn("/start", ctx.channel.send.call_args_list[1].args[0])

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
        ctx = self._ctx(guild_id=guild_id)

        with patch.object(self.bot.helperObj, "both", AsyncMock()), \
             patch.object(self.bot.helperObj, "printEmbed", AsyncMock()):
            await self._command("make-teams").callback(ctx, use_roles=True)

        # only the trailing /start reminder — no role explanation needed
        ctx.channel.send.assert_awaited_once()
        self.assertNotIn("Roles need", ctx.channel.send.call_args.args[0])
        self.assertIn("/start", ctx.channel.send.call_args.args[0])


class RankedCommandTests(BotModuleTestCase):
    async def test_delegates_to_ranked_team_helper(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "rankedTeamHelper", mock):
            await self._command("ranked").callback(ctx)
        mock.assert_awaited_once_with(ctx)


class CaptainsCommandTests(BotModuleTestCase):
    async def test_requires_at_least_two_in_voice_channel(self):
        ctx = self._ctx()
        ctx.user.voice = None

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
            await self._command("ranked-captains").callback(
                ctx, captain_1=cap1, captain_2=cap2, use_random=False
            )

        mock.assert_awaited_once_with(ctx, cap1, cap2, ranked=True)

    async def test_use_random_picks_two_distinct_captains_from_voice_channel(self):
        # Regression test: this path used to store a plain Python list as
        # the "players" column, which sqlite3 can't bind as a parameter
        # (InterfaceError) — it crashed before ever picking a captain.
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


class ReportCorrectWinnerCommandTests(BotModuleTestCase):
    async def test_delegates_with_resolved_team_value(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reportCorrectWinnerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("report-correct-winner").callback(ctx, choice)
        mock.assert_awaited_once_with(ctx, 2, None)

    async def test_delegates_with_match_id(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "reportCorrectWinnerHelper", mock):
            choice = app_commands.Choice(name="Team 2", value=2)
            await self._command("report-correct-winner").callback(ctx, choice, match_id=42)
        mock.assert_awaited_once_with(ctx, 2, 42)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
