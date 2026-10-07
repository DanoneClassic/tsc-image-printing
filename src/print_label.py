#!/usr/bin/env python3
"""Print an image on a TSC label printer (40x40 mm label, TSPL2 BITMAP).

Usage:
    print-label picture.jpg              # detect DPI -> build -> validate -> print 1 label
    print-label template.prn             # send a ready-made .prn file as-is
    print-label picture.jpg --dry-run    # build + validate + preview, do NOT print
    print-label picture.jpg --size 58x40 # custom label size in mm (default 40x40)
    print-label --text "Привет" --dry-run      # render text instead of an image
    print-label --text "Line 1\\nLine 2" --font-size 6 --bold --align left
    print-label picture.jpg --serial COM3      # print over a COM port instead of TCP
    print-label label.prn --serial /dev/ttyUSB0 --baud 115200
    print-label --check                  # only query printer model / DPI (TCP)
    print-label --check --serial COM3    # query model / DPI over the COM port

Accepts any Pillow-readable image: transparency is flattened onto white,
EXIF orientation is honored, CMYK/16-bit/palette modes are normalized.
Photos are dithered (Floyd-Steinberg) automatically; pure line art is
thresholded. Override with --dither / --no-dither.

Text (--text) is rendered with a TrueType font into the same 1-bit bitmap, so
Cyrillic works and the label goes through the same build/validate/print path.
"""

import argparse
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

PRINTER_IP = "172.16.88.112"
PRINTER_PORT = 9100
DEFAULT_BAUD = 9600  # TSC serial default: 9600,N,8,1 (override with --baud)
LABEL_MM = 40  # default label side, mm (square standard); override with --size
FALLBACK_DPI = 203
MODELS_300DPI = ("300", "310", "345", "346")  # TE300/TE310, TTP-345/346M etc.

# Fraction of midtone pixels above which an image is treated as a photo
# (measured: line-art memes 0.03-0.04, color/shadowed art 0.05+, photos 0.3+)
MIDTONE_LO, MIDTONE_HI = 32, 224
MIDTONE_FRACTION = 0.05

# Text labels: TrueType fonts with Cyrillic, tried in order (Windows, then Linux)
FONTS_REGULAR = ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
FONTS_BOLD = ("arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")
TEXT_MARGIN_MM = 2  # blank border around the text block
MIN_FONT_DOTS = 8
TEXT_ALIGNS = ("left", "center", "right")

# .prn templates and previews are stored next to the real script location
OUT_DIR = Path(__file__).resolve().parent

# Header mirrors the production templates in prj-inmark/data/labels/*.tspl
def build_header(width_mm: int = 58, height_mm: int = 80) -> bytes:
    return (
        f"SIZE {width_mm} mm, {height_mm} mm\r\n"
        "GAP 2 mm,0 mm\r\n"
        "DIRECTION 1\r\n"
        "REFERENCE 0,0\r\n"
        "CLS\r\n"
    ).encode("ascii")


# Default 40x40 header, kept as a module constant for the standard label
HEADER = build_header()


def footer(copies: int) -> bytes:
    return f"\r\nPRINT {copies},1\r\nEOP\r\n".encode("ascii")


def label_dots(dpi: int, mm: int = LABEL_MM) -> int:
    # nominal 203/300 dpi are really 203.2/299.7 (8 and 11.8 dots/mm), so
    # round, don't floor: 40 mm -> 320 dots @203, 472 @300
    dots = round(mm * dpi / 25.4)
    return dots - dots % 8  # BITMAP width must be a whole number of bytes


# --- image normalization ---------------------------------------------------


