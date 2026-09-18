# OctoBot command guide

All commands are slash commands and work in any channel OctoBot can see. Replies marked **(private)** are visible only to you.

Tiers come from the bot's role profiles (`/settings view role`). On the OctoWoW server the defaults are:

| Tier | Roles | Bot permissions |
| --- | --- | --- |
| Helper+ | Helper, Moderator, Admin | Timeout users (Helper max `1h`), Remove timeouts |
| Mod+ | Moderator, Admin | Everything above plus Warn, View warnings, Check history, Ban, Whisper, Manage settings (max timeout `4w`) |
| Admin only | Server owner, anyone with Discord **Administrator** | Everything, plus `/settings` and `/config` |

Anyone with Discord Administrator or the server owner always has every bot permission, regardless of profile.

---

## Everyone

Public tracker commands. Admins can restrict any of them to specific roles with `/config command-role`.

| Command | What it does |
| --- | --- |
| `/status` | Current realm, login and authentication status plus active community reports. |
| `/uptime` | Official uptime statistics. |
| `/incidents` | Recent outages and degraded incidents. |
| `/announcement` | The latest official OctoWoW announcement. |
| `/report realm issue [details]` | Report a connection problem right now. Groups with other reports; enough reports in 10 minutes flag the realm as degraded. |
| `/reports` | Current community reports, grouped by realm and issue. |
| `/radio` | Booty Bay Pirate Radio: live/AutoDJ state, listeners, now playing, next shows. |
| `/nextshows` | Every scheduled show for the next 14 days with date, countdown, end time and the DJ's Twitch link. |
| `/authcheck` | Immediate diagnostic of the OctoWoW authentication endpoint. Only works in the designated channel. |
| `/help` | Lists only the commands you can use. |

Automatic: the radio role is pinged at each scheduled show's start, the `/nextshows` list is posted when a show ends, and a member who gains the newer membership role loses the legacy one.

---

## Helper+ commands

### `/timeout user duration reason`
Times the user out. Duration uses `s` `m` `h` `d` `w` and can be combined: `30m`, `1h30m`, `2d`. Reason is required and goes to the Discord audit log, the mod log and the user's DM.

- Helpers can issue up to **1 hour**; Moderators/Admins up to **4 weeks**.
- Refuses: yourself, the owner, bots, administrators, anyone with a role equal to or above yours.
- If already timed out, use `/untimeout` first.
- If message cleanup is on (`/settings timeout-cleanup`), the user's messages from the last N minutes are deleted and the count is shown.
- Recorded as case `T-####` in `/check`. The DM does not say who issued it. **(private reply)**

### `/untimeout user [reason]`
Lifts an active timeout early. The case is removed from `/check` counts but kept for audit. **(private reply)**

---

## Mod+ commands

### Acting on users

| Command | What it does |
| --- | --- |
| `/warn user reason` | Records a warning (`W-####`) and DMs the user the reason. The DM does not name you. |
| `/note user note` | Silent staff note (`N-####`) in the user's `/check` history. The user is **not** told. Works on users who have left. |
| `/whisper user message` | DMs the user a message "from the server" (up to 2000 chars). The DM does not name you; the mod log does. |
| `/ban user reason` | Bans the user (also works on someone who already left). DM first, then ban. Recorded as `B-####`. The case **stays** in `/check` after an unban and gains an "Unbanned" date automatically, so staff can see it if the user is ever let back in. Bots and higher roles are refused. |

All replies are private. Everything is posted to the mod-log channel with your name.

### Looking things up

| Command | What it does |
| --- | --- |
| `/check user [remove]` | Full moderation history: counts of warnings, timeouts, bans and notes, current timeout state, and every case with date, moderator and reason. Paged with ◀ ▶. Accepts users no longer in the server (e.g. banned). Add `remove:W-0003` (any case ID shown in the list) to drop that case from history; the row is kept for audit. |
| `/warnings user [channel]` | Just the warnings. Add `channel` to post them publicly there instead of privately. |
| `/history user [amount]` | The user's last 1–50 server messages (default 10), oldest to newest, with channel and jump link. Deleted messages stay listed and are marked. Paged with ◀ ▶. Uses the same access as `/check`. |

