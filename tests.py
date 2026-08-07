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
import itertools
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, mock_open, patch

import discord
from discord import app_commands

from TourneyClasses import Player, Team
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
    "betting_state, betting_message_id, betting_channel_id)"
)
ECONOMY_SCHEMA = (
    "CREATE TABLE economy(guildId, userId, username, balance, wins, losses, "
    "gold_wagered, gold_won, gold_lost, last_daily, PRIMARY KEY(guildId, userId))"
)
WAGERS_SCHEMA = (
    "CREATE TABLE wagers(guildId, userId, username, team, amount, "
    "PRIMARY KEY(guildId, userId))"
)


def make_db():
    db = sqlite3.connect(":memory:")
    cursor = db.cursor()
    cursor.execute(SERVERS_SCHEMA)
    cursor.execute(ECONOMY_SCHEMA)
    cursor.execute(WAGERS_SCHEMA)
    db.commit()
    return db, cursor


def insert_guild_row(cursor, db, guild_id=GUILD_ID, name="Test Guild"):
    cursor.execute(
        "INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'NONE', NULL, NULL)",
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
    def __init__(self, name, id=None, bot=False):
        self.id = id if id is not None else next_id()
        self.name = name
        self.global_name = name
        self.display_name = name
        self.bot = bot
        self.voice = None
        self.mention = f"<@{self.id}>"
        self.move_to = AsyncMock()
        self.create_dm = AsyncMock(return_value=FakeDMChannel())


class FakeMessage:
    def __init__(self, id=None):
        self.id = id if id is not None else next_id()
        self.add_reaction = AsyncMock()


class FakeChannel:
    def __init__(self, name, id=None, members=None):
        self.name = name
        self.id = id if id is not None else next_id()
        self.members = members if members is not None else []
        self.send = AsyncMock()
        self.create_invite = AsyncMock(return_value="https://discord.gg/fake-invite")

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, id=GUILD_ID, name="Test Guild", channels=None, members=None):
        self.id = id
        self.name = name
        self.channels = channels if channels is not None else []
        self.members = members if members is not None else []

    async def create_voice_channel(self, name):
        channel = FakeChannel(name)
        self.channels.append(channel)
        return channel


class FakeInteraction:
    def __init__(self, guild, user, channel=None):
        self.guild = guild
        self.user = user
        self.channel = channel if channel is not None else FakeChannel("text-channel")
        self.response = AsyncMock()
        self.followup = AsyncMock()


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
    def __init__(self, guild_id, message_id, channel_id, emoji, member=None):
        self.guild_id = guild_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.emoji = emoji
        self.member = member


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
# Economy: /daily, /balance, ensureEconomyRow
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

    async def test_balance_embed_reports_stats_net_win_overall(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 901, "Alice")
        self.cursor.execute(
            "UPDATE economy SET balance=1234, wins=2, losses=1, gold_wagered=500, "
            "gold_won=300, gold_lost=80 WHERE guildId=? AND userId=?",
            (GUILD_ID, 901),
        )
        self.db.commit()

        ctx = self._ctx()
        await self.helperObj.balanceHelper(ctx)

        ctx.response.send_message.assert_awaited_once()
        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Balance"], "1234 gold")
        self.assertEqual(values["Wins"], "2")
        self.assertEqual(values["Losses"], "1")
        self.assertEqual(values["Gold Wagered"], "500")
        # net = gold_won - gold_lost = 300 - 80 = 220, shown with an
        # explicit "+" since the player is up overall
        self.assertEqual(values["Net Gold Won/Lost"], "+220 gold")
        # win rate = 2 / (2 + 1) = 66.7%
        self.assertEqual(values["Win Rate"], "66.7%")

    async def test_balance_embed_shows_negative_net_when_overall_down(self):
        self.helperObj.ensureEconomyRow(GUILD_ID, 902, "Bob")
        self.cursor.execute(
            "UPDATE economy SET wins=1, losses=3, gold_won=50, gold_lost=200 "
            "WHERE guildId=? AND userId=?",
            (GUILD_ID, 902),
        )
        self.db.commit()

        ctx = self._ctx(user_id=902, name="Bob")
        await self.helperObj.balanceHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Net Gold Won/Lost"], "-150 gold")
        self.assertEqual(values["Win Rate"], "25.0%")

    async def test_balance_embed_win_rate_na_with_no_games_played(self):
        ctx = self._ctx()
        await self.helperObj.balanceHelper(ctx)

        embed = ctx.response.send_message.call_args.kwargs["embed"]
        values = {f.name: f.value for f in embed.fields}
        self.assertEqual(values["Win Rate"], "N/A")
        self.assertEqual(values["Net Gold Won/Lost"], "+0 gold")


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
            "set-team-size", "set-team-channels", "start", "wager", "daily",
            "balance", "help", "make-teams", "return", "captains", "choose",
            "clear", "notify", "notify-role", "roll", "randomize-roles",
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
    async def test_ignores_bot_reactions(self):
        payload = SimpleNamespace(member=FakeMember("BotUser", bot=True), guild_id=1)
        with patch.object(self.bot.helperObj, "handleWinnerReaction", AsyncMock()) as mock:
            await self.bot.on_raw_reaction_add(payload)
        mock.assert_not_awaited()

    async def test_ignores_reactions_with_no_member(self):
        payload = SimpleNamespace(member=None, guild_id=1)
        with patch.object(self.bot.helperObj, "handleWinnerReaction", AsyncMock()) as mock:
            await self.bot.on_raw_reaction_add(payload)
        mock.assert_not_awaited()

    async def test_ignores_dm_reactions(self):
        payload = SimpleNamespace(member=FakeMember("User"), guild_id=None)
        with patch.object(self.bot.helperObj, "handleWinnerReaction", AsyncMock()) as mock:
            await self.bot.on_raw_reaction_add(payload)
        mock.assert_not_awaited()

    async def test_delegates_valid_guild_member_reaction(self):
        payload = SimpleNamespace(member=FakeMember("User"), guild_id=1)
        with patch.object(self.bot.helperObj, "handleWinnerReaction", AsyncMock()) as mock:
            await self.bot.on_raw_reaction_add(payload)
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

    async def test_daily_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "dailyHelper", mock):
            await self._command("daily").callback(ctx)
        mock.assert_awaited_once_with(ctx)

    async def test_balance_delegates(self):
        ctx = self._ctx()
        mock = AsyncMock()
        with patch.object(self.bot.helperObj, "balanceHelper", mock):
            await self._command("balance").callback(ctx)
        mock.assert_awaited_once_with(ctx)

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
            await self._command("set-team-channels").callback(ctx, team1="Red", team2="Blue")
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

        await self._command("set-team-size").callback(ctx, sizechange=4)

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

        mock.assert_awaited_once_with(ctx, cap1, cap2)

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

        async def fake_captains_helper(c, captain1, captain2):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
