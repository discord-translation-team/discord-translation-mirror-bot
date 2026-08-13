from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


BANNER_SIZE = (1200, 420)
MAX_SOURCE_DIMENSION = 8_000
CORNER_RADIUS = 48
BACKGROUND_SHADE_ALPHA = 110
AVATAR_SIZE = 230
AVATAR_POSITION = (60, 95)
TEXT_LEFT = 350
TEXT_RIGHT = 1140
NAME_MAX_SIZE = 72
NAME_MIN_SIZE = 38
SERVER_MAX_SIZE = 42
SERVER_MIN_SIZE = 24
TEXT_SPACING = 30
BORDER_WIDTH = 8
TEXT_BOLDEN_OFFSETS = ((0, 0), (1, 0), (0, 1), (1, 1))


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
        accent_color: tuple[int, int, int] = (88, 101, 242),
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

        shade = Image.new("RGBA", BANNER_SIZE, (0, 0, 0, BACKGROUND_SHADE_ALPHA))
        canvas = Image.alpha_composite(canvas, shade)

        avatar = self._avatar_image(avatar_bytes, display_name)
        avatar_mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(avatar_mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        canvas.paste(avatar, AVATAR_POSITION, avatar_mask)

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
            bold=True,
        )

        name_box = draw.textbbox((0, 0), name_text, font=name_font)
        server_box = draw.textbbox((0, 0), server_text, font=server_font)
        name_height = name_box[3] - name_box[1]
        server_height = server_box[3] - server_box[1]
        block_height = name_height + TEXT_SPACING + server_height
        name_y = (BANNER_SIZE[1] - block_height) // 2 - name_box[1]
        server_y = name_y + name_height + TEXT_SPACING - server_box[1]

        self._draw_heavy_text(draw, (TEXT_LEFT, name_y), name_text, name_font, (255, 255, 255, 255))
        self._draw_heavy_text(draw, (TEXT_LEFT, server_y), server_text, server_font, (225, 230, 238, 255))

        outer_mask = Image.new("L", BANNER_SIZE, 0)
        ImageDraw.Draw(outer_mask).rounded_rectangle(
            (0, 0, BANNER_SIZE[0] - 1, BANNER_SIZE[1] - 1),
            radius=CORNER_RADIUS,
            fill=255,
        )
        inner_mask = Image.new("L", BANNER_SIZE, 0)
        ImageDraw.Draw(inner_mask).rounded_rectangle(
            (
                BORDER_WIDTH,
                BORDER_WIDTH,
                BANNER_SIZE[0] - 1 - BORDER_WIDTH,
                BANNER_SIZE[1] - 1 - BORDER_WIDTH,
            ),
            radius=CORNER_RADIUS - BORDER_WIDTH,
            fill=255,
        )

        framed_canvas = Image.new("RGBA", BANNER_SIZE, (0, 0, 0, 0))
        framed_canvas.paste(canvas, (0, 0), inner_mask)
        border_layer = Image.new("RGBA", BANNER_SIZE, (*accent_color, 255))
        border_mask = ImageChops.subtract(outer_mask, inner_mask)
        framed_canvas.paste(border_layer, (0, 0), border_mask)
        canvas = framed_canvas

        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _draw_heavy_text(
        draw: ImageDraw.ImageDraw,
        position: tuple[int, int],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
    ) -> None:
        x, y = position
        for offset_x, offset_y in TEXT_BOLDEN_OFFSETS:
            draw.text((x + offset_x, y + offset_y), text, font=font, fill=fill)

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
