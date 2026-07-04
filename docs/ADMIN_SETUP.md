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
