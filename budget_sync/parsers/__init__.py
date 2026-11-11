"""CSV transaction parsers. Each parser module exposes:
    detect(header) -> bool          header: the CSV's column names
    parse(path, source) -> list[Transaction]
Add a new bank by adding a module and listing it in PARSERS."""
import csv
from pathlib import Path

from ..models import Transaction
from . import rbc

PARSERS = [rbc]


def read_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [h.strip() for h in next(csv.reader(f), [])]


def parse_file(path: Path, source: str) -> list[Transaction]:
    """source: the file's path relative to the data folder, recorded on each transaction."""
    header = read_header(path)
    for p in PARSERS:
        if p.detect(header):
            return p.parse(path, source)
    raise ValueError(f"no parser recognises these columns: {', '.join(header[:8]) or '(empty file)'}")
