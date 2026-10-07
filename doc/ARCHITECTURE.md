# Архитектура

Код лежит в `src/` (без пакета, два модуля): CLI/библиотека `print_label.py` и веб-GUI `gui.py`, который импортирует его как `pl`.

## Поток данных

```
картинка | текст ──► 1-bit изображение ──► build_prn (TSPL) ──► validate_prn ──► send_raw ──► принтер
 image_to_label        mode "1", размер       SIZE/GAP/BITMAP/       проверка      TCP:9100 /
 text_to_label         = мм × DPI             PRINT                  длины данных  COM / Windows-очередь
```

`.prn`-файл пропускает всё до `send_raw` и отправляется как есть.

## src/print_label.py

| Элемент | Назначение |
|---|---|
| `label_dots(dpi, mm)` | миллиметры → точки |
| `normalize_image`, `should_dither` | приведение режима/EXIF/прозрачности; фото или штрих-арт |
| `image_to_label(path, dpi, dither, w, h, method)` | картинка → 1-bit; `METHODS`: auto, floyd, atkinson, bayer, threshold |
| `load_font`, `wrap_text`, `_layout_text` | TrueType-шрифт с кириллицей (`FONTS_*`), перенос по словам |
| `text_to_label(text, dpi, w, h, font_mm, align, bold)` | текст → 1-bit; возвращает `(img, размер шрифта в точках, помещается)`; `font_mm=None` — автоподбор |
| `build_header`, `footer`, `build_prn` | сборка TSPL2 |
| `validate_prn`, `extract_bitmap` | проверка собранного файла |
| `Target` | куда печатать: TCP (`ip`,`port`), serial, Windows-принтер |
| `query_model`, `detect_dpi` | запрос `~!T`; по умолчанию 203 dpi |
| `send_raw(payload, target)` | отправка |
| `parse_size("40" / "58x40")` | размер этикетки, мм |
| `main()` | CLI (флаги — в `README.md`) |

## src/gui.py

Локальный HTTP-сервер (`127.0.0.1:8765`, при занятом порте — следующий), страница встроена в константу `PAGE`.

| Эндпоинт | Действие |
|---|---|
| `GET /` | страница |
| `POST /preview` | JSON `{png (base64), info}` |
| `POST /print` | собирает и отправляет, возвращает лог |
| `POST /check` | запрос модели принтера |

Тело запроса — файл или текст (UTF-8), опции — в заголовке `X-Opts` (URL-кодированный JSON: `conn, ip, port, serial, baud, win, emu, size, copies, dpi, dither, mode, fsize, align, bold`), имя файла — `X-Filename`. Лимит загрузки — `MAX_BODY`.

Ключевые функции: `build_job` → `_text_label` / `_image_label`, `make_target`, `parse_font_mm`, `method_from_opt`.

## Режим эмулятора
`--emulator` / галка «Эмулятор» меняют формат BITMAP под `tsc-emulator` (CRLF, инверсия битов). На реальный принтер — не включать.
