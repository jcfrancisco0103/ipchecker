# IP Checker

Discord bot that checks whether this server responds to ping:

| Location | IP           |
|----------|--------------|
| Manila   | 64.29.17.65  |

## Setup

1. Install Python 3.10 or newer.
2. Create a bot at <https://discord.com/developers/applications> and copy its token.
3. In this folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

4. Paste the token into `.env`. For instant slash commands, also set `GUILD_ID` to your server's ID (enable Developer Mode in Discord, then right-click the server).
5. Start the bot:

```powershell
python bot.py
```

6. Open the invite link printed in the console and add the bot to your server.

## Docker

With Docker running and `.env` filled in:

```powershell
docker compose up -d --build
docker compose logs -f
```

Stop it with `docker compose down`. The `/watch` list is kept in a Docker volume, so it survives a restart. The token is read from `.env` at startup and is not copied into the image.

## Commands

- `/status` checks the Manila server.
- `/watch` posts a live status message and refreshes it. Requires Manage Server.
- `/unwatch` stops the live message. Requires Manage Server.

Use the **Refresh** button on a status message to check again immediately.

To monitor a different host, edit `SERVERS` at the top of `bot.py`.
