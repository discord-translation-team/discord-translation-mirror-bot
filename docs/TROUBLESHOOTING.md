# Discord Translation Bot - Troubleshooting

## /setup_server says "I need Manage Channels"

Fix:

- Give the bot Manage Channels in Server Settings -> Roles.
- Check category and channel overrides do not deny the bot.

## Bot does not assign language roles

Fix:

- Give the bot Manage Roles.
- Move the bot role above all language roles.
- Run `/language_role_list`.
- Run `/setup_check`.

## Bot cannot send messages in translation channels

Fix:

For every translation channel, the bot must have:

- View Channel
- Send Messages
- Embed Links
- Read Message History

## User cannot see their translation channel

Fix:

- User must select a language in `#choose-language`.
- Check that the matching role was assigned.
- Check channel permissions:
  - `@everyone` -> View Channel denied
  - language role -> View Channel allowed
  - bot -> View Channel, Send Messages, Embed Links, and Read Message History allowed

## Dropdown does not appear

Fix:

- Run `/language_setup_message channel:#choose-language`.
- Bot needs Send Messages and Embed Links in `#choose-language`.
- Run `/setup_check`.

## Language is missing from dropdown

Fix:

- Language must be in the supported list.
- Translation channel must be configured.
- Run `/translation_channel_list`.
- Refresh setup message with `/language_setup_message`.

## Unsupported language code ENG / EG / UA

Fix:

- Remove bad mappings:

```text
/translation_channel_remove target_language:eng
/translation_channel_remove target_language:eg
/translation_channel_remove target_language:ua
```

- Use:
  - `en` for English
  - `ar` for Arabic
  - `uk` for Ukrainian
  - `ro` for Romanian

## Reacting with the translation emoji does nothing

Fix:

- Bot must see the source channel.
- Bot needs Read Message History.
- User must have selected a language.
- Translation channel and role must be configured.
- Run `/setup_check`.

## Duplicate setup messages

Fix:

- New versions track the setup message.
- Run `/language_setup_message channel:#choose-language` to refresh it.
- Delete old manual duplicate messages if needed.

## Welcome message is not sent

Fix:

- Enable `SERVER MEMBERS INTENT` in Discord Developer Portal.
- Run `/welcome status`.
- Check that welcome is enabled.
- Give the bot View Channel, Send Messages, Embed Links, and Attach Files in the welcome channel.
- If a configured channel was deleted, run `/welcome setup` again.

## Welcome setup rejects the image

- Upload PNG, JPEG, WEBP, or GIF.
- Keep the file at or below 5 MB.
- Use an image at least 400×100 pixels. The bot crops it to a compact wide static banner with rounded corners.

## Welcome avatar is missing

- The bot first uses the member's Discord display avatar.
- If Discord avatar download temporarily fails, the bot uses a generated fallback and still sends the welcome message.
- Run `/welcome preview` to verify the current banner renderer.

## Change welcome text without starting over

- Run `/welcome edit` to preserve the channels, banner, button destination, and enabled state.
- Run `/welcome banner` when only the image must change.

## Welcome color is rejected

- Use six-digit HEX format, for example `/welcome color color:#A020F0`.
- The same color is used for the banner border and the Discord embed accent.
- Existing settings are preserved when an invalid color is rejected.

## Channel name in welcome text is not clickable

- Plain text such as `#rules` is not enough because it does not contain a channel ID.
- Insert a channel mention in the form `<#123456789012345678>` or paste the URL from Discord `Copy Channel Link`.
- The bot converts a valid channel of the current server into a clickable `#channel-name` link.
- Run `/welcome status` to find deleted channels, malformed references, or links to another server.
- A link does not bypass Discord permissions: members can open only channels they are allowed to view.

## Reminder does not send

- Confirm the date and time are UTC and `/reminder list` shows a future next send.
- Give the bot `View Channel` and `Send Messages` in the selected channel.
- For `everyone` or `here`, also give the bot `Mention Everyone`.
- Check Railway logs for `reminder_send_failed`; reconnects and restarts are retried by the scheduler.

If `/reminder edit` asks for a numeric ID after autocomplete, confirm the bot is running the current release and reselect the reminder from the refreshed list. The selected autocomplete entry should be accepted directly. Standard and custom Discord emoji can be used in both reminder title and message; a deleted or inaccessible custom emoji may not render for Discord users.

## Monthly reminder did not run

A monthly reminder for day 29, 30, or 31 is skipped when that day does not exist in the month. It is not moved to the last day.

## Cleanup does not remove messages

- Confirm the channel appears in `/cleanup list`.
- Run `/cleanup list` and verify the configured `HH:MM UTC` time; `00:00` is only the default.
- Give the bot `View Channel`, `Read Message History`, and `Manage Messages`.
- Pinned messages are intentionally preserved, and no completion message is posted.
