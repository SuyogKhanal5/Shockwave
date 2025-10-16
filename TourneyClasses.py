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
        serializedCut = serialized[1:-1]
        serializedArr = serializedCut.split(',')
        
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
        self.size += 1

    def remove_player(self, player: Player) -> None:
        self.players.remove(player)
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
    
    def get_captain(self) -> discord.Member:
        return self.captain
    
    def get_size(self) -> int:
        return self.size
    
    def serializeTeam(self) -> str:
        playerString = ''
        captain = ''
        
        for player in self.players:
            serialized = player.serializePlayer()
            
            playerString += str(len(serialized)) + serialized

        if self.captain is not None:
            captain = self.captain.serializePlayer()

        return '[{}, {}, {}, {}, {}, {}, {}, {}]'.format(self.id, self.name, playerString, self.size, self.voice_channel, captain, self.wins, self.losses)

    def deserializeTeam(self, serialized: str) -> None:
        serializedCut = serialized[1:-1]
        serializedArr = serializedCut.split(', ')
        
        self.id = serializedArr[0]
        self.name = serializedArr[1]

        # convert serialized players to Player objects

        newPlayerList = [] 

        currentPlayer = ""
        i = 0
        
        for playerIndex in range(int(serializedArr[3])):
            # iterate till (
            tupleLen = ''
            currentPlayer = ""
            j = 0

            while serializedArr[2][i+j] != '(':
                tupleLen += serializedArr[2][i+j]
                j += 1

            if tupleLen != '':
                if tupleLen[0] == ')':
                    tupleLen = tupleLen[1:]

                tupleLen = int(tupleLen)
                
                for k in range(i+2, i+tupleLen+2):
                    currentPlayer += serializedArr[2][k]

                player = Player()
                player.deserializePlayer(currentPlayer[1:])

                newPlayerList.append(player)
                i += tupleLen + j

        self.players = newPlayerList
        self.size = serialized[3]
        self.voice_channel = serialized[4]
        self.captain = serialized[5]
        self.wins = serialized[6]
        self.losses = serialized[7]

class Match:
    def __init__(self) -> None:
        self.team1 = None
        self.team2 = None
        self.finished = False
        self.winner = None

class Tournament():
    def __init__(self) -> None:
        pass