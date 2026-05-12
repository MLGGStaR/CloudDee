"""Command-line entrypoint for manual ops, debugging, and the cron itself.

Examples:
    python cli.py daily
    python cli.py ingest --source ntsb_aviation --limit 25
    python cli.py score
    python cli.py produce --channel final-approach --dry-run
    python cli.py oauth-init                    # one-time, gets refresh tokens
    python cli.py status
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Legacy Windows console (cp1252) can't render Unicode in our log/help output.
# Force UTF-8 so we don't crash on a → or an em-dash.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import typer
from rich import print as rprint
from rich.table import Table

from src import ingest as ingest_mod
from src.config import channel_by_slug, load_settings
from src.db import connect, top_records_for_channel
from src.pipeline import produce_one_for_channel, run_daily
from src.score import score_pending
from src.utils import setup_logging


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def daily():
    """Run the full daily pipeline (ingest → score → produce → upload)."""
    run_daily()


@app.command()
def ingest(source: str = typer.Option(..., help="Source slug, e.g. ntsb_aviation"),
           limit: int = typer.Option(100, help="Max records to fetch this run")):
    """Pull records from one source."""
    settings = load_settings()
    setup_logging(settings.log_level)
    if source not in settings.sources:
        rprint(f"[red]Unknown source:[/red] {source}")
        rprint(f"Available: {', '.join(sorted(settings.sources))}")
        raise typer.Exit(2)
    with connect(settings.db_path) as conn:
        n = ingest_mod.run(settings, conn, source)
    rprint(f"[green]Ingested {n} new records from {source}[/green]")


@app.command()
def score(limit: int = typer.Option(100, help="Max records to score this pass")):
    """Score all unscored records."""
    settings = load_settings()
    setup_logging(settings.log_level)
    with connect(settings.db_path) as conn:
        n = score_pending(settings, conn, limit=limit)
    rprint(f"[green]Scored {n} records[/green]")


@app.command()
def produce(channel: str = typer.Option(..., help="Channel slug"),
            dry_run: bool = typer.Option(False, "--dry-run", help="Skip YouTube upload")):
    """Produce one video for a channel."""
    import os
    settings = load_settings()
    if dry_run:
        os.environ["DOCKET_DRY_RUN"] = "1"
        settings.dry_run = True
    setup_logging(settings.log_level)
    ch = channel_by_slug(settings, channel)
    if ch is None:
        rprint(f"[red]Unknown channel:[/red] {channel}")
        raise typer.Exit(2)
    with connect(settings.db_path) as conn:
        result = produce_one_for_channel(settings, conn, ch)
    if result is None:
        rprint("[yellow]No eligible record found[/yellow]")
    else:
        rprint(json.dumps(result, indent=2))


@app.command()
def status():
    """Print queue + recent run summary."""
    settings = load_settings()
    setup_logging(settings.log_level)
    with connect(settings.db_path) as conn:
        rec_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        prod = conn.execute(
            "SELECT channel_slug, status, COUNT(*) FROM productions GROUP BY channel_slug, status"
        ).fetchall()

        rprint(f"[bold]Records:[/bold] {rec_count}    [bold]Scored:[/bold] {scored}")
        if prod:
            t = Table(title="Productions")
            t.add_column("Channel"); t.add_column("Status"); t.add_column("Count", justify="right")
            for r in prod:
                t.add_row(r[0], r[1], str(r[2]))
            rprint(t)

        for ch in settings.channels:
            if not ch.enabled:
                continue
            top = top_records_for_channel(
                conn, channel_slug=ch.slug, limit=3, min_total=15,
                sources=ch.sources or None,
            )
            if not top:
                continue
            t = Table(title=f"Top candidates · {ch.slug}")
            t.add_column("Total"); t.add_column("Title")
            for r, s in top:
                t.add_row(str(s.drama + s.novelty + s.visualization), r.title[:80])
            rprint(t)


@app.command("oauth-init")
def oauth_init():
    """Run an interactive OAuth flow to get a YouTube refresh token for one channel.
    Run once per channel. Prints a JSON snippet to drop into YT_REFRESH_TOKENS_JSON."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = load_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        rprint("[red]GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env[/red]")
        raise typer.Exit(2)

    client_config = {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    # Scopes:
    #   youtube.upload   — videos.insert (upload the file)
    #   youtube          — videos/playlists read+write, set thumbnails
    #   youtube.force-ssl — captions.insert + commentThreads.insert
    #                      (both required for our captions + auto-comment
    #                      flow; without it those calls return 403
    #                      "insufficientPermissions")
    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
    )
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    rprint("\n[bold green]Refresh token obtained.[/bold green]")
    rprint("Add this to your YT_REFRESH_TOKENS_JSON env var (replace channel-slug):\n")
    rprint(json.dumps({"channel-slug": creds.refresh_token}, indent=2))


@app.command("tiktok-oauth-init")
def tiktok_oauth_init():
    """One-time: get a TikTok refresh token for the channel's TikTok account.

    Reads TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET from the environment
    (see .env / GitHub Actions secrets). Opens TikTok's auth URL — sign in
    with the channel's TikTok account, click Authorize, then copy the
    `code` query param from the redirect URL back into this terminal.
    Prints the refresh_token to drop into the TIKTOK_REFRESH_TOKEN secret.
    """
    import os
    import urllib.parse

    from src.upload.tiktok import exchange_code_for_tokens

    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not client_key or not client_secret:
        rprint("[red]TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET must be set in .env[/red]")
        raise typer.Exit(2)

    redirect_uri = "https://mlggstar.github.io/CloudDee/"
    state = "clouddee-oauth"
    params = {
        "client_key": client_key,
        "scope": "user.info.basic,video.publish",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"https://www.tiktok.com/v2/auth/authorize/?{urllib.parse.urlencode(params)}"

    rprint("\n[bold]1.[/bold] Open this URL in your browser (logged into the right TikTok account):\n")
    rprint(f"   {auth_url}\n")
    rprint("[bold]2.[/bold] Sign in to TikTok and click Authorize.")
    rprint(f"[bold]3.[/bold] Your browser will land on {redirect_uri} with [cyan]?code=XXX&state={state}[/cyan]")
    rprint("[bold]4.[/bold] Copy the [cyan]code[/cyan] value (everything between [cyan]code=[/cyan] and [cyan]&state=[/cyan]) and paste below.\n")

    code = typer.prompt("Paste the code value")

    tokens = exchange_code_for_tokens(
        client_key=client_key,
        client_secret=client_secret,
        code=code.strip(),
        redirect_uri=redirect_uri,
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        rprint(f"[red]No refresh_token in response. Full response:[/red]\n{tokens}")
        raise typer.Exit(2)

    rprint("\n[bold green]TikTok refresh token obtained.[/bold green]")
    rprint("Add this to your [cyan]TIKTOK_REFRESH_TOKEN[/cyan] GitHub Actions secret:\n")
    rprint(refresh_token)


if __name__ == "__main__":
    app()
