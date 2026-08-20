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
        # Target roster size for a persistent team (set via /team-create) -
        # distinct from `size`, which just mirrors len(players). Ephemeral
        # game-formation teams (/make-teams, /captains, /ranked, ...) never
        # set this and leave it None.
        self.team_size = None
        # Local file path to this team's uploaded logo image, or None if it
        # hasn't set one. Just a path (e.g. into a saved-uploads folder) -
        # not the image data itself.
        self.logo_path = None

    def add_player(self, player: Player) -> None:
        self.players.append(player)
        # Kept in sync manually - deserializeTeam uses self.size (not
        # len(self.players)) to know how many players to read back out of
        # the serialized string.
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

    def get_captain(self):
        return self.captain

    def get_size(self) -> int:
        return self.size

    def get_team_size(self) -> int:
        return self.team_size

    def set_team_size(self, team_size: int) -> None:
        self.team_size = team_size

    def get_logo_path(self) -> str:
        return self.logo_path

    def set_logo_path(self, logo_path: str) -> None:
        self.logo_path = logo_path

    def serializeTeam(self) -> str:
        playerString = ''
        captain = ''

        for player in self.players:
            serialized = player.serializePlayer()
            playerString += str(len(serialized)) + serialized

        if self.captain is not None and isinstance(self.captain, Player):
            captain = self.captain.serializePlayer()

        return '[{}, {}, {}, {}, {}, {}, {}, {}, {}, {}]'.format(
            self.id, self.name, playerString, self.size,
            self.voice_channel, captain, self.wins, self.losses, self.team_size,
            self.logo_path
        )

    def deserializeTeam(self, serialized: str) -> None:
        serializedCut = serialized[1:-1]
        serializedArr = serializedCut.split(', ')

        # Cast to int so get_id() doesn't return a string after a roundtrip
        # through the database.
        self.id = int(serializedArr[0]) if serializedArr[0] not in ('', 'None') else None
        self.name = serializedArr[1]

        playerString = serializedArr[2]
        newPlayerList = []

        # Walks the length-prefixed playerString until it's fully consumed
        # rather than trusting serializedArr[3] (self.size) as a count,
        # since self.size isn't guaranteed to equal len(self.players) in
        # every code path.
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

        self.size = int(serializedArr[3]) if serializedArr[3] not in ('', 'None') else len(newPlayerList)
        self.voice_channel = serializedArr[4]

        # Parsed back into a Player (not left as the raw "(id,name)" string)
        # so get_captain() returns something with a usable .get_id() after a
        # roundtrip through the database.
        captainStr = serializedArr[5]
        if captainStr:
            captain = Player()
            captain.deserializePlayer(captainStr)
            self.captain = captain
        else:
            self.captain = None

        self.wins = int(serializedArr[6]) if serializedArr[6] not in ('', 'None') else 0
        self.losses = int(serializedArr[7]) if serializedArr[7] not in ('', 'None') else 0

        # team_size was added after this format was already in use - older
        # serialized teams won't have a 9th field, so index defensively
        # instead of assuming it's there.
        if len(serializedArr) > 8 and serializedArr[8] not in ('', 'None'):
            self.team_size = int(serializedArr[8])
        else:
            self.team_size = None

        # Same defensive treatment for logo_path, added as a 10th field
        # after team_size - older serialized teams have neither.
        if len(serializedArr) > 9 and serializedArr[9] not in ('', 'None'):
            self.logo_path = serializedArr[9]
        else:
            self.logo_path = None


class Match:
    def __init__(self) -> None:
        self.team1 = None
        self.team2 = None
        self.finished = False
        self.winner = None


class Tournament:
    def __init__(self, name=None, team_size=None, num_teams=None, double_elimination=False) -> None:
        self.name = name
        self.team_size = team_size
        # Also doubles as the bracket size - how many team slots the
        # bracket is built for.
        self.num_teams = num_teams
        self.double_elimination = double_elimination
        self.teams = []    # Team objects registered for this tournament
        self.bracket = []  # rounds of matches, populated once the bracket is seeded
        # Losers bracket - only populated for a double-elimination
        # tournament (see helpers.buildLosersBracket). `losers_bracket_nodes`
        # is every node (leaves and results) in one flat list, the same
        # shape `bracket` uses; `losers_bracket_rounds` groups the RESULT
        # nodes by round (round sizes don't follow a simple halving pattern
        # the way the winners bracket's do, so unlike `bracket` this can't
        # be re-derived from the flat list alone - it's stored explicitly).
        self.losers_bracket_nodes = []
        self.losers_bracket_rounds = []
        # Which winners-bracket round_index each losers_bracket_rounds[i]
        # needs fully RESOLVED before it can start - None means it only
        # depends on the previous losers round finishing, not on any new
        # winners-bracket input. See helpers.buildLosersBracket, which is
        # the only thing that ever populates this.
        self.losers_bracket_wb_dependency = []
        # "after_winners" (default - the original behavior): the losers
        # bracket doesn't start at all until the winners bracket has
        # crowned its champion. "interleaved": each losers round starts
        # the moment the winners round it depends on resolves, without
        # waiting for the rest of the winners bracket to finish. See
        # helpers._startLosersBracketInterleaving.
        self.losers_bracket_timing = "after_winners"

    def get_name(self) -> str:
        return self.name

    def set_name(self, name: str) -> None:
        self.name = name

    def get_team_size(self) -> int:
        return self.team_size

    def set_team_size(self, team_size: int) -> None:
        self.team_size = team_size

    def get_num_teams(self) -> int:
        return self.num_teams

    def set_num_teams(self, num_teams: int) -> None:
        self.num_teams = num_teams

    def is_double_elimination(self) -> bool:
        return self.double_elimination

    def set_double_elimination(self, double_elimination: bool) -> None:
        self.double_elimination = double_elimination

    def get_teams(self) -> list:
        return self.teams

    def get_bracket(self) -> list:
        return self.bracket

    def set_bracket(self, bracket: list) -> None:
        self.bracket = bracket

    def get_losers_bracket_nodes(self) -> list:
        return self.losers_bracket_nodes

    def get_losers_rounds(self) -> list:
        return self.losers_bracket_rounds

    def get_losers_bracket_wb_dependency(self) -> list:
        return self.losers_bracket_wb_dependency

    def set_losers_bracket(self, nodes: list, rounds: list, wb_dependency: list = None) -> None:
        self.losers_bracket_nodes = nodes
        self.losers_bracket_rounds = rounds
        self.losers_bracket_wb_dependency = wb_dependency if wb_dependency is not None else []

    def get_losers_bracket_timing(self) -> str:
        return self.losers_bracket_timing

    def set_losers_bracket_timing(self, timing: str) -> None:
        self.losers_bracket_timing = timing

    # Adds `team` to this tournament's registered teams. A player can sit
    # on multiple teams in the server's teams table, but not on two
    # different teams entered into the SAME tournament - raises if `team`
    # shares a player with a team already registered here.
    def register_team(self, team: Team) -> None:
        registered_ids = {
            player.get_id() for existing in self.teams for player in existing.get_players()
        }
        for player in team.get_players():
            if player.get_id() in registered_ids:
                raise ValueError(
                    f"{player.get_name()} is already on a team registered for this tournament"
                )
        self.teams.append(team)


