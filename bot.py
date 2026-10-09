"""Discord bot that checks whether configured server IPs respond to ping.

Setup:
1. Copy .env.example to .env and set DISCORD_TOKEN.
2. Optionally set GUILD_ID so slash commands appear immediately.
3. pip install -r requirements.txt
4. python bot.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
WATCHES_PATH = BASE_DIR / "watches.json"
PING_COUNT = 2
PING_TIMEOUT_MS = 2000

log = logging.getLogger("ipchecker")


@dataclass(frozen=True)
class Server:
    key: str
    name: str
    ip: str


@dataclass(frozen=True)
class CheckResult:
    server: Server
    online: bool
    sent: int
    received: int
    latency_ms: int | None
    error: str | None = None


# Hosts this bot is allowed to probe. Only these addresses are ever pinged.
SERVERS: tuple[Server, ...] = (
    Server("manila", "Manila", "64.29.17.65"),
)


def validate_ipv4(ip: str) -> str:
    address = ipaddress.ip_address(ip)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"{ip} is not an IPv4 address")
    return str(address)


for server in SERVERS:
    validate_ipv4(server.ip)


def ping_command(ip: str) -> list[str]:
    """Build a single-host ICMP ping command. `ip` must already be a validated address."""
    system = platform.system().lower()
    if system == "windows":
        return ["ping", "-n", str(PING_COUNT), "-w", str(PING_TIMEOUT_MS), ip]
    if system == "darwin":
        return ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT_MS), ip]
    timeout_seconds = max(1, PING_TIMEOUT_MS // 1000)
    return ["ping", "-c", str(PING_COUNT), "-W", str(timeout_seconds), ip]


def parse_ping(output: str, returncode: int, sent: int) -> tuple[bool, int, int | None]:
    """Return (online, replies, average latency ms) from ping stdout."""
    times = [float(value) for value in re.findall(r"time[=<]\s*(\d+(?:\.\d+)?)", output, re.IGNORECASE)]
    if times:
        latency = int(round(sum(times) / len(times)))
        return True, len(times), latency
    if returncode == 0:
        return True, sent, None
    return False, 0, None


def format_latency(latency_ms: int | None) -> str:
    if latency_ms is None:
        return "latency unavailable"
    if latency_ms <= 0:
        return "<1 ms"
    return f"{latency_ms} ms"


async def check_server(server: Server) -> CheckResult:
    ip = validate_ipv4(server.ip)
    command = ping_command(ip)
    kwargs: dict = {}
    if platform.system().lower() == "windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError:
        return CheckResult(server, False, PING_COUNT, 0, None, "Ping is not available on this machine")

    timeout = PING_COUNT * (PING_TIMEOUT_MS / 1000 + 1) + 3
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return CheckResult(server, False, PING_COUNT, 0, None, "Ping timed out")

    output = (stdout or b"").decode(errors="replace")
    if stderr:
        output += "\n" + stderr.decode(errors="replace")
    returncode = process.returncode if process.returncode is not None else 1
    online, received, latency = parse_ping(output, returncode, PING_COUNT)
    state = "online" if online else "offline"
    log.info("%s (%s) is %s (%s/%s replies)", server.name, ip, state, received, PING_COUNT)
    return CheckResult(server, online, PING_COUNT, received, latency if online else None)


async def check_servers(servers: tuple[Server, ...] | list[Server]) -> list[CheckResult]:
    return list(await asyncio.gather(*(check_server(server) for server in servers)))


def select_servers(key: str | None) -> list[Server]:
    if key is None:
        return list(SERVERS)
    matches = [server for server in SERVERS if server.key == key]
    if not matches:
        raise KeyError(key)
    return matches


def build_embed(results: list[CheckResult]) -> discord.Embed:
    if any(result.error for result in results):
        color = 0xFAA61A
    elif results and all(result.online for result in results):
        color = 0x3BA55D
    elif any(result.online for result in results):
        color = 0xFAA61A
    else:
        color = 0xED4245

    online_count = sum(1 for result in results if result.online and not result.error)
    embed = discord.Embed(
        title="Server Availability",
        description=f"{online_count} of {len(results)} online",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    for result in results:
        if result.error:
            status = "Error"
            detail = f"`{result.server.ip}`\n{result.error}"
        elif result.online:
            status = "Online"
            detail = (
                f"`{result.server.ip}`\n"
                f"{result.received}/{result.sent} replies · {format_latency(result.latency_ms)}"
            )
        else:
            status = "Offline"
            detail = f"`{result.server.ip}`\nNo reply"
        icon = {"Online": "🟢", "Offline": "🔴", "Error": "🟠"}[status]
        embed.add_field(
            name=f"{icon} {result.server.name} — {status}",
            value=detail,
            inline=True,
        )
    embed.set_footer(text="ICMP ping")
    return embed


def missing_channel_access(interaction: discord.Interaction) -> str | None:
    """Return a user-facing message when the bot cannot post in the command's channel."""
    channel = interaction.channel
    guild = interaction.guild
    if guild is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return None
    me = guild.me
    if me is None:
        return "Re-invite the bot with the link in the console, then try again."
    permissions = channel.permissions_for(me)
    required = (
        ("view_channel", "View Channel"),
        ("send_messages", "Send Messages"),
        ("embed_links", "Embed Links"),
        ("read_message_history", "Read Message History"),
    )
    missing = [label for attribute, label in required if not getattr(permissions, attribute)]
    if not missing:
        return None
    return (
        "I can't post in this channel. Allow these permissions for the bot: "
        + ", ".join(missing)
        + "."
    )


