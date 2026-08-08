import discord
from TourneyClasses import Team, Tournament, Match, Player
import random
import asyncio
import datetime
import json
import numpy as np

BETTING_DURATION_SECONDS = 60
WINNER_REPORT_DELAY_SECONDS = 3
DAILY_GOLD_AMOUNT = 1000
TEAM_EMOJIS = {1: "1️⃣", 2: "2️⃣"}
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
            await ctx.channel.send('Team Channels Not Set! Use "/set-team-channels" to set teams.')

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

    # Maps a raw elo number to a League-style "emoji tier division" label,
    # e.g. "\U0001f537 Platinum III" or "\U0001f7e3 Master" once divisions
    # stop applying. ELO_TIERS is sorted ascending, so the last threshold
    # at or below elo wins — e.g. exactly 1000 is Platinum, not Gold;
    # anything above the top tier's threshold is still Challenger.
    def eloRankLabel(self, elo):
        tier_index = 0
        for i, (threshold, _name, _emoji) in enumerate(ELO_TIERS):
            if elo >= threshold:
                tier_index = i
            else:
                break

        threshold, name, emoji = ELO_TIERS[tier_index]

        if tier_index >= ELO_DIVISIONED_TIER_COUNT:
            return f"{emoji} {name}"

        span = ELO_TIERS[tier_index + 1][0] - threshold
        offset = max(elo - threshold, 0)
        segment_size = span / len(ELO_DIVISIONS)
        division_index = min(int(offset // segment_size), len(ELO_DIVISIONS) - 1)

        return f"{emoji} {name} {ELO_DIVISIONS[division_index]}"

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
        # Every team-formation path (/make-teams, /captains, /ranked,
        # /ranked-captains) runs through here first — resetting is_ranked
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

    async def wagerHelper(self, ctx, amount: int, team: int):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        if amount <= 0:
            await ctx.response.send_message("Wager amount must be greater than 0.")
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

    # Kicks off the betting window for the game that was just /start'd.
    # Cancels/refunds any previous unresolved game first so a re-/start
    # never leaves an orphaned timer or stranded bets behind.
    async def startBettingHelper(self, ctx):
        guild_id = ctx.guild.id

        await self.cancelBettingHelper(guild_id, ctx.channel)

        self.update(guild_id, "betting_state", "OPEN")
        self.update(guild_id, "betting_message_id", None)
        self.update(guild_id, "betting_channel_id", ctx.channel.id)

        await ctx.channel.send(
            "🎲 Betting is now open! Use `/wager <amount> <team>` to bet on this game. "
            f"Betting closes in {BETTING_DURATION_SECONDS} seconds."
        )

        # BUG-PRONE PATTERN AVOIDED: awaiting asyncio.sleep() directly inside
        # this command handler would still (technically) let other
        # interactions run, since asyncio.sleep() yields control. But it
        # would keep this command's own Interaction/task alive and blocked
        # for a full minute, and a cancelled game (/return) would have no
        # way to stop it from firing later. Running it as its own Task makes
        # both of those explicit and lets cancelBettingHelper cancel it.
        task = asyncio.create_task(self._bettingTimer(guild_id, ctx.channel))
        self.bettingTasks[guild_id] = task

    async def _bettingTimer(self, guild_id, channel):
        try:
            await asyncio.sleep(BETTING_DURATION_SECONDS)

            self.update(guild_id, "betting_state", "CLOSED")
            await channel.send("🔒 Betting is now closed! No more wagers will be accepted for this game.")

            await asyncio.sleep(WINNER_REPORT_DELAY_SECONDS)

            msg = await channel.send(
                "Which team won? React with 1️⃣ for Team 1 or 2️⃣ for Team 2 to record the result "
                "and pay out bets."
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
    # 1️⃣/2️⃣ reaction on the stored betting message and pays out bets.
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
        # async below, so a second reaction (e.g. both 1️⃣ and 2️⃣ clicked
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
    #           gold_won, gold_lost, game_wins, game_losses, elo} — all
    #           values are deltas to ADD to that user's economy row.
    #   summary: display-only info for formatResultMessage().
    def computeGameDeltas(self, wagers, team1_roster, team2_roster, elo_lookup, winning_team, is_ranked=False):
        deltas = {}

        def bump(user_id, username, **kwargs):
            entry = deltas.setdefault(user_id, {
                "username": username, "balance": 0, "wins": 0, "losses": 0,
                "gold_wagered": 0, "gold_won": 0, "gold_lost": 0,
                "game_wins": 0, "game_losses": 0, "elo": 0,
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
        # games started via /ranked or /ranked-captains (is_ranked=True),
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
                )
            for user_id, username in team2_roster:
                bump(
                    user_id, username, elo=elo_delta2,
                    game_wins=1 if winning_team == 2 else 0,
                    game_losses=0 if winning_team == 2 else 1,
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
                "game_wins = game_wins + ?, game_losses = game_losses + ?, elo = elo + ? "
                "WHERE guildId=? AND userId=?",
                (
                    sign * d["balance"], sign * d["wins"], sign * d["losses"],
                    sign * d["gold_wagered"], sign * d["gold_won"], sign * d["gold_lost"],
                    sign * d["game_wins"], sign * d["game_losses"], sign * d["elo"],
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
    async def reportCorrectWinnerHelper(self, ctx, correct_team):
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
            "game_wins, game_losses, elo FROM economy WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        balance, bet_wins, bet_losses, gold_wagered, gold_won, gold_lost, game_wins, game_losses, elo = \
            self.cursor.fetchone()

        net_gold = gold_won - gold_lost

        bet_games = bet_wins + bet_losses
        bet_win_rate = f"{(bet_wins / bet_games) * 100:.1f}%" if bet_games > 0 else "N/A"

        games_played = game_wins + game_losses
        game_win_rate = f"{(game_wins / games_played) * 100:.1f}%" if games_played > 0 else "N/A"

        elo_rank = self.eloRankLabel(elo)

        embed = discord.Embed(
            title=f"{target.display_name}'s Stats", color=discord.Color.gold()
        )
        embed.add_field(name="Elo", value=f"{elo} ({elo_rank})", inline=True)
        embed.add_field(name="Game Record", value=f"{game_wins}W - {game_losses}L", inline=True)
        embed.add_field(name="Game Win Rate", value=game_win_rate, inline=True)
        embed.add_field(name="Balance", value=f"{balance} gold", inline=True)
        embed.add_field(name="Bet Record", value=f"{bet_wins}W - {bet_losses}L", inline=True)
        embed.add_field(name="Bet Win Rate", value=bet_win_rate, inline=True)
        embed.add_field(name="Net Gold Won/Lost", value=f"{net_gold:+d} gold", inline=True)
        embed.add_field(name="Gold Wagered", value=str(gold_wagered), inline=True)

        await ctx.response.send_message(embed=embed)

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