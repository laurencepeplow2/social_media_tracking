"""Run each comment through Claude, write results back to the full_dataset tab.

Behaviours in this version:
- Follow-up gating: each `question_group` has one `root` question and zero or
  more `follow_up`s. Follow-ups only run when the root verdict is "yes".
- Structured outputs (`output_config.format` json_schema) so the verdict
  enum and one-sentence justification are guaranteed parseable.
- Prompt caching: the (role + question) prefix is sent as cacheable system
  blocks. Below Haiku 4.5's 4K-token cache threshold today — `usage` will
  show `cache_creation`/`cache_read_input_tokens = 0` until the role grows
  — but the request shape is correct for when prompts expand.
- Left-join MEP metadata (Last Name, First Name, Country, Group, National
  party, New vs. re-elected, ENVI, TRAN, ITRE) from column U of the "MEPs "
  tab on the T&E sheet. Join key is the canonical X/Twitter URL.
- One wide row per comment is written to the `full_dataset` tab of the
  social_media_tracking sheet, prefixed with the joined MEP fields and
  suffixed with one (verdict, justification) pair per question.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from extract_handles import Handle, cell_hyperlink, normalise

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
PRICE_PER_MTOK = {  # Haiku 4.5 per Anthropic pricing
    "input": 1.0,
    "output": 5.0,
    "cache_write": 1.25,   # 1.25× input for 5-min TTL
    "cache_read": 0.10,    # 0.1× input
}

MEP_TAB = "MEPs "  # the trailing space is in the source sheet, not a typo
MEP_FIELDS = [
    "Last Name", "First Name", "Country", "Group", "National party",
    "New vs. re-elected", "ENVI", "TRAN", "ITRE",
]
MEP_TWITTER_COLUMN_INDEX = 20  # column U
MEP_HEADER_ROW_INDEX = 1       # row 2 in the sheet (row 1 is a section banner)

OUTPUT_TAB = "full_dataset"


@dataclass(frozen=True)
class Config:
    model: str
    role: str
    minimum_characters: int


@dataclass(frozen=True)
class Question:
    question_id: str
    team: str
    question_type: str       # "root" or "follow_up"
    question_group: str
    question_tag: str
    question: str


@dataclass(frozen=True)
class Comment:
    social_media_address: str
    comment: str
    timestamp: str


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"analyse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient", "google", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path


def sheets_service(service_account_file: Path):
    creds = Credentials.from_service_account_file(str(service_account_file), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_grid(service, spreadsheet_id: str, tab: str) -> list[list[dict]]:
    fields = "sheets.data.rowData.values(formattedValue,hyperlink,textFormatRuns(format/link/uri))"
    response = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, ranges=[tab], fields=fields,
    ).execute()
    sheets = response.get("sheets", [])
    if not sheets or not sheets[0].get("data"):
        return []
    return [r.get("values", []) for r in sheets[0]["data"][0].get("rowData", [])]


def load_config(service, spreadsheet_id: str) -> Config:
    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="config",
    ).execute().get("values", [])
    kv = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
    return Config(
        model=kv["claude_model_selection"],
        role=kv["claude_role"],
        minimum_characters=int(kv["minimum_characters"]),
    )


def load_questions(service, spreadsheet_id: str) -> list[Question]:
    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="questions_for_ai",
    ).execute().get("values", [])
    if not rows:
        return []
    headers, *data = rows
    headers = [h.strip() for h in headers]
    out = []
    for row in data:
        padded = row + [""] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        if not rec.get("question", "").strip():
            continue
        out.append(Question(
            question_id=rec.get("question_id", "").strip(),
            team=rec.get("team", "").strip(),
            question_type=rec.get("question_type", "").strip(),
            question_group=rec.get("question_group", "").strip(),
            question_tag=rec.get("question_tag", "").strip(),
            question=rec["question"].strip(),
        ))
    return out


def load_comments(csv_path: Path) -> list[Comment]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            Comment(
                social_media_address=(row.get("social_media_address") or "").strip(),
                comment=(row.get("comment") or "").strip(),
                timestamp=(row.get("timestamp") or "").strip(),
            )
            for row in reader
            if (row.get("comment") or "").strip()
        ]


def load_mep_lookup(service, spreadsheet_id: str) -> dict[str, dict[str, str]]:
    """Return {normalised_twitter_url: {field: value, ...}} for every MEP with a Twitter URL."""
    grid = fetch_grid(service, spreadsheet_id, MEP_TAB)
    if len(grid) <= MEP_HEADER_ROW_INDEX:
        logging.warning(f"MEPs tab has no data rows (got {len(grid)} rows)")
        return {}
    header_cells = grid[MEP_HEADER_ROW_INDEX]
    data_rows = grid[MEP_HEADER_ROW_INDEX + 1:]
    header_labels = [(c.get("formattedValue") or "").strip() for c in header_cells]
    field_index = {f: header_labels.index(f) for f in MEP_FIELDS if f in header_labels}
    missing = set(MEP_FIELDS) - field_index.keys()
    if missing:
        logging.warning(f"MEPs tab is missing fields: {sorted(missing)}")
    logging.info(f"MEPs field indices: {field_index}")

    lookup: dict[str, dict[str, str]] = {}
    matched_twitter = 0
    for row in data_rows:
        if MEP_TWITTER_COLUMN_INDEX >= len(row):
            continue
        twitter_url = cell_hyperlink(row[MEP_TWITTER_COLUMN_INDEX])
        if not twitter_url:
            continue
        norm = normalise(twitter_url, "X (Twitter)")
        if not isinstance(norm, Handle):
            continue
        matched_twitter += 1
        record = {
            field: (row[idx].get("formattedValue") or "").strip() if idx < len(row) else ""
            for field, idx in field_index.items()
        }
        lookup[norm.url] = record
    logging.info(f"Built MEP lookup: {len(lookup)} unique twitter URLs from {matched_twitter} cells")
    return lookup


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "no"]},
        "justification": {"type": "string"},
    },
    "required": ["verdict", "justification"],
    "additionalProperties": False,
}


def ask_claude(client: anthropic.Anthropic, config: Config, question: Question, comment: str) -> dict:
    """One classification call. Structured output, cached system prefix."""
    system_blocks = [
        {"type": "text", "text": config.role},
        {
            "type": "text",
            "text": (
                "Answer this question about the comment. Reply with verdict "
                f'"yes" or "no" and a one-sentence justification.\n\n'
                f"Question: {question.question}"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    start = time.monotonic()
    response = client.messages.create(
        model=config.model,
        max_tokens=512,
        system=system_blocks,
        messages=[{"role": "user", "content": f'Comment:\n"""\n{comment}\n"""'}],
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    )
    elapsed = time.monotonic() - start
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    parsed = json.loads(text)
    usage = response.usage
    return {
        "verdict": parsed.get("verdict", ""),
        "justification": parsed.get("justification", ""),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "elapsed_seconds": round(elapsed, 2),
        "stop_reason": response.stop_reason,
    }


def estimate_cost(in_tok: int, out_tok: int, cache_w: int, cache_r: int) -> float:
    return (
        in_tok / 1e6 * PRICE_PER_MTOK["input"]
        + out_tok / 1e6 * PRICE_PER_MTOK["output"]
        + cache_w / 1e6 * PRICE_PER_MTOK["cache_write"]
        + cache_r / 1e6 * PRICE_PER_MTOK["cache_read"]
    )


def group_questions(questions: list[Question]) -> dict[str, dict]:
    """Group questions by question_group. Each group → {"root": Question, "follow_ups": [Question, ...]}."""
    groups: dict[str, dict] = defaultdict(lambda: {"root": None, "follow_ups": []})
    for q in questions:
        key = q.question_group or q.question_id
        if q.question_type == "root":
            if groups[key]["root"] is not None:
                logging.warning(f"Multiple root questions for group {key!r}; keeping the first")
            else:
                groups[key]["root"] = q
        else:
            groups[key]["follow_ups"].append(q)
    return dict(groups)


def write_to_sheet(service, spreadsheet_id: str, tab: str, rows: list[list]) -> None:
    """Clear `tab` and write `rows` starting at A1. Requires Editor permission."""
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=tab, body={},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def main() -> int:
    load_dotenv()
    output_dir = Path(os.environ.get("OUTPUT_DIR", "data"))
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = setup_logging(Path("logs"))
    logging.info("=" * 78)
    logging.info("Starting comment analysis")
    logging.info(f"Log file: {log_path}")

    service_account_file = Path(os.environ["SERVICE_ACCOUNT_FILE"])
    mep_spreadsheet_id = os.environ["SPREADSHEET_ID"]
    config_spreadsheet_id = os.environ["CONFIG_SPREADSHEET_ID"]
    comments_csv = output_dir / "test_comments.csv"

    if not comments_csv.exists():
        logging.error(f"Missing input file: {comments_csv}")
        return 1

    service = sheets_service(service_account_file)

    logging.info(f"Reading config from {config_spreadsheet_id}")
    config = load_config(service, config_spreadsheet_id)
    logging.info(f"  model              = {config.model}")
    logging.info(f"  minimum_characters = {config.minimum_characters}")
    logging.info(f"  role               = {config.role!r}")

    questions = load_questions(service, config_spreadsheet_id)
    logging.info(f"Loaded {len(questions)} question(s)")
    for q in questions:
        logging.info(f"  Q{q.question_id} [{q.question_type:9}] group={q.question_group!r} tag={q.question_tag!r}")
    groups = group_questions(questions)
    logging.info(f"Grouped into {len(groups)} question_group(s):")
    for gname, g in groups.items():
        root = g["root"].question_tag if g["root"] else "(no root)"
        followups = [f.question_tag for f in g["follow_ups"]]
        logging.info(f"  {gname}: root={root}, follow_ups={followups}")

    logging.info(f"Loading MEP lookup from {mep_spreadsheet_id} / '{MEP_TAB}' tab")
    mep_lookup = load_mep_lookup(service, mep_spreadsheet_id)

    comments = load_comments(comments_csv)
    logging.info(f"Loaded {len(comments)} comment(s) from {comments_csv}")

    client = anthropic.Anthropic()
    logging.info("Anthropic client initialised")

    # Build output columns
    question_columns: list[tuple[str, Question]] = []
    for q in questions:  # preserve sheet order
        question_columns.append((f"{q.question_tag}_verdict", q))
        question_columns.append((f"{q.question_tag}_justification", q))
    header_row = (
        MEP_FIELDS
        + ["social_media_address", "comment", "timestamp"]
        + [name for name, _ in question_columns]
    )
    output_rows: list[list] = [header_row]

    # Local CSV mirror of the wide output (handy when sheet write fails)
    csv_mirror = output_dir / "full_dataset.csv"

    totals = {"calls": 0, "skipped_short": 0, "skipped_followup": 0, "errors": 0,
              "input_tokens": 0, "output_tokens": 0, "cache_w": 0, "cache_r": 0, "elapsed": 0.0}
    mep_hits = 0
    mep_misses = 0

    for i, c in enumerate(comments, start=1):
        comment_len = len(c.comment)
        if comment_len < config.minimum_characters:
            logging.info(f"[{i}/{len(comments)}] SKIP short ({comment_len} < {config.minimum_characters}) {c.social_media_address}")
            totals["skipped_short"] += 1
            continue

        # MEP lookup (left join — keep row even if no match)
        norm = normalise(c.social_media_address, "X (Twitter)")
        mep_record: dict[str, str] = {}
        if isinstance(norm, Handle):
            mep_record = mep_lookup.get(norm.url, {})
            if mep_record:
                mep_hits += 1
            else:
                mep_misses += 1
                logging.warning(f"  no MEP match for {norm.url}")
        else:
            mep_misses += 1
            logging.warning(f"  could not normalise {c.social_media_address!r}: {norm.reason}")

        # Per-question answers, keyed by column name
        answers: dict[str, str] = {name: "" for name, _ in question_columns}

        logging.info(f"[{i}/{len(comments)}] {c.social_media_address}  ({comment_len} chars)  mep={mep_record.get('Last Name','') or '<no match>'}")
        for gname, g in groups.items():
            root_q = g["root"]
            if root_q is None:
                logging.warning(f"  group {gname!r} has no root; running follow-ups directly")
                runlist = g["follow_ups"]
                gate_passed = True
            else:
                # Run root
                try:
                    result = ask_claude(client, config, root_q, c.comment)
                except Exception as e:
                    logging.exception(f"  root Q{root_q.question_id} failed: {e}")
                    totals["errors"] += 1
                    continue
                _accumulate(totals, result)
                logging.info(f"  Q{root_q.question_id} [root        {root_q.question_tag}] -> {result['verdict']:3}  in={result['input_tokens']:>4} out={result['output_tokens']:>4} cw={result['cache_creation_input_tokens']} cr={result['cache_read_input_tokens']} {result['elapsed_seconds']}s")
                logging.debug(f"     justification: {result['justification']}")
                answers[f"{root_q.question_tag}_verdict"] = result["verdict"]
                answers[f"{root_q.question_tag}_justification"] = result["justification"]
                gate_passed = result["verdict"] == "yes"
                runlist = g["follow_ups"]

            if not gate_passed:
                for fq in runlist:
                    answers[f"{fq.question_tag}_verdict"] = "skipped"
                    answers[f"{fq.question_tag}_justification"] = "root verdict was 'no'"
                    totals["skipped_followup"] += 1
                logging.info(f"     root='no' -> skipped {len(runlist)} follow-up(s)")
                continue

            for fq in runlist:
                try:
                    result = ask_claude(client, config, fq, c.comment)
                except Exception as e:
                    logging.exception(f"  follow-up Q{fq.question_id} failed: {e}")
                    totals["errors"] += 1
                    continue
                _accumulate(totals, result)
                logging.info(f"  Q{fq.question_id} [follow_up   {fq.question_tag}] -> {result['verdict']:3}  in={result['input_tokens']:>4} out={result['output_tokens']:>4} cw={result['cache_creation_input_tokens']} cr={result['cache_read_input_tokens']} {result['elapsed_seconds']}s")
                logging.debug(f"     justification: {result['justification']}")
                answers[f"{fq.question_tag}_verdict"] = result["verdict"]
                answers[f"{fq.question_tag}_justification"] = result["justification"]

        # Assemble the wide row
        row = (
            [mep_record.get(f, "") for f in MEP_FIELDS]
            + [c.social_media_address, c.comment, c.timestamp]
            + [answers[name] for name, _ in question_columns]
        )
        output_rows.append(row)

    # Local CSV mirror
    with csv_mirror.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(output_rows)
    logging.info(f"Wrote local mirror -> {csv_mirror}")

    # Write to Google Sheet
    try:
        logging.info(f"Writing {len(output_rows)-1} rows to {config_spreadsheet_id} / '{OUTPUT_TAB}'")
        write_to_sheet(service, config_spreadsheet_id, OUTPUT_TAB, output_rows)
        logging.info(f"  -> wrote to https://docs.google.com/spreadsheets/d/{config_spreadsheet_id}/edit#gid=...")
    except Exception as e:
        logging.error(f"Sheet write failed: {type(e).__name__}: {e}")
        logging.error("  (most likely cause: service account is Viewer; grant Editor on the sheet)")

    # Summary
    cost = estimate_cost(totals["input_tokens"], totals["output_tokens"], totals["cache_w"], totals["cache_r"])
    logging.info("=" * 78)
    logging.info("Run summary")
    logging.info(f"  comments processed       = {len(comments)}")
    logging.info(f"  skipped (too short)      = {totals['skipped_short']}")
    logging.info(f"  follow-ups skipped (gate)= {totals['skipped_followup']}")
    logging.info(f"  API calls                = {totals['calls']}")
    logging.info(f"  errors                   = {totals['errors']}")
    logging.info(f"  MEP joins matched/missed = {mep_hits}/{mep_misses}")
    logging.info(f"  tokens in / out          = {totals['input_tokens']:,} / {totals['output_tokens']:,}")
    logging.info(f"  cache write / read       = {totals['cache_w']:,} / {totals['cache_r']:,}")
    logging.info(f"  total elapsed (API)      = {totals['elapsed']:.2f}s")
    logging.info(f"  estimated cost           = ${cost:.6f}")
    logging.info("=" * 78)
    return 0


def _accumulate(totals: dict, result: dict) -> None:
    totals["calls"] += 1
    totals["input_tokens"] += result["input_tokens"]
    totals["output_tokens"] += result["output_tokens"]
    totals["cache_w"] += result["cache_creation_input_tokens"]
    totals["cache_r"] += result["cache_read_input_tokens"]
    totals["elapsed"] += result["elapsed_seconds"]


if __name__ == "__main__":
    sys.exit(main())
