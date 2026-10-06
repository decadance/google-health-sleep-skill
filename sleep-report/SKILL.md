---
name: sleep-report
description: Sleep analysis from Google Health data (Fitbit / Pixel Watch → Google Health API) — duration, stages, efficiency, schedule regularity, resting HR / HRV / SpO2, trends and concrete recommendations. Use when the user asks about their sleep ("how did I sleep", "sleep report", "analyze my sleep this week/month", "як я спав", "как я спал") or invokes /sleep-report. Optional args - number of days (default 14; "last night" = 3 with focus on the most recent night), or "artifact" to also publish an HTML page with charts. Answer in the user's language.
---

# Sleep report from Google Health

Data path: **Fitbit / Pixel Watch → Fitbit (Google Health) app on the phone → Google Health cloud → Google Health API** (`https://health.googleapis.com/v4`). The legacy Fitbit Web API was shut down in September 2026; this skill only uses the new API.

Credentials live outside the skill folder: `~/.config/google-health/client_secret.json` and `~/.config/google-health/token.json`. Never print their contents.

## Steps

1. **Check setup**: do both files exist in `~/.config/google-health/`?
   - No `client_secret.json` → walk the user through "One-time setup" below and stop.
   - `client_secret.json` present but no `token.json` → run authorization (step 1a).

   1a. **Authorization** (once; again whenever fetch exits with code 4):
   ```
   python -u "<this-skill-dir>/scripts/auth.py" --no-browser
   ```
   The script prints a consent URL and waits up to 5 minutes for Google to redirect to `http://127.0.0.1:<port>/`. Run it in the background, then open the printed URL in a browser where the user is signed in to the Google account linked to their Fitbit. **The user clicks the consent buttons themselves** — tell them to pass the "Google hasn't verified this app" screen via *Continue* (it is their own app) and to **tick every checkbox**. Without `--no-browser` the script opens the system browser itself.

2. **Fetch data** (N = days from the args, default 14; 30 is better for trends):
   ```
   python "<this-skill-dir>/scripts/fetch_sleep.py" --days N
   ```
   stdout is JSON: `nights[]` (one entry per sleep session; `is_main` marks the main sleep of a day, the rest are naps), `daily_metrics{date:{type:{field:value, source}}}`, `exercise[]`, `counts`, `errors`, `nights_by_source`, `duplicate_sleep_sessions_dropped`, `raw_dump` (path to the raw API responses).

   Exit codes: `2` — no client_secret, `3` — token endpoint error, `4` — token expired/revoked → redo 1a.

3. **Check data quality before analyzing:**
   - Non-empty `errors`: `403 SERVICE_DISABLED` → Google Health API not enabled in the Cloud project; `403 PERMISSION_DENIED` / insufficient scopes → re-authorize with all checkboxes; `400` on a filter → the filter syntax changed, read `raw_dump` and the docs, fix `fetch_sleep.py`.
   - `counts.sleep > 0` but `nights` is empty or has empty `stages_min` / `asleep_min: null` → response schema differs; read the first records in `raw_dump`, fix `summarize_sleep()` and rerun. Never invent numbers.
   - `counts.sleep == 0` → the phone probably hasn't synced or the tracker wasn't worn; say so and suggest opening the Fitbit app to sync.
   - `daily_metrics` field names come straight from the API (e.g. `dailyHeartRateVariability.averageHeartRateVariabilityMilliseconds`); check `raw_dump` if units are unclear.

   **Known data quirks:**
   - Google Health aggregates **several sources** — `FITBIT:<device>`, `HEALTH_KIT:com.apple.health.*` (Apple Health synced via Health Connect), `HEALTH_CONNECT:<package>`. The script drops sleep sessions duplicated across sources (overlap > 50%) and prefers Fitbit for daily metrics; every night/metric keeps a `source` field.
   - **Never compare absolute values across sources** — resting HR and HRV are computed differently per device. Compute trends within one source and call out device changes explicitly.
   - Nights whose `stages_min` only has `ASLEEP` have no stage breakdown (typically short naps from other sources) — exclude them from stage statistics.
   - Filter syntax: sleep — `sleep.interval.end_time >= "<RFC3339>"`; daily metrics — **snake_case** `daily_resting_heart_rate.date >= "YYYY-MM-DD"`; exercise — `exercise.interval.civil_start_time >= "YYYY-MM-DD"`. Sleep/exercise `pageSize` max is 25.

