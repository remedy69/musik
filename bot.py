import asyncio
import os
import random
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands
import yt_dlp

# =========================
# Config
# =========================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "").strip() or "!"
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
# Required for commands to be read properly
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# =========================
# yt-dlp / ffmpeg
# =========================

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "impersonate": "chrome",
}

if YOUTUBE_COOKIES:
    YDL_OPTIONS["cookiefile"] = YOUTUBE_COOKIES

# --- NEW PO TOKEN CONFIGURATION ---
po_token = os.getenv("PO_TOKEN", "").strip()
visitor_data = os.getenv("VISITOR_DATA", "").strip()

if po_token or visitor_data:
    YDL_OPTIONS["extractor_args"] = {"youtube": {"player-client": ["web", "mweb"]}}
    if po_token:
        YDL_OPTIONS["extractor_args"]["youtube"]["po_token"] = [po_token]
    if visitor_data:
        YDL_OPTIONS["extractor_args"]["youtube"]["visitor_data"] = [visitor_data]

BASE_FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
BASE_FFMPEG_OPTIONS = "-vn"

FILTER_PRESETS = {
    "off": "",
    "bassboost": "bass=g=8",
    "nightcore": "asetrate=48000*1.25,aresample=48000,atempo=1.1",
    "vaporwave": "asetrate=48000*0.8,aresample=48000,atempo=1.0",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
}

# =========================
# Guild state
# =========================

@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by: str

class GuildPlayer:
    def __init__(self) -> None:
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume: int = 100
        self.loop_mode: str = "off"   # off / track / queue
        self.filter_name: str = "off"
        self.autoplay: bool = False
        self.stay_247: bool = False
        self.text_channel_id: Optional[int] = None
        self.last_url: Optional[str] = None

players: dict[int, GuildPlayer] = {}

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]

def build_source(stream_url: str, volume: int, filter_name: str) -> discord.PCMVolumeTransformer:
    audio_filter = FILTER_PRESETS.get(filter_name, "")
    options = BASE_FFMPEG_OPTIONS
    if audio_filter:
        options += f' -af "{audio_filter}"'
    source = discord.FFmpegPCMAudio(
        stream_url,
        before_options=BASE_FFMPEG_BEFORE_OPTIONS,
        options=options,
    )
    return discord.PCMVolumeTransformer(source, volume=max(0.0, min(volume / 100.0, 2.0)))

async def extract_track(query: str, requested_by: str) -> Track:
    def _extract() -> Track:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if "entries" in info and info["entries"]:
                info = next((e for e in info["entries"] if e), None)

            if not info:
                raise RuntimeError("No results found.")

            stream_url = info.get("url")
            title = info.get("title") or "Unknown title"
            webpage_url = info.get("webpage_url") or query

            if not stream_url:
                raise RuntimeError("Could not get a playable stream URL.")

            return Track(
                title=title,
                webpage_url=webpage_url,
                stream_url=stream_url,
                requested_by=requested_by,
            )

    return await asyncio.to_thread(_extract)

async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Join a voice channel first.")

    voice_client = ctx.guild.voice_client
    if voice_client is None:
        return await ctx.author.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
    
    # 4006 Fix: Force cleanup and reconnect if we are on a stale gateway session
    if voice_client.channel != ctx.author.voice.channel:
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            pass
        return await ctx.author.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
        
    return voice_client

async def maybe_send_text(guild: discord.Guild, content: str, view: Optional[discord.ui.View] = None) -> None:
    player = get_player(guild.id)
    if not player.text_channel_id:
        return
    channel = guild.get_channel(player.text_channel_id)
    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(content, view=view)
        except discord.HTTPException:
            pass

