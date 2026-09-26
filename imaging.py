"""Shared, non-destructive preparation for thumbnails and local OCR."""

from PIL import Image, ImageOps


def manuscript_rgb(image: Image.Image) -> Image.Image:
    oriented = ImageOps.exif_transpose(image)
    if "A" in oriented.getbands() or "transparency" in oriented.info:
        rgba = oriented.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return oriented.convert("RGB")
