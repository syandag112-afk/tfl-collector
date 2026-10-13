# Headway Desk – GitHub Actions collector (no AWS)

## What it does
| Workflow | When | What |
|---|---|---|
| `collect` | every 5 min | A) calls the app's `/api/ea/poll` (3 tries, backoff on 429/5xx/timeout) so the app keeps its own 90-day history. B) takes one TfL snapshot of **all ~675 bus routes**, works out which buses reached which stops, and stores them. Never saves an empty snapshot. |
| `daily` | 04:40 London (03:40 GMT in winter) | Builds one CSV per route for yesterday's service day (scheduled vs expected vs actual, delay, dwell estimate), publishes it as a **Release** `day-YYYY-MM-DD`, deletes releases older than 90 days, and makes a small commit so GitHub never disables the schedule. |
| `watchdog` | every 30 min | Fails if no `collect` run has succeeded for over 30 minutes. GitHub emails you when it fails. |

## Where the data lives
- **App history** (what Headway Desk shows): on the app server, 90 days, filled by step A.
- **Per-run raw files**: workflow artifacts `raw-<day>-<run>`, kept 3 days.
- **Daily CSVs**: Releases tab → `day-YYYY-MM-DD` → `tfl-bus-YYYY-MM-DD.zip` (one `<route>.csv.gz` per route), kept 90 days.
- Nothing large is committed to the repo, so it stays tiny (a few KB of code plus one line a day).

## Steps for you (about 10 minutes)
1. Go to https://github.com/signup. Enter your email, a password and a username, and verify the email. No card is needed.
2. Top right: **+** → **New repository**. Name it `headway-collector`. Choose **Public**, because public repos get unlimited free Actions minutes. A private repo's free 2,000 minutes a month would run out in about a week at this rate. The data is public TfL data. Click **Create repository**.
3. On the new repo page, click **uploading an existing file**. Unzip `headway-github-collector.zip` on your computer and drag in **everything inside the folder**, including the `.github` folder. If your computer hides `.github`, do step 3b instead. Click **Commit changes**.
   - 3b. Click **Add file → Create new file**. Type the name `.github/workflows/collect.yml`, paste that file's contents, then **Commit**. Repeat for `daily.yml` and `watchdog.yml`.
4. Click the **Actions** tab. If it asks, click **I understand my workflows, go ahead and enable them**.
5. Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and click **Save**. The daily job needs this to create releases.
6. Start it now: **Actions → collect → Run workflow**. It should go green in about 1 minute. After that it runs by itself every 5 minutes.
7. (Optional) Get a free TfL key at https://api-portal.tfl.gov.uk (sign up → Products → subscribe → copy the primary key). Then go to **Settings → Secrets and variables → Actions → New repository secret**, set the name to `TFL_APP_KEY`, paste the key, and click **Add secret**. This gives higher rate limits.
8. Make sure failure emails are on: your avatar → **Settings → Notifications → Actions → "Notify me for failed workflows only"** (email).

## Seeing runs and data
- Runs: **Actions** tab (green = OK, red = failed).
- Daily CSVs: the **Releases** link on the right side of the repo home page.
- If the app moves to a new address: **Settings → Secrets and variables → Actions → Variables → New variable**. Name it `POLL_URL` and set it to the full poll URL.

## Things to know
- GitHub's timer is best effort. Runs can start 5–20 minutes late at busy times, and occasionally one is skipped. A 5-minute gap barely affects the data. The watchdog only alerts after 30 minutes.
- GitHub turns off scheduled workflows after 60 days with no repo activity. The daily job commits `LAST_ROLLUP.txt` every day, which counts as activity.
- Actual times are inferred from when a bus drops off a stop's live predictions. Snapshots are 5 minutes apart, so expect a typical accuracy of ±1–3 minutes. Duty, running number and the iBus codes aren't in TfL's public feed.
- Days before this goes live, such as 11 Oct, can't be recovered.