async def start_next(guild: discord.Guild) -> None:
    player = get_player(guild.id)
    voice_client = guild.voice_client

    if voice_client is None:
        player.current = None
        return

    next_track: Optional[Track] = None

    if player.loop_mode == "track" and player.current is not None:
        next_track = player.current
    elif player.queue:
        next_track = player.queue.popleft()
        if player.loop_mode == "queue" and player.current is not None:
            player.queue.append(player.current)
    elif player.autoplay and player.last_url:
        try:
            next_track = await extract_track(player.last_url, "AutoPlay")
        except Exception:
            next_track = None

    if next_track is None:
        player.current = None
        if not player.stay_247:
            try:
                await voice_client.disconnect()
            except Exception:
                pass
        return

    player.current = next_track
    player.last_url = next_track.webpage_url

    source = build_source(
        next_track.stream_url,
        player.volume,
        player.filter_name,
    )

    def _after_play(error: Optional[Exception]) -> None:
        if error:
            print(f"Playback error: {error}")
        fut = asyncio.run_coroutine_threadsafe(start_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print(f"Queue continuation error: {exc}")

    voice_client.play(source, after=_after_play)
    await maybe_send_text(
        guild,
        f"🎶 **Now playing:** {next_track.title}\nRequested by: {next_track.requested_by}",
        view=MusicPanelView(),
    )

# =========================
# Persistent controls
# =========================

class MusicPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, custom_id="music_pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, custom_id="music_resume")
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, custom_id="music_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        vc = interaction.guild.voice_client
        player = get_player(interaction.guild.id)
        player.queue.clear()
        player.current = None
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("⏹ Stopped and disconnected.", ephemeral=True)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

# =========================
# Events
# =========================

@bot.event
async def on_ready() -> None:
    bot.add_view(MusicPanelView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as exc:
        print(f"Slash sync failed: {exc}")
    print(f"Logged in as {bot.user} ({bot.user.id})")

# =========================
# Commands
# =========================

@bot.command(name="play")
async def play_cmd(ctx: commands.Context, *, query: str) -> None:
    player = get_player(ctx.guild.id)
    player.text_channel_id = ctx.channel.id

    try:
        vc = await ensure_voice(ctx)
        track = await extract_track(query, str(ctx.author))
        player.queue.append(track)

        if vc.is_playing() or vc.is_paused():
            await ctx.send(f"➕ Added to queue: **{track.title}**")
        else:
            await start_next(ctx.guild)
    except Exception as exc:
        await ctx.send(f"❌ {exc}")

@bot.command(name="skip")
async def skip_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭ Skipped.")
    else:
        await ctx.send("Nothing to skip.")

@bot.command(name="stop")
async def stop_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    player = get_player(ctx.guild.id)
    player.queue.clear()
    player.current = None
    if vc:
        await vc.disconnect()
        await ctx.send("⏹ Stopped and disconnected.")
    else:
        await ctx.send("Not connected.")

@bot.command(name="pause")
async def pause_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Paused.")
    else:
        await ctx.send("Nothing is playing.")

@bot.command(name="resume")
async def resume_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("Nothing is paused.")

@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context) -> None:
    player = get_player(ctx.guild.id)
    lines = []
    if player.current:
        lines.append(f"**Now:** {player.current.title}")
    if player.queue:
        for idx, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"`{idx}.` {track.title}")
    if not lines:
        await ctx.send("Queue is empty.")
        return
    await ctx.send("\n".join(lines))

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx: commands.Context) -> None:
    player = get_player(ctx.guild.id)
    if player.current:
        await ctx.send(
            f"🎶 **Now playing:** {player.current.title}\n"
            f"Requested by: {player.current.requested_by}"
        )
    else:
        await ctx.send("Nothing is playing.")

@bot.command(name="clear")
async def clear_cmd(ctx: commands.Context) -> None:
    player = get_player(ctx.guild.id)
    player.queue.clear()
    await ctx.send("🧹 Queue cleared.")

@bot.command(name="shuffle")
async def shuffle_cmd(ctx: commands.Context) -> None:
    player = get_player(ctx.guild.id)
    if len(player.queue) < 2:
        await ctx.send("Need at least 2 queued songs to shuffle.")
        return
    items = list(player.queue)
    random.shuffle(items)
    player.queue = deque(items)
    await ctx.send("🔀 Queue shuffled.")

@bot.command(name="remove")
async def remove_cmd(ctx: commands.Context, index: int) -> None:
    player = get_player(ctx.guild.id)
    if index < 1 or index > len(player.queue):
        await ctx.send("Invalid queue index.")
        return
    items = list(player.queue)
    removed = items.pop(index - 1)
    player.queue = deque(items)
    await ctx.send(f"Removed: **{removed.title}**")

