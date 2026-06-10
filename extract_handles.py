"""Extract unique social-media handles per platform from the MEP mapping sheet.

The "Social media" tab stores platform URLs as rich-text hyperlinks layered
over display text ("X (Twitter)", "Facebook", "Instagram"), so we go through
the Sheets API's grid-data endpoint to read `cell.hyperlink` directly — the
Values API only returns the display text.

URLs are normalised per platform (twitter.com → x.com, query strings stripped,
handles lowercased) so duplicates collapse and the output is ready to feed
into a tracking pipeline. URLs that fail platform checks are written to a
sibling `_skipped.csv` for audit.
"""
from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
PLATFORM_COLUMNS = ["X (Twitter)", "Facebook", "Instagram"]
GRID_FIELDS = "sheets.data.rowData.values(formattedValue,hyperlink,textFormatRuns(format/link/uri))"

X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}
FACEBOOK_HOSTS = {"facebook.com", "fb.com", "m.facebook.com"}
INSTAGRAM_HOSTS = {"instagram.com"}


@dataclass(frozen=True)
class Handle:
    handle: str
    url: str


@dataclass(frozen=True)
class Skipped:
    raw_url: str
    reason: str


def sheets_service(service_account_file: Path):
    creds = Credentials.from_service_account_file(str(service_account_file), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_grid(service, spreadsheet_id: str, worksheet_name: str) -> list[list[dict]]:
    response = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[worksheet_name],
        fields=GRID_FIELDS,
    ).execute()
    sheets = response.get("sheets", [])
    if not sheets or not sheets[0].get("data"):
        return []
    return [row.get("values", []) for row in sheets[0]["data"][0].get("rowData", [])]


def cell_hyperlink(cell: dict) -> str:
    """Return the first non-empty URL on the cell, from any of the link surfaces."""
    if not cell:
        return ""
    if (url := cell.get("hyperlink")):
        return url
    for run in cell.get("textFormatRuns", []) or []:
        if (url := run.get("format", {}).get("link", {}).get("uri")):
            return url
    value = (cell.get("formattedValue") or "").strip()
    return value if value.startswith(("http://", "https://")) else ""


def normalise(raw_url: str, platform: str) -> Handle | Skipped:
    """Normalise a platform URL into a Handle, or return Skipped with a reason."""
    if not raw_url:
        return Skipped(raw_url, "empty")
    parsed = urlparse(raw_url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/")

    if platform == "X (Twitter)":
        if host not in X_HOSTS:
            return Skipped(raw_url, f"wrong host for X: {host or '(none)'}")
        if not path:
            return Skipped(raw_url, "no path / handle missing")
        handle = path.split("/")[0].lstrip("@").lower()
        if not handle:
            return Skipped(raw_url, "empty handle")
        return Handle(handle, f"https://x.com/{handle}")

    if platform == "Facebook":
        if host not in FACEBOOK_HOSTS:
            return Skipped(raw_url, f"wrong host for Facebook: {host or '(none)'}")
        if path == "profile.php":
            fbid = parse_qs(parsed.query).get("id", [""])[0]
            if not fbid:
                return Skipped(raw_url, "profile.php with no id")
            return Handle(fbid, f"https://facebook.com/profile.php?id={fbid}")
        if not path:
            return Skipped(raw_url, "no path / handle missing")
        handle = path.split("/")[0].lower()
        return Handle(handle, f"https://facebook.com/{handle}")

    if platform == "Instagram":
        if host not in INSTAGRAM_HOSTS:
            return Skipped(raw_url, f"wrong host for Instagram: {host or '(none)'}")
        if not path:
            return Skipped(raw_url, "no path / handle missing")
        handle = path.split("/")[0].lower()
        return Handle(handle, f"https://instagram.com/{handle}")

    return Skipped(raw_url, f"unknown platform: {platform}")


def unique_preserve_order(handles):
    seen, out = set(), []
    for h in handles:
        if h.handle not in seen:
            seen.add(h.handle)
            out.append(h)
    return out


def write_handles_csv(path: Path, handles: list[Handle]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["handle", "url"])
        writer.writerows((h.handle, h.url) for h in handles)


def write_skipped_csv(path: Path, skipped: list[Skipped]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["raw_url", "reason"])
        writer.writerows((s.raw_url, s.reason) for s in skipped)


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def main() -> int:
    load_dotenv()
    service_account_file = Path(os.environ["SERVICE_ACCOUNT_FILE"])
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    worksheet_name = os.environ["WORKSHEET_NAME"]
    output_dir = Path(os.environ.get("OUTPUT_DIR", "data"))
    output_dir.mkdir(parents=True, exist_ok=True)

    service = sheets_service(service_account_file)
    grid = fetch_grid(service, spreadsheet_id, worksheet_name)
    if not grid:
        print("Worksheet is empty.")
        return 1

    header_cells, *data_rows = grid
    header_labels = [(c.get("formattedValue") or "").strip() for c in header_cells]
    try:
        column_index = {name: header_labels.index(name) for name in PLATFORM_COLUMNS}
    except ValueError as e:
        print(f"Missing expected column: {e}")
        return 1

    print(f"Loaded {len(data_rows)} data rows from '{worksheet_name}'.\n")

    for column in PLATFORM_COLUMNS:
        idx = column_index[column]
        raw_urls = [
            cell_hyperlink(row[idx])
            for row in data_rows
            if idx < len(row) and cell_hyperlink(row[idx])
        ]
        results = [normalise(u, column) for u in raw_urls]
        handles = unique_preserve_order(r for r in results if isinstance(r, Handle))
        skipped = [r for r in results if isinstance(r, Skipped)]

        slug = slugify(column)
        handles_path = output_dir / f"{slug}_handles.csv"
        skipped_path = output_dir / f"{slug}_skipped.csv"
        write_handles_csv(handles_path, handles)
        write_skipped_csv(skipped_path, skipped)

        print(f"  {column}:")
        print(f"    raw URLs:       {len(raw_urls)}")
        print(f"    unique handles: {len(handles)} -> {handles_path}")
        print(f"    skipped:        {len(skipped)} -> {skipped_path}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
