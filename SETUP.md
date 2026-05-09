# Setup — what YOU need to do

This file is the only thing you have to read. Follow it top to bottom and the
pipeline will be running daily, by itself, by the end.

There is no part of this you can skip. Most of it is one-time configuration.
Total wall time, including waiting on Google's verification screens: about
two to three hours. After that, you do nothing.

---

## 0. The 60-second mental model

You will:
1. Get API keys from two paid services (OpenAI + Anthropic) and one free one
   (Pexels). Total cost for first month: roughly $5–10 in usage credits.
2. Create three free YouTube channels.
3. Make a Google Cloud project and click through OAuth so the pipeline can
   upload to those channels on its behalf.
4. Push this repo to your own GitHub.
5. Paste the keys into GitHub Actions secrets.
6. Push once. From then on, GitHub runs the pipeline daily for free.

Everything below is a more detailed version of that.

---

## 1. AI provider keys

### 1a. Anthropic (for scoring + script writing)

1. Go to <https://console.anthropic.com>.
2. Create an account, add a payment method, deposit $20.
3. Go to **API Keys** → **Create key**. Name it `docket`.
4. Copy the key starting with `sk-ant-…` somewhere safe. You will paste it
   into GitHub later.

### 1b. OpenAI (for TTS + image gen)

1. Go to <https://platform.openai.com>.
2. Create an account, add a payment method, deposit $20.
3. Go to **API Keys** → **Create new secret key**. Name it `docket`.
4. Copy the key starting with `sk-…` somewhere safe.

That's $40 of credit, which at our cost per video (~$0.60–1.00) buys you
40–60 videos. You will not need to top up for at least a month at three
channels per day.

---

## 2. Pexels (free stock footage)

1. Go to <https://www.pexels.com/api/>.
2. Sign up (no card).
3. Confirm email, click **Your API Key**.
4. Copy the key.

This is optional but strongly recommended — without it, every visual is AI
generated, which costs more.

---

## 3. (Optional) CourtListener

If you want federal court records (used by The Verdict Files channel):

1. Go to <https://www.courtlistener.com> → register (free).
2. Account settings → **API key**. Copy it.

If you skip this, the pipeline falls back to RSS for that channel — fewer
records, lower quality.

---

## 4. Create three YouTube channels

This is the slowest single step. YouTube wants you to use one Google account
per channel for cleanliness, though you can run multiple channels from a
single Brand Account if you prefer.

The cleanest approach:

1. In a fresh browser profile (or incognito), create a new Google account for
   each channel:
   - `final-approach.docket@gmail.com`
   - `verdict-files.docket@gmail.com`
   - `filed-under-wrong.docket@gmail.com`

   You can name them anything; the email isn't shown publicly.

2. For each account, go to <https://www.youtube.com> while signed in →
   click your avatar → **Create channel**.

3. Customize each channel: name (matches `config/channels.yaml`), profile
   image, banner, an "About" sentence. Five minutes per channel. Don't
   overthink — you can polish later.

4. **Verify each channel** at <https://www.youtube.com/verify>. This unlocks
   custom thumbnails and longer videos. Otherwise the pipeline can upload but
   not set thumbnails.

---

## 5. Google Cloud project + YouTube API + OAuth

This is the one fiddly step. Do it once.

### 5a. Create the project

1. Go to <https://console.cloud.google.com>.
2. Top bar → project dropdown → **New project**. Name it `docket`. Create.
3. Make sure the new project is selected in the top bar.

### 5b. Enable the YouTube Data API v3

1. Search bar → "YouTube Data API v3" → click the result.
2. Click **Enable**.

### 5c. Configure the OAuth consent screen

1. Left nav → **APIs & Services** → **OAuth consent screen**.
2. User type: **External**. Create.
3. App name: `docket`. User support email: yours. Developer contact: yours.
   Save and continue.