def normalize_image(img: Image.Image) -> Image.Image:
    """Any Pillow mode -> 8-bit grayscale, upright, transparency on white."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, rgba).convert("RGB")
    elif img.mode.startswith(("I;16", "I")) or img.mode == "F":
        # 16/32-bit and float grayscale: naive convert('L') clips at 255
        img = img.convert("I").point(lambda v: v / 256, mode="L")
    elif img.mode not in ("L", "RGB"):
        img = img.convert("RGB")  # P without transparency, CMYK, YCbCr, ...
    return img if img.mode == "L" else img.convert("L")


def should_dither(gray: Image.Image) -> bool:
    hist = gray.histogram()
    midtones = sum(hist[MIDTONE_LO:MIDTONE_HI]) / sum(hist)
    return midtones > MIDTONE_FRACTION


METHODS = ("auto", "floyd", "atkinson", "bayer", "threshold")

_BAYER8 = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37),
    (63, 31, 55, 23, 61, 29, 53, 21),
)


def _bayer_1bit(gray: Image.Image) -> Image.Image:
    """Ordered dithering with an 8x8 Bayer matrix (pure Pillow)."""
    w, h = gray.size
    thr = [(m * 4 + 2) for row in _BAYER8 for m in row]  # 2..254
    tile = Image.new("L", (8, 8))
    tile.putdata(thr)
    full = Image.new("L", (w, h))
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            full.paste(tile, (x, y))
    # white where pixel > threshold
    from PIL import ImageChops

    diff = ImageChops.subtract(gray, full)  # >0 only where gray > threshold
    return diff.point(lambda v: 255 if v > 0 else 0).convert("1", dither=Image.Dither.NONE)


def _atkinson_1bit(gray: Image.Image) -> Image.Image:
    """Atkinson error diffusion (1/8 to 6 neighbours) in pure Python."""
    w, h = gray.size
    buf = bytearray(gray.tobytes())
    for y in range(h):
        row = y * w
        for x in range(w):
            old = buf[row + x]
            new = 255 if old >= 128 else 0
            buf[row + x] = new
            err = (old - new) // 8
            if not err:
                continue
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and ny < h:
                    i = ny * w + nx
                    buf[i] = min(255, max(0, buf[i] + err))
    out = Image.frombytes("L", (w, h), bytes(buf))
    return out.convert("1", dither=Image.Dither.NONE)


def image_to_label(
    image_path: str | Path,
    dpi: int,
    dither: bool | None = None,
    width_mm: int = LABEL_MM,
    height_mm: int = LABEL_MM,
    method: str | None = None,
) -> tuple[Image.Image, bool]:
    """Load anything Pillow can read -> 1-bit label image (width_mm x height_mm)."""
    try:
        img = Image.open(image_path)
        img.load()  # force decode now so errors surface here
    except FileNotFoundError:
        sys.exit(f"ERROR: file not found: {image_path}")
    except (UnidentifiedImageError, OSError) as exc:
        sys.exit(f"ERROR: cannot decode image {image_path}: {exc}")

    width_dots = label_dots(dpi, width_mm)
    height_dots = label_dots(dpi, height_mm)
    gray = normalize_image(img)
    # fit into the label without distortion, pad with white
    gray = ImageOps.pad(
        gray, (width_dots, height_dots), method=Image.LANCZOS, color=255
    )
    if method is None:
        method = "auto" if dither is None else ("floyd" if dither else "threshold")
    if method not in METHODS:
        sys.exit(f"ERROR: unknown method {method!r}, expected one of {METHODS}")
    if method == "auto":
        method = "floyd" if should_dither(gray) else "threshold"
    if method == "atkinson":
        bw = _atkinson_1bit(gray)
    elif method == "bayer":
        bw = _bayer_1bit(gray)
    else:
        bw = gray.convert(
            "1",
            dither=Image.Dither.FLOYDSTEINBERG
            if method == "floyd"
            else Image.Dither.NONE,
        )
    return bw, method != "threshold"


# --- text rendering ---------------------------------------------------------


def load_font(size_dots: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = FONTS_BOLD if bold else FONTS_REGULAR
    for name in names:
        try:
            return ImageFont.truetype(name, size_dots)
        except OSError:
            continue
    sys.exit(f"ERROR: no TrueType font found (tried {', '.join(names)})")


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap to max_width dots; explicit newlines are kept."""
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if line and font.getlength(candidate) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines


