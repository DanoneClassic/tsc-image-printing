# tsc-image-printing

Печать картинок и текста (включая кириллицу) на термопринтерах TSC (TSPL2) через CLI или локальный веб-интерфейс.

## Быстрый старт
```
make          # список команд
make gui      # веб-интерфейс на http://127.0.0.1:8765 (venv создаётся сам)
make test     # тесты, без принтера
make dry TEXT="Привет"   # собрать метку без печати
```
Нужен Python ≥ 3.10; зависимости: `pillow`, `pyserial`.

## CLI
```
python print_label.py picture.jpg                 # печать (DPI определяется сам)
python print_label.py picture.jpg --dry-run       # собрать и проверить, не печатать
python print_label.py --text "Привет\nмир" --bold --align left --font-size 6
python print_label.py label.prn                   # готовый .prn как есть
python print_label.py --check                     # модель и DPI принтера
```
| Флаг | Смысл |
|---|---|
| `--size 58x40` | размер этикетки, мм (по умолчанию 40x40) |
| `--copies N` | число копий |
| `--dpi 203/300` | пропустить автоопределение |
| `--method` / `--dither` / `--no-dither` | преобразование в 1-bit |
| `--text`, `--font-size MM`, `--align`, `--bold` | печать текста |
| `--ip`, `--port`, `--serial`, `--baud` | подключение: TCP (по умолчанию), COM |
| `--emulator` | режим tsc-emulator, не для реальных принтеров |

## GUI
Выбор подключения (TCP / COM / Windows-принтер), режим «Картинка» или «Текст», точный предпросмотр 1-bit, печать.

## Структура
| Путь | Что |
|---|---|
| `print_label.py` | логика меток и CLI |
| `gui.py` | веб-GUI |
| `tests/` | pytest |
| `Makefile` | команды запуска |
| `doc/ARCHITECTURE.md` | устройство и контракты |
| `doc/AI_GUIDE.md` | правила для ИИ-агентов |

## Безопасность
Репозиторий приватный; доступ выдаётся приглашением (Settings → Collaborators). Не коммитить `*.prn`, сборки (`dist/`, `build/`) и секреты.
