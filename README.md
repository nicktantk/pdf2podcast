# Pdf2Podcast

Turn papers and ebook PDFs into chaptered audio companions with a shared library,
streaming playback, and synchronized transcripts.

- Near-verbatim source narration, with citations and page furniture filtered out.
- A second voice introduces chapters and gives concise paragraph summaries.
- Native iOS and Mac Catalyst apps, plus a browser client.
- Paragraph-level audio checkpoints, parallel processing, and resumable conversions.
- Sleep timers, playback speed controls, transcript navigation, and summary skipping.

## Repository layout

```text
apps/Pdf2Podcast/ios/       iOS app, PDF share extension, and shared player
apps/Pdf2Podcast/macos/     Mac Catalyst target and copy-based PDF importer
apps/Pdf2Podcast/web/       Browser client
plugins/pdf2podcast/        Standalone backend and PDF processing pipeline
docs/                      Processing design notes
```

This repository contains the standalone application, not the older Jarvis fleet
gateway. Xcode projects are generated from the checked-in `project.yml` files.

## Run the backend

Install Python 3.10 or newer and FFmpeg, including `ffprobe`. Then:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
chmod 600 .env
```

Edit the local `.env` file with your own OpenAI API key and a newly generated
mobile token. Generate a token locally with `openssl rand -hex 32`. The provider
API key belongs only on the backend; clients need only the mobile token.

```sh
set -a
source .env
set +a
pdf2podcast-server --host 127.0.0.1 --port 8766
```

On Windows, run the same commands in Git Bash, using `python -m venv .venv`,
`source .venv/Scripts/activate`, and skipping `chmod`. FFmpeg can be installed with
`winget install Gyan.FFmpeg`; open a new terminal afterwards so it is on `PATH`.

The server accepts `OPENAI_API_KEY`, or a private key file selected with
`PDF2PODCAST_OPENAI_KEY_FILE`. To use DeepSeek instead, uncomment the DeepSeek block
in `.env`; it sets `DEEPSEEK_API_KEY`, the DeepSeek base URL and model, and a higher
output token limit. Uploaded PDFs, checkpoints, transcripts, and finished
audio remain under `PDF2PODCAST_DATA_DIR`, which defaults to `~/.pdf2podcast`.
Speech currently uses Edge TTS; an ElevenLabs integration is not included.

For remote devices, expose the backend through an HTTPS reverse proxy or tunnel.
For example, with Cloudflare's `cloudflared` installed:

```sh
cloudflared tunnel --url http://127.0.0.1:8766
```

Use the resulting HTTPS URL in the clients. A temporary tunnel URL can change
when the tunnel restarts. All clients using the shared mobile token see the same
library. Treat that token as a credential and do not publish it or put it in a URL.
This is a shared-token prototype, not per-user authentication or a public upload service.

## iOS app

Install Xcode and XcodeGen. From `apps/Pdf2Podcast/ios`:

```sh
xcodegen generate
open Pdf2Podcast.xcodeproj
```

Choose your own development team for the app and its share extension, then build
for your iPhone. Open connection settings and enter your backend URL and mobile
token. No provider key or prefilled mobile token is included in the source.

## Mac app

See [the Mac build instructions](apps/Pdf2Podcast/macos/README.md). The Mac target
shares the iOS player and uses a copy-based importer with temporary upload storage.
Connection settings are stored separately on each device; library data lives on
the shared backend.

## Web app

```sh
python3 -m http.server 8080 --directory apps/Pdf2Podcast/web
```

Open `http://localhost:8080`, then enter the backend URL and mobile token in
connection settings. For a hosted HTTPS frontend, use an HTTPS backend as well.
The web client stores its connection settings in that browser.

## Credential checks

Keep `.env`, key files, signing material, local settings exports, uploaded PDFs,
generated audio, and build products out of Git. The repository's ignore rules
exclude these artifacts, but ignore rules do not sanitize files already tracked.

With Gitleaks installed, scan the working tree and every reachable commit before
pushing:

```sh
gitleaks dir . --redact --max-decode-depth 2
gitleaks git . --log-opts="--all" --redact --max-decode-depth 2
```

The configuration extends the standard secret rules with a rule for unlabelled
long hexadecimal credentials. If a real credential is ever published, rotate it;
deleting the current file alone does not remove historical exposure.
