import discord
from TourneyClasses import Team, Tournament, Match, Player
import random
import asyncio
import datetime
import numpy as np

BETTING_DURATION_SECONDS = 60
WINNER_REPORT_DELAY_SECONDS = 3
DAILY_GOLD_AMOUNT = 1000
TEAM_EMOJIS = {1: "1️⃣", 2: "2️⃣"}
WINNER_EMOJIS = {emoji: team for team, emoji in TEAM_EMOJIS.items()}

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

    async def captainsHelper(self, ctx, captain_1, captain_2):
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

        await self.clearTeamsHelper(ctx)

        captain1 = Player(captain_1.id, captain_1.name)
        captain2 = Player(captain_2.id, captain_2.name)

        self.update(ctx.guild.id, "captain1", captain1.serializePlayer())
        self.update(ctx.guild.id, "captain2", captain2.serializePlayer())
        self.update(ctx.guild.id, "mode", "Captains")

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

        await ctx.response.send_message("Captains selected!")
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
            "(guildId, userId, username, balance, wins, losses, gold_wagered, gold_won, gold_lost, last_daily) "
            "VALUES(?, ?, ?, 0, 0, 0, 0, 0, 0, NULL)",
            (guild_id, user_id, username)
        )
        self.cursor.execute(
            "UPDATE economy SET username=? WHERE guildId=? AND userId=?",
            (username, guild_id, user_id)
        )
        self.db.commit()

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

    async def balanceHelper(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        self.ensureEconomyRow(guild_id, user_id, ctx.user.name)

        self.cursor.execute(
            "SELECT balance, wins, losses, gold_wagered, gold_won, gold_lost FROM economy "
            "WHERE guildId=? AND userId=?",
            (guild_id, user_id)
        )
        balance, wins, losses, gold_wagered, gold_won, gold_lost = self.cursor.fetchone()

        net_gold = gold_won - gold_lost
        games_played = wins + losses
        win_rate = f"{(wins / games_played) * 100:.1f}%" if games_played > 0 else "N/A"

        embed = discord.Embed(
            title=f"{ctx.user.display_name}'s Wallet", color=discord.Color.gold()
        )
        embed.add_field(name="Balance", value=f"{balance} gold", inline=True)
        embed.add_field(name="Wins", value=str(wins), inline=True)
        embed.add_field(name="Losses", value=str(losses), inline=True)
        embed.add_field(name="Win Rate", value=win_rate, inline=True)
        embed.add_field(name="Gold Wagered", value=str(gold_wagered), inline=True)
        embed.add_field(name="Net Gold Won/Lost", value=f"{net_gold:+d} gold", inline=True)

        await ctx.response.send_message(embed=embed)

    # ---------------- Betting ----------------

    # True if `user_id` is a rostered player (either side) in the game
    # /start most recently moved into channels — used to stop players from
    # betting on their own game.
    def isPlayerInCurrentGame(self, guild_id, user_id):
        player_ids = set()
        for column in ("team1", "team2"):
            serialized = self.get(guild_id, column)
            if not serialized:
                continue
            team = Team()
            team.deserializeTeam(serialized)
            player_ids.update(p.get_id() for p in team.get_players())

        return user_id in player_ids

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

        if not allWagers:
            await channel.send(f"**Team {winning_team}** wins! No bets were placed on this game.")
        else:
            winningBets = [w for w in allWagers if w[2] == winning_team]
            losingBets = [w for w in allWagers if w[2] != winning_team]

            winningPool = sum(w[3] for w in winningBets)
            losingPool = sum(w[3] for w in losingBets)

            lines = [f"**Team {winning_team}** wins! Paying out bets..."]

            for user_id, username, _team, amount in losingBets:
                self.ensureEconomyRow(guild_id, user_id, username)
                self.cursor.execute(
                    "UPDATE economy SET losses = losses + 1, gold_wagered = gold_wagered + ?, "
                    "gold_lost = gold_lost + ? WHERE guildId=? AND userId=?",
                    (amount, amount, guild_id, user_id)
                )

            if not winningBets:
                lines.append("Nobody bet on the winning team — all bets were lost.")

            for user_id, username, _team, amount in winningBets:
                self.ensureEconomyRow(guild_id, user_id, username)

                if winningPool > 0:
                    payout = round(amount + (amount / winningPool) * losingPool)
                else:
                    payout = amount
                profit = payout - amount

                self.cursor.execute(
                    "UPDATE economy SET balance = balance + ?, wins = wins + 1, "
                    "gold_wagered = gold_wagered + ?, gold_won = gold_won + ? "
                    "WHERE guildId=? AND userId=?",
                    (payout, amount, profit, guild_id, user_id)
                )
                lines.append(f"{username} won {payout} gold (bet {amount})")

            self.db.commit()

            await channel.send("\n".join(lines))

        if guild is not None and await self.moveMembersToOriginalChannel(guild):
            await channel.send("Moved everyone back to the original channel!")

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