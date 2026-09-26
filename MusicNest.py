import asyncio
import os
import shutil
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

# Determine the available FFmpeg binary path
def get_ffmpeg_executable():
    # 1. Local bin folder (if downloaded during Render build)
    local_path = os.path.join(os.getcwd(), "bin", "ffmpeg")
    if os.path.isfile(local_path) and os.access(local_path, os.X_OK):
        return local_path
    
    # 2. System PATH
    system_path = shutil.which("ffmpeg")
    if system_path:
        return system_path
    
    return "ffmpeg"

FFMPEG_EXECUTABLE = get_ffmpeg_executable()

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# Get the bot's public Render URL dynamically to help the user configure their app
PUBLIC_BOT_URL = os.getenv("RENDER_EXTERNAL_URL", "https://<your-render-app-name>.onrender.com").rstrip("/")

# In-memory storage for syncing requests and active sessions across multiple users
pending_requests = {}  # user_id -> list of [{"id": req_id, "query": query}]
active_sessions = {}   # user_id -> {"vc": VoiceClient, "channel": TextChannel}


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        print("Registering slash commands...")
        await self.tree.sync()

        # Render Web Server & Playback Sync Endpoints
        app = web.Application()
        app.router.add_get('/', self.handle_index)
        app.router.add_get('/healthz', self.handle_healthz)
        app.router.add_get('/api/requests', self.handle_get_requests)
        app.router.add_post('/api/resolve', self.handle_post_resolve)

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"📡 Web server listening on port {port} for App Sync & Render Keep-Alive.")

    async def handle_index(self, request):
        return web.Response(text="MusicNest bot is online.")

    async def handle_healthz(self, request):
        return web.Response(text="OK")

    # API: Android App polls this to fetch new song requests made on Discord
    async def handle_get_requests(self, request):
        user_id = request.query.get("userId")
        if not user_id:
            return web.json_response([])
        
        user_reqs = pending_requests.pop(str(user_id), [])
        return web.json_response(user_reqs)

    # API: Android App posts the resolved InnerTune audio stream URL here
    async def handle_post_resolve(self, request):
        data = await request.json()
        user_id = str(data.get("userId"))
        stream_url = data.get("stream_url")
        title = data.get("title", "Unknown Track")
        url = data.get("url", "")
        duration = data.get("duration", 0)
        thumbnail = data.get("thumbnail")

        session = active_sessions.get(user_id)
        if not session:
            return web.json_response({"status": "error", "message": "No active session for this user ID"})

        vc = session["vc"]
        channel = session["channel"]

        if not vc or not vc.is_connected():
            return web.json_response({"status": "error", "message": "Bot is no longer in a Voice Channel"})

        def after_playing(error):
            if error:
                print(f"❌ Player error: {error}")

        try:
            # Stop any playing audio before playing the new track
            if vc.is_playing() or vc.is_paused():
                vc.stop()

            audio = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTIONS)
            vc.play(audio, after=after_playing)

            # Post beautiful "Now Playing on MusicNest" embed to Discord Voice Text Channel
            embed = discord.Embed(
                title="🎶 Now Playing on MusicNest",
                description=f"**[{title}]({url})**",
                color=discord.Color.from_rgb(186, 75, 62)
            )
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            embed.set_footer(text="MusicNest Active Bypass Stream")

            if duration:
                m, s = divmod(duration, 60)
                embed.add_field(name="Duration", value=f"{m:02d}:{s:02d}", inline=True)

            self.loop.create_task(channel.send(embed=embed))
            return web.json_response({"status": "success"})

        except Exception as e:
            print(f"❌ Failed to play resolved stream: {e}")
            self.loop.create_task(channel.send(f"❌ Failed to stream play resolved track: `{e}`"))
            return web.json_response({"status": "error", "message": str(e)})


bot = MusicBot()


@bot.event
async def on_ready():
    print(f"📡 MusicNest Bot is online as {bot.user}!")
    print(f"🔧 Using FFmpeg executable: {FFMPEG_EXECUTABLE}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="MusicNest App"
        )
    )


# --- DISCORD COMMANDS ---

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


@bot.tree.command(name="play", description="Request a song on Discord and bypass it through your MusicNest App")
@app_commands.describe(query="Song title, artist, or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ You must be in a Voice Channel to stream music!")

    vc = interaction.guild.voice_client
    if not vc:
        channel = interaction.user.voice.channel
        vc = await channel.connect()

    user_id = str(interaction.user.id)

    # 1. Save active voice connection session for asynchronous callback
    active_sessions[user_id] = {
        "vc": vc,
        "channel": interaction.channel,
        "userId": interaction.user.id
    }

    # 2. Register request in pending requests for this specific user
    req_id = f"req_{int(asyncio.get_event_loop().time() * 1000)}"
    if user_id not in pending_requests:
        pending_requests[user_id] = []
    
    pending_requests[user_id].append({
        "id": req_id,
        "query": query
    })

    # 3. Notify the user of bypass sync with exact Render Setup URL
    await interaction.followup.send(
        f"📲 Requesting **\"{query}\"** to official MusicNest Android app...\n\n"
        f"💡 *Tip: If playback does not start, make sure you have entered this bot's URL in your **MusicNest Settings > Render Server URL**:\n"
        f"`{PUBLIC_BOT_URL}`*"
    )


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

    user_id = str(interaction.user.id)
    pending_requests.pop(user_id, None)
    active_sessions.pop(user_id, None)

    vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("🛑 Stopped playback, cleared bypass sessions, and left Voice Channel.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ Error: DISCORD_BOT_TOKEN environment variable not set.")
    else:
        bot.run(token)