def _layout_text(
    text: str, size_dots: int, bold: bool, max_width: int, max_height: int
) -> tuple[ImageFont.FreeTypeFont, list[str], int, bool]:
    font = load_font(size_dots, bold)
    lines = wrap_text(text, font, max_width)
    line_height = sum(font.getmetrics())
    fits = line_height * len(lines) <= max_height and all(
        font.getlength(line) <= max_width for line in lines
    )
    return font, lines, line_height, fits


def text_to_label(
    text: str,
    dpi: int,
    width_mm: int = LABEL_MM,
    height_mm: int = LABEL_MM,
    font_mm: float | None = None,
    align: str = "center",
    bold: bool = False,
) -> tuple[Image.Image, int, bool]:
    """Render text -> 1-bit label image; returns (image, font size in dots, fits).

    font_mm=None picks the largest font at which the wrapped text fits.
    """
    width_dots = label_dots(dpi, width_mm)
    height_dots = label_dots(dpi, height_mm)
    margin = round(TEXT_MARGIN_MM * dpi / 25.4)
    max_width = max(8, width_dots - 2 * margin)
    max_height = max(8, height_dots - 2 * margin)

    if font_mm is None:
        lo, hi = MIN_FONT_DOTS, max(MIN_FONT_DOTS, max_height)
        while lo < hi:  # largest size that still fits
            mid = (lo + hi + 1) // 2
            if _layout_text(text, mid, bold, max_width, max_height)[3]:
                lo = mid
            else:
                hi = mid - 1
        size_dots = lo
    else:
        size_dots = max(MIN_FONT_DOTS, round(font_mm * dpi / 25.4))
    font, lines, line_height, fits = _layout_text(
        text, size_dots, bold, max_width, max_height
    )

    # drawing on a mode-"1" image disables antialiasing: crisp thermal output
    bw = Image.new("1", (width_dots, height_dots), 1)
    draw = ImageDraw.Draw(bw)
    y = (height_dots - line_height * len(lines)) // 2
    for line in lines:
        free = width_dots - 2 * margin - font.getlength(line)
        x = margin + {"left": 0, "center": free / 2, "right": free}[align]
        draw.text((round(x), y), line, font=font, fill=0)
        y += line_height
    return bw, size_dots, fits


# --- TSPL assembly and validation ------------------------------------------

# The tsc-emulator (infra/emulators/tsc-emulator-*) deviates from real firmware:
# it only recognizes BITMAP when the header line is terminated by CRLF, and it
# treats 1-bits as black. Real printers must NOT get either tweak, so both are
# opt-in via emulator=True (CLI: --emulator).
EMULATOR_EOL = b"\r\n"


def _bitmap_marker(width_bytes: int, height_dots: int, emulator: bool = False) -> bytes:
    marker = f"BITMAP 0,0,{width_bytes},{height_dots},0,".encode("ascii")
    return marker + EMULATOR_EOL if emulator else marker


def build_prn(
    bw: Image.Image,
    copies: int = 1,
    width_mm: int = LABEL_MM,
    height_mm: int = LABEL_MM,
    emulator: bool = False,
) -> bytes:
    width_dots, height_dots = bw.width, bw.height
    width_bytes = width_dots // 8
    data = bw.tobytes()  # packed 1bpp, MSB first, bit 0=black -- matches TSPL
    expected = width_bytes * height_dots
    if len(data) != expected:
        sys.exit(f"ERROR: bitmap size {len(data)} != {expected}")
    if emulator:
        data = bytes(b ^ 0xFF for b in data)
    bitmap_cmd = _bitmap_marker(width_bytes, height_dots, emulator)
    return build_header(width_mm, height_mm) + bitmap_cmd + data + footer(copies)


