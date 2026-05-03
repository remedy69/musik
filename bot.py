import asyncio
import os
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN. Add it in Railway Variables or your local .env file.")

intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "ignoreerrors": True,
    "noplaylist": False,
    "cachedir": False,
    "cookiefile": os.getenv("YTDLP_COOKIES_FILE"),
    "js_runtimes": {"deno": {}},
}

# Remove None values so yt-dlp does not receive an empty cookiefile.
YTDL_OPTIONS = {k: v for k, v in YTDL_OPTIONS.items() if v is not None}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
URL_RE = re.compile(r"https?://", re.IGNORECASE)


def fmt_time(seconds: Optional[int]) -> str:
    if not seconds:
        return "Live"
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: Optional[int]
    author: str
    thumbnail: Optional[str]
    requester_id: int
    requester_name: str


async def ytdl_extract(query: str, requester: discord.abc.User) -> List[Track]:
    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            search = query if URL_RE.search(query) else f"ytsearch1:{query}"
            return ydl.extract_info(search, download=False)

    data = await loop.run_in_executor(None, extract)
    if not data:
        return []

    entries = data.get("entries") if isinstance(data, dict) else None
    if entries:
        raw_tracks = [entry for entry in entries if entry]
    else:
        raw_tracks = [data]

    tracks: List[Track] = []
    for item in raw_tracks[:25]:
        if not item:
            continue
        stream_url = item.get("url")
        webpage_url = item.get("webpage_url") or item.get("original_url") or query
        if not stream_url:
            continue
        tracks.append(
            Track(
                title=item.get("title") or "Unknown title",
                webpage_url=webpage_url,
                stream_url=stream_url,
                duration=item.get("duration"),
                author=item.get("uploader") or item.get("channel") or "Unknown",
                thumbnail=item.get("thumbnail"),
                requester_id=requester.id,
                requester_name=requester.display_name,
            )
        )
    return tracks


class MusicState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: List[Track] = []
        self.history: List[Track] = []
        self.current: Optional[Track] = None
        self.volume: float = 0.65
        self.loop_current: bool = False
        self.panel_message: Optional[discord.Message] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.lock = asyncio.Lock()

    def make_source(self, track: Track) -> discord.PCMVolumeTransformer:
        audio = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        return discord.PCMVolumeTransformer(audio, volume=self.volume)


states: Dict[int, MusicState] = {}


def get_state(guild: discord.Guild) -> MusicState:
    if guild.id not in states:
        states[guild.id] = MusicState(guild.id)
    return states[guild.id]


