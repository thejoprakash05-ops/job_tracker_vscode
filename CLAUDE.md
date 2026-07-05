# Job Application Optimizer — Project Rules

## Always optimize Anthropic API calls

This app makes several Claude API calls per job application (JD extraction, resume
tailoring, cover letter writing, match analysis, and career-portal discovery). When
adding or modifying code that calls `client.messages.create`, optimize for cost and
latency by default:

- **Do not downgrade the model used for resume tailoring, cover letters, JD extraction,
  or match analysis.** Resume/cover letter quality takes priority over API cost for
  these calls — keep them on `claude-sonnet-4-6`. Cheaper models (e.g.
  `claude-haiku-4-5-20251001`) are only acceptable for auxiliary calls that don't affect
  the content sent to employers (e.g. career-portal/ATS discovery lookups).
- **Don't make a call you don't need.** Before adding a new API call, check whether the
  data is already available from an earlier response in the same request, can be
  computed without a model, or can be cached/reused across runs for the same job.
- **Trim prompt input.** Truncate large inputs (raw scraped HTML/text) to what's actually
  needed instead of sending full page text.
- **Cap `max_tokens` to the actual expected output size** rather than leaving generous
  headroom by default.
- **Batch instead of looping** when making per-item calls (e.g. extracting multiple
  fields) if a single call can return structured JSON for all of them at once.

When in doubt, surface the cost/latency tradeoff explicitly rather than silently
picking the most expensive option.
