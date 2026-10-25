# Forward Bot Recovery Service — Behavior Specification

## Purpose and scope

This document describes the behavior implemented by `transfer.py` as read on 2026-10-01. Use it as the functional brief for a rewrite. The service monitors a configured fleet of Telegram bots and restores an unavailable bot under a replacement Telegram username, then restores its profile, deployment token, references, public announcements, and optional standby capacity.

The service supports two recovery modes:

1. **Manual replacement:** a person creates a replacement bot and submits its username through IncogNoteBot's anonymous link. They transfer ownership to the selected receiving Telegram account. If a pool account receives it, the service transfers it onward to the main account.
2. **Automatic replacement:** an optional creator account creates bots through BotFather and transfers them to the main account. Optional prepared backups keep one spare bot ready for each configured bot. If automatic recovery is delayed by the creator cooldown beyond the automatic wait limit, or encounters a Telegram flood wait, it falls back to the manual workflow if no replacement has already been claimed.

The main account ultimately owns every recovered bot. Bot tokens are never stored in recovery state; a replacement token is written to the deployed bot's YAML configuration.

## Actors and configured entities

- **Main account:** primary Telethon user account, owns deployed bots, receives replacements directly when no pool is configured, performs final BotFather setup, and publishes announcements.
- **Pool accounts:** optional ordered accounts used as privacy relays. The submitter transfers a replacement to one pool account; the service transfers ownership from that pool account to the main account.
- **Creator account:** optional separate user account used by automatic mode to create bots and transfer them to the main account.
- **AbuseNotification:** incoming Telegram sender whose messages can trigger recovery when they contain configured bot handles.
- **IncogNoteBot (`@IncogNoteBot`):** creates one-time anonymous submission links, forwards submitted messages to the receiving account, and has the link revocation interaction.
- **BotFather:** creates bots, transfers ownership, restores bot profile/settings, and regenerates tokens.
- **Configured bot mapping:** a friendly name, handle prefix, deployment config path and token key, restart command, and current handle.

## Configuration contract

The config is YAML, normally `config.yml` beside the script. Relative paths resolve from the config file's directory. `--config PATH` selects another file; `--check-config` validates and exits without starting Telegram clients. `LOG_LEVEL` controls logging.

Required top-level sections and values:

- `telegram`: common `api_id` and `api_hash`; `main` account with `username` and `session`; optional `pool` list of accounts with those two fields.
- `announcement`: `channel`, `group`, `recovery_template`, and `restored_template`.
- Optional `anonymous_submissions.retain_link_after_recovery` defaults to `false`. When true, keep the IncogNote link active and accept later valid bot submissions as spare-bot donations. Optional `anonymous_submissions.donation_template` defaults to `Contribute a spare {friendly_name} bot by sending its @username here: {anonymous_link}` and is posted after each restored announcement while retention is enabled.
- `cloudflare`: required mapping; `project_name` and `pages_url` are read when publishing a restored page.
- `bots`: non-empty list. Each item needs `handle_prefix`, `friendly_name`, `config_path`, `token_path`, and `restart_command`. `restart_command` may be a shell-like string (parsed with `shlex`) or a non-empty argument list. `handle` is optional, but required on the first start if no snapshot exists for that mapping.
- Optional `automatic_recovery`: `enabled` and `prepare_bots` booleans default to false. `creator` is optional when automatic recovery can use donated/prepared main-owned spares; it is required to create bots when no spare exists and whenever `prepare_bots` is enabled. Configure `username`, `session`, `api_id`, and `api_hash`; `password` is optional and supplies the creator's BotFather 2FA password.
- Optional `state_path` defaults to `.state/recovery.json`; `snapshot_dir` defaults to `.state/snapshots`.

Usernames are normalized to include `@`. Account/session and bot config paths are resolved relative to the config file. Prefixes must use Telegram username characters and start with a letter; duplicate prefixes are rejected case-insensitively. A configured `handle` must be a valid Telegram bot username ending in `bot`. Replacement usernames submitted manually must match exactly `<handle_prefix><digits>bot` (case-insensitive). The implementation does not reject overlapping distinct prefixes, even though a handle could then match more than one mapping.

