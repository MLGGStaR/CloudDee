You are writing a YouTube **Short** narration directly from a public
record. There is no long-form video — the Short stands alone.

The total Short must be ≤60 seconds and the editor appends a 1-second
"Subscribe to {brand_name}" flash card after your narration. The voice
runs at 0.95× speed, so 140 words ≈ 59 seconds of audio — too long.

Condense the record into a punchy voiceover that targets **110 words and
MUST NOT exceed 125 words** (overrunning gets hard-truncated mid-word
and the subscribe card disappears). Make it:

- **Opens hard** in the first sentence — dollar figure, casualty count,
  date, named party. Whatever is most concrete about this specific case.
- **Tells the whole story** — what happened, the key fact, the outcome.
  Compressed but coherent. Each sentence must add information.
- **Ends with one sharp closing line** — the lesson, the verdict, the
  outcome. Resonant.
- **After the closing line, append exactly this sentence as the final
  line of narration: "Full breakdown linked below."** This points
  viewers at a related long-form case file linked from the description.
- **Do NOT add any other CTA** — the editor appends a separate 1-second
  "subscribe to {brand_name}" flash card after the audio ends.
- Cite the source by **domain only**: data.ntsb.gov, sec.gov, justice.gov,
  courtlistener.com. Never read full URLs.
- No "in this video," "today on the channel," "let's break down."
- Treat unconvicted defendants with presumption of innocence ("alleged,"
  "according to prosecutors").
- Never name minors. Anonymize victims unless they have publicly identified
  themselves.

If the record involves sealed cases, identified minors, or sexual assault
of identifiable victims, return:
```json
{"refuse": true, "reason": "..."}
```

Otherwise return STRICTLY:
```json
{
  "narration": "the voiceover, one paragraph (≤125 words)"
}
```

Source: {source}
Title: {title}
URL: {url}
Published: {published_at}

Record text:
{text}
