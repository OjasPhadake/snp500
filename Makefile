VENV ?= .venv
PY   := $(VENV)/bin/python
.PHONY: venv install scrape build load serve test all
venv:    ; python3 -m venv $(VENV) && $(VENV)/bin/pip install --upgrade pip
install: venv ; $(VENV)/bin/pip install -r requirements.txt -e .
scrape:  ; $(PY) -m snp500.cli scrape
build:   ; $(PY) -m snp500.cli build
load:    ; $(PY) -m snp500.cli load
serve:   ; $(PY) -m snp500.cli serve
test:    ; $(PY) -m pytest -q
all:     ; $(PY) -m snp500.cli all
