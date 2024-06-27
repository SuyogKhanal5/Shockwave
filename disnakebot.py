# Import Statements
import random
import numpy as np
import os.path as path
import sqlite3
import disnake

# Get token from text file

token = ""

with open("token.txt") as f:
    token = f.readline()

# Connect to Database

dataFolder = "data/guildData/serverInfo/"  # CHANGE TO / IF ON WINDOWS MACHINE!!!!!!!!!!
dbpath = dataFolder + "main.db"

exist = path.isfile(dbpath)

mainDB = sqlite3.connect(dbpath)
cursor = mainDB.cursor()

if not exist:
    cursor.execute("CREATE TABLE servers(guildId, serverName, original_channel, result1, result2, playerString, team1, team2, players, channel1, channel2, captain1, captain2, using_captains, captainNum, drafted, team_size)")

# Hash Map

roles = {0: "Top - ", 1: "Jungle - ", 2: "Mid - ", 3: "Bottom - ", 4: "Support - "}

# Set Intents

intents = disnake.Intents.default()
intents.typing = False
intents.presences = False
intents.guilds = True

class Shockwave(disnake.Client):
    # Events

    async def on_ready(self):
        print('Command: Shockwave')

    async def on_guild_join(guild):
        cursor.execute("INSERT INTO servers VALUES(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)", (guild.id, guild.name))
        mainDB.commit()

    async def on_guild_remove(guild):
        cursor.execute("""DELETE FROM servers WHERE guildId=?""", (guild.id,))
        mainDB.commit()

client = Shockwave()
client.run(token)