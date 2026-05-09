You are designing a YouTube thumbnail concept. Output a JSON object describing
ONE thumbnail.

The thumbnail will be 1280x720, with the channel's accent color visible. It
should follow the proven YouTube thumbnail pattern for documentary / news
channels:

- A dominant, high-contrast image
- 2–4 words of large text overlaid (sans-serif, white with black outline)
- A small cue badge (RED / BREAKDOWN / EVIDENCE / NEW) in the corner
- Clear focal subject

Avoid clickbait punctuation (no exclamation points, no all-caps screaming).

Return STRICTLY:

```json
{
  "title_text": "2-4 words, all caps, the impact line",
  "subtitle_text": "optional 3-6 word secondary line, mixed case",
  "badge_text": "single short word like RED, NEW, BROKEN",
  "image_prompt": "Detailed prompt for an image generator, describing the dominant visual."
}
```

The video is about:

TITLE: {video_title}
SUMMARY: {summary}
CHANNEL: {channel_name}
ACCENT: {accent_color}