def make_panel_embed(state: MusicState) -> discord.Embed:
    track = state.current
    if not track:
        embed = discord.Embed(title="🦁 MUSIC PANEL", description="Nothing is playing.", color=0x5865F2)
        return embed

    embed = discord.Embed(title="🦁 MUSIC PANEL", color=0x5865F2)
    embed.description = f"💿 [{track.title}]({track.webpage_url})"
    embed.add_field(name="👥 Requested By", value=f"<@{track.requester_id}>", inline=True)
    embed.add_field(name="🕘 Music Duration", value=fmt_time(track.duration), inline=True)
    embed.add_field(name="🎙️ Music Author", value=track.author[:256], inline=True)
    embed.add_field(name="🔊 Volume", value=f"{int(state.volume * 100)}%", inline=True)
    embed.add_field(name="🔁 Loop", value="On" if state.loop_current else "Off", inline=True)
    embed.add_field(name="📜 Queue", value=f"{len(state.queue)} song(s)", inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class MusicPanel(discord.ui.View):
    def __init__(self, state: MusicState):
        super().__init__(timeout=None)
        self.state = state

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=make_panel_embed(self.state), view=self)

    @discord.ui.button(label="Down", emoji="🔉", style=discord.ButtonStyle.secondary)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        self.state.volume = max(0.05, self.state.volume - 0.10)
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.state.volume
        await self.refresh(interaction)

    @discord.ui.button(label="Back", emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if self.state.history:
            if self.state.current:
                self.state.queue.insert(0, self.state.current)
            self.state.queue.insert(0, self.state.history.pop())
            if vc:
                vc.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
        await self.refresh(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Up", emoji="🔊", style=discord.ButtonStyle.secondary)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        self.state.volume = min(2.0, self.state.volume + 0.10)
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.state.volume
        await self.refresh(interaction)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        random.shuffle(self.state.queue)
        await self.refresh(interaction)

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.loop_current = not self.state.loop_current
        await self.refresh(interaction)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.queue.clear()
        self.state.loop_current = False
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await self.refresh(interaction)



async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Join a voice channel first.", ephemeral=True)
        return None

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc and vc.channel != channel:
        await vc.move_to(channel)
    elif not vc:
        vc = await channel.connect(self_deaf=True)
    return vc


async def update_panel(state: MusicState):
    if not state.text_channel:
        return
    embed = make_panel_embed(state)
    view = MusicPanel(state)
    try:
        if state.panel_message:
            await state.panel_message.edit(embed=embed, view=view)
        else:
            state.panel_message = await state.text_channel.send(embed=embed, view=view)
    except discord.NotFound:
        state.panel_message = await state.text_channel.send(embed=embed, view=view)


async def play_next(guild: discord.Guild):
    state = get_state(guild)
    vc = guild.voice_client
    if not vc:
        return

    async with state.lock:
        if state.current and state.loop_current:
            next_track = state.current
        else:
            if state.current:
                state.history.append(state.current)
                state.history = state.history[-10:]
            if not state.queue:
                state.current = None
                await update_panel(state)
                return
            next_track = state.queue.pop(0)
            state.current = next_track

        source = state.make_source(next_track)

        def after_playing(error):
            if error:
                print(f"Player error: {error}")
            asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

        vc.play(source, after=after_playing)
        await update_panel(state)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}. Slash commands synced.")


@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    vc = await ensure_voice(interaction)
    if vc:
        await interaction.followup.send(f"Joined {vc.channel.mention}.", ephemeral=True)


@bot.tree.command(name="play", description="Play a YouTube song, search, or playlist")
@app_commands.describe(query="Example: eminem - lose yourself")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return

    state = get_state(interaction.guild)
    state.text_channel = interaction.channel

    await interaction.followup.send(f"Searching YouTube for `{query}`...")
    tracks = await ytdl_extract(query, interaction.user)
    if not tracks:
        await interaction.followup.send("I could not find a playable YouTube result.")
        return

    state.queue.extend(tracks)
    first = tracks[0]

    if len(tracks) == 1:
        added = discord.Embed(title="🦁 Song Added to Queue", color=0x5865F2)
        added.description = f"[{first.title}]({first.webpage_url}) [`{fmt_time(first.duration)}`]"
        await interaction.followup.send(embed=added)
    else:
        await interaction.followup.send(f"Added `{len(tracks)}` songs to the queue.")

    if not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)
    else:
        await update_panel(state)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the current song")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear the queue")
async def stop(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    state.queue.clear()
    state.loop_current = False
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
    await update_panel(state)
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="leave", description="Leave the voice channel")
async def leave(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    state.queue.clear()
    state.current = None
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
        await interaction.response.send_message("Left the voice channel.")
    else:
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current queue")
async def queue(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    if not state.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines = []
    for index, track in enumerate(state.queue[:10], start=1):
        lines.append(f"`{index}.` [{track.title}]({track.webpage_url}) `{fmt_time(track.duration)}`")
    more = "" if len(state.queue) <= 10 else f"\n...and {len(state.queue) - 10} more."
    embed = discord.Embed(title="📜 Queue", description="\n".join(lines) + more, color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show the music panel")
async def nowplaying(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    state.text_channel = interaction.channel
    await interaction.response.send_message(embed=make_panel_embed(state), view=MusicPanel(state))


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    random.shuffle(state.queue)
    await update_panel(state)
    await interaction.response.send_message("Queue shuffled.")


@bot.tree.command(name="loop", description="Toggle loop for the current song")
async def loop(interaction: discord.Interaction):
    state = get_state(interaction.guild)
    state.loop_current = not state.loop_current
    await update_panel(state)
    await interaction.response.send_message(f"Loop is now {'on' if state.loop_current else 'off'}.")


@bot.tree.command(name="volume", description="Set volume from 1 to 200")
@app_commands.describe(amount="Volume percent, example: 75")
async def volume(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200]):
    state = get_state(interaction.guild)
    state.volume = amount / 100
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    await update_panel(state)
    await interaction.response.send_message(f"Volume set to {amount}%.")


bot.run(TOKEN)
