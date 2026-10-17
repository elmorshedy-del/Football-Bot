# Barcelona goal-feed latency observer (standalone)

Isolated, read-only research service for the October 17, 2026 Barcelona–Betis game. No connections to orders, trading, or the live Football-Bot service. This branch is **not merged**.

## Deploy once Railway billing permits creating new services

Create a **new** Railway service in project `Football-Bot`, using repo `elmorshedy-del/Football-Bot` branch `experiment/sportradar-barca-goal-latency-2026-10-17`; set the new service's root directory to `/experiments/goal-feed-latency`. Existing `football-bot` and `kalchi-kill` remain unchanged. Start command: `npm start`.

Set `SPORTRADAR_API_KEY` as a **secret** Railway service variable. Do not commit or paste this key in logs. `SPORTRADAR_ACCESS_LEVEL=trial` unless you know the key is licensed for `production`.

Logs emit `GOAL_RELATED_EVENT_FIRST_SEEN` (possible goal, score change, VAR), `SCORE_CHANGE_FIRST_OBSERVED`, `SOURCE_EVENT_REVISED`, and heartbeats as JSON. Fields `source_processing_or_update_at`, `received_at`, and `source_to_receipt_ms` measure Sportradar **processing-to-observer** delay; they do not establish the physical goal-to-data latency. Poll detection adds 0–one polling interval plus network variation. Events observed on the first poll are explicitly marked baseline, not fresh.

**Quota:** Sportradar's default trial includes 1,000 total calls over a rolling 30-day window, at 1 request per second. This collector defaults to 10-second polls and a maximum of 850 calls, without assuming the whole quota remains. A licensed `production` key defaults to 1,500 ms polls; verify your quota before running. `POLL_MS`, `MAX_CALLS`, `MAX_MINUTES`, and `MATCH_ID` override defaults.

If no calls appear, inspect `CONFIG_ERROR`, `HTTP_ERROR` or `POLL_EXCEPTION`. Even a successful deployment won't collect data retroactively.
