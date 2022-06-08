import discord
from discord.ext import commands
import numpy as np

intents = discord.Intents.default()
intents.members = True

client = commands.Bot(command_prefix = '.', intents=intents)
token = 'token'

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('10 Man??'))
    print('Bot is online')

@client.command(aliases = ['james', 'hashmap'])
async def createRandom(ctx):
    channel = ctx.message.author.voice.channel
    members = []
    for i in channel.members:
        members.append(i.name)
    
    x = np.array(members)
    result = ""

    roles = {0 : "top", 1 : "jg", 2 : "mid", 3 : "adc", 4 : "sup"}
    np.random.shuffle(x)

    for i in range(10):
        if(i == 0):
            result += "TEAM 1:" + "\n"
        if(i == 5):
            result += "\n"+ "TEAM 2:" + "\n"
        result += str(x[i])
        result += " " + roles[i%5]
        result += "\n"

    team_embed = discord.Embed(title = "TEAMS", description = result, color = discord.Color.red())
    await ctx.send(embed = team_embed)

client.run(token)
