PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
TEXT ?= Привет

.DEFAULT_GOAL := help
.PHONY: help venv gui run test dry check clean

help: ## показать команды
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-6s %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q pillow pyserial pytest

venv: $(BIN)/python ## создать .venv и поставить зависимости

gui: $(BIN)/python ## запустить GUI (http://127.0.0.1:8765)
	$(BIN)/python src/gui.py

run: gui

test: $(BIN)/python ## прогнать тесты
	$(BIN)/python -m pytest -q -p no:cacheprovider tests

dry: $(BIN)/python ## собрать метку из TEXT="..." без печати
	$(BIN)/python src/print_label.py --text "$(TEXT)" --dry-run

check: $(BIN)/python ## опросить принтер (модель, DPI)
	$(BIN)/python src/print_label.py --check

clean: ## удалить кэши и сгенерированные .prn / preview
	rm -rf __pycache__ tests/__pycache__ .pytest_cache
	rm -rf src/__pycache__
	rm -f *.prn preview-*.png src/*.prn src/preview-*.png