4. Scopes step: click **Add or remove scopes**. Search `youtube`. Tick:
   - `.../auth/youtube.upload`
   - `.../auth/youtube`
   Save and continue.
5. Test users step: click **Add users**. Add the email of every YouTube
   channel account you created in step 4. Save and continue.
6. Summary step: click **Back to dashboard**.

> Your app will stay in "testing" mode forever. That is fine — testing-mode
> apps work indefinitely for the test users you listed.

### 5d. Create OAuth client credentials

1. Left nav → **APIs & Services** → **Credentials**.
2. **Create credentials** → **OAuth client ID**.
3. Application type: **Desktop app**. Name: `docket-cli`. Create.
4. A modal pops up with **Client ID** and **Client secret**. Copy both.

### 5e. Get a refresh token for each channel (run locally)

You need to do this once per channel, on your laptop. The pipeline only
needs the resulting refresh token; it doesn't need a browser at runtime.

1. On your laptop, clone your fork (covered in step 6 below if you haven't
   yet). Then:

   ```bash
   cd docket
   pip install -r requirements.txt
   cp .env.example .env
   ```

2. Open `.env` and paste:
   - `ANTHROPIC_API_KEY=…`
   - `OPENAI_API_KEY=…`
   - `GOOGLE_CLIENT_ID=…`
   - `GOOGLE_CLIENT_SECRET=…`

3. For **each channel**, in a fresh browser session signed into that channel's
   Google account, run:

   ```bash
   python cli.py oauth-init
   ```

   A browser window opens. Sign in with **that channel's** Google account.
   Approve the scopes. The CLI prints a JSON snippet like:

   ```json
   {"channel-slug": "1//0gAbc..."}
   ```

   Replace `channel-slug` with the actual slug from `config/channels.yaml`
   (e.g. `final-approach`).

4. After all three channels, build the combined JSON yourself:

   ```json
   {
     "final-approach":    "1//0gAaa…",
     "verdict-files":     "1//0gBbb…",
     "filed-under-wrong": "1//0gCcc…"
   }
   ```

5. This combined JSON is what goes into the `YT_REFRESH_TOKENS_JSON` GitHub
   secret. Save it.

---

## 6. Push to your own GitHub

1. Make a new GitHub repo. Private is fine — GitHub Actions free tier on
   private repos gives you 2,000 minutes/month, well above what you need.
