from queue import Empty
import discord
from discord.ext import commands
import numpy as np

intents = discord.Intents.default()
intents.members = True

client = commands.Bot(command_prefix = '.', intents=intents)
token = 'token'

team_size = 5
team1 = []
team2 = []

channel1 = None
channel2 = None

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('10 Man??'))
    print('Bot is online')

@client.command(aliases = ['size'])
async def setTeamSize(ctx, *, sizeChange):
    global team_size
    team_size = sizeChange

    await ctx.send("Set team size!")

@client.command(aliases = ['james', 'hashmap'])
async def createRandom(ctx):
    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i)
    
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
            result1 += roles[i%5] + " "
            result1 += str(x[i])
            result1 += "\n"
        else:
            result2 += roles[i%5] + " "
            result2 += str(x[i])
            result2 += "\n"

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
    if not team1 or not team2:
        await ctx.send("Team is empty! Set teams before using this command.")
    elif channel1 == None or channel2 == None:
        await ctx.send("Channels not set! Set channels before using this command")
    else:
        counter = 0
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
            result1 += roles[i%5] + " "
            result1 += str(x[i])
            result1 += "\n"
        else:
            result2 += roles[i%5] + " "
            result2 += str(x[i])
            result2 += "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

    counter = 0
    
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

client.run(token)