The templates receive these fields:

- Recovery: `{friendly_name}`, `{old_handle}`, `{handle_prefix}`, `{anonymous_link}`.
- Restored: `{friendly_name}`, `{new_handle}`, `{old_handle}`.

`prepare_bots` only has an effect when `enabled` is also true, because backup creation/reconciliation is gated on both flags. The current config loader nevertheless requires and starts the creator if `prepare_bots` alone is true.

## Startup and ongoing monitoring

Startup order:

1. Load persisted state.
2. Register incoming-message handlers on main and pool accounts.
3. Start and identity-check the main account. Attempt to start the creator (if configured) and pool accounts; unavailable creator/pool accounts are logged and startup continues. A creator startup failure does not make it a receiving account.
4. Ensure each configured bot has a settings snapshot. Existing snapshots are validated. Missing snapshots are created from the configured `handle`; without that handle startup fails.
5. Reconcile persisted prepared-bot handles, check configured bots, resume in-progress recoveries, and create missing prepared backups.
6. Start a deletion monitor (five-minute interval) and a BotFather cooldown monitor (30-second interval). The service remains connected through the main and pool clients.

There are three recovery triggers:

- **Abuse event:** an incoming message from `AbuseNotification` is scanned for `@bot` handles. Handles matching configured prefixes start recovery.
- **Startup health check:** for each configured handle, recovery starts if it cannot be resolved, is not a bot, is marked deleted, is blocked by the main account, or fails the `/about` conversation probe.
- **Recurring deletion check:** every five minutes, each configured handle is resolved. Invalid/unoccupied usernames and entities marked deleted start recovery. Transient network/RPC errors are ignored until a later check. This recurring check does not repeat the blocked or `/about` probe.

Only one active recovery per handle prefix is permitted. A periodic check skips mappings that already have a recovery. If configured pools exist, a connected pool with the fewest active assigned recoveries is selected (ties follow config order); there is no fallback to main when every configured pool is disconnected. With no pool, main is the receiver if connected.

## Manual recovery workflow

1. Create and persist a recovery record with the old handle, receiver, random four-character reward code, and `starting` stage.
2. Ask IncogNoteBot for a unique link using `/start`, then `/link`. Expect a URL shaped like `https://t.me/IncogNoteBot?start=<token>`. The request is made through the receiver account, with per-account serialization.
3. Send the recovery announcement to the configured announcement channel using the recovery template. Save its message ID and move to `waiting_submission`.
4. Incoming IncogNoteBot messages are considered submissions only if the message has a button whose label contains “block”. The service extracts bot usernames from the message and associates it with a pending recovery for that receiving account. It requires exactly one valid replacement handle; if a single recovery is pending but no valid handle is present, it replies with the expected naming pattern. Ambiguous matches are ignored and logged.
5. Verify the candidate by resolving it as a bot and sending `/start` through the receiving account. Save candidate handle(s) and the IncogNoteBot message ID; set `new_handle`. Move to `waiting_main` for direct-to-main recovery or `waiting_pool` for pool recovery. Reply to the anonymous message telling the submitter to transfer ownership to the receiving account.
6. Wait for a BotFather message indicating ownership transfer and containing a candidate handle:
   - **Direct to main:** the main account sees the ownership message, verifies it uniquely matches a saved candidate, and begins finalization.
   - **Through a pool:** the pool account sees the ownership message while at `waiting_pool`. It moves to `transferring`, opens the replacement as a bot through main (`/start`), and transfers ownership from pool to main. It then moves to `waiting_main`. Main's BotFather ownership message starts finalization.
7. When a main-account ownership message uniquely matches a candidate at `transferring` or `waiting_main`, record the candidate and move to `finalizing`.