def change_lines(previous: dict[str, bool], results: list[CheckResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        if result.error:
            continue
        before = previous.get(result.server.key)
        if before is None or before == result.online:
            continue
        state = "online" if result.online else "offline"
        lines.append(f"**{result.server.name}** (`{result.server.ip}`) is {state}.")
    return lines


class StatusView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.primary,
        custom_id="ipchecker:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        results = await check_servers(SERVERS)
        await interaction.edit_original_response(embed=build_embed(results), view=self)


class IPChecker(discord.Client):
    def __init__(self, guild_id: int | None, watch_interval: int) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.watch_interval = watch_interval
        self.tree = app_commands.CommandTree(self)
        self._watch_tasks: dict[int, asyncio.Task[None]] = {}
        self._watch_messages: dict[int, int] = {}
        self._resumed = False
        self._commands_synced = False
        self._guild_commands_copied = False
        self._register_commands()

    def _register_commands(self) -> None:
        server_choices = [
            app_commands.Choice(name=f"{server.name} ({server.ip})", value=server.key)
            for server in SERVERS
        ]

        @self.tree.command(name="status", description="Check whether the servers are online")
        @app_commands.describe(server="Server to check. Leave empty to check all of them.")
        @app_commands.choices(server=server_choices)
        async def status(
            interaction: discord.Interaction,
            server: app_commands.Choice[str] | None = None,
        ) -> None:
            await interaction.response.defer()
            key = server.value if server else None
            results = await check_servers(select_servers(key))
            await interaction.edit_original_response(embed=build_embed(results), view=StatusView())

        @self.tree.command(name="watch", description="Post a live status message and keep it updated")
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        async def watch(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.edit_original_response(content="Use this in a text channel.")
                return

            existing = self._watch_tasks.get(channel.id)
            if existing and not existing.done():
                await interaction.edit_original_response(
                    content="This channel is already being monitored. Use `/unwatch` to stop it."
                )
                return

            blocked = missing_channel_access(interaction)
            if blocked:
                await interaction.edit_original_response(content=blocked)
                return

            results = await check_servers(SERVERS)
            try:
                message = await channel.send(embed=build_embed(results), view=StatusView())
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content=(
                        "I can't post in this channel. In the channel permissions, allow the bot "
                        "to View Channel, Send Messages, Embed Links, and Read Message History."
                    )
                )
                return
            self._start_watch(channel.id, message, immediate=False)
            self._persist_watches()
            await interaction.edit_original_response(content=f"Monitoring server status in {channel.mention}.")

        @self.tree.command(name="unwatch", description="Stop the live status updates in this channel")
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        async def unwatch(interaction: discord.Interaction) -> None:
            channel = interaction.channel
            if channel is None or not self._stop_watch(channel.id):
                await interaction.response.send_message(
                    "This channel is not being monitored.",
                    ephemeral=True,
                )
                return
            self._persist_watches()
            await interaction.response.send_message("Stopped monitoring this channel.", ephemeral=True)

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            if isinstance(error, app_commands.MissingPermissions):
                message = "You need the Manage Server permission to do that."
            elif isinstance(error, app_commands.NoPrivateMessage):
                message = "Use that command in a server channel."
            else:
                log.exception("Command failed", exc_info=error)
                message = "Something went wrong while checking the servers."

            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        tasks = list(self._watch_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    def invite_url(self) -> str:
        permissions = 1024 + 2048 + 16384 + 65536  # View Channel, Send Messages, Embed Links, Read History
        return (
            "https://discord.com/api/oauth2/authorize"
            f"?client_id={self.application_id}&permissions={permissions}"
            "&scope=bot%20applications.commands"
        )

    async def setup_hook(self) -> None:
        self.add_view(StatusView())

    async def _sync_commands(self) -> None:
        if self._commands_synced:
            return
        try:
            if self.guild_id is not None:
                guild = discord.Object(id=self.guild_id)
                if not self._guild_commands_copied:
                    self.tree.copy_global_to(guild=guild)
                    self._guild_commands_copied = True
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %s commands to guild %s", len(synced), self.guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %s global commands (they can take up to an hour to appear)", len(synced))
        except discord.Forbidden:
            log.error(
                "Could not register commands for server %s. The bot is not in that server, "
                "or GUILD_ID is not that server's ID. Open the invite link above, add the bot, "
                "then restart.",
                self.guild_id,
            )
            return
        self._commands_synced = True

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("Invite: %s", self.invite_url())
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="Manila"),
        )
        await self._sync_commands()
        if not self._resumed:
            self._resumed = True
            await self._resume_watches()

    def _start_watch(self, channel_id: int, message: discord.Message, immediate: bool) -> None:
        existing = self._watch_tasks.get(channel_id)
        if existing and not existing.done():
            existing.cancel()
        self._watch_messages[channel_id] = message.id
        self._watch_tasks[channel_id] = asyncio.create_task(
            self._watch_loop(channel_id, message, immediate),
            name=f"watch-{channel_id}",
        )

    def _stop_watch(self, channel_id: int) -> bool:
        task = self._watch_tasks.pop(channel_id, None)
        if task is None:
            return False
        self._watch_messages.pop(channel_id, None)
        task.cancel()
        return True

    async def _watch_loop(self, channel_id: int, message: discord.Message, immediate: bool) -> None:
        previous: dict[str, bool] | None = None
        if not immediate:
            await asyncio.sleep(self.watch_interval)
        while True:
            results = await check_servers(SERVERS)
            channel = message.channel
            if previous is not None and isinstance(channel, discord.abc.Messageable):
                lines = change_lines(previous, results)
                if lines:
                    try:
                        await channel.send("\n".join(lines))
                    except discord.HTTPException:
                        log.warning("Could not send a status alert in channel %s", channel_id)
            previous = {
                result.server.key: result.online for result in results if not result.error
            }
            try:
                await message.edit(embed=build_embed(results), view=StatusView())
            except discord.NotFound:
                log.info("Status message %s is gone; stopping watch", message.id)
                self._watch_tasks.pop(channel_id, None)
                self._watch_messages.pop(channel_id, None)
                self._persist_watches()
                return
            except discord.HTTPException:
                log.exception("Could not update status message %s", message.id)
            await asyncio.sleep(self.watch_interval)

    def _persist_watches(self) -> None:
        payload = {
            "watches": [
                {"channel_id": channel_id, "message_id": message_id}
                for channel_id, message_id in self._watch_messages.items()
                if channel_id in self._watch_tasks and not self._watch_tasks[channel_id].done()
            ]
        }
        temporary = WATCHES_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(WATCHES_PATH)

    async def _resume_watches(self) -> None:
        if not WATCHES_PATH.exists():
            return
        try:
            data = json.loads(WATCHES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Could not read %s", WATCHES_PATH)
            return

        for entry in data.get("watches", []):
            try:
                channel_id = int(entry["channel_id"])
                message_id = int(entry["message_id"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                if not isinstance(channel, discord.abc.Messageable):
                    raise TypeError(f"Channel {channel_id} is not messageable")
                message = await channel.fetch_message(message_id)
            except (discord.HTTPException, TypeError, ValueError):
                log.warning("Dropping watch for channel %s; the message is unavailable", channel_id)
                continue
            self._start_watch(channel_id, message, immediate=True)
            log.info("Resumed monitoring in channel %s", channel_id)
        self._persist_watches()


def load_settings() -> tuple[str, int | None, int]:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and paste your bot token.")

    raw_guild = os.getenv("GUILD_ID", "").strip()
    guild_id = int(raw_guild) if raw_guild else None

    try:
        interval = int(os.getenv("WATCH_INTERVAL", "60"))
    except ValueError:
        interval = 60
    interval = min(3600, max(30, interval))
    return token, guild_id, interval


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    token, guild_id, interval = load_settings()
    client = IPChecker(guild_id, interval)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