def validate_prn(
    raw: bytes,
    dpi: int,
    copies: int = 1,
    width_mm: int = LABEL_MM,
    height_mm: int = LABEL_MM,
    emulator: bool = False,
) -> None:
    header = build_header(width_mm, height_mm)
    width_dots = label_dots(dpi, width_mm)
    height_dots = label_dots(dpi, height_mm)
    width_bytes = width_dots // 8
    marker = _bitmap_marker(width_bytes, height_dots, emulator)
    checks = {
        "header present": raw.startswith(header),
        "BITMAP marker present": marker in raw,
        "footer present": raw.endswith(footer(copies)),
        "data length exact": len(raw)
        == len(header) + len(marker) + width_bytes * height_dots + len(footer(copies)),
    }
    for check_name, ok in checks.items():
        print(f"  [{'OK' if ok else 'FAIL'}] {check_name}")
    if not all(checks.values()):
        sys.exit("VALIDATION FAILED")
    print("VALIDATION PASSED")


def extract_bitmap(
    raw: bytes,
    dpi: int,
    width_mm: int = LABEL_MM,
    height_mm: int = LABEL_MM,
    emulator: bool = False,
) -> Image.Image:
    """Decode the BITMAP payload back into an image (for round-trip checks)."""
    width_dots = label_dots(dpi, width_mm)
    height_dots = label_dots(dpi, height_mm)
    width_bytes = width_dots // 8
    marker = _bitmap_marker(width_bytes, height_dots, emulator)
    start = raw.index(marker) + len(marker)
    data = raw[start : start + width_bytes * height_dots]
    if emulator:
        data = bytes(b ^ 0xFF for b in data)
    return Image.frombytes("1", (width_dots, height_dots), data)


# --- printer I/O ------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Where to send TSPL: a Windows print queue, a serial/COM port, or TCP."""

    ip: str = PRINTER_IP
    port: int = PRINTER_PORT
    serial_port: str | None = None  # e.g. "COM3" (Windows) or "/dev/ttyUSB0"
    baud: int = DEFAULT_BAUD
    win_printer: str | None = None  # Windows spooler queue name (USB printers)

    @property
    def is_serial(self) -> bool:
        return self.serial_port is not None

    @property
    def is_windows(self) -> bool:
        return self.win_printer is not None

    def describe(self) -> str:
        if self.is_windows:
            return f"Windows printer {self.win_printer!r}"
        if self.is_serial:
            return f"{self.serial_port} @ {self.baud} baud"
        return f"{self.ip}:{self.port}"


def _open_serial(target: Target, timeout: float):
    """Open the serial port, or exit with a clear message if pyserial is absent."""
    try:
        import serial  # pyserial, optional dependency (only for --serial)
    except ImportError:
        sys.exit("ERROR: pyserial not installed -- run: pip install pyserial")
    return serial.Serial(
        target.serial_port, target.baud, timeout=timeout, write_timeout=timeout
    )


def _import_win32print():
    """Import pywin32's win32print, or exit with an actionable message."""
    try:
        import win32print  # part of pywin32, Windows-only
    except ImportError:
        sys.exit("ERROR: pywin32 not installed -- run: pip install pywin32")
    return win32print


def list_windows_printers() -> None:
    """Print the names of installed Windows printers (for --windows-printer)."""
    win32print = _import_win32print()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [p[2] for p in win32print.EnumPrinters(flags)]
    if not names:
        print("no Windows printers found")
        return
    print("Windows printers:")
    for name in names:
        print(f"  {name}")


