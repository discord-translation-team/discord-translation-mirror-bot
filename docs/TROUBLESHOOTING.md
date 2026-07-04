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