4. **Compute and analyze** (main sleeps; naps as a separate line). Compute with a short Python script over the JSON, not by eye.
   - **Duration:** mean/median `asleep_min`, nights < 7 h and < 6 h, min/max. Adult target 7–9 h.
   - **Schedule:** mean bedtime and wake time and their SD in minutes (> 60 min = irregular); weekdays vs weekends ("social jet lag", > 1 h is bad). Handle midnight wrap-around (00:30 is late, not early).
   - **Stages** (% of time asleep): typical adult ranges — deep ~13–23%, REM ~20–25%, light ~50–60%. Wrist trackers estimate stages from HR and motion — it is an estimate, not polysomnography; look at trends, not single nights.
   - **Efficiency:** `efficiency_pct` (≥ 85% is normal), wake bouts and short awakenings.
   - **Recovery:** resting HR, HRV, breathing rate, SpO2, temperature — trend over the period and link to bad nights (HRV below personal median + RHR above → under-recovered; temperature + breathing rate jump → body may be fighting something).
   - **Temperature:** wrist trackers measure *skin* temperature (~33–35 °C), so the absolute value means little — use the deviation `nightlyTemperatureCelsius − baselineTemperatureCelsius` from `daily-sleep-temperature-derivations`. Flag it only when the deviation is **≥ +0.5 °C two nights in a row**; then check whether breathing rate and resting HR are also above the personal average (all three up → possibly getting sick; temperature alone → maybe a warm room or heavy blanket). A single cold night (−0.5…−1 °C) is usually a loose band or a cool room.
   - **Correlations, only with ≥ 10 nights:** late bedtime → less deep sleep? late workouts (`exercise` ending < 3 h before bed) → worse efficiency / higher RHR? weekends vs weekdays. Claim a link only if the numbers show it, with a small-sample caveat.
   - **Last night** — a short separate block compared to the personal average.

5. **Report** — in chat, in the user's language:
   1. **Summary in 2–3 sentences.**
   2. **Table per night** (date, bedtime, wake, sleep, deep/REM %, efficiency, RHR, HRV); for > 14 days use weekly averages instead.
   3. **What's good / what's bad** — bullets with numbers.
   4. **3–5 concrete recommendations** tied to the findings, not generic advice. E.g. "bedtime drifts 23:10–02:40 (SD 75 min) — fix wake-up at 08:00 including weekends".
   5. One-line disclaimer: not a medical assessment; persistently low SpO2 (< 90–92%), loud snoring or strong daytime sleepiness → see a doctor / sleep specialist.

6. **If the args contain `artifact`** (or the user asks for charts) — build an HTML page: last-night hypnogram, per-night stacked stage bars with a 7 h reference line, bedtime→wake range bars per night, small multiples for RHR / HRV / SpO2 (colored by source, lines never cross sources), recommendations. Publish it as an Artifact.

## One-time setup (done by the user)

The Google Health API needs your own OAuth client — there is no ready-made connector.

1. https://console.cloud.google.com/ → create a project.
2. **APIs & Services → Library** → **Google Health API** → *Enable* (accepts the Google Health API Developer Terms).
3. **Google Auth Platform → Branding**: app name, support email, audience **External**, contact email, accept the User Data Policy.
4. **Data Access → Add or remove scopes** → paste manually:
   - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
   - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
   - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
   → *Update* → *Save*. (They land under "restricted scopes" — expected.)
5. **Audience → Test users** → add the Google account your Fitbit is linked to.
6. **Clients → Create client** → **Desktop app** → *Create* → **Download JSON**. The secret is shown only once. If the download does not work, copy the client ID and secret into a file of this shape:
   ```json
   {"installed":{"client_id":"…apps.googleusercontent.com","client_secret":"GOCSPX-…","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":["http://localhost"]}}
   ```
7. Save it as `~/.config/google-health/client_secret.json`, then run `/sleep-report`.

**Token lifetime:** while the app is in *Testing*, Google issues refresh tokens that expire after **7 days**, so re-consent is needed weekly (the skill detects it via exit code 4). Google Health scopes are *restricted*, so moving the app to *In production* requires Google verification — for personal use, weekly re-consent is simpler.
