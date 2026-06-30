# Privacy

Discord Translation Mirror Bot processes messages only in source channels that server admins configure with `/translate_setup`.

By default, the bot does not log full message content. Operational logs may include Discord IDs such as guild IDs, channel IDs, message IDs, route IDs, and webhook IDs for debugging and auditability.

The mock translation provider transforms text locally into:

```text
[{target_language}] {text}
```

When the Gemini provider is enabled, text from configured source channels is sent to the Gemini API for translation. The bot does not log full message content by default.

When the OpenAI provider is enabled, text from configured source channels is sent to the OpenAI API for translation. The bot does not log full message content by default.

Future real translation providers may receive message text for translation. Before enabling another real provider, server operators should review that provider's privacy policy and update users about where message text may be sent.
