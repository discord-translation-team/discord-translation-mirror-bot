# Discord Translation Mirror Bot

An MVP Discord bot that translates Discord messages on demand into configured language-specific translation channels. It supports a local mock provider for development plus OpenAI and Gemini providers for real AI-style translation. Legacy always-on mirror mode is still present, but disabled by default.

Example on-demand flow:

```text
Admin: /translation_channel_set ru #ru-translation
User:  /set_language ru
User reacts 🌐 to a message in #global-chat

Bot posts in #ru-translation:
Привет всем

[Original](https://discord.com/channels/{guild_id}/{source_channel_id}/{message_id})
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

The default product mode is on-demand channel translation:

```env
ON_DEMAND_CHANNEL_TRANSLATION_ENABLED=true
REACTION_TRANSLATION_ENABLED=true
CONTEXT_MENU_TRANSLATION_ENABLED=true
LEGACY_MIRROR_MODE_ENABLED=false
REACTION_TRANSLATE_EMOJI=🌐
```

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

## Railway PostgreSQL

For local development, `DATABASE_URL` can stay as:

```env
DATABASE_URL=sqlite+aiosqlite:///./bot.db
```

On Railway, attach a Railway PostgreSQL service and use the `DATABASE_URL` provided by Railway. If Railway provides a URL beginning with `postgresql://`, the bot converts it internally to `postgresql+asyncpg://` for SQLAlchemy async support. The bot never logs the full database URL or database password.

## Discord Invite Permissions

In the Discord Developer Portal:

1. Enable the `MESSAGE CONTENT INTENT` for the bot.
2. Generate an OAuth2 invite URL with scopes `bot` and `applications.commands`.
3. Give the bot these permissions:
   - View Channels
   - Read Message History
   - Send Messages
   - Add Reactions if feedback reactions are enabled later
   - Manage Webhooks only for legacy mirror mode
   - Use Slash Commands

## Slash Commands

- `/set_language target_language`
  Sets your personal target language for on-demand translation.
- `/my_language`
  Shows your configured target language.
- `/translation_channel_set target_language channel`
  Admin command that maps a language to a translation channel.
- `/translation_channel_list`
  Admin command that lists configured language channels.
- `/translation_channel_remove target_language`
  Admin command that removes a language channel mapping.
- `/translate_setup source_channel target_channel target_language`
  Legacy mirror command. Creates or updates a source-channel mirror route when `LEGACY_MIRROR_MODE_ENABLED=true`.
- `/translate_list`
  Lists active legacy mirror routes for the current server.
- `/translate_remove source_channel target_language`
  Disables matching legacy mirror routes.
- `/translate_status`
  Shows feature flags, provider, model, configured channel count, monthly token counts, monthly character count, and database status.
- `/translate_test text target_language`
  Returns a translated preview using the currently selected provider without saving anything.

## User Flow

1. An admin configures language channels:

```text
/translation_channel_set ru #ru-translation
/translation_channel_set en #en-translation
```

2. A user sets their language:

```text
/set_language ru
```

Admins can also create a persistent setup menu so users do not need to type `/set_language`:

```text
/language_setup_message #choose-language
```

Recommended setup flow:

1. Create translation channels such as `#ru-translation` and `#en-translation`.
2. Configure them:

```text
/translation_channel_set target_language:ru channel:#ru-translation
/translation_channel_set target_language:en channel:#en-translation
```

3. Create the user setup message:

```text
/language_setup_message channel:#choose-language
```

User flow:

1. Open `#choose-language`.
2. Select a language from the dropdown.
3. React with `🌐` to any message the bot can read.
4. The bot posts the translation to the configured channel for that language.
5. Users can also use the message context menu: Apps -> Translate.

## MVP Limitations

- Supported providers are `mock`, `openai`, and `gemini`.
- DeepL remains a placeholder for future work.
- Attachments, embeds, stickers, and replies are not translated yet.
- Messages over `MAX_MESSAGE_CHARS` are skipped when `SKIP_MESSAGES_OVER_LIMIT=true`.
- Translation cache keys include source text hash, target language, provider, and model.
- Translated text is sanitized so mention tokens are visually broken before sending.
- Translated posts use `allowed_mentions` with no parsing so `@everyone`, `@here`, user, and role mentions do not ping.
- Logs include IDs and operational events, but not full user message content.