@bot.command(name="loop")
async def loop_cmd(ctx: commands.Context, mode: str) -> None:
    mode = mode.lower().strip()
    if mode not in {"off", "track", "queue"}:
        await ctx.send("Use: `off`, `track`, or `queue`.")
        return
    player = get_player(ctx.guild.id)
    player.loop_mode = mode
    await ctx.send(f"🔁 Loop mode set to **{mode}**.")

@bot.command(name="autoplay")
async def autoplay_cmd(ctx: commands.Context, mode: str) -> None:
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await ctx.send("Use: `on` or `off`.")
        return
    player = get_player(ctx.guild.id)
    player.autoplay = mode == "on"
    await ctx.send(f"📻 AutoPlay **{'enabled' if player.autoplay else 'disabled'}**.")

@bot.command(name="247")
async def stay247_cmd(ctx: commands.Context, mode: str) -> None:
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await ctx.send("Use: `on` or `off`.")
        return
    player = get_player(ctx.guild.id)
    player.stay_247 = mode == "on"
    await ctx.send(f"🕒 24/7 mode **{'enabled' if player.stay_247 else 'disabled'}**.")

@bot.command(name="volume")
async def volume_cmd(ctx: commands.Context, amount: int) -> None:
    if amount < 0 or amount > 200:
        await ctx.send("Volume must be between 0 and 200.")
        return
    player = get_player(ctx.guild.id)
    player.volume = amount

    vc = ctx.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = max(0.0, min(amount / 100.0, 2.0))

    await ctx.send(f"🔊 Volume set to **{amount}%**.")

@bot.command(name="filter")
async def filter_cmd(ctx: commands.Context, name: str) -> None:
    name = name.lower().strip()
    if name not in FILTER_PRESETS:
        await ctx.send(f"Available filters: {', '.join(FILTER_PRESETS.keys())}")
        return

    player = get_player(ctx.guild.id)
    player.filter_name = name
    vc = ctx.guild.voice_client

    if vc and player.current and (vc.is_playing() or vc.is_paused()):
        current = player.current
        vc.stop()
        player.current = current
        if player.loop_mode != "track":
            player.queue.appendleft(current)

    await ctx.send(f"🎛 Filter set to **{name}**.")

@bot.command(name="panel")
async def panel_cmd(ctx: commands.Context) -> None:
    get_player(ctx.guild.id).text_channel_id = ctx.channel.id
    await ctx.send("Music controls:", view=MusicPanelView())

# =========================
# Slash commands
# =========================

@bot.tree.command(name="play", description="Play a song from a URL or search query.")
async def slash_play(interaction: discord.Interaction, query: str) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    ctx = await bot.get_context(interaction.message) if interaction.message else None
    player = get_player(interaction.guild.id)
    player.text_channel_id = interaction.channel_id

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await interaction.user.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
        elif vc.channel != interaction.user.voice.channel:
            # 4006 Fix: Force disconnect stale sessions during slash command connections
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = await interaction.user.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)

        track = await extract_track(query, str(interaction.user))
        player.queue.append(track)

        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(f"➕ Added to queue: **{track.title}**")
        else:
            await start_next(interaction.guild)
            await interaction.followup.send(f"✅ Loaded: **{track.title}**")
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}")

@bot.tree.command(name="skip", description="Skip the current track.")
async def slash_skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭ Skipped.")
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop playback and disconnect.")
async def slash_stop(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    player = get_player(interaction.guild.id)
    player.queue.clear()
    player.current = None
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("⏹ Stopped and disconnected.")
    else:
        await interaction.response.send_message("Not connected.", ephemeral=True)

@bot.tree.command(name="join", description="Join your current voice channel.")
async def slash_join(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.user.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
        elif vc.channel != interaction.user.voice.channel:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            await interaction.user.voice.channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
        await interaction.followup.send("✅ Joined voice.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not join voice: {exc}", ephemeral=True)

@bot.tree.command(name="leave", description="Leave the voice channel.")
async def slash_leave(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
        await interaction.response.send_message("👋 Left voice.", ephemeral=True)
    else:
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)

bot.run(TOKEN)