Candidate message IDs are retained so the eventual thank-you can reply to the original anonymous submission. More than one candidate can be remembered for a recovery; ownership messages must uniquely match one of them.

### Retained anonymous link and spare-bot donations

When `anonymous_submissions.retain_link_after_recovery` is false (the default), retain the current behavior: after the last active recovery using a receiver finishes, revoke its IncogNote link. If retention is turned off later, revoke saved links at startup unless a manual recovery is actively using one. When true, do not revoke it. Persist the active link by receiving-account username and reuse it for later recovery announcements instead of asking IncogNoteBot for a fresh link each time. Automatic recoveries should create a link at finalization if one does not exist yet. After restoration, post `anonymous_submissions.donation_template` to the announcement channel, rendered with `{friendly_name}`, `{new_handle}`, `{old_handle}`, `{handle_prefix}`, and `{anonymous_link}`, so the retained link remains available to users.

With retention enabled, an IncogNoteBot submission that does not match an active recovery can donate a spare bot. Accept exactly one username matching one configured mapping's `<handle_prefix><digits>bot` pattern, resolve it as a bot and send `/start` through the receiving account, and reject the configured current/live handle. Persist the donation before replying with the receiving account to which ownership must be transferred. If the link belongs to a pool account, transfer the donated bot from that pool account to main after the donor's transfer completes. Save it in the main-owned backup queue under its configured prefix only after main ownership is confirmed; reply to the original anonymous message with a receipt where possible.

Pending donations and their receiver, message ID, and transfer stage must survive restart. On startup, check main and receiver ownership to complete interrupted donations or resume pool-to-main transfers. Persist Mini App-to-chat fallback and BotFather cooldowns for donation transfers just like other ownership transfers. During an active recovery, a valid submission for that recovery's prefix remains a recovery candidate; a different configured prefix may be submitted as a spare donation.

Donated bots join the same FIFO backup queue used by automatic recovery. The oldest main-owned spare is claimed first. A detected bot failure should use an available saved spare even when `automatic_recovery.enabled` is false; with no spare, follow the configured manual/automatic recovery mode. Donated backups can accumulate; automatic recovery should use an available backup even if the creator account is offline, and only require the creator when no spare is available. If `prepare_bots` is enabled, replenish after consuming a spare only when the queue is empty.

## Automatic creation and prepared backups

Automatic mode first claims an available main-owned backup, including donated backups. It only requires a connected creator account when it must create a bot because the backup queue is empty. If the creator is not configured or available and no spare exists, it switches to manual recovery. A newly created bot username is generated as the configured prefix plus a random 2-, 3-, or 4-digit number and `bot`; its BotFather title is the mapping's friendly name. Unavailable usernames are retried with a new number.

Bot creation/transfer uses the BotFather Mini App where possible. Each new restoration starts with the Mini App, even when a previous BotFather chat cooldown is still active. A Mini App rate limit switches that operation to BotFather chat for its remaining work. If chat is also rate limited, pause until its cooldown ends and resume that same operation in chat. Do not persist a global creator chat-mode flag across independent operations. The Mini App authenticates using Telegram WebView init data, persists its auth cookies and API hash beside the creator session, and checks the cached session against the Telegram account ID before reuse. Chat transfer can require the optional creator 2FA password.

Creation operations on the creator account are spaced at least 300 seconds apart. The next allowed time and unfinished created handles are persisted. If the required wait is more than 300 seconds, automatic recovery raises an automatic-wait condition and switches to manual recovery before claiming a replacement. Telegram flood waits also trigger manual fallback if no replacement has been claimed. Once `new_handle` is claimed, those conditions do not switch modes.

BotFather “too many attempts” chat limits are persisted as a roughly 24-hour cooldown, per recovery or globally for prepared-bot operations. A recovery keeps its chat-mode fallback while that recovery is active; a pending creator operation keeps its fallback until that bot is prepared or the operation is otherwise resolved. The 30-second cooldown monitor resumes due transfers/recoveries or prepared-bot work in the same mode. An independent restoration starts with the Mini App again, even during a saved chat cooldown. Other automatic errors remain persisted at their current stage and are logged; generic errors are not periodically retried by the cooldown monitor.

