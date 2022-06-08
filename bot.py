import discord
from discord.ext import commands
import numpy as np

client = commands.Bot(command_prefix = '.')
token = 'token'

@client.event
async def on_ready():
    await client.change_presence(activity = discord.Game('10 Man??'))
    print('Bot is online')

@client.command(aliases = ['james', 'hashmap'])
async def createRandom(ctx):
    x = np.array(["Smit", "Patrick", "Amaan", "Suyog", "Pranav", "Pengu", "David", "James", "Keith", "Ahmed"])
    
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