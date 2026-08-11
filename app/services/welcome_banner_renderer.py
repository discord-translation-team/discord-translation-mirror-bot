from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


BANNER_SIZE = (1200, 300)
MAX_SOURCE_DIMENSION = 8_000
AVATAR_SIZE = 190
AVATAR_POSITION = (55, 55)
TEXT_LEFT = 300
TEXT_RIGHT = 1140
NAME_MAX_SIZE = 54
NAME_MIN_SIZE = 30
SERVER_MAX_SIZE = 32
SERVER_MIN_SIZE = 20


class WelcomeBannerError(ValueError):
    pass


class WelcomeBannerRenderer:
    def render(
        self,
        *,
        banner_bytes: bytes,
        avatar_bytes: bytes | None,
        display_name: str,
        server_name: str,
    ) -> bytes:
        banner = self._open_image(banner_bytes, "banner")
        if banner.width > MAX_SOURCE_DIMENSION or banner.height > MAX_SOURCE_DIMENSION:
            raise WelcomeBannerError("Banner dimensions are too large")

        canvas = ImageOps.fit(
            banner.convert("RGB"),
            BANNER_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        ).convert("RGBA")

        shade = Image.new("RGBA", BANNER_SIZE, (0, 0, 0, 0))
        shade_draw = ImageDraw.Draw(shade)
        shade_draw.rounded_rectangle((25, 30, 1175, 270), radius=34, fill=(0, 0, 0, 105))
        canvas = Image.alpha_composite(canvas, shade)

        avatar = self._avatar_image(avatar_bytes, display_name)
        avatar_mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(avatar_mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        canvas.paste(avatar, AVATAR_POSITION, avatar_mask)

        border_draw = ImageDraw.Draw(canvas)
        x, y = AVATAR_POSITION
        border_draw.ellipse(
            (x - 4, y - 4, x + AVATAR_SIZE + 4, y + AVATAR_SIZE + 4),
            outline=(255, 255, 255, 220),
            width=4,
        )

        draw = ImageDraw.Draw(canvas)
        max_width = TEXT_RIGHT - TEXT_LEFT
        name_text, name_font = self._fit_text(
            draw,
            display_name or "New member",
            max_width,
            NAME_MAX_SIZE,
            NAME_MIN_SIZE,
            bold=True,
        )
        server_text, server_font = self._fit_text(
            draw,
            server_name or "Discord server",
            max_width,
            SERVER_MAX_SIZE,
            SERVER_MIN_SIZE,
            bold=False,
        )

        name_box = draw.textbbox((0, 0), name_text, font=name_font, stroke_width=1)
        server_box = draw.textbbox((0, 0), server_text, font=server_font)
        name_height = name_box[3] - name_box[1]
        server_height = server_box[3] - server_box[1]
        spacing = 14
        block_height = name_height + spacing + server_height
        name_y = (BANNER_SIZE[1] - block_height) // 2 - name_box[1]
        server_y = name_y + name_height + spacing - server_box[1]

        draw.text(
            (TEXT_LEFT, name_y),
            name_text,
            font=name_font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 190),
        )
        draw.text(
            (TEXT_LEFT, server_y),
            server_text,
            font=server_font,
            fill=(225, 230, 238, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 170),
        )

        output = BytesIO()
        canvas.convert("RGB").save(output, format="PNG", optimize=True)
        return output.getvalue()

    def validate_banner(self, banner_bytes: bytes) -> None:
        image = self._open_image(banner_bytes, "banner")
        if image.width > MAX_SOURCE_DIMENSION or image.height > MAX_SOURCE_DIMENSION:
            raise WelcomeBannerError("Banner dimensions are too large")
        if image.width < 400 or image.height < 100:
            raise WelcomeBannerError("Banner image is too small")

    def _avatar_image(self, avatar_bytes: bytes | None, display_name: str) -> Image.Image:
        if avatar_bytes:
            try:
                avatar = self._open_image(avatar_bytes, "avatar")
                return ImageOps.fit(
                    avatar.convert("RGB"),
                    (AVATAR_SIZE, AVATAR_SIZE),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            except WelcomeBannerError:
                pass

        placeholder = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), (88, 101, 242))
        draw = ImageDraw.Draw(placeholder)
        initial = (display_name.strip()[:1] or "?").upper()
        font = self._font(80, bold=True)
        box = draw.textbbox((0, 0), initial, font=font)
        draw.text(
            ((AVATAR_SIZE - (box[2] - box[0])) / 2, (AVATAR_SIZE - (box[3] - box[1])) / 2 - box[1]),
            initial,
            font=font,
            fill="white",
        )
        return placeholder

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        value: str,
        max_width: int,
        max_size: int,
        min_size: int,
        *,
        bold: bool,
    ) -> tuple[str, ImageFont.ImageFont]:
        value = value.strip().replace("\n", " ") or "Unknown"
        for size in range(max_size, min_size - 1, -2):
            font = self._font(size, bold=bold)
            safe_value = self._font_safe_text(draw, value, font)
            if draw.textlength(safe_value, font=font) <= max_width:
                return safe_value, font

        font = self._font(min_size, bold=bold)
        safe_value = self._font_safe_text(draw, value, font)
        ellipsis = "…"
        while safe_value and draw.textlength(safe_value + ellipsis, font=font) > max_width:
            safe_value = safe_value[:-1]
        return (safe_value.rstrip() + ellipsis) if safe_value != value else safe_value, font

    @staticmethod
    def _font_safe_text(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.ImageFont) -> str:
        try:
            draw.textbbox((0, 0), value, font=font)
            return value
        except (UnicodeEncodeError, OSError):
            return value.encode("ascii", "replace").decode("ascii")

    @staticmethod
    def _font(size: int, *, bold: bool) -> ImageFont.ImageFont:
        candidates = (
            ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
            ("Arial Bold.ttf", "Arial.ttf"),
        )
        for bold_name, regular_name in candidates:
            try:
                return ImageFont.truetype(bold_name if bold else regular_name, size=size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    @staticmethod
    def _open_image(data: bytes, label: str) -> Image.Image:
        try:
            image = Image.open(BytesIO(data))
            image.load()
            return ImageOps.exif_transpose(image).copy()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise WelcomeBannerError(f"Invalid {label} image") from exc