With both `automatic_recovery.enabled` and `prepare_bots` true, the service tries to maintain one already-created and already-transferred backup for each configured mapping:

- On startup, leave saved main-owned backups in queue order. Inspect only an interrupted `pending_creations` handle: clear stale pending state if that same handle is already queued; if main owns it, move it into the queue; if the creator still owns it and another spare is queued, leave it pending without retrying the transfer. Resume its creator transfer only when the queue is empty and preparation is enabled.
- Missing backups are created and transferred from creator to main, then added to the persisted backup list.
- Automatic recovery claims the oldest prepared handle; if none exists, it creates one. After recovery it tries to replenish the spare.
- A crash during creation/transfer can reuse `pending_creations` instead of starting a new bot.

Automatic recovery skips the anonymous submission and human ownership-transfer stages. It claims a replacement, opens/configures the dialog, and runs the same restoration/finalization work as manual recovery. Backups are owned by main before they are claimed.

The reward code/100-credit thank-you is meaningful only when an anonymous submission exists. Fresh automatic recoveries have no reward code or submission message; finalization currently writes an empty `recovery.reward_code` value into the deployed config and skips the thank-you reply. Automatic recoveries that fall back to manual also retain the empty code in the current implementation.

## BotFather protocol details (undocumented Mini App and chat flows)

The Mini App API below is an internal Telegram web app interface, not a stable public API. The rewrite needs to reproduce the current wire behavior if it keeps this integration; isolate it behind a small adapter and expect the page/auth response format to change.

### Mini App authentication and HTTP requests

- Base page: `https://webappinternal.telegram.org/botfather`.
- The user client asks Telegram for BotFather's WebView URL with `messages.RequestMainWebViewRequest`, using BotFather as both peer and bot and `platform="tdesktop"`.
- Extract `tgWebAppData` from either the URL fragment or query. This is Telegram WebView `initData`; do not log it.
- GET the base page with a cookie jar. The page is expected to expose an `apiUrl` of the form `/api?hash=<letters-digits-underscore-hyphen>`. The current parser tolerates escaped slashes in the HTML and uses the `hash` as the API hash.
- POST URL-encoded forms to `https://webappinternal.telegram.org/botfather/api?hash=<api_hash>`, retaining cookies across page and API requests. Auth form fields are `method=auth` and `_auth=<tgWebAppData>`.
- Authentication response must be a JSON object without an `error`, contain `api_url` whose path ends in `/api` and whose query has a valid `hash`, and set a non-expired `stel_bot_father_token` cookie. The returned hash replaces the page hash for subsequent API requests.
- Default HTTP headers used by the current helper: `User-Agent: Mozilla/5.0`; `Accept: application/json, text/javascript, */*; q=0.01`; `Accept-Language: en-GB,en;q=0.9,en-US;q=0.8`. Form requests additionally send form-urlencoded `Content-Type`, `Origin: https://webappinternal.telegram.org`, `X-Requested-With: XMLHttpRequest`, and `Priority: u=1, i`. Use `/create` as the `Referer` for `createBot`, `/bot/<bid>/transfer` for `requestTransferBot`, and the base page for auth and other methods.
- Treat an API `error` value equal to `Error` as BotFather rate limiting before applying authentication-expiry checks. A redirect to `/auth` alone does not establish expiry; for other responses, HTTP 401/403 or body text explicitly indicating expired/invalid auth, token, hash, or session means authentication should be refreshed. The current helper also refreshes once on the Mini App's `Bad request` error, then retries the API call.
- An API response must be a 2xx JSON object. An `error` value equal to `Error` switches the current recovery or pending creator operation to chat mode. `Bad request` triggers the one auth refresh/retry described above. Other `error` values fail the operation. Transport failures fail the operation.
- Persist cookies and API hash per Telegram session, alongside the session file as `<session filename>.botfather.cookies.txt` and `<session filename>.botfather.json`. Cache JSON includes `api_hash` and Telegram `user_id`; when reusing it, compare the current account ID and reauthenticate if it changed. Clear `stel_ssid` and `stel_bot_father_token` before a fresh auth attempt. Use private file permissions. Do not log request cookies, auth form contents, or complete response bodies.

