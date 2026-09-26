import asyncio
import os
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands
from pytubefix import YouTube, Search

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}


class PyTubeSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')

    @classmethod
    async def create_source(cls, query: str, loop=None):
        loop = loop or asyncio.get_event_loop()

        def extract():
            # Check if query is a URL or a search phrase
            if query.startswith(("http://", "https://")):
                yt = YouTube(query, client='ANDROID')
            else:
                s = Search(query)
                if not s.results:
                    raise Exception("No tracks found for the search query.")
                yt = s.results[0]
                # Force client on the selected video
                yt.client = 'ANDROID'

            # Fetch the best available audio stream
            audio_stream = yt.streams.filter(only_audio=True).order_by('abr').desc().first()
            if not audio_stream:
                audio_stream = yt.streams.get_audio_only()
                
            if not audio_stream:
                raise Exception("Could not find a valid audio stream.")

            stream_url = audio_stream.url
            data = {
                'title': yt.title,
                'url': yt.watch_url,
                'duration': yt.length,
                'thumbnail': yt.thumbnail_url,
            }
            return stream_url, data

        stream_url, data = await loop.run_in_executor(None, extract)
        return cls(discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS), data=data)


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.queue = []

    async def setup_hook(self):
        print("Registering slash commands...")
        await self.tree.sync()

        # Render port binding keep-alive
        app = web.Application()
        app.router.add_get('/', lambda r: web.Response(text="MusicNest bot is running."))
        app.router.add_get('/healthz', lambda r: web.Response(text="OK"))

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"📡 Web server listening on port {port} for Render.")


bot = MusicBot()


@bot.event
async def on_ready():
    print(f"📡 MusicNest Bot is online as {bot.user}!")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="MusicNest App"
        )
    )


# --- COMMANDS ---

@bot.tree.command(name="join", description="Make the MusicNest bot join your Voice Channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message(
            "❌ You must be connected to a Voice Channel to use this command!",
            ephemeral=True
        )

    channel = interaction.user.voice.channel
    if interaction.guild.voice_client is not None:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()

    await interaction.response.send_message(f"🔊 Successfully joined **{channel.name}**! Ready to stream.")


@bot.tree.command(name="play", description="Request and play a high-fidelity track via YouTube")
@app_commands.describe(query="Song title, artist, or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ You must be in a Voice Channel to request songs!")

    vc = interaction.guild.voice_client
    if not vc:
        channel = interaction.user.voice.channel
        vc = await channel.connect()

    try:
        player = await PyTubeSource.create_source(query, loop=bot.loop)
        bot.queue.append(player)

        if not vc.is_playing() and not vc.is_paused():
            await play_next(interaction, vc)
        else:
            embed = discord.Embed(
                title="📥 Song Added to Queue",
                description=f"**[{player.title}]({player.url})** has been added to the playlist.",
                color=discord.Color.from_rgb(186, 75, 62)
            )
            if player.thumbnail:
                embed.set_thumbnail(url=player.thumbnail)
            embed.add_field(name="Position in Queue", value=f"#{len(bot.queue)}", inline=True)
            if player.duration:
                m, s = divmod(player.duration, 60)
                embed.add_field(name="Duration", value=f"{m:02d}:{s:02d}", inline=True)
            await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"Error fetching audio with pytubefix: {e}")
        await interaction.followup.send("❌ Could not retrieve or play this song. Please try a different query.")


async def play_next(interaction: discord.Interaction, vc):
    if len(bot.queue) > 0:
        current_player = bot.queue.pop(0)

        def after_playing(error):
            if error:
                print(f"Player error: {error}")
            coro = play_next(interaction, vc)
            fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"Error calling next track: {e}")

        vc.play(current_player, after=after_playing)

        embed = discord.Embed(
            title="🎶 Now Playing on MusicNest",
            description=f"**[{current_player.title}]({current_player.url})**",
            color=discord.Color.from_rgb(186, 75, 62)
        )
        if current_player.thumbnail:
            embed.set_thumbnail(url=current_player.thumbnail)
        embed.set_footer(text="MusicNest Social VC Player • Stream Active")

        if current_player.duration:
            m, s = divmod(current_player.duration, 60)
            embed.add_field(name="Duration", value=f"{m:02d}:{s:02d}", inline=True)

        await interaction.channel.send(embed=embed)


@bot.tree.command(name="skip", description="Skip the current track")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is currently playing!", ephemeral=True)

    vc.stop()
    await interaction.response.send_message("⏭️ Skipped current track!")


@bot.tree.command(name="stop", description="Stop streaming and leave the Voice Channel")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ I'm not connected to any Voice Channel!", ephemeral=True)

    bot.queue.clear()
    vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("🛑 Stopped playback, cleared queue, and left Voice Channel.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ Error: DISCORD_BOT_TOKEN environment variable not set.")
    else:
        bot.run(token)
