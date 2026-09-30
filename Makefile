# Shortcuts for macOS/Linux. On Windows (no `make`) run the same targets with:
#     python scripts/fm.py <target>
PY ?= python3
TARGETS := setup doctor up up-lite down ps logs migrate clean dev test lint smoke
.PHONY: help $(TARGETS)

help:
	@$(PY) scripts/fm.py help

$(TARGETS):
	@$(PY) scripts/fm.py $@
