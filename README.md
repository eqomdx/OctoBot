# OctoBot v1.0.10

OctoBot combines the existing **OctoTracker** and **OctoCop** projects into one Discord bot process and one Discord application/token.

## Included systems

### OctoTracker
- Realm/authentication monitoring
- Status dashboard and alerts
- `/status`, `/uptime`, `/incidents`
- Official announcement monitoring and `/announcement`
- Community `/report`, `/reports`, moderator report tools
- Booty Bay Pirate Radio monitoring and `/radio`
- `/nextshows` lists every upcoming scheduled show in the next `RADIO_SCHEDULE_DAYS` (default 14) with absolute (`<t:…:f>`) and relative (`<t:…:R>`) Discord timestamps plus the DJ's Twitch link; same access rule as `/radio`.
  - Each scheduled show is pinged to `DISCORD_RADIO_PING_ROLE_ID` in `DISCORD_RADIO_CHANNEL_ID` at its scheduled start time, naming the DJ from the station schedule. One ping per show; a start missed while the bot was down is still announced if the show is under 10 minutes in.
  - When an announced show's scheduled end passes, the `/nextshows` list is posted to the same channel automatically (no ping), once per show.
  - `RADIO_LIVE_ALERTS=true` additionally pings when a DJ goes live outside the schedule.
  - The ping lists three ways to listen: in-game radio, the radio website, and the live DJ's own Twitch stream (Whiski, Mossa, Tekeela, Sabellwind are built in; `RADIO_DJ_STREAMS` overrides the table).
- Tracker command role configuration under `/config command-role`

### OctoCop moderation
- `/timeout` with `s`, `m`, `h`, `d`, `w` durations and combined values such as `1h30m`
- `/untimeout`
  - Removing a timeout early voids that timeout from `/check` history, timeout count, and total timeout time while retaining the audit record.
