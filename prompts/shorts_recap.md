You are writing a YouTube **Short** recap of a long-form video.

The total Short must be ≤60 seconds and the editor appends a 1-second
"Subscribe to {brand_name}" flash card after your narration. The voice
runs at 0.95× speed, so 140 words ≈ 59 seconds of audio — too long.

The original long-form narration is below. Condense it into a punchy,
fast-paced voiceover that targets **110 words and MUST NOT exceed 125
words** (overrunning gets hard-truncated mid-word and the subscribe
card disappears). Make it:

- **Opens hard** in the first sentence — dollar figure, casualty count,
  date, named aircraft, name of the defendant. Whatever is most concrete.
- **Tells the whole arc** — what happened, how it happened, what was found.
  Compressed but coherent. Do not just paraphrase the long-form's hook.
- **Ends with one sharp closing line** — the lesson, the verdict, the
  outcome. Resonant.
- **Does NOT end with a CTA** — the editor appends a 1-second "subscribe to
  {brand_name}" flash card after the audio ends.
- Uses the same source-citation rules as the long-form: cite by domain only
  (e.g. "data.ntsb.gov", "sec.gov", "justice.gov"). Never read full URLs.
- No "in this video," "today on the channel," "let's break down."

Return STRICTLY a JSON object:

```json
{
  "narration": "the voiceover text, all in one paragraph (≤125 words)"
}
```

The original video's title was: {video_title}

The original full-length narration was:

{full_narration}
