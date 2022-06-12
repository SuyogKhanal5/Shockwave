from queue import Empty
from random import randrange
import discord
from discord.ext import commands
import numpy as np

intents = discord.Intents.default()
intents.members = True

client = commands.Bot(command_prefix = '.', intents=intents, help_command=None)

token = ''
original_channel = ""
captainNum = 1
drafted = 2

with open('token.txt') as f:
    token = f.read()

team_size = 5
team1 = []
team2 = []

teamList1 = ""
teamList2 = ""
players = []
members = []

team1ids = []
team2ids = []

using_captains = False

channel1 = None
channel2 = None

captain1 = None
captain2 = None

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('Use .commands for a list of commands.'))
    print('Bot is online')

@client.command(aliases = ['size'])
async def setTeamSize(ctx, *, sizeChange):
    global team_size
    team_size = sizeChange

    await ctx.send("Set team size!")

@client.command(aliases = ['james', 'hashmap'])
async def fullRandom(ctx):
    channel = ctx.message.author.voice.channel
    global members
    for i in channel.members:
        members.append(i)
    
    m = np.array(members)

    result1 = ""
    result2 = ""

    roles = {0 : "Top - ", 1 : "Jungle - ", 2 : "Mid - ", 3 : "Bottom - ", 4 : "Support - "}
    np.random.shuffle(m)

    names = []
    
    for i in m:
        names.append(i.name)
    
    global ids
    ids = []
    
    for i in m:
        ids.append(i.id)

    x = np.array(m)

    for i in range(team_size*2):
        if(i < team_size):
            result1 += roles[i%5] + " " + str(x[i]) + "\n"
        else:
            result2 += roles[i%5] + " " + str(x[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

@client.command()
async def setTeamChannels(ctx, *, teams):
    teamsList = teams.split()

    global channel1
    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

@client.command()
async def move(ctx):
    global original_channel
    original_channel = ctx.message.author.voice.channel
    global channel1
    global channel2

    if not team1 or not team2:
        await ctx.send("Team is empty! Set teams before using this command.")
    elif channel1 == None or channel2 == None:
        await ctx.send("Channels not set! Set channels before using this command.")
    else:
        counter = 0
        if (using_captains):
            for i in team1ids:
                member = ctx.guild.get_member(i)
                await member.move_to(channel1)
            for i in team2ids:
                member =  ctx.guild.get_member(i)
                await member.move_to(channel2)
        else:
            for i in range(0,10):
                if counter <=4:
                    id = ids[i]
                    member = ctx.guild.get_member(id)
                    await member.move_to(channel1)
                else:
                    id = ids[i]
                    member = ctx.guild.get_member(id)
                    await member.move_to(channel2)
                counter += 1

@client.command()
async def help(ctx, *, specific):
    if (specific == "setTeamSize"):
        helpembed = discord.Embed(title = "setTeamSize", description = "Change the size of the team. \nWARNING: This command doesn't work on \".fullRandom\" or \".fullRandomAll\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "fullRandom"):
        helpembed = discord.Embed(title = "fullRandom", description = "Create random teams of random roles. Aliases are \"james\" and \"hashmap\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "fullRandomAll"):
        helpembed = discord.Embed(title = "fullRandomAll", description = "Create random teams of random roles, set team channels, and move members all in one command.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "move"):
        helpembed = discord.Embed(title = "move", description = "Move team members into set channels. Make sure channels are set already with .setTeamChannels.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "setTeamChannels"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Takes in two arguments seperated by space, two names of voice channels. \nWARNING: This command does not work with voice channels that have spaces in their name. Try hypenating voice channels that need spaces.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "random"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Randomize teams without assigning roles. Useful with setTeamSize for use in other games.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "randomAll"):
        helpembed = discord.Embed(title = "setTeamChannels", description = "Randomize teams without assigning roles and move into channels all in one command. Useful with setTeamSize for use in other games.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "returnAll"):
        helpembed = discord.Embed(title = "returnAll", description = "Return all players to their original channel.\n WARNING: \".move\", \"fullRandomAll\", or \"randomAll\" must be used before using this.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "captains"):
        helpembed = discord.Embed(title = "captains", description = "Start the process of creating new teams. Parameters are team captains.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "choose"):
        helpembed = discord.Embed(title = "choose", description = "Team captains can use this command to choose new team members in alternating order.\nWARNING: Use this command after using \".captains\" or \".randomCaptains\"", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "clearAll"):
        helpembed = discord.Embed(title = "clearAll", description = "Clear all stored teams.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    elif (specific == "notify"):
        helpembed = discord.Embed(title = "notify", description = "Sends a direct message to who you mention in order to invite them to the 10 man.", color = discord.Color.blue())
        await ctx.send(embed = helpembed)
    else:
        await ctx.send("Command not found!")

@client.command(aliases = ["commands"])
async def commandList(ctx):
    helpembed = discord.Embed(title = "Commands", description = "setTeamSize\nfullRandom\nfullRandomAll\nmove\nsetTeamChannels\nrandom\nrandomAll\nreturnAll\ncaptains\nchoose\nrandomCaptains", color = discord.Color.blue())
    await ctx.send(embed = helpembed)
    await ctx.send("Type \".help  ____\" in order to get info on a specific command.")

@client.command()
async def fullRandomAll(ctx, *, teams):
    teamsList = teams.split()

    global channel1
    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i)

    if (len(members) < 10):
        await ctx.send("You must have exactly 10 people in the call to use this command.")
    elif (len(members) > 10):
        await ctx.send("You must have exactly 10 people in the call to use this command.")
        return

    m = np.array(members)

    result1 = ""
    result2 = ""

    roles = {0 : "Top - ", 1 : "Jungle - ", 2 : "Mid - ", 3 : "Bot - ", 4 : "Support - "}
    np.random.shuffle(m)

    names = []
    
    for i in m:
        names.append(i.name)
    
    global ids
    ids = []
    
    for i in m:
        ids.append(i.id)

    x = np.array(m)

    for i in range(team_size*2):
        if(i < team_size):
            result1 += roles[i%5] + " " + str(x[i]) + "\n"
        else:
            result2 += roles[i%5] + " " + str(x[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

    counter = 0

    global original_channel
    original_channel = ctx.message.author.voice.channel
    
    for i in range(0,10):
        if counter <=4:
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel1)
        else:
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel2)
        counter += 1

@client.command()
async def random(ctx):
    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i)
    
    m = np.array(members)

    np.random.shuffle(m)

    names = []
    
    for i in m:
        names.append(i.name)
    
    global ids
    ids = []
    
    for i in m:
        ids.append(i.id)

    x = np.array(m)

    result1 = ""
    result2 = ""

    for i in range(0, len(ids)):
        if(i < (len(ids)/2)):
            result1 += str(x[i]) + "\n"
        else:
            result2 += str(x[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

@client.command()
async def randomAll(ctx, *, teams):
    teamsList = teams.split()

    global channel1
    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i)
    
    m = np.array(members)

    np.random.shuffle(m)

    names = []
    
    for i in m:
        names.append(i.name)
    
    global ids
    ids = []
    
    for i in m:
        ids.append(i.id)

    x = np.array(m)

    result1 = ""
    result2 = ""

    for i in range(0,len(ids)):
        if(i <= len(ids)/2):
            result1 += str(x[i]) + "\n"
        else:
            result2 += str(x[i]) + "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

    global original_channel
    original_channel = ctx.message.author.voice.channel

    counter = 0
    
    for i in range(0,len(ids)):
        if counter <=(len(ids)/2):
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel1)
        else:
            id = ids[i]
            member = ctx.guild.get_member(id)
            await member.move_to(channel2)
        counter += 1

@client.command()
async def returnAll(ctx):
    global original_channel
    if (original_channel == ""):
        await ctx.send("You have not been seperated into team voice channels! Use \".move\" first.")
    else:
        if (using_captains):
            for i in team1ids:
                member = ctx.guild.get_member(i)
                await member.move_to(original_channel)
            for i in team2ids:
                member =  ctx.guild.get_member(i)
                await member.move_to(original_channel)
        else:
            for i in range(0,10):
                id = ids[i]
                member = ctx.guild.get_member(id)
                await member.move_to(original_channel)

@client.command()
async def captains(ctx, captain_1: discord.Member, captain_2: discord.Member):
    global captain1
    global captain2
    global members
    global players
    global using_captains
    global original_channel
    original_channel = ctx.message.author.voice.channel
    using_captains = True

    if (captain_1 == None or captain_2 == None):
        await ctx.send("Mention two team captains!")
    else:
        captain1 = captain_1
        captain2 = captain_2

        global teamList1
        global teamList2
        
        teamList1 += str(captain1.display_name)
        teamList2 += str(captain2.display_name)

        team1ids.append(captain1.id)
        team2ids.append(captain2.id)

        team1.append(captain1)
        team2.append(captain2)

        team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
        team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

        await ctx.send(embed = team1_embed)
        await ctx.send(embed = team2_embed)
        
        channel = ctx.message.author.voice.channel
        playersString = ""
        for player in channel.members:
            if(player.display_name != captain1.display_name and player.display_name != captain2.display_name):
                players.append(player.display_name)
                playersString += player.display_name + "\n"
         
        players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
        await ctx.send(embed = players_embed)
        
        await ctx.send("Captains selected!")
        await ctx.send(captain_1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def captainsAll(ctx, captain_1: discord.Member, captain_2: discord.Member, *, teams):
    global captain1
    global captain2
    global members
    global players
    global using_captains
    global original_channel
    original_channel = ctx.message.author.voice.channel
    using_captains = True

    teamsList = teams.split()

    global channel1
    channel1 = discord.utils.get(ctx.guild.channels, name = teamsList[0])

    global channel2
    channel2 = discord.utils.get(ctx.guild.channels, name = teamsList[1])

    await ctx.send("Channels set!")

    if (captain_1 == None or captain_2 == None):
        await ctx.send("Mention two team captains!")
    else:
        captain1 = captain_1
        captain2 = captain_2

        global teamList1
        global teamList2
        
        teamList1 += str(captain1.display_name)
        teamList2 += str(captain2.display_name)

        team1ids.append(captain1.id)
        team2ids.append(captain2.id)

        team1.append(captain1)
        team2.append(captain2)

        team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
        team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

        await ctx.send(embed = team1_embed)
        await ctx.send(embed = team2_embed)
        
        channel = ctx.message.author.voice.channel
        playersString = ""
        for player in channel.members:
            if(player.display_name != captain1.display_name and player.display_name != captain2.display_name):
                players.append(player.display_name)
                playersString += player.display_name + "\n"
         
        players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
        await ctx.send(embed = players_embed)
        
        await ctx.send("Captains selected!")
        await ctx.send(captain_1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def choose(ctx, member: discord.Member):
    global teamList1
    global teamList2
    global captainNum
    global captain1
    global captain2
    global players
    switch = True

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):
            
            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):

            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True): 
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")


@client.command()
async def clearAll(ctx):
    global original_channel
    global captainNum
    global drafted
    global team_size
    global team1
    global team2
    global teamList1
    global teamList2
    global channel1
    global channel2
    global captain1
    global captain2
    
    original_channel = ""
    captainNum = 1
    drafted = 2
    team_size = 5
    team1 = []
    team2 = []
    teamList1 = None
    teamList2 = None
    channel1 = None
    channel2 = None
    captain1 = None
    captain2 = None

    await ctx.send("Cleared!")

@client.command()
async def clearTeams(ctx):
    global original_channel
    global captainNum
    global drafted
    global team_size
    global team1
    global team2
    global teamList1
    global teamList2
    global channel1
    global channel2
    global captain1
    global captain2
    
    original_channel = ""
    captainNum = 1
    drafted = 2
    team_size = 5
    team1 = []
    team2 = []
    teamList1 = None
    teamList2 = None
    captain1 = None
    captain2 = None

    await ctx.send("Cleared!")

@client.command(aliases = ['invite'])
async def notify(ctx, member: discord.Member):
    global team_size
    channel = await member.create_dm()
    invite_channel = ctx.message.author.voice.channel
    invite_link = await invite_channel.create_invite(max_uses=1,unique=True)
    content = ctx.message.author.name + " has invited you to a " + str(team_size * 2) + ' man!\n\n' + str(invite_link)
    await channel.send(content)
    await ctx.send('Sent an invite for the ' + str(team_size * 2) + ' man!')

@client.command()
async def randomCaptains(ctx):
    channel = ctx.message.author.voice.channel
    
    global members
    global captain1
    global captain2
    global members
    global players
    global using_captains
    global original_channel

    global teamList1
    global teamList2

    original_channel = ctx.message.author.voice.channel
    using_captains = True

    for i in channel.members:
        members.append(i)
    
    m = np.array(members)

    np.random.shuffle(m)

    captain1 = m[0]
    captain2 = m[1]

    teamList1 += str(captain1.display_name)
    teamList2 += str(captain2.display_name)

    team1ids.append(captain1.id)
    team2ids.append(captain2.id)

    team1.append(captain1)
    team2.append(captain2)

    team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)
        
    channel = ctx.message.author.voice.channel
    playersString = ""
    
    for player in channel.members:
        if (player.display_name != captain1.display_name and player.display_name != captain2.display_name):
            players.append(player.display_name)
            playersString += player.display_name + "\n"
         
    players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
    await ctx.send(embed = players_embed)
        
    await ctx.send("The captains are <@{}>".format(captain1.id) + " and <@{}>".format(captain2.id))
    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")

@client.command()
async def sravika(ctx):
    num = randrange(5) + 1
    s = "pp" + str(num) + ".jpg"
    await ctx.send("le pp", file = discord.File(s))

@client.command()
async def chooseRandom(ctx):
    global teamList1
    global teamList2
    global captainNum
    global captain1
    global captain2
    global players
    global player_members
    player_members = []
    switch = True

    channel = ctx.message.author.voice.channel

    if (len(players) == 0):
        await ctx.send("All players have been selected")

    for player in channel.members:
        if(player.display_name != captain1.display_name and player.display_name != captain2.display_name and players.__contains__(player.display_name) == True):
            player_members.append(player)
    
    m = np.array(player_members)

    np.random.shuffle(m)

    member = m[0]

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")

@client.command()
async def chooseGroup(ctx, *_available_players: discord.Member):
    global teamList1
    global teamList2
    global captainNum
    global captain1
    global captain2
    global players
    global player_members
    player_members = []
    switch = True


    items = _available_players
    
    available_players = []

    for i in items:
        available_players.append(i)

    print(available_players[0])

    channel = ctx.message.author.voice.channel

    if (len(players) == 0):
        await ctx.send("All players have been selected")
    
    m = np.array(available_players)

    np.random.shuffle(m)

    member = m[0]

    if drafted < (team_size * 2):
        if (captainNum == 1 and ctx.message.author.id == captain1.id):

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):    
                teamList1 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)

            if (team1.__contains__(member) == False and team2.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team1ids.append(member.id)
                team1.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)
            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
        elif (captainNum == 2 and ctx.message.author.id == captain2.id):
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                teamList2 += "\n" + member.display_name

            team1_embed = discord.Embed(title = "TEAM 1", description = teamList1, color = discord.Color.blue())
            team2_embed = discord.Embed(title = "TEAM 2", description = teamList2, color = discord.Color.red())

            await ctx.send(embed = team1_embed)
            await ctx.send(embed = team2_embed)
            
            if (team2.__contains__(member) == False and team1.__contains__(member) == False and players.__contains__(member.display_name) == True):
                players.remove(member.display_name)
                team2ids.append(member.id)
                team2.append(member)
                playersString = ""
                for player in players:
                    playersString += player + "\n"
            else:
                playersString = ""
                switch = False
                for player in players:
                    playersString += player + "\n"
                await ctx.send("Player has already been selected or does not exist in the player list.")

            players_embed = discord.Embed(title = "PLAYERS", description = playersString, color = discord.Color.dark_purple())
            await ctx.send(embed = players_embed)

            if(players == []):
                await ctx.send("You've drafted the maximum number of people for the team size! Use \".move\" to move everyone to the channels!")
            else:
                if (switch):
                    captainNum = 1
                    await ctx.send(captain1.mention + ", type \".choose  @_____\" to pick a player for your team")
                else:
                    captainNum = 2
                    await ctx.send(captain2.mention + ", type \".choose  @_____\" to pick a player for your team")
        else:
            if ((captainNum == 1 and ctx.message.author.id == captain2.id) or (captainNum == 2 and ctx.message.author.id == captain1.id)):
                await ctx.send("Not Your Turn!")
            elif (ctx.message.author.id != captain1.id and ctx.message.author.id != captain2.id):
                await ctx.send("Only team captains can use this command!")


client.run(token)
