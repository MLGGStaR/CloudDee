You are writing a 55-second YouTube **Short** narration directly from a
public record. There is no long-form video — the Short stands alone.

Condense the record into a punchy ~140-word voiceover (no more than 150
words) that:

- **Opens hard** in the first sentence — dollar figure, casualty count,
  date, named party. Whatever is most concrete about this specific case.
- **Tells the whole story** — what happened, the key fact, the outcome.
  Compressed but coherent. Each sentence must add information.
- **Ends with one sharp closing line** — the lesson, the verdict, the
  outcome. Resonant.
- **Does NOT end with a CTA** — the editor appends a 3-second "subscribe to
  {brand_name}" outro card after the audio ends.
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
  "narration": "the 55-second voiceover, one paragraph"
}
```

Source: {source}
Title: {title}
URL: {url}
Published: {published_at}

Record text:
{text}
