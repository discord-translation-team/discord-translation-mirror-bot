# Discord Translation Mirror Bot

An MVP Discord bot that relays messages from configured source channels into language-specific mirror channels. It supports a local mock provider for development plus OpenAI and Gemini providers for real AI-style translation.

Example:

```text
[ru] Hello everyone

——
🌐 Translated to: ru
🔗 Original: <https://discord.com/channels/{guild_id}/{source_channel_id}/{message_id}>
```

## Setup

Create a virtual environment with Python 3.11 or newer, then install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create your local environment file:

```bash
copy .env.example .env
```

Set `DISCORD_BOT_TOKEN` in `.env`.

For mock local development, keep:

```env
TRANSLATION_PROVIDER=mock
```

For OpenAI translation:

1. Create an OpenAI API key.
2. Add `OPENAI_API_KEY` to `.env`.
3. Set `TRANSLATION_PROVIDER=openai`.
4. Set `OPENAI_TRANSLATION_MODEL=gpt-5.4-mini`.
5. Restart the bot.

```env
TRANSLATION_PROVIDER=openai
OPENAI_API_KEY=your_api_key_here
OPENAI_TRANSLATION_MODEL=gpt-5.4-mini
OPENAI_TRANSLATION_QUALITY_MODEL=gpt-5.4-mini
MAX_MESSAGE_CHARS=1500
SKIP_MESSAGES_OVER_LIMIT=true
```

For Gemini translation:

1. Create a Gemini API key in Google AI Studio.
2. Add `GEMINI_API_KEY` to `.env`.
3. Set `TRANSLATION_PROVIDER=gemini`.
4. Set `GEMINI_TRANSLATION_MODEL=gemini-2.5-flash-lite`.
5. Restart the bot.

```env
TRANSLATION_PROVIDER=gemini
GEMINI_API_KEY=your_api_key_here
GEMINI_TRANSLATION_MODEL=gemini-2.5-flash-lite
MAX_MESSAGE_CHARS=1500
SKIP_MESSAGES_OVER_LIMIT=true
```

## Run Locally

```bash
python -m app.bot
```

On startup the bot creates local SQLite tables when using the default `DATABASE_URL` and syncs slash commands.

## Discord Invite Permissions

In the Discord Developer Portal:

1. Enable the `MESSAGE CONTENT INTENT` for the bot.
2. Generate an OAuth2 invite URL with scopes `bot` and `applications.commands`.
3. Give the bot these permissions:
   - View Channels
   - Read Message History
   - Send Messages
   - Manage Webhooks
   - Use Slash Commands

## Slash Commands

- `/translate_setup source_channel target_channel target_language`
  Creates or updates a route and creates or reuses a webhook in the target channel.
- `/translate_list`
  Lists active routes for the current server.
- `/translate_remove source_channel target_language`
  Disables the matching active route.
- `/translate_status`
  Shows provider, model, active route count, monthly token counts, monthly character count, and database status.
- `/translate_test text target_language`
  Returns a translated preview using the currently selected provider without saving anything.

## MVP Limitations

- Supported providers are `mock`, `openai`, and `gemini`.
- DeepL remains a placeholder for future work.
- Message edits and deletes are not mirrored yet.
- Attachments, embeds, stickers, and replies are not mirrored yet.
- Messages over `MAX_MESSAGE_CHARS` are skipped when `SKIP_MESSAGES_OVER_LIMIT=true`.
- Translation cache keys include source text hash, target language, provider, and model.
- Webhook messages use `allowed_mentions` with no parsing so mirrored `@everyone`, `@here`, user, and role mentions do not ping.
- Translated text is also sanitized so mention tokens are visually broken before sending.
- Logs include IDs and operational events, but not full user message content.
