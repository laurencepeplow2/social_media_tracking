# social_media_tracking

Extracts unique X (Twitter), Facebook, and Instagram handles for Members of the European Parliament from the T&E MEP mapping Google Sheet, normalises them per platform, and writes one CSV per platform ready to feed into a tracking pipeline.

## What it does

1. Authenticates to Google Sheets via a service account
2. Reads the **Social media** tab of the configured spreadsheet
3. For each row, pulls the hyperlink target from the `X (Twitter)`, `Facebook`, and `Instagram` columns — the cells display platform names as link text, with the actual URL stored as a rich-text hyperlink, so we use the Sheets API's grid-data endpoint (`cell.hyperlink`) rather than the Values API
4. Normalises URLs per platform:
   - `twitter.com` / `mobile.twitter.com` → `x.com`
   - Strips `www.`, query strings, tracking params (`?igsh=…`, `?utm_source=qr`, `?hl=de`), and trailing slashes
   - Lowercases handles; strips `@` prefix
   - Preserves Facebook numeric profile IDs (`profile.php?id=N`)
5. Deduplicates and writes per-platform CSVs

## Project structure

```
social_media_tracking/
├── extract_handles.py      # main script
├── requirements.txt        # pinned dependencies
├── .env.example            # copy to .env and fill in
├── .gitignore              # excludes secrets, venv, outputs
├── service_account.json    # NOT committed — see Setup
├── .env                    # NOT committed — see Setup
└── data/                   # generated output, gitignored
    ├── x__twitter_handles.csv
    ├── x__twitter_skipped.csv
    ├── facebook_handles.csv
    ├── facebook_skipped.csv
    ├── instagram_handles.csv
    └── instagram_skipped.csv
```

## Setup

### 1. Clone and create a virtual environment

```powershell
git clone https://github.com/<your-org>/social_media_tracking.git
cd social_media_tracking
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Add the Google service account key

Drop your `service_account.json` (downloaded from Google Cloud Console) into the project root. The file is gitignored.

### 3. Share the target Google Sheet with the service account

The service account's email (from `service_account.json` → `client_email`) needs **Viewer** access on the sheet. In Google Sheets: **Share** → paste the email → set role to Viewer → **untick "Notify people"** (service accounts have no inbox).

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in:

```ini
SPREADSHEET_ID=...        # the long ID from the sheet URL
WORKSHEET_NAME=Social media
SERVICE_ACCOUNT_FILE=service_account.json
OUTPUT_DIR=data
ANTHROPIC_API_KEY=...     # only if you extend the project to call Claude
```

The Spreadsheet ID is the long string in the sheet URL: `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`.

## Run

```powershell
.venv\Scripts\python.exe extract_handles.py
```

Example output:

```
Loaded 721 data rows from 'Social media'.

  X (Twitter):
    raw URLs:       380
    unique handles: 374 -> data\x__twitter_handles.csv
    skipped:        6 -> data\x__twitter_skipped.csv

  Facebook:
    raw URLs:       387
    unique handles: 373 -> data\facebook_handles.csv
    skipped:        7 -> data\facebook_skipped.csv

  Instagram:
    raw URLs:       343
    unique handles: 337 -> data\instagram_handles.csv
    skipped:        6 -> data\instagram_skipped.csv
```

## Outputs

### `<platform>_handles.csv` — one row per unique normalised handle

| handle | url |
|---|---|
| vilimsky | https://x.com/vilimsky |
| stegerpetra | https://facebook.com/stegerpetra |
| haraldvilimsky | https://instagram.com/haraldvilimsky |

### `<platform>_skipped.csv` — URLs that failed the platform check, with a reason

| raw_url | reason |
|---|---|
| https://www.linkedin.com/in/... | wrong host for Instagram: linkedin.com |
| https://el-gr.facebook.com/... | wrong host for Facebook: el-gr.facebook.com |

Common skip reasons:
- **Wrong host** — wrong platform pasted into the column (e.g. LinkedIn URL in the Instagram column)
- **EuroParl prefix** — the URL got concatenated with the MEP's EuroParl page URL
- **Locale-prefixed host** — `el-gr.facebook.com`, `it-it.facebook.com`, `sk-sk.facebook.com` (could be rescued in a future pass)

## Security

- `service_account.json` and `.env` are gitignored. Never commit them.
- `data/` is gitignored — outputs are regenerable from the source sheet.
- The service account has read-only Sheets scope (`spreadsheets.readonly`).

## Dependencies

- `gspread` + `google-auth` — convenience client for shared-prefix Sheets calls
- `google-api-python-client` — raw Sheets API for hyperlink extraction
- `python-dotenv` — load `.env` into `os.environ`
- `anthropic` — Claude SDK (for future LLM-driven extensions)
