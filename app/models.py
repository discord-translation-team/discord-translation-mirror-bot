from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ChannelRoute(TimestampMixin, Base):
    __tablename__ = "channel_routes"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "source_channel_id",
            "target_channel_id",
            "target_language",
            name="uq_route_target_language",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_channel_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    target_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    webhook_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    webhook_token: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class MessageMapping(TimestampMixin, Base):
    __tablename__ = "message_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    original_message_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    original_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    translated_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    original_message_url: Mapped[str] = mapped_column(String(512), nullable=False)


class UserLanguageSetting(TimestampMixin, Base):
    __tablename__ = "user_language_settings"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_user_language_setting"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)


class TranslationChannelSetting(TimestampMixin, Base):
    __tablename__ = "translation_channel_settings"
    __table_args__ = (
        UniqueConstraint("guild_id", "target_language", name="uq_translation_channel_setting"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LanguageRoleSetting(TimestampMixin, Base):
    __tablename__ = "language_role_settings"
    __table_args__ = (
        UniqueConstraint("guild_id", "target_language", name="uq_language_role_setting"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LanguageSetupMessage(TimestampMixin, Base):
    __tablename__ = "language_setup_messages"
    __table_args__ = (
        UniqueConstraint("guild_id", name="uq_language_setup_message_guild"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class OnDemandTranslationMapping(TimestampMixin, Base):
    __tablename__ = "on_demand_translation_mappings"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "original_message_id",
            "target_language",
            name="uq_on_demand_translation_mapping_unique_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    original_message_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    original_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    target_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    translated_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_message_url: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class TranslationCache(Base):
    __tablename__ = "translation_cache"
    __table_args__ = (
        UniqueConstraint(
            "source_text_hash",
            "target_language",
            "provider",
            "model",
            name="uq_translation_cache_provider_model",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_text_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="mock", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="mock", nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class GuildUsageMonthly(TimestampMixin, Base):
    __tablename__ = "guild_usage_monthly"
    __table_args__ = (
        UniqueConstraint("guild_id", "month", "provider", "model", name="uq_guild_usage_month_provider_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="mock", nullable=False)
    characters_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_limit: Mapped[int] = mapped_column(Integer, nullable=False)