def _send_windows_printer(payload: bytes, name: str) -> None:
    """Send raw TSPL to a Windows print queue using the RAW datatype.

    RAW bypasses the driver's rendering so the printer receives the TSPL bytes
    verbatim -- the correct way to reach a USB TSC printer that has no COM port.
    """
    win32print = _import_win32print()
    try:
        handle = win32print.OpenPrinter(name)
    except Exception as exc:  # pywin32 raises pywintypes.error, not OSError
        sys.exit(f"ERROR: cannot open Windows printer {name!r}: {exc}")
    try:
        win32print.StartDocPrinter(handle, 1, ("print-label", None, "RAW"))
        win32print.StartPagePrinter(handle)
        win32print.WritePrinter(handle, payload)
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
    except Exception as exc:
        sys.exit(f"ERROR: cannot send to Windows printer {name!r}: {exc}")
    finally:
        win32print.ClosePrinter(handle)


def query_model(target: Target, timeout: float = 3.0) -> str:
    """Ask the printer its model via ~!T over the chosen transport."""
    if target.is_windows:
        return ""  # spooler is write-only (RAW); DPI must come from --dpi
    try:
        if target.is_serial:
            with _open_serial(target, timeout) as ser:
                ser.write(b"~!T\r\n")
                return ser.read(256).decode("ascii", errors="replace").strip()
        with socket.create_connection((target.ip, target.port), timeout=timeout) as sock:
            sock.sendall(b"~!T\r\n")
            sock.settimeout(timeout)
            return sock.recv(256).decode("ascii", errors="replace").strip()
    except (socket.timeout, OSError):
        return ""


def detect_dpi(target: Target) -> int:
    model = query_model(target)
    if not model:
        print(f"WARNING: printer did not answer ~!T, assuming {FALLBACK_DPI} dpi")
        return FALLBACK_DPI
    dpi = 300 if any(m in model for m in MODELS_300DPI) else 203
    print(f"PRINTER: {model}, {dpi} dpi")
    return dpi


def send_raw(payload: bytes, target: Target) -> None:
    dest = target.describe()
    if target.is_windows:
        _send_windows_printer(payload, target.win_printer)
        print(f"SENT: {len(payload)} bytes to {dest}")
        return
    try:
        if target.is_serial:
            with _open_serial(target, timeout=10) as ser:
                ser.write(payload)
                ser.flush()
        else:
            with socket.create_connection((target.ip, target.port), timeout=10) as sock:
                sock.sendall(payload)
    except OSError as exc:
        sys.exit(f"ERROR: cannot send to {dest}: {exc}")
    print(f"SENT: {len(payload)} bytes to {dest}")


# --- CLI ---------------------------------------------------------------------


