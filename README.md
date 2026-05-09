# Docket

Fully automated YouTube content empire built on freshly released U.S. government public records.

A daily cron pulls new records from PACER (via CourtListener), NTSB, SEC EDGAR, DOJ press releases, and FOIA.gov. Claude scores each for narrative drama. Top picks per channel are turned into AI-narrated, FFmpeg-assembled videos and uploaded to YouTube — all without human input.

## What it does

```
06:00 UTC  →  Pull yesterday's records from every source
06:30 UTC  →  Score them with Claude Haiku
07:00 UTC  →  Pick top story per active channel
07:15 UTC  →  Generate 6–12 minute script with Claude Sonnet
07:45 UTC  →  Voice it with OpenAI TTS
08:00 UTC  →  Generate visuals (stock + AI fallback)
08:30 UTC  →  Assemble video with FFmpeg
09:00 UTC  →  Upload to YouTube via Data API
```

Owner workload after setup: **roughly fifteen minutes a week**, mostly checking flagged content reports.

## Cost

- **Infrastructure:** $0 (GitHub Actions free tier).
- **Per video:** ~$0.60–1.00 in API usage (Claude + OpenAI TTS + image gen).
- **Three channels, daily:** ~$70/month in total API cost.

No subscriptions. No VPS. No third-party SaaS. The only cards on file are OpenAI and Anthropic, billed by usage.

## What you need to do

See [SETUP.md](SETUP.md) for the full step-by-step. Short version:

1. Fork this repo to your own GitHub.
2. Get API keys: [Anthropic](https://console.anthropic.com), [OpenAI](https://platform.openai.com).
3. Create three YouTube channels (one Google account each is cleanest).
4. Create a Google Cloud project, enable the YouTube Data API v3, generate OAuth refresh tokens.
5. Drop everything into GitHub Actions secrets.
6. Push. Done.

After that you do nothing. The cron runs daily.

## What's in the box

```
docket/
├── README.md                 # This file
├── SETUP.md                  # Step-by-step setup guide for the user
├── ARCHITECTURE.md           # How the pipeline works internally
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
├── .gitignore
├── schema.sql                # SQLite schema
├── cli.py                    # Manual run / debugging entrypoint
├── config/
│   ├── channels.yaml         # Channel definitions (sources, voice, prompts, niche)
│   └── sources.yaml          # Data source configurations
├── prompts/                  # Claude prompt templates
│   ├── score.md
│   ├── script_aviation.md
│   ├── script_court.md
│   ├── script_sec.md
│   └── thumbnail.md
├── src/
│   ├── config.py             # YAML/env loading
│   ├── db.py                 # SQLite layer
│   ├── utils.py              # Shared helpers
│   ├── pipeline.py           # Daily orchestrator
│   ├── score.py              # Claude scoring
│   ├── script.py             # Claude script generation
│   ├── voice.py              # OpenAI TTS
│   ├── images.py             # OpenAI images + stock fallback
│   ├── render.py             # FFmpeg video assembly
│   ├── thumbnail.py          # Thumbnail generation
│   ├── ingest/
│   │   ├── base.py
│   │   ├── ntsb.py
│   │   ├── sec.py
│   │   ├── courtlistener.py
│   │   └── doj.py
│   ├── upload/
│   │   └── youtube.py
│   └── stock/
│       └── pexels.py
├── assets/
│   ├── music/                # (You drop royalty-free music beds here)
│   ├── fonts/                # (Inter / Bebas Neue, public domain)
│   └── intros/               # (Optional 3-second per-channel sting)
└── .github/workflows/
    └── daily.yml             # Cron schedule + secret wiring
```

## Quick local test

You can run the entire pipeline against one record on your laptop before wiring GitHub Actions:

```bash
git clone <your-fork>
cd docket
pip install -r requirements.txt
cp .env.example .env  # then fill in keys
python cli.py ingest --source ntsb --limit 10
python cli.py score
python cli.py produce --channel final-approach --dry-run
```

The `--dry-run` flag stops short of YouTube upload so you can inspect the .mp4 locally.

## License

MIT. Build your empire.
