You are designing a YouTube thumbnail. Output ONE JSON object.

The thumbnail will be 1280x720. The composite engine will draw your text on
top, plus a red highlight circle on the focal subject. Aim for **maximum
click-through** — this is the single most important asset of the video.

The thumbnails that win in this niche always do these things:

- **A specific NUMBER** as the dominant text — a length of time ("8 SECONDS"),
  a casualty count ("2 DEAD"), a dollar figure ("$47M GONE"), or an altitude
  ("12,000 FEET"). The number should come straight from the story.
- **A strong noun pairing** — short subtitle that reframes the number. E.g.
  number = "8 SECONDS" / subtitle = "to disaster". number = "$47M" /
  subtitle = "and nobody noticed".
- **A focal subject in the image** — an aircraft, a courthouse, a person,
  wreckage. Centered or rule-of-thirds, dramatic light.
- **Implied stakes** — emotional, not safe. "What he didn't see" beats
  "Aviation accident report."
- **A circle highlight on the focal subject.** The composite engine adds it
  for you; just describe what to highlight in `circle_subject`.

Avoid:
- All-caps screaming sentences.
- Question marks. Punctuation in titles.
- Generic stock photo aesthetic.
- Text that explains the obvious.

Return STRICTLY:

```json
{
  "title_text": "2-3 word impact line, ALL CAPS — should contain the NUMBER if at all possible",
  "subtitle_text": "3-5 word reframing line, mixed case",
  "badge_text": "single short word, e.g. 'NTSB', 'EVIDENCE', 'BREAKING', 'EXCLUSIVE'",
  "image_prompt": "Detailed prompt for the base image — strong focal subject, dramatic lighting, photographic, no text, no logos. Specify the aircraft type / setting precisely so the image is recognizable to the niche audience.",
  "circle_subject": "Brief noun phrase for what the red circle should highlight, e.g. 'the aircraft', 'the cockpit windscreen', 'the engine cowling'. The composite uses this to position the circle."
}
```

The video is about:

TITLE: {video_title}
SUMMARY: {summary}
CHANNEL: {channel_name}
ACCENT: {accent_color}