### Clearing history

**`/clearcheck user`** — wipes the user's entire `/check` profile after a confirmation button. Rows are kept for audit; an active Discord timeout is not lifted.

### Banned-word filter

Any message containing a banned word is deleted and the author timed out for 30 seconds (no message cleanup). Logged with the message text; recorded as a timeout case. Staff are exempt. Edited messages are re-checked.

| Command | What it does |
| --- | --- |
| `/word list` | The banned words, alphabetical. |
| `/word add word` | Adds a word or phrase. Matching is whole-word and case-insensitive; phrases match with spaces, hyphens or joined (`sand nigger` also catches `sand-nigger` and `sandnigger`). |
| `/word remove word` | Removes one. Autocompletes from the list. |

All replies are private. Adds and removes go to the mod log.

### Raid protection (`/lockdown …`)

While lockdown is on, every new joiner is DMed "We are currently in Lockdown. Please try again soon" and banned for **7 days**. The bot unbans them automatically when the 7 days are up. Bots that join are ignored.

| Command | What it does |
| --- | --- |
| `/lockdown toggle on\|off` | `on` starts a lockdown for the configured timer (running it again extends it); `off` ends it now. |
| `/lockdown timer minutes` | How long a lockdown lasts before switching itself off (1–1440, default 30). |
| `/lockdown status` | On/off, when it ends, how many joiners were banned this lockdown, how many are still banned, and the all-time total. |

All replies are private. Start, end and every ban go to the mod log.

### Community reports (Discord Manage Messages or Moderate Members)

| Command | What it does |
| --- | --- |
| `/reports-details` | Each active report with who filed it and their details. |
| `/reports-clear` | Clears all active reports. |

---

## Admin only commands

These require Discord **Administrator** (or server owner); bot role profiles do not grant them.

### Bot settings (`/settings …`)

| Command | What it does |
| --- | --- |
| `/settings bootstrap helper moderator log_channel [admin]` | One-shot setup of the Helper / Moderator / Admin profiles and the mod-log channel. |
| `/settings permission role permission allow\|deny` | Grant or deny one capability for a role: Timeout, Warn, View warnings, Check, Remove timeouts, Ban, Whisper, Manage settings. |
| `/settings timeout-limit role duration` | Max timeout a role may issue, e.g. `1h`, `2d`, `1w`. |
| `/settings log-channel channel` | Where moderation actions are logged. |
| `/settings timeout-cleanup minutes` | Delete the timed-out user's messages from the previous N minutes (0–1440; `0` = off). Set to `5` for the usual behaviour. |
| `/settings dm-warnings true\|false` | DM users when warned. |
| `/settings dm-timeouts true\|false` | DM users when timed out. |
| `/settings dm-bans true\|false` | DM users when banned. |
| `/settings view role` | Show a role's bot permissions and timeout limit. |

### Tracker access

| Command | What it does |
| --- | --- |
| `/config command-role add command role` | Restrict a public tracker command (`status`, `uptime`, `incidents`, `announcement`, `report`, `reports`, `radio`) to a role. A command with no roles configured is open to everyone. `/nextshows` follows the `radio` rule. |
| `/config command-role remove command role_id` | Remove one of those role restrictions. |
| `/config command-role list` | Show all restrictions. |

---

## Making a moderation command Admin-only

Moderator and Admin profiles are identical by default. To reserve something for Admins, deny it on the Moderator role:

```text
/settings permission role:@Moderator permission:Ban users access:deny
```

Discord Administrators always keep every capability.

## Quick reference: case IDs

| Prefix | Meaning |
| --- | --- |
| `W-####` | Warning |
| `T-####` | Timeout (manual or word-filter) |
| `B-####` | Ban |
| `N-####` | Staff note |

Removing a case from `/check` (`remove:`, `/clearcheck`, `/untimeout`) never deletes the database row — it only hides it from counts and listings.