def parse_size(text: str) -> tuple[int, int]:
    """Parse a label size in mm: '40' -> (40, 40), '58x40' -> (58, 40).

    Tolerant of common Russian-keyboard typos: Cyrillic 'х', '×', '*' and ','
    all work as the separator, and a trailing 'mm'/'мм' unit is ignored -- so
    a look-alike Cyrillic letter no longer breaks the whole run.
    """
    cleaned = text.lower().replace(" ", "").replace("мм", "").replace("mm", "")
    parts = [p for p in re.split(r"[xх×*,]", cleaned) if p]  # latin x, cyrillic х
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid size {text!r}: use e.g. 40 or 58x40"
        )
    if len(nums) == 1:
        width = height = nums[0]
    elif len(nums) == 2:
        width, height = nums
    else:
        raise argparse.ArgumentTypeError(
            f"invalid size {text!r}: use e.g. 40 or 58x40"
        )
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("label dimensions must be positive")
    return width, height


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="print-label",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", nargs="?", help="image file or ready-made .prn")
    parser.add_argument("--check", action="store_true", help="query printer and exit")
    parser.add_argument("--dry-run", action="store_true", help="do not print")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dither", action="store_true", help="force Floyd-Steinberg")
    mode.add_argument("--no-dither", action="store_true", help="force plain threshold")
    parser.add_argument("--text", help=r"print this text instead of an image (\n = new line)")
    parser.add_argument(
        "--font-size", type=float, metavar="MM", help="text height in mm (default: auto-fit)"
    )
    parser.add_argument("--align", choices=TEXT_ALIGNS, default="center", help="text alignment")
    parser.add_argument("--bold", action="store_true", help="bold text")
    parser.add_argument(
        "--method",
        choices=METHODS,
        help="1-bit conversion (overrides --dither/--no-dither)",
    )
    parser.add_argument("--copies", type=int, default=1, help="labels to print")
    parser.add_argument(
        "--size",
        type=parse_size,
        default=(LABEL_MM, LABEL_MM),
        metavar="WxH",
        help="label size in mm, e.g. 40 or 58x40 (default 40x40)",
    )
    parser.add_argument(
        "--emulator",
        action="store_true",
        help="tsc-emulator quirks (CRLF after BITMAP header, inverted bits); not for real printers",
    )
    parser.add_argument("--dpi", type=int, choices=(203, 300), help="skip detection")
    parser.add_argument("--ip", default=PRINTER_IP, help="TCP printer IP (default)")
    parser.add_argument("--port", type=int, default=PRINTER_PORT, help="TCP port")
    parser.add_argument(
        "--serial",
        metavar="PORT",
        help='print over a COM/serial port instead of TCP, e.g. COM3 or /dev/ttyUSB0',
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help=f"serial baud rate (default {DEFAULT_BAUD})"
    )
    args = parser.parse_args()

    printer = Target(ip=args.ip, port=args.port, serial_port=args.serial, baud=args.baud)

    if args.check:
        detect_dpi(printer)
        return
    if args.text is not None and args.target:
        parser.error("give either a target file or --text, not both")
    if args.text is None and not args.target:
        parser.error("target image or .prn file is required (or use --text / --check)")
    if args.text is not None and not args.text.strip():
        parser.error("--text must not be empty")
    if args.font_size is not None and args.font_size <= 0:
        parser.error("--font-size must be positive")
    if args.copies < 1:
        parser.error("--copies must be >= 1")
    if args.serial and args.baud <= 0:
        parser.error("--baud must be positive")

    target = Path(args.target or "text")
    if target.suffix.lower() == ".prn":
        if not target.is_file():
            sys.exit(f"ERROR: file not found: {target}")
        if args.dry_run:
            print(f"DRY-RUN: would send {target} ({target.stat().st_size} bytes)")
            return
        send_raw(target.read_bytes(), printer)
        return

    width_mm, height_mm = args.size
    dpi = args.dpi or detect_dpi(printer)
    if args.text is not None:
        text = args.text.replace("\\n", "\n")
        bw, font_dots, fits = text_to_label(
            text, dpi, width_mm, height_mm, args.font_size, args.align, args.bold
        )
        dithered = False
        print(f"TEXT: font {font_dots} dots")
        if not fits:
            print("WARNING: text does not fit the label and will be clipped")
    else:
        dither = True if args.dither else False if args.no_dither else None
        bw, dithered = image_to_label(
            target, dpi, dither, width_mm, height_mm, method=args.method
        )
    raw = build_prn(bw, args.copies, width_mm, height_mm, args.emulator)

    size_tag = f"{width_mm}x{height_mm}"
    prn_path = OUT_DIR / f"{target.stem}-{size_tag}.prn"
    preview_path = OUT_DIR / f"preview-{target.stem}-{size_tag}.png"
    prn_path.write_bytes(raw)
    bw.save(preview_path)
    print(f"BUILT: {prn_path} ({len(raw)} bytes)")
    print(f"BITMAP: {bw.width}x{bw.height} dots, {'dithered' if dithered else 'threshold'}")
    print(f"PREVIEW: {preview_path}")
    validate_prn(raw, dpi, args.copies, width_mm, height_mm, args.emulator)

    if args.dry_run:
        print("DRY-RUN: not sending to printer")
        return
    send_raw(raw, printer)


if __name__ == "__main__":
    main()
