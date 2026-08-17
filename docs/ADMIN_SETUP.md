# Discord Translation Bot - Admin Setup Guide

## What the bot does

- Users choose their translation language in `#choose-language`.
- Users react with the configured translation emoji to messages.
- The bot posts translation output to the user's language channel.
- Users only see their own translation channel when roles and permissions are configured correctly.

## Required bot permissions

Required:

- Manage Channels
- Manage Roles
- View Channels
- Send Messages
- Embed Links
- Read Message History
- Add Reactions
- Use Application Commands

Important:

- The bot role must be above all language roles in Server Settings -> Roles.
- Discord does not allow a bot to assign roles above its own top role.

## Quick setup

Recommended command:

```text
/setup_server languages:ru,en,fr,ar,tr,es,uk,ro source_channel:#general
```

`source_channel` is optional. If omitted, the bot creates or reuses `#global-chat`.

The bot can translate from any visible channel where it can read message history and receive reaction events.

## After /setup_server

Run:

```text
/setup_check
```

If `/setup_check` warns about role hierarchy:

- Go to Server Settings -> Roles.
- Move the bot role above `lang-*` roles or language flag roles.

## User flow

1. User opens `#choose-language`.
2. User selects a language from the dropdown.
3. Bot assigns the matching language role.
4. User reacts with the configured translation emoji to a message.
5. Translation appears in their language channel.

## Adding a new language later

Example for Romanian:

```text
/setup_server languages:ru,en,fr,ar,tr,es,uk,ro source_channel:#general
```

Or manual setup:

1. Create role:
   `lang-ro`
2. Create channel:
   `#ro-translation`
3. Configure mappings:

```text
/translation_channel_set target_language:ro channel:#ro-translation
/language_role_set target_language:ro role:@lang-ro
```

4. Refresh setup message:

```text
/language_setup_message channel:#choose-language
```

5. Run:

```text
/setup_check
```

## Supported language codes

| Code | Language |
| --- | --- |
| en | English |
| ru | Russian |
| es | Spanish |
| fr | French |
| ar | Arabic |
| tr | Turkish |
| uk | Ukrainian |
| de | German |
| pt | Portuguese |
| it | Italian |
| zh | Chinese |
| ja | Japanese |
| ko | Korean |
| pl | Polish |
| hi | Hindi |
| bn | Bengali |
| id | Indonesian |
| vi | Vietnamese |
| nl | Dutch |
| fa | Persian |
| ro | Romanian |

Warnings:

- Use `en`, not `eng`.
- Use `ar`, not `eg`.
- Use `uk`, not `ua`.
- Use `ro` for Romanian.

## Maintenance commands

- `/setup_check`
- `/setup_cleanup`
- `/translation_channel_list`
- `/language_role_list`
- `/translate_status`

## Welcome setup

1. Enable `SERVER MEMBERS INTENT` for the bot in Discord Developer Portal.
2. Give the bot `View Channel`, `Send Messages`, `Embed Links`, and `Attach Files` in the welcome channel.
3. Run `/welcome setup` and provide:
   - the welcome channel;
   - an image up to 5 MB;
   - whether the language button is enabled;
   - the language channel when the button is enabled.
4. Enter the welcome text in the form. Use `{user}` for the new-member mention and `{server_name}` for the current server name.
5. For a clickable channel link, insert `<#channel_id>` or paste the value from Discord `Copy Channel Link`. The bot validates the channel and displays it as a `#channel-name` link. Plain text such as `#rules` is not a link.
6. Run `/welcome preview` and `/welcome status`.

The button is optional. If it is enabled without a selected channel, the bot responds with `Выберите языковой канал`.

Use `/welcome disable` to stop messages without deleting the setup and `/welcome enable` to resume them.

The uploaded image is automatically cropped to a compact wide banner with rounded corners and full-image shading. For every new member, the bot adds a circular avatar on the left, a large bold display name on the right, and the server name below it.

The welcome text and banner are displayed in one Discord embed. The button belongs to the same message and appears directly below the embed because Discord does not allow buttons inside an embed.

To update an existing setup:

- `/welcome edit` changes only the welcome text and button label;
- `/welcome banner` changes only the banner image;
- `/welcome color color:#A020F0` changes the shared banner-border and embed-accent color; use HEX format `#RRGGBB`;
- `/welcome preview` shows the same personalized rendering used for a real join.

The default color is Discord purple `#5865F2`.

## Reminder setup

Use `/reminder add` and provide `channel`, `repeats`, UTC `time`, `title`, and `message`.

- `date` is required only for `once` and uses `YYYY-MM-DD`.
- `weekday` is required only for `weekly`.
- `day_of_month` is required only for `monthly`; missing dates such as February 31 are skipped.
- `every` defaults to `1`; use `2` for every other day/week/month.
- `offset_minutes` defaults to `0` and accepts up to `10080`.
- `start_date` optionally anchors a repeating schedule.
- `mention_everyone_or_here` can be `none`, `everyone`, or `here`.

Use `/reminder list` to see IDs. `/reminder edit` and `/reminder remove` offer those records through autocomplete; removal requires confirmation. Reminder output is plain text, and an enabled mass mention appears only below the message body.

## Daily cleanup setup

Give the bot `View Channel`, `Read Message History`, and `Manage Messages`, then run `/cleanup add channel:#channel`. The bot deletes all unpinned messages in that selected channel daily at `00:00 UTC`. It keeps pinned messages and posts no notification.

Use `/cleanup list`, `/cleanup edit`, and `/cleanup remove` to manage the selected channels. Removal requires confirmation.
