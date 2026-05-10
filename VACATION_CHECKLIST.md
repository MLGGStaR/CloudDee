# Vacation Checklist — July 25 → August 30, 2026

The pipeline is designed to run autonomously, but a few things have absolute
deadlines or external dependencies you should sanity-check **before you
leave**. None take more than five minutes.

---

## 🔴 CRITICAL — must verify before leaving

### 1. Google Cloud OAuth app status

**The single most likely thing to break a 5-week run.**

In Google Cloud Console → **APIs & Services → OAuth consent screen**,
check the **Publishing status** field.

- ✅ **"In production"** — refresh tokens last indefinitely. You're safe.
- ❌ **"Testing"** — refresh tokens expire **7 days after issuance**. The
  channel will silently stop uploading mid-vacation.

If it says "Testing":
- Click **"Publish app"** (this submits for Google verification).
- For sensitive scopes like `youtube.upload`, verification can take 1–7
  business days. **Submit at least 2 weeks before departure.**
- Don't wait until the day before — Google can request additional
  documentation (privacy policy URL, demo video) and the back-and-forth
  takes time.

Alternative if you can't get verified in time: do nothing. The pipeline
will run but uploads will fail after 7 days, and you'll have a backlog of
unpublished videos to catch up when you return.

### 2. API balance top-ups

The daily run burns roughly:

| API | Per video | Per day (4 videos) | 5 weeks (35 days) |
|---|---|---|---|
| Anthropic (Sonnet 4.6 scripts + Haiku scoring) | ~$0.15 | ~$0.60 | **~$22** |
| OpenAI (TTS-HD + Whisper + gpt-image-1) | ~$0.40 | ~$1.60 | **~$56** |

Top up to at least:
- **Anthropic console** → Plan & billing → make sure balance is **≥ $40**.
- **OpenAI billing** → make sure balance is **≥ $80**.

Pad both numbers — running out mid-vacation means dead days. Cost of
padding is zero; cost of running dry is several missed videos.

### 3. YouTube quota status

You requested an increase to 50K/day. Status:
- Check **Google Cloud Console → APIs & Services → YouTube Data API v3 →
  Quotas** before leaving.
- If still at the default 10K, the daily run (6,400 units) fits but you
  have zero headroom for retries. If approved (50K), you're golden.

### 4. Final dry-run smoke test

Run a dry-run (Actions → docket-daily → Run workflow → dry_run=true,
longs=1, shorts=1) **the day before you leave** to confirm everything
still renders. Download the `videos-*` artifact, eyeball the MP4s. If
they look right, you're good.

---

## 🟡 What auto-recovers if something goes wrong

The pipeline has belt-and-suspenders error handling for these:

| Failure | Auto-recovery |
|---|---|
| One source (NTSB / DOJ / SEC) goes down | Other sources still ingest, pipeline continues |
| One day's records are all unscoreable | Run completes empty, tomorrow tries again |
| One video render fails | Logged to summary, other videos in the batch still publish |
| YouTube returns quota-exceeded | Circuit breaker stops further uploads that day, tomorrow resumes |
| Synthetic-media API flag rejected | Retried without the flag (description still has AI disclosure) |
| GitHub Actions cron drops a day | Next day's run fires, dedup state preserved in repo + cache |
| GitHub Actions cache evicted | Daily heartbeat commit keeps SQLite backed up to repo |
| 60-day workflow disable timer | Daily bot commit resets the timer indefinitely |
| GitHub Node.js default flip (June 2026) | `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` env keeps us on the new runtime; all `actions/*` we use are at latest major and Node 24-compatible |

---

## 🔴 What does NOT auto-recover (and how to spot it)

| Failure | Symptom | What to do |
|---|---|---|
| OAuth token expired | Every run fails with "invalid_grant" in upload log | You must re-auth locally and update the `YT_REFRESH_TOKENS_JSON` GitHub secret. **Not fixable remotely from a phone.** |
| Anthropic/OpenAI balance hits zero | Runs fail mid-pipeline with "insufficient funds" | Top up in the respective console. ~5 min from a phone browser. |
| Channel strike / suspension | Uploads fail with policy error | Manual appeal via YouTube Studio. |
| GitHub Actions tier exceeded | Workflows queue indefinitely | You're nowhere near the 2,000 free min/month with 4 videos/day. Not a real risk. |

---

## 🟢 What to monitor (if you check in remotely)

The fast version, takes 30 seconds from a phone:

1. **GitHub → CloudDee repo → Actions tab.** If recent runs are all green
   ✅, everything's fine. If you see red ❌, click the most recent failure
   and look for the error type (probably one of the rows in the
   "does NOT auto-recover" table above).

2. **YouTube Studio → Content.** Are videos appearing daily? If not, check
   Actions tab.

You should not need to do anything else.

---

## 🛠 If you need to intervene from your phone

GitHub mobile app + your YouTube channel + your Anthropic/OpenAI
dashboards are all you need. Most issues are:

- "balance low" → tap "Add funds"
- "OAuth expired" → can't fix from phone, ask a trusted person with repo
  access, or accept the gap

Do not try to fix code from a phone. If a code bug appears, just wait
until you're back.

---

## 📝 Day-before-departure quick checklist

Copy-paste this, tick as you go:

- [ ] OAuth app is **"In production"** in Google Cloud Console
- [ ] Anthropic balance ≥ $40
- [ ] OpenAI balance ≥ $80
- [ ] Final dry-run completed and MP4s look right
- [ ] YouTube Studio shows no pending strikes/warnings
- [ ] You can log into the GitHub mobile app