### Mini App operations

**Create bot:** POST form fields `method=createBot`, `title=<friendly_name>`, `about=`, `username=<generated_handle_without_at>`, and `userpic=`. Success requires JSON `ok: true` and a positive integer `bot_id` (a boolean is not a valid integer ID). If BotFather reports a username taken/exists/not available/unavailable/occupied, generate another handle and retry. Other errors fail creation.

**Request ownership transfer:** resolve the bot and recipient through Telegram first; the bot must be a bot and recipient must be a user. POST form fields `method=requestTransferBot`, `bid=<bot numeric ID>`, and `recipient_id=<recipient numeric user ID>`. A successful 2xx JSON response without `error` means the request was accepted, but does not mean ownership transfer is complete. Before POST, listen for a new BotFather message or edit matching the requested bot and recipient and containing a `Yes, I am sure` button. Then click that button and validate the final response (chat sequence below).

### BotFather chat creation

Use an exclusive BotFather conversation with a 90-second conversation timeout. Send the following in order:

1. `/newbot`. The next response must contain `name` (case-insensitive); otherwise fail.
2. The friendly name/title. The next response must contain `username`; otherwise fail.
3. The generated username, without `@`.
4. Treat response text containing `already taken`, `already occupied`, `not available`, or `unavailable` as a name collision and retry another generated username. Text containing `error`, `invalid`, `failed`, or `sorry` is a rejection. Accept success only if the response contains a bot token matching `\b\d{5,}:[A-Za-z0-9_-]{20,}\b` or contains the requested username. Never persist or log the returned token; this path only uses it as evidence BotFather created the bot.

At each prompt, text containing `too many attempts` means a BotFather chat rate limit. The generic interactive helper listens for a new message or edit from BotFather after the prior message ID, waits up to 60 seconds, and accepts the response if it has the expected button/text or if that step is configured to accept any response. It waits one second after sending/clicking before waiting for the update. Remove temporary handlers in `finally`.

### BotFather chat ownership transfer

The fallback transfer uses an exclusive BotFather conversation (90 seconds) and requires an existing BotFather chat message as a baseline. Send/click in this exact sequence:

1. Send `/mybots`; accept the next response.
2. Click the button whose label contains the bot handle (case-insensitive, without leading `@`). The resulting message must offer a button containing `Transfer Ownership`.
3. Click `Transfer Ownership`. The resulting message must offer `Choose recipient`.
4. Click `Choose recipient`; accept the next response.
5. Send the receiving account's normalized username, including `@`. The resulting message must offer `Yes, I am sure`.
6. Click `Yes, I am sure`, passing the optional 2FA password if the button marks `requires_password`.
7. Final text is a failure if it contains `error`, `failed`, `invalid`, or `sorry`. Otherwise require at least one of `transfer`, `ownership`, `owned`, `success`, `done`, `worked`, or `new home` as confirmation.

For Mini App initiated transfer, install BotFather new-message and message-edited listeners before issuing `requestTransferBot`. A confirmation is a message newer than the saved baseline whose text contains both the bot handle and recipient username (case-insensitive, without `@`) and whose buttons include `Yes, I am sure`. Search the ten most recent BotFather messages immediately after the request before waiting up to 60 seconds for the event. Click the confirmation's `yes, i am sure` button. The click response is expected to contain one of `worked`, `new home`, `error`, `failed`, `invalid`, or `sorry`; then apply the same final success/failure text validation above. A Mini App rate-limit response switches to the chat transfer sequence.

