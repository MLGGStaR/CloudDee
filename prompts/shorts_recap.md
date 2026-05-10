You are writing a YouTube **Short** recap of a long-form video.

The total Short is 60 seconds. Your narration must fit in **at most 56
seconds of speech** so a 1-second "Subscribe to {brand_name}" flash card
can be appended after.

The original long-form narration is below. Condense it into a punchy,
fast-paced voiceover that targets **130 words and MUST NOT exceed 140
words** (overrunning gets cut off mid-word). Make it:

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
  "narration": "the voiceover text, all in one paragraph (≤140 words)"
}
```

The original video's title was: {video_title}

The original full-length narration was:

{full_narration}