2. From inside `docket/`:

   ```bash
   git init
   git add .
   git commit -m "initial docket pipeline"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

---

## 7. Add GitHub Actions secrets

In your GitHub repo:

1. **Settings** → **Secrets and variables** → **Actions** → **New
   repository secret**.

2. Add each of the following (name → value):

   | Name | Value |
   |------|-------|
   | `ANTHROPIC_API_KEY` | from step 1a |
   | `OPENAI_API_KEY` | from step 1b |
   | `PEXELS_API_KEY` | from step 2 (optional) |
   | `COURTLISTENER_API_TOKEN` | from step 3 (optional) |
   | `GOOGLE_CLIENT_ID` | from step 5d |
   | `GOOGLE_CLIENT_SECRET` | from step 5d |
   | `YT_REFRESH_TOKENS_JSON` | the combined JSON from step 5e |

---

## 8. (Recommended) Drop in royalty-free music beds

The pipeline will run without music, but a music bed makes the videos
substantially better.

1. Download three royalty-free instrumental tracks from
   <https://pixabay.com/music/> (search "documentary," "tense strings,"
   "corporate") or <https://freesound.org>. Each track ~3–5 minutes long
   (the renderer loops them).

2. Rename and place them in `assets/music/`:
   - `aviation_drone.mp3`
   - `tense_strings.mp3`
   - `corporate_minor.mp3`

3. Commit and push.

---

## 9. (Recommended) Drop in fonts

Bold sans-serif fonts make thumbnails legible. The pipeline tries Bebas Neue
first, then Inter Bold, then OS defaults.

1. Download <https://fonts.google.com/specimen/Bebas+Neue> and
   <https://fonts.google.com/specimen/Inter>.
2. Place `BebasNeue-Regular.ttf` and `Inter-Bold.ttf` in `assets/fonts/`.
3. Commit and push.

---

## 10. Run a dry-run from GitHub

1. In your repo, go to the **Actions** tab.
2. Click **docket-daily** → **Run workflow**.
3. Set `dry_run` to `true`. Run.
4. Wait 5–15 minutes. The job should complete green.
5. Download the artifact named `run-<id>` and inspect:
   - `script.json` — does the script read well?
   - `thumbnail.png` — is the thumbnail decent?

If both look good, run again with `dry_run = false`. The next run will upload
to YouTube for real. Check each channel.

If something looks wrong, fix the prompt in `prompts/` or the channel config
in `config/channels.yaml`, push, re-run.

---

## 11. Walk away

You are done. The cron now runs at 11:00 UTC every day. Each morning, the
pipeline will:

- Pull yesterday's records from every source.
- Score them with Haiku.
- Pick the top story per channel.
- Write a script, voice it, find visuals, render the video.
- Upload to YouTube as **public**.

Look at your YouTube Studio analytics whenever you feel like it. You can
disable a channel by editing `config/channels.yaml` and setting
`enabled: false`, then pushing.

---

## What this costs you per month, in detail

| Item | Cost |
|------|-----:|
| GitHub Actions (private repo, ≤2,000 min/mo) | **$0** |
| Pexels, CourtListener, FOIA.gov, NTSB, SEC | **$0** |
| Anthropic — scoring (~30 records × Haiku/day) | **~$1** |
| Anthropic — script writing (3 scripts/day × Sonnet) | **~$8** |
| OpenAI — TTS (3 × 8-min/day, tts-1-hd) | **~$30** |
| OpenAI — thumbnails + occasional AI images | **~$15–20** |
| **Approx total** | **~$55–60 / month for 3 channels daily** |

That includes scoring all your candidate records. There are no fixed costs.
If a channel goes dark, the cost goes down.

---

## Common things that go wrong

**"Quota exceeded" from YouTube.** YouTube allots ~10,000 quota units per
day per project. Each upload costs ~1,600 units, so you can do six uploads/day
per project. With three channels uploading once each, you're nowhere near
the cap. If you scale to ten channels, request a quota increase
([form here](https://support.google.com/youtube/contact/yt_api_form)) — they
usually approve within a week.

**"AdSense rejected my channel."** AdSense requires 1,000 subs and 4,000
public watch hours in the trailing year for the YouTube Partner Program. The
pipeline gets you to that — patience. Don't apply early.

**"Thumbnails aren't appearing."** YouTube custom thumbnails require the
channel to be **verified** (step 4 above).

**"A run failed in GitHub Actions."** Click the run, scroll to the failed
step, read the log. Common: an API key not set, the SQLite cache restoring
oddly, or an ffmpeg error from a malformed source record. Fix and re-run
manually with **Run workflow**.

**"The script is dry / boring."** Tune `prompts/script_*.md`. The prompt is
the script writer. You can A/B by editing the temperature in `src/script.py`.

**"A channel is repeatedly failing on the same record type."** Check `state/
docket.db` (SQLite). The `productions` table has the error column. Edit the
relevant prompt, push, and the pipeline will pick a different record next
day.

---

## What the pipeline will NOT do for you

- It will not respond to YouTube comments.
- It will not handle copyright strikes (read the email and respond).
- It will not negotiate sponsorships (those will start arriving via DM and
  email after you cross ~25K subs on a channel; see PassionFroot and
  Famebit).
- It will not file your taxes. This is real income; track it from day one.

---

## What "done" looks like

You should be able to come back to this repo in six months and:

- See the GitHub Actions tab full of green daily runs.
- See three (or more) YouTube channels with steady daily uploads.
- See the analytics climb.
- Touch nothing.

That is the bargain.
