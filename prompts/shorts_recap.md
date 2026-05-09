You are writing a 55-second YouTube **Short** recap of a long-form video.

The original long-form narration is below. Condense it into a punchy,
fast-paced ~55-second voiceover (around **140 words**, no more than 150) that:

- **Opens hard** in the first sentence — dollar figure, casualty count,
  date, named aircraft, name of the defendant. Whatever is most concrete.
- **Tells the whole arc** — what happened, how it happened, what was found.
  Compressed but coherent. Do not just paraphrase the long-form's hook.
- **Ends with one sharp closing line** — the lesson, the verdict, the
  outcome. Resonant.
- **Does NOT end with a CTA** — the editor appends a 3-second "subscribe to
  {brand_name}" outro card after the audio ends.
- Uses the same source-citation rules as the long-form: cite by domain only
  (e.g. "data.ntsb.gov", "sec.gov", "justice.gov"). Never read full URLs.
- No "in this video," "today on the channel," "let's break down."

Return STRICTLY a JSON object:

```json
{
  "narration": "the 55-second voiceover text, all in one paragraph"
}
```

The original video's title was: {video_title}

The original full-length narration was:

{full_narration}
