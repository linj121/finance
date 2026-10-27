"""Statement parsers. Each parser module exposes:
    detect(path, first_page_text) -> bool
    parse(path) -> Statement (transactions + closing balance, if the file has one)
Add a new bank by adding a module and listing it in PARSERS."""
from pathlib import Path

import pdfplumber

from ..models import Statement
from . import rbc

PARSERS = [rbc]


def first_page_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            return (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    return path.read_text(encoding="utf-8-sig", errors="replace")[:4000]


def parse_file(path: Path) -> Statement:
    text = first_page_text(path)
    for p in PARSERS:
        if p.detect(path, text):
            return p.parse(path)
    raise ValueError("no parser recognises this file")