- `/ban user reason`
  - Bans the user (a member or someone who already left) with a required reason. The reason goes to the Discord audit log, the mod log and the user's DM.
  - The ban is recorded as a `B-####` case in `/check`. It **stays** there after an unban so staff can see it if the user is ever let back in; the unban date is added to the case automatically when the ban is lifted (by anyone, including via Discord's ban list).
  - Requires the **Ban users** bot permission. Moderator/Admin profiles have it; Helper does not.
- `/whisper user message`
  - DMs the user as the bot ("Message from <server>"), without naming who sent it. Logged to the mod-log channel with the sender. Requires the **Whisper users** permission (granted to roles that can warn).
- `/warn`
- `/note user note`
  - Like `/warn` but silent: recorded as an `N-####` case in `/check` and the mod log, no DM. Uses the **Warn users** permission. Works for users no longer in the server.
- `/warnings`
- `/history user:@User amount:10`
  - Shows a user's most recent messages across all readable server text channels and active threads. `amount` defaults to 10 and supports 1-50.
  - The selected messages are shown oldest-to-newest, ending with **Most recent**, and every entry identifies its channel.
  - New messages are indexed while OctoBot is online; when history is short, the command performs a server-wide newest-first merged backfill rather than scanning one channel at a time.
  - Indexed messages remain visible if later deleted (including timeout cleanup) and are labelled **deleted**, so moderation context is not lost.
  - Uses the same access permission as `/check`, so the default Helper role cannot use it.
- `/check`
  - Shows the full active moderation history, including every warning/timeout/ban reason, moderator, timestamp, timeout duration, and cleanup details.
  - Accepts users who are no longer in the server, so a banned user's record can be reviewed before an unban.
  - Moderators/admins can remove individual history entries directly from the ephemeral `/check` panel using the red ❌ case buttons.
- `/clearcheck`
  - Clears the user's full `/check`/`/warnings` profile after a confirmation prompt.
  - History removal is soft-delete only: database rows remain for audit and an active Discord timeout is not lifted.
- `/settings ...` moderation configuration
- Banned-word filter (replaces the Arcane keyword filter): `/word list`, `/word add word`, `/word remove word` (settings permission; replies are visible only to you).
  - A message containing a banned word is deleted and the author is timed out for 30 seconds (no message-history cleanup). Recorded as a timeout case in `/check`, DMed if timeout DMs are on, and logged to the mod-log channel with the message text.
  - Matching is whole-word and case-insensitive; phrases are allowed and match with spaces, hyphens or no separator. `/word list` is alphabetical. The Arcane word list is seeded automatically the first time the filter starts with an empty list (`octocop/default_banned_words.py`). Server owner, administrators and anyone with a staff profile are exempt. Edited messages are re-checked.
  - The bot needs **Manage Messages** in filtered channels and **Moderate Members**.
- Legacy-role cleanup: a member who gains role `1547371277474603028` automatically loses role `1547037223558451291`. `/rolesweep` (settings permission) does a one-off pass over every member, also treating `1547038342661804194`, `1547038337297154078` and `1547038296025333880` as triggers. Configurable via `ROLE_CLEANUP_*`; needs the **Server Members Intent** enabled in the Developer Portal.
- Helper default timeout limit: 1 hour
- Moderator/Admin default timeout limit: Discord maximum (4 weeks)
- Optional timeout message cleanup window
- Warning, timeout and ban DMs (the DM never names the moderator)
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

## v1.0.5 message-history ordering

`/history` merges readable channel histories by Discord message ID so fallback scanning is server-wide newest-first rather than channel-by-channel. The selected last X messages are displayed oldest-to-newest with a channel on every entry.

## v1.0.6 bans and anonymous DMs

`/ban user reason` bans a member (or a user who already left) and records a `B-####` case that stays in `/check` even after an unban. The bot watches Discord unban events and stamps the unban date onto the case. A new **Ban users** permission gates the command; existing Moderator/Admin profiles receive it automatically on upgrade, Helper does not. `/settings dm-bans` controls the ban DM. Warning, timeout and ban DMs no longer name the moderator who issued them.

## v1.0.7 schedule-driven radio pings, Twitch links, /nextshows

Radio pings are now driven by the station schedule: the next show's DJ and start time are read from `/api/station/.../schedule` and the role is pinged when that time arrives, whether or not the DJ has connected yet. Live-detection pings caused duplicate announcements (early connects and stream dropouts each produced a fresh "now live") and are now opt-in via `RADIO_LIVE_ALERTS`; when enabled they never repeat a slot already announced from the schedule.

The ping links the radio website and the live DJ's Twitch stream (Whiski, Mossa, Tekeela, Sabellwind built in; `RADIO_DJ_STREAMS` overrides). `/nextshows` lists every upcoming show in the next `RADIO_SCHEDULE_DAYS` (default 14) with absolute and relative Discord timestamps. The schedule is read as a date window because AzuraCast only expands recurring shows for one.

## v1.0.8 recurring shows and end-of-show follow-up

The schedule is read as a `start`/`end` date window (`RADIO_SCHEDULE_DAYS`, default 14) because AzuraCast only expands recurring shows for a date range, so `/nextshows` now lists every repeat. When an announced show's scheduled end passes, the `/nextshows` list is posted to the radio channel automatically, once per show.

## v1.0.9 legacy-role cleanup

Members who gain role `1547371277474603028` automatically lose role `1547037223558451291`; `/rolesweep` does a one-off pass using the wider trigger list. The bot now requests the Server Members Intent, which must be enabled in the Discord Developer Portal before this version is started.

## v1.0.10 whisper, notes, word filter, /history paging

`/whisper` DMs a user as the bot (new **Whisper users** permission, granted to roles that can warn). `/note` records a silent staff note in `/check`. The banned-word filter replaces the Arcane keyword filter: `/word list|add|remove`, deletion plus a 30-second timeout, seeded with the migrated list on first start. `/history` is now one message with Previous/Next buttons and never exceeds Discord's embed size limit.
