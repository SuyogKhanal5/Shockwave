import discord
from discord.ext import commands
import numpy as np

intents = discord.Intents.default()
intents.members = True

client = commands.Bot(command_prefix = '.', intents=intents)
token = 'token'

team_size = 5

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('10 Man??'))
    print('Bot is online')

@client.command(aliases = ['size'])
async def setTeamSize(ctx, *, sizeChange):
    team_size = sizeChange

@client.command(aliases = ['james', 'hashmap'])
async def createRandom(ctx):
    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i.name)
    
    x = np.array(members)

    result1 = ""
    result2 = ""

    roles = {0 : "top", 1 : "jg", 2 : "mid", 3 : "adc", 4 : "sup"}
    np.random.shuffle(x)

    for i in range(team_size*2):
        if(i < team_size):
            result1 += str(x[i])
            result1 += " " + roles[i%5]
            result1 += "\n"
        else:
            result2 += str(x[i])
            result2 += " " + roles[i%5]
            result2 += "\n"

    team1_embed = discord.Embed(title = "TEAM 1", description = result1, color = discord.Color.blue())
    team2_embed = discord.Embed(title = "TEAM 2", description = result2, color = discord.Color.red())

    await ctx.send(embed = team1_embed)
    await ctx.send(embed = team2_embed)

client.run(token)
