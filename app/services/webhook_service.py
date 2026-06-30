from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


class WebhookService:
    WEBHOOK_NAME = "Translation Mirror Bot"

    async def create_or_reuse(self, channel: discord.TextChannel) -> discord.Webhook:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            if webhook.name == self.WEBHOOK_NAME and webhook.token:
                return webhook

        webhook = await channel.create_webhook(
            name=self.WEBHOOK_NAME,
            reason="Translation mirror route setup",
        )
        logger.info(
            "created_translation_webhook",
            extra={"guild_id": channel.guild.id, "target_channel_id": channel.id, "webhook_id": webhook.id},
        )
        return webhook

    async def get_for_route(
        self,
        channel: discord.TextChannel,
        webhook_id: int,
    ) -> discord.Webhook | None:
        for webhook in await channel.webhooks():
            if webhook.id == webhook_id and webhook.token:
                return webhook
        return None