Button matching is substring, case-insensitive. If the button requires a password and none is configured, fail with an explicit missing-creator-2FA error. Retry button click `FloodWaitError` after the indicated number of seconds. A click must return an acknowledgement; callback text containing `too many attempts` triggers the 24-hour BotFather cooldown. The event-driven service recognizes completed ownership messages only when the sender is BotFather and message text contains `ownership of the bot`; it then matches usernames against the saved candidate list.

### BotFather commands for snapshot, restore, and ownership lookup

These are separate BotFather chat operations. The current helper generally sends `/cancel` first and consumes the response to clear any unfinished interaction.

- **Read a source bot's command list:** `/cancel` → `/token` → source username. Extract the token using `\b\d{5,}:[A-Za-z0-9_-]{20,}\b`, start a temporary bot client with it, and call Telegram `bots.GetBotCommands` for `BotCommandScopeDefault`, empty `lang_code`. Disconnect the temporary bot client afterward. Do not save the source token.
- **Set bot name/about/description:** for each operation separately, send `/cancel`, then one of `/setname`, `/setabouttext`, `/setdescription`; consume the response after each command; send target bot username and consume the target prompt; send the corresponding value (normalize line endings; send `/empty` for empty text); consume the result. Any result containing `error` is failure.
- **Set default commands:** send `/cancel`, `/setcommands`, target username, then one line per command as `<command> - <description>`, or `/empty` for an empty command list. Any result containing `error` is failure.
- **Regenerate token:** send `/cancel`, `/revoke`, target username. Extract and return the matching token from the response; missing token is failure. This is done after profile/photo/commands are restored.
- **Check bots owned by an account:** send `/cancel`, `/mybots`; inspect button labels for bot usernames. This is used to detect whether an interrupted creator transfer already made the bot visible under main ownership.

These prompt flows use exclusive conversations with 45-second timeouts. Most helper calls consume one response after each send but do not validate exact prompt text; only the new-bot creation and ownership flows perform the explicit phrase/button checks listed above. Do not assume prompts are stable or localized beyond the current English substring checks.

## Settings snapshot and restoration

The first snapshot is created from the configured current bot and stored at:

`<snapshot_dir>/<lowercase handle_prefix>/snapshot.json`

It contains source handle, bot name, about text, description, default command list, and optional profile photo filename. Commands are read by obtaining the source token through BotFather and using a temporary bot client. The token itself is not written to the snapshot. Snapshot metadata and photo files are written with restrictive permissions where supported.

When a replacement is ready, finalization is checkpointed through these ordered stages:

1. **Restore bot settings (`finalizing_restore`):** use BotFather to set name, about, description, profile photo if present, and default commands from the snapshot. Replace references to old matching handles in text/commands. Revoke/regenerate the replacement token.
2. Write the new token to the configured bot's `token_path` dotted YAML key. Write the reward code to `recovery.reward_code`. Restart the configured process with `restart_command`, then wait ten seconds.
3. **Runtime about (`finalizing_about`):** read `/about` from the restored bot. If its response contains old handles matching this prefix, replace them, issue `/about <updated text>`, then `/reload`.
4. **Service config (`finalizing_config`):** update this recovery service's own matching `bots[].handle` to the new username and update in-memory config.
5. **Telegram descriptions (`finalizing_descriptions`):** replace old matching handles in the announcement channel and group descriptions.
6. **Public page (`finalizing_publish`):** fetch the current Cloudflare Pages index from `pages_url`, update its first `Updated: ...` text to today's date and the restored friendly name, replace matching old handles, then deploy the generated `index.html` with `npx wrangler pages deploy`, `project_name`, and `.env`.
7. **Restored announcement (`finalizing_announce`):** send the restored template to the announcement channel after replacing references to matching old handles. If link retention is enabled, ensure a reusable IncogNote link exists and also post the configured donation template with that link.
8. **Submitter thank-you (`finalizing_cleanup`):** reply through the receiving account to the saved anonymous submission message, state the restored bot and that 100 credits are awarded after sending the reward code in that bot.
9. **Delete recovery announcement (`finalizing_delete_announcement`):** delete the original recovery announcement; an already-invalid message ID is tolerated.
10. **Revoke and finish (`finalizing_revoke`):** if this receiver has no other active recoveries and the record has a link, either retain it in `anonymous_links` when `anonymous_submissions.retain_link_after_recovery` is true, or revoke it through IncogNoteBot by sending `/change` then `Yes, I am sure`. Remove the recovery record from state.

