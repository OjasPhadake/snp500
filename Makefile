.PHONY: install scrape build load serve test all
install: ; pip install -r requirements.txt && pip install -e .
scrape:  ; snp500 scrape
build:   ; snp500 build
load:    ; snp500 load
serve:   ; snp500 serve
test:    ; pytest -q
all:     ; snp500 all