# One slot in a bracket. Each pair of sibling nodes (linked to each other
# via `opponent`) shares a single `next` node - the empty slot ahead of
# them that the winner of their match replaces, same as a real bracket
# printout. `previous` is one of the two nodes that fed into this one
# (the other is reachable via `previous.opponent`) - None for round-one
# nodes, since they start with an actual team rather than a TBD winner.
# `next` is None only for the finals node, since there's no round after it.
#
# `loser`/`drop_to` are only ever set on a WINNERS-bracket result node (one
# with `.previous` set) in a double-elimination tournament: `loser` is the
# team that lost that match (set alongside `.team`, the winner, once the
# match resolves), and `drop_to` is the losers-bracket leaf that loser
# drops into. A bye-produced result (only one side ever had a team) never
# gets a real `.loser` - there was no match, so nobody to drop down.
class BracketNode:
    def __init__(self, team=None) -> None:
        self.team = team
        self.opponent = None
        self.next = None
        self.previous = None
        self.loser = None
        self.drop_to = None


# Bracket persistence: a flat list of BracketNode is a graph (nodes point
# at each other), not something json.dumps can handle directly, so these
# convert to/from a list of plain dicts referencing each other by index
# into that same list. `drop_to` points into a DIFFERENT node list (the
# losers bracket, for a winners-bracket node) - pass it as `drop_targets`
# so its index is resolved against that list instead of `nodes` itself;
# omit it when serializing a bracket whose nodes never set `drop_to` (the
# losers bracket's own nodes never do).
def serialize_bracket(nodes: list, drop_targets: list = None) -> list:
    index_of = {id(node): i for i, node in enumerate(nodes)}
    drop_index_of = {id(node): i for i, node in enumerate(drop_targets)} if drop_targets else {}

    def index_or_none(node):
        return index_of[id(node)] if node is not None else None

    return [
        {
            "team": node.team.serializeTeam() if node.team is not None else None,
            "loser": node.loser.serializeTeam() if node.loser is not None else None,
            "opponent": index_or_none(node.opponent),
            "next": index_or_none(node.next),
            "previous": index_or_none(node.previous),
            "drop_to": drop_index_of.get(id(node.drop_to)) if node.drop_to is not None else None,
        }
        for node in nodes
    ]


def deserialize_bracket(data: list, drop_targets: list = None) -> list:
    nodes = []
    for entry in data:
        node = BracketNode()
        if entry["team"] is not None:
            team = Team()
            team.deserializeTeam(entry["team"])
            node.team = team
        if entry.get("loser") is not None:
            loser = Team()
            loser.deserializeTeam(entry["loser"])
            node.loser = loser
        nodes.append(node)

    for node, entry in zip(nodes, data):
        node.opponent = nodes[entry["opponent"]] if entry["opponent"] is not None else None
        node.next = nodes[entry["next"]] if entry["next"] is not None else None
        node.previous = nodes[entry["previous"]] if entry["previous"] is not None else None
        drop_to_index = entry.get("drop_to")
        if drop_targets is not None and drop_to_index is not None:
            node.drop_to = drop_targets[drop_to_index]

    return nodes


# Losers-bracket round grouping persistence - `rounds` is a list of rounds,
# each a list of RESULT nodes from `nodes` (the flat, already-serializable
# list from serialize_bracket/deserialize_bracket above). Round sizes in
# the losers bracket don't follow the winners bracket's simple halving
# pattern, so this grouping can't be recomputed from the graph alone the
# way _bracketRounds does for the winners bracket - it's stored explicitly
# as index lists into `nodes`.
def serialize_losers_rounds(rounds: list, nodes: list) -> list:
    index_of = {id(node): i for i, node in enumerate(nodes)}
    return [[index_of[id(node)] for node in round_nodes] for round_nodes in rounds]


def deserialize_losers_rounds(data: list, nodes: list) -> list:
    return [[nodes[i] for i in round_indices] for round_indices in data]