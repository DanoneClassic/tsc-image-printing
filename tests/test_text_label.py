import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gui  # noqa: E402
import print_label as pl  # noqa: E402

pytest.importorskip("PIL.ImageFont")
try:
    pl.load_font(20)
except SystemExit:
    pytest.skip("no TrueType font installed", allow_module_level=True)


def black_pixels(img: Image.Image) -> int:
    return img.convert("L").tobytes().count(0)


def test_wrap_keeps_explicit_newlines():
    font = pl.load_font(20)
    assert pl.wrap_text("a\nb", font, 1000) == ["a", "b"]


def test_wrap_breaks_long_line_by_words():
    font = pl.load_font(20)
    lines = pl.wrap_text("word " * 20, font, 100)
    assert len(lines) > 1
    assert all(font.getlength(line) <= 100 or " " not in line for line in lines)


@pytest.mark.parametrize("dpi", [203, 300])
def test_label_size_matches_dpi(dpi):
    bw, _, _ = pl.text_to_label("Привет", dpi, 40, 40)
    assert bw.mode == "1"
    assert bw.size == (pl.label_dots(dpi, 40),) * 2


def test_text_is_drawn_and_cyrillic_not_blank():
    bw, _, fits = pl.text_to_label("Привет", 203)
    assert fits and black_pixels(bw) > 0


@pytest.mark.parametrize("align", pl.TEXT_ALIGNS)
def test_align_moves_ink(align):
    bw, _, _ = pl.text_to_label("Hi", 203, font_mm=5, align=align)
    xs = [x for x in range(bw.width) for y in range(bw.height) if bw.getpixel((x, y)) == 0]
    mid = (min(xs) + max(xs)) / 2
    expected = {"left": bw.width * 0.25, "center": bw.width / 2, "right": bw.width * 0.75}[align]
    assert abs(mid - expected) < bw.width * 0.2
    assert (align == "left") == (min(xs) < bw.width * 0.1)


def test_bold_has_more_ink():
    plain, _, _ = pl.text_to_label("Hello", 203, font_mm=6)
    bold, _, _ = pl.text_to_label("Hello", 203, font_mm=6, bold=True)
    assert black_pixels(bold) > black_pixels(plain)


def test_autofit_larger_for_short_text():
    _, short, _ = pl.text_to_label("A", 203)
    _, long, _ = pl.text_to_label("A" * 40, 203)
    assert short > long


def test_oversized_font_reports_not_fitting():
    _, _, fits = pl.text_to_label("Hello world", 203, font_mm=30)
    assert not fits


def test_font_has_minimum():
    _, size, _ = pl.text_to_label("x", 203, font_mm=0.1)
    assert size == pl.MIN_FONT_DOTS


@pytest.mark.parametrize("raw,expected", [("", None), ("авто", None), ("6", 6.0), ("6,5 мм", 6.5)])
def test_parse_font_mm(raw, expected):
    assert gui.parse_font_mm(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "0", "-3"])
def test_parse_font_mm_rejects(raw):
    with pytest.raises(ValueError):
        gui.parse_font_mm(raw)


OPTS = {"mode": "text", "dpi": "203", "size": "40x40"}


def test_build_job_text_produces_valid_prn():
    job = gui.build_job("Привет".encode(), "", OPTS)
    assert job["raw"].startswith(b"SIZE 40 mm")
    assert job["png"] and "текст" in job["info"]


def test_build_job_empty_text_rejected():
    with pytest.raises(ValueError):
        gui.build_job(b"  \n", "", OPTS)


def test_build_job_unknown_align_falls_back():
    gui.build_job(b"Hi", "", {**OPTS, "align": "zzz"})


def test_build_job_warns_when_text_does_not_fit():
    job = gui.build_job(b"Hello world", "", {**OPTS, "fsize": "30"})
    assert "не помещается" in job["info"]


def test_build_job_text_ignores_prn_filename():
    job = gui.build_job(b"Hi", "x.prn", OPTS)
    assert job["png"]


def test_build_job_image_still_works():
    buf = io.BytesIO()
    Image.new("L", (50, 50), 128).save(buf, "PNG")
    job = gui.build_job(buf.getvalue(), "a.png", {"dpi": "203"})
    assert "floyd" in job["info"] or "threshold" in job["info"]


def test_build_job_prn_passthrough():
    job = gui.build_job(b"RAW", "t.prn", {"dpi": "203"})
    assert job["raw"] == b"RAW" and job["png"] is None
