# OctoBot v1.0.4

OctoBot combines the existing **OctoTracker** and **OctoCop** projects into one Discord bot process and one Discord application/token.

## Included systems

### OctoTracker
- Realm/authentication monitoring
- Status dashboard and alerts
- `/status`, `/uptime`, `/incidents`
- Official announcement monitoring and `/announcement`
- Community `/report`, `/reports`, moderator report tools
- Booty Bay Pirate Radio monitoring and `/radio`
- Tracker command role configuration under `/config command-role`

### OctoCop moderation
- `/timeout` with `s`, `m`, `h`, `d`, `w` durations and combined values such as `1h30m`
- `/untimeout`
  - Removing a timeout early voids that timeout from `/check` history, timeout count, and total timeout time while retaining the audit record.
- `/warn`
- `/warnings`
- `/history user:@User amount:10`
  - Shows a user's recent server messages, newest first. `amount` defaults to 10 and supports 1-50.
  - New messages are indexed while OctoBot is online; when history is short, the command performs a bounded best-effort scan of recent readable channel history.
  - Indexed messages remain visible if later deleted (including timeout cleanup) and are labelled **deleted**, so moderation context is not lost.
  - Uses the same access permission as `/check`, so the default Helper role cannot use it.
- `/check`
  - Shows the full active moderation history, including every warning/timeout reason, moderator, timestamp, timeout duration, and cleanup details.
  - Moderators/admins can remove individual history entries directly from the ephemeral `/check` panel using the red ❌ case buttons.
- `/clearcheck`
  - Clears the user's full `/check`/`/warnings` profile after a confirmation prompt.
  - History removal is soft-delete only: database rows remain for audit and an active Discord timeout is not lifted.
- `/settings ...` moderation configuration
- Helper default timeout limit: 1 hour
- Moderator/Admin default timeout limit: Discord maximum (4 weeks)
- Optional timeout message cleanup window
- Warning and timeout DMs
- Moderation action logs

## Commands can be used everywhere

There is no bot-command-channel restriction. Slash commands can be used in any server channel where OctoBot has permission to view/respond. An old `DISCORD_BOT_COMMANDS_CHANNEL_ID` entry may remain in an existing environment, but OctoBot does not use it to restrict commands.

## One shared database

Both systems use:

```text
DATABASE_PATH=data/octobot.db
```

Live data is stored in `data/octobot.db`. Update ZIPs intentionally do **not** include a live database, so replacing bot code will not overwrite tracker/moderation/message history. The database is ignored by Git so Discord user/report/moderation history is not accidentally published to a public repository.

`tools/merge_databases.py` can be used if you need to merge newer copies later:

```powershell
python tools/merge_databases.py data/octotracker.db data/timeoutbot.db data/octobot.db
```

## Setup on Windows

```powershell
py -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python main.py
```

Or run `SETUP_WINDOWS.bat` once, edit `.env`, then use `RUN_WINDOWS.bat`.

## Hosting

For bot-hosting.net (or another normal Python bot host):

- Startup command: `python main.py`
- Install command: `pip install -r requirements.txt`
- Add the variables from `.env.example` in the host environment panel.
- Upload/preserve `data/octobot.db` if you want the included historical state.

Only **one** `DISCORD_TOKEN` is required. Use the Discord application that you want to become OctoBot. Stop the old standalone OctoTracker/OctoCop processes before starting OctoBot so two bots do not perform the same monitoring/actions at once.

## Required Discord permissions

Tracker features need normal channel read/send/embed permissions in their configured channels. Moderation additionally needs:

- View Channels
- Send Messages
- Embed Links
- Read Message History
- Manage Messages (for timeout cleanup)
- Moderate Members

For `/history`, also enable **Message Content Intent** under **Discord Developer Portal → Bot → Privileged Gateway Intents**. `Read Message History` and `View Channels` determine which channels can be backfilled/read.

Place the OctoBot Discord role above members/roles it needs to timeout.

## Preconfigured OctoWoW IDs

The `.env.example` contains the current supplied guild/channel/role IDs, including:

- Guild: `1547014756463026197`
- Status/alert: `1547281778841100388`
- Announcements: `1547025378479181895`
- Radio channel: `1547109894732120104`
- Radio ping role: `1547108101747118100`
- Moderation logs: `1547053413899051038`
- Helper: `1547031604113707028`
- Moderator: `1547031685944713307`
- Admin: `1547031493262573668`

## Moderation defaults

On startup, the environment role IDs seed missing profiles only. Existing `/settings` changes in SQLite are not overwritten.

- Helper: `/timeout` + `/untimeout`, maximum issued timeout `1h`
- Moderator: full moderation access, maximum `4w`
- Admin: full moderation/settings access, maximum `4w`

The merged database currently has automatic timeout message cleanup set to `0` minutes (disabled). Enable it with, for example:

```text
/settings timeout-cleanup minutes:5
```

## Notes

PyNaCl/davey voice warnings are harmless; OctoBot does not use Discord voice.
