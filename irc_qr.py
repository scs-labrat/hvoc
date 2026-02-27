"""QR code generation and decoding for VOC share strings.

Generate:
    from irc_qr import generate_qr
    path = generate_qr("CHAN:eyJr...", label="#general", kind="channel")

Decode:
    from irc_qr import decode_qr
    data = decode_qr("/path/to/qrcode.png")  # returns "CHAN:eyJr..." or None
"""

import os
import re
import time

from irc_log import get_logger

log = get_logger(__name__)

_QR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcodes")

# VOC branded colours
_BG_COLOUR = "#0d1117"      # dark background
_FG_COLOUR = "#58a6ff"      # cyan-blue foreground
_ACCENT = "#c9d1d9"         # light grey for text

# Image extensions we recognise as QR code files
QR_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def decode_qr(filepath: str) -> str | None:
    """Decode a QR code image and return the embedded string.

    Handles VOC's branded QR codes (light-on-dark) by automatically
    trying both normal and inverted images.

    Args:
        filepath: Path to a PNG/JPG/BMP image containing a QR code.

    Returns:
        The decoded string (e.g. "CHAN:eyJr..." or "DIR:eyJr..."), or None.
    """
    filepath = os.path.expanduser(filepath.strip().strip('"').strip("'"))

    if not os.path.isfile(filepath):
        log.error("QR file not found: %s", filepath)
        return None

    try:
        from PIL import Image, ImageOps
    except ImportError:
        log.error("Pillow not installed — run: pip install Pillow")
        return None

    img = Image.open(filepath)
    log.debug("Decoding QR from %s (%dx%d, %s)", filepath, *img.size, img.mode)

    # Try multiple decode strategies
    gray = img.convert("L")
    attempts = [
        ("original", gray),
        ("inverted", ImageOps.invert(gray)),
    ]

    # Also try with header/footer cropped (for VOC branded QR codes)
    w, h = gray.size
    if h > 100:
        margin_top = int(h * 0.08)
        margin_bot = int(h * 0.06)
        cropped = gray.crop((0, margin_top, w, h - margin_bot))
        cropped_inv = ImageOps.invert(cropped)
        attempts.append(("cropped", cropped))
        attempts.append(("cropped+inverted", cropped_inv))

    for label, attempt_img in attempts:
        result = _try_decode(attempt_img, label)
        if result:
            return result

    log.warning("Could not decode QR from %s (tried %d strategies)",
                filepath, len(attempts))
    return None


def _try_decode(img, label: str) -> str | None:
    """Try decoding a PIL Image with available QR libraries."""
    # Strategy 1: pyzbar (most reliable)
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        results = pyzbar_decode(img)
        if results:
            data = results[0].data.decode("utf-8", errors="replace")
            log.info("QR decoded via pyzbar (%s): %d chars", label, len(data))
            return data
    except ImportError:
        pass
    except Exception as e:
        log.debug("pyzbar failed (%s): %s", label, e)

    # Strategy 2: OpenCV
    try:
        import cv2
        import numpy as np
        arr = np.array(img)
        detector = cv2.QRCodeDetector()
        data, pts, straight = detector.detectAndDecode(arr)
        if data:
            log.info("QR decoded via opencv (%s): %d chars", label, len(data))
            return data
    except ImportError:
        pass
    except Exception as e:
        log.debug("opencv failed (%s): %s", label, e)

    return None


def is_qr_image_path(text: str) -> bool:
    """Check if text looks like a path to a QR code image file."""
    text = text.strip().strip('"').strip("'")
    _, ext = os.path.splitext(text)
    return ext.lower() in QR_IMAGE_EXTENSIONS


def generate_qr(data: str, label: str = "", kind: str = "share") -> str | None:
    """Generate a QR code PNG for a share string.

    Args:
        data:  The full share string (CHAN:... or DIR:...)
        label: Human-readable label (channel name, community name, etc)
        kind:  "channel" or "directory" — used in the title text and filename

    Returns:
        Absolute path to the saved PNG, or None on failure.
    """
    try:
        import qrcode
        from qrcode.image.styledpil import StyledPilImage
        from qrcode.image.styles.colormasks import SolidFillColorMask
    except ImportError:
        log.warning("qrcode package not installed — run: pip install qrcode[pil]")
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning("Pillow not installed — run: pip install Pillow")
        return None

    os.makedirs(_QR_DIR, exist_ok=True)

    # Sanitise label for filename
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "", label.replace("#", "").replace(" ", "_"))
    if not safe_label:
        safe_label = kind
    filename = f"voc_{kind}_{safe_label}.png"
    filepath = os.path.join(_QR_DIR, filename)

    try:
        # Generate QR code
        qr = qrcode.QRCode(
            version=None,       # auto-size
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)

        # Parse colours
        bg_rgb = _hex_to_rgb(_BG_COLOUR)
        fg_rgb = _hex_to_rgb(_FG_COLOUR)

        colour_mask = SolidFillColorMask(
            back_color=bg_rgb,
            front_color=fg_rgb,
        )
        qr_img = qr.make_image(
            image_factory=StyledPilImage,
            color_mask=colour_mask,
        ).convert("RGB")

        qr_w, qr_h = qr_img.size

        # Build the final image with header and footer
        padding = 20
        header_h = 50
        footer_h = 40 if label else 0
        total_h = header_h + qr_h + footer_h + padding * 2
        total_w = max(qr_w + padding * 2, 300)

        canvas = Image.new("RGB", (total_w, total_h), bg_rgb)
        draw = ImageDraw.Draw(canvas)

        # Load a font (fall back to default if needed)
        title_font = _get_font(18)
        label_font = _get_font(14)

        # Header text
        kind_title = "DIRECTORY" if kind == "directory" else "CHANNEL"
        header_text = f"VOC {kind_title}"
        _draw_centred(draw, header_text, total_w, padding + 5,
                      font=title_font, fill=_hex_to_rgb(_ACCENT))

        # Paste QR code centred
        qr_x = (total_w - qr_w) // 2
        qr_y = header_h + padding // 2
        canvas.paste(qr_img, (qr_x, qr_y))

        # Footer label
        if label:
            _draw_centred(draw, label, total_w, qr_y + qr_h + 8,
                          font=label_font, fill=_hex_to_rgb(_FG_COLOUR))

        canvas.save(filepath, "PNG")
        log.info("QR code saved: %s (%dx%d)", filepath, total_w, total_h)
        return filepath

    except Exception as e:
        log.error("QR generation failed: %s", e)
        return None


def _hex_to_rgb(hex_colour: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to (r, g, b) tuple."""
    h = hex_colour.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _get_font(size: int):
    """Try to load a decent font, fall back to Pillow default."""
    from PIL import ImageFont
    # Try common system fonts
    candidates = [
        "DejaVuSansMono.ttf",
        "DejaVuSans.ttf",
        "Consolas.ttf",           # Windows
        "consola.ttf",            # Windows alt
        "LiberationMono-Regular.ttf",
        "FreeMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "C:\\Windows\\Fonts\\consola.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    # Fall back to default bitmap font
    return ImageFont.load_default()


def _draw_centred(draw, text: str, canvas_width: int, y: int,
                  font=None, fill=(255, 255, 255)):
    """Draw text horizontally centred."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (canvas_width - text_w) // 2
    draw.text((x, y), text, font=font, fill=fill)
