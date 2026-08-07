import discord


class Player:
    def __init__(self, id=None, name=None) -> None:
        self.id = id
        self.name = name

    def set_id(self, id: int) -> None:
        self.id = id

    def get_id(self) -> int:
        return self.id

    def get_name(self) -> str:
        return self.name

    def set_name(self, name: str) -> None:
        self.name = name

    # Convert from discord.Member to Player obj
    def convertFromMember(self, member: discord.Member) -> None:
        self.id = member.id
        self.name = member.name

    def serializePlayer(self) -> str:
        return '({},{})'.format(self.id, self.name)

    def deserializePlayer(self, serialized: str) -> None:
        # Expects the FULL "(id,name)" string, including the parens.
        serializedCut = serialized[0:-1]  # drop trailing ')'
        serializedArr = serializedCut.split(',')

        if serializedArr[0][0] == '(':
            serializedArr[0] = serializedArr[0][1:]

        self.id = int(serializedArr[0])
        self.name = serializedArr[1]


class Team:
    def __init__(self) -> None:
        self.id = None
        self.name = ''
        self.players = []
        self.size = 0
        self.voice_channel = ""
        self.captain = None
        self.wins = 0
        self.losses = 0

    def add_player(self, player: Player) -> None:
        self.players.append(player)
        # BUG FIX: this was commented out, which meant self.size stayed 0
        # forever. Since deserializeTeam used self.size to know how many
        # players to read back out of the serialized string, every team
        # deserialized with ZERO players regardless of how many were added.
        self.size += 1

    def remove_player(self, player: Player) -> None:
        self.players.remove(player)
        # BUG FIX: keep size in sync when removing too.
        self.size -= 1

    def set_name(self, name: str) -> None:
        self.name = name

    def addWin(self) -> None:
        self.wins += 1

    def addLoss(self) -> None:
        self.losses += 1

    def set_winner(self, winner: bool) -> None:
        self.winner = winner

    def set_voice_channel(self, voice_channel: discord.VoiceChannel) -> None:
        self.voice_channel = str(voice_channel)

    def set_captain(self, captain: Player) -> None:
        if captain not in self.players:
            raise ValueError('Captain must be a player on the team')

        self.captain = captain

    def set_id(self, id: int) -> None:
        self.id = id

    def get_id(self) -> int:
        return self.id

    def get_name(self) -> str:
        return self.name

    def get_players(self) -> list:
        return self.players

    def get_score(self) -> int:
        return self.score

    def get_winner(self) -> bool:
        return self.winner

    def get_voice_channel(self) -> discord.VoiceChannel:
        return self.voice_channel

    def get_captain(self):
        return self.captain

    def get_size(self) -> int:
        return self.size

    def serializeTeam(self) -> str:
        playerString = ''
        captain = ''

        for player in self.players:
            serialized = player.serializePlayer()
            playerString += str(len(serialized)) + serialized

        if self.captain is not None and isinstance(self.captain, Player):
            captain = self.captain.serializePlayer()

        return '[{}, {}, {}, {}, {}, {}, {}, {}]'.format(
            self.id, self.name, playerString, self.size,
            self.voice_channel, captain, self.wins, self.losses
        )

    def deserializeTeam(self, serialized: str) -> None:
        serializedCut = serialized[1:-1]
        serializedArr = serializedCut.split(', ')

        self.id = serializedArr[0]
        self.name = serializedArr[1]

        playerString = serializedArr[2]
        newPlayerList = []

        # BUG FIX: the old loop used `range(int(serializedArr[3]))` to decide
        # how many players to parse out — but serializedArr[3] is `self.size`,
        # which (before the add_player fix above) was always 0, and even
        # after the fix isn't guaranteed to equal len(self.players) in every
        # code path. Instead, walk the length-prefixed playerString until
        # it's fully consumed, which is self-describing and doesn't depend
        # on a separate counter being correct.
        i = 0
        while i < len(playerString):
            j = 0
            lengthStr = ''
            while playerString[i + j] != '(':
                lengthStr += playerString[i + j]
                j += 1

            length = int(lengthStr)
            playerData = playerString[i + j: i + j + length]

            player = Player()
            player.deserializePlayer(playerData)
            newPlayerList.append(player)

            i += j + length

        self.players = newPlayerList

        # BUG FIX: these all previously indexed into `serialized` (the raw
        # un-split original string) instead of `serializedArr` (the split
        # fields), so they were reading garbage characters by position
        # rather than the actual fields.
        self.size = int(serializedArr[3]) if serializedArr[3] not in ('', 'None') else len(newPlayerList)
        self.voice_channel = serializedArr[4]
        self.captain = serializedArr[5]
        self.wins = int(serializedArr[6]) if serializedArr[6] not in ('', 'None') else 0
        self.losses = int(serializedArr[7]) if serializedArr[7] not in ('', 'None') else 0


class Match:
    def __init__(self) -> None:
        self.team1 = None
        self.team2 = None
        self.finished = False
        self.winner = None


class Tournament():
    def __init__(self) -> None:
        pass