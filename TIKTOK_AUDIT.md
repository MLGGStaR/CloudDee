# TikTok App Audit — Submission Guide

To unlock public auto-posting on TikTok, the CloudDee app must be audited.
Until then, every upload is forced to `privacy_level=SELF_ONLY` by TikTok
regardless of what the API requests. The audit submission requires a short
**screen-recorded demo video** of the integration. This guide tells you
exactly what to record, what to type into the form, and what to submit.

Total time: **~10 minutes of recording + clicking submit.** Review then
takes ~7 days.

---

## 1. Before recording — one-time setup

You need three things on screen / configured:

1. **Your CloudDee homepage open in a browser:**
   `https://mlggstar.github.io/CloudDee/`
2. **A terminal in your `Desktop/1M/docket` folder** with your `.env`
   populated (the same one used for `python cli.py oauth-init`).
3. **The TikTok mobile app installed** on a phone you can record (or use
   tiktok.com on a desktop browser instead — same flow).

Pick a record to be the demo upload — e.g. the most recent NTSB short
that's already been rendered and is sitting in `output/final-approach/`.
You can also trigger a fresh dry-run with `longs=0, shorts=1, dry_run=true`
to render one specifically for the demo.

---

## 2. What to record (target: 90–120 seconds)

Use any screen recorder (Windows: Win+G, macOS: Cmd+Shift+5). Record in
order:

### Scene A — the website (5 seconds)
- Browser at `https://mlggstar.github.io/CloudDee/`. Pan slowly so the
  reviewer can see the homepage, "About CloudDee" section, and the
  Terms / Privacy links.

### Scene B — initiating Login Kit auth (15 seconds)
- Cut to your terminal.
- Type: `python cli.py tiktok-oauth-init`
- Show the printed authorization URL.
- Open that URL in your browser.

### Scene C — TikTok consent screen (10 seconds)
- TikTok consent screen appears. Show:
  - "CloudDee wants to access your account"
  - Scopes listed: **user.info.basic, video.publish**
- Click **Authorize**.

### Scene D — code paste back into CLI (10 seconds)
- Browser redirects to `https://mlggstar.github.io/CloudDee/?code=...&state=...`
- Highlight the `code` value in the URL bar.
- Cut back to terminal, paste the code at the prompt.
- Refresh token is printed.

### Scene E — initiating an upload (15 seconds)
- In the terminal, run (for the demo):
  `python cli.py daily` (or whatever single-short command you prefer)
- Show the log lines:
  - `tiktok init OK publish_id=...`
  - `tiktok upload bytes OK`
  - `tiktok status: PROCESSING_UPLOAD`
  - `tiktok publish complete: ...`

### Scene F — the result on TikTok (15 seconds)
- Open the TikTok app (or tiktok.com).
- Show your profile.
- The newly posted video appears (will be `Only me` / private — that's
  expected and correct for an unaudited app, point it out: this is
  exactly why we're submitting for audit).
- Tap the video to show it plays, has the correct caption with hashtags.

### Scene G — close (5 seconds)
- Brief end screen or cut. Total ~90–120 seconds.

---

## 3. What to put in the submission form

### "Explain how each product and scope works" (the 1000-char field)

Use the same text we already drafted:

```
CloudDee is a server-to-server automation that publishes AI-narrated
documentary breakdowns of US public records (NTSB aviation reports,
federal court filings, DOJ press releases, SEC enforcement) to a single
TikTok account owned by the developer.

Login Kit (user.info.basic): Used once at initial setup for the
developer to authenticate and obtain a refresh token for their own
TikTok account. The token is stored as an encrypted GitHub Actions
secret and used solely to authenticate subsequent automated uploads.
The app has a single registered user — the developer themselves.

Content Posting API (video.publish): A scheduled GitHub Actions
workflow runs daily. It posts ~3 vertical 9:16 mp4 videos (each ≤60s)
to the developer's TikTok account via Direct Post. Each video is
original editorial content built from publicly available US Government
records. Captions include the title, a source citation, and an
AI-content disclosure. No third-party data is collected; no third-party
content is posted.
```

### "Upload at least one demo video"
- Upload the screen recording from section 2.

### Anything else they ask
- App category: **News / Education / Documentary**
- Target audience: **Adults interested in public-records journalism**
- Why public scope is needed: *"The app posts the developer's own
  original editorial content to the developer's own TikTok account. The
  audience is the public; private-only posting would defeat the
  channel's purpose."*

---

## 4. After submission

- Review usually takes 3–7 business days.
- TikTok may email follow-up questions. Reply within 48h to avoid the
  review being closed.
- Once approved: in GitHub, go to **Settings → Secrets and variables →
  Actions → Variables tab** and set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`.
  Next cron run will start posting publicly. **No code changes needed.**

If review is **rejected**, the rejection notice will explain why — most
common: demo video didn't clearly show the OAuth flow + an actual API
call. Re-record covering the missing scene, re-submit. No penalty.