The ordering and checkpoint names are part of the restart behavior. A restart resumes active stages from their saved checkpoint. Operations between a side effect and its next checkpoint may repeat after a crash; some actions (for example announcement send or process restart) are not inherently idempotent.

## Handle replacement rules

References are replaced only when they look like the mapping's prefix followed by one or more digits and `bot`, case-insensitively. The `@` is optional and preserved when present. This rule is applied to snapshot text/commands, runtime about text, chat descriptions, restored announcement text, and the published HTML. The service config's `handle` is set directly to the replacement handle. The matching rule intentionally does not rewrite arbitrary usernames or handles belonging to other mappings.

## State and concurrency

The JSON state file contains active recoveries (including stage, handles, receiver, anonymous link, relevant Telegram message IDs, candidate handles/message IDs, reward code, per-recovery BotFather mode/cooldown), reusable anonymous links by receiver, pending backup donations, prepared backup handles, pending creator handles and their per-operation BotFather mode, and creator pacing/cooldown timestamps. Ignore the legacy global `creator_botfather_chat_mode` field so each new operation tries the Mini App first. Older state using `pool_username` is migrated to `receiver_username`. State writes use a temporary file then replace the target and attempt mode `0600`.

The implementation serializes shared state; BotFather operations per account; anonymous-link operations per account; finalization per recovery; config writes; chat description edits; and Pages publishes. The rewrite should retain equivalent protection where concurrent incoming messages or multiple mappings can overlap.

## Operational and security requirements

- Use Telethon user sessions for main, pools, and optional creator. Startup verifies each session's actual username matches config.
- Use the common Telegram API ID/hash for main and pools; creator has its own API ID/hash.
- Keep session files, config (example instructs `0600`), state, Mini App cookies/cache, bot deployment config, and generated tokens private. State deliberately excludes bot tokens.
- Recovery configuration and snapshots are local files. Cloudflare deploy requires Wrangler to be installed/authenticated separately and `.env` to be available to the deploy command.
- Log one concise line when each Mini App request completes: operation, HTTP method and path, status, elapsed time, and a short sanitized response summary when useful. Connection failures should include the operation and exception type. Never log init data, API hashes, cookies, bot tokens, or raw request/response bodies.
- Shutdown/disconnect handling should cancel the recurring monitor tasks cleanly.

## Current-behavior caveats for the rewrite agent

- Periodic monitoring is deletion-focused; blocked status and `/about` responsiveness are checked only at startup.
- Recovery and prepared-backup state is durable, but there is no general scheduled retry for arbitrary failed operations. BotFather cooldowns have a specific retry loop; most other failures wait for a restart or a later external event.
- Announcement and finalization checkpoints are saved after external effects, so a crash can duplicate an announcement or other side effect. Design deliberate idempotency for a rewrite.
- Pool selection does not fall back to main if a configured pool exists but all pools are offline.
- `prepare_bots: true` with `enabled: false` loads/starts the creator but does not run backup preparation.
- Fresh automatic recoveries and their manual fallbacks do not generate a reward code, even though finalization writes the field to the deployed bot config.
- Bot mappings reject duplicate prefixes but do not reject overlapping prefixes; enforce unambiguous mapping in a rewrite.
- `/about` startup probe failures are treated as recovery-worthy; transient network/RPC failures in the recurring deletion check are ignored.
- BotFather chat “too many attempts” pauses work for about 24 hours. The app rate-limit fallback and chat cooldown are separate behaviors.
