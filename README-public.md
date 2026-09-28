# Job Board Agent

A scheduled agent that scans companies' own applicant tracking systems (ATS), filters
postings against one or more candidate profiles, and pushes new matches to Telegram or
email. It runs on GitHub Actions, so it needs no server and no paid infrastructure.

Most companies don't run their own careers page — they use Greenhouse, Lever, Ashby,
Workable and similar platforms, and most of those expose a public endpoint that returns
every open role as structured data. This agent goes to that source instead of scraping
job aggregators, which makes results fresher and far more reliable.

## What it does

1. **Detects the board.** For each company it probes the supported ATS providers with a
   few likely identifiers, then caches whichever one answers, so later runs go straight
   to the right endpoint.
2. **Collects postings** from all companies in parallel.
3. **Filters** by title keywords and location, per candidate profile.
4. **Deduplicates** across sources and against everything already reported.
5. **Scores (optional).** With scoring enabled, each new posting is sent to an LLM
   together with the matching profile's resume and preferences, returning a 0–100 fit
   score and a one-line rationale.
6. **Notifies** via Telegram and/or email, and writes the list to `data/for_review.md`.

## Supported ATS providers

| Provider | Detection | Description text |
|---|---|---|
| Greenhouse | automatic | yes |
| Lever | automatic | yes |
| Ashby | automatic | yes |
| SmartRecruiters | automatic | no |
| Workable | automatic | yes |
| Recruitee | automatic | yes |
| BambooHR | automatic | no |
| Comeet | manual (uid + token) | yes |
| Workday | manual (tenant URL) | no |

An optional discovery layer (SerpAPI, free tier) searches Google Jobs for the configured
roles, surfaces companies that aren't on the list yet, and adds them to the recurring
scan — so coverage grows on its own.

## Design notes

**Multiple profiles.** A candidate often targets more than one kind of role. Each profile
carries its own resume, keyword rules and score threshold; a posting is evaluated against
every profile it matches and reported with the best result, so it's clear which resume to
send.

**Scoring is optional.** LLM scoring is the only part that costs money, so it is off by
default. With it off the agent still delivers a filtered list; the ranking step can be
done manually or by an assistant reading `data/for_review.md`.

**Transient failures are not conclusions.** Rate limiting and 5xx responses are retried
with backoff and never cached as "this company has no board" — an early version did that
and silently dropped companies for days.

**Empty boards are rejected during detection.** Some providers answer with a valid, empty
account for any identifier. Treating that as a match produced confident, wrong results, so
detection now requires at least one real posting.

**State lives in the repo.** `data/seen_jobs.json` and `data/ats_cache.json` are committed
back by the workflow, which keeps the agent stateless between runs and makes the daily
commit double as a liveness signal for GitHub's scheduler.

## Setup

1. Fork or copy this repository (private is fine).
2. Put your resume in a text file and point `resume_file` at it, then edit `profiles` in
   `config.yaml` and the list in `companies.yaml`.
3. Add repository secrets under **Settings → Secrets and variables → Actions**:

   | Secret | Required |
   |---|---|
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | for Telegram delivery |
   | `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | for email delivery |
   | `ANTHROPIC_API_KEY` | only if `scoring.enabled: true` |
   | `SERPAPI_KEY` | only for the discovery layer |

4. Run it from the **Actions** tab, or let the schedule in
   `.github/workflows/job-agent.yml` handle it.

Locally:

```bash
pip install -r requirements.txt
python agent.py --dry-run   # no scoring, no notifications
python agent.py
```

## Limitations

- LinkedIn is deliberately not scraped; its terms forbid it. Use its own job alerts.
- Large enterprises often run closed career systems with no public endpoint.
- Without scoring, filtering is title- and location-based only, so postings whose
  requirements rule you out still arrive.
- GitHub's scheduler queues cron jobs and can delay or skip a run; off-the-hour times and
  a second daily run mitigate this.

## License

MIT
