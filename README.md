# Tech Office Sign-In

A sign-in kiosk that replaces the paper form outside your tech's office.

- **`/`** — the start screen: two big buttons, **Arriving** and **Leaving**.
  Both forms come back here once the confirmation screen clears.
- **`/signin`** (Arriving) — students enter their name, student ID and reason,
  then press Submit. The server stamps the date and time. No password.
- **`/signout`** (Leaving) — students type their student ID and press Sign out.
  The server fills in the time out on that student's latest sign-in of the day.
- **`/admin`** — the padlock in the top-left corner. One password field. Once
  signed in, the whole log is shown as a table, with a filter box and **Log
  out**. A wrong password shows a red message.
  - **Export CSV** opens a menu: **Export all**, or type a student ID to get a
    CSV of that student's visits only.
  - **Delete all** erases every visit. You must type `DELETE` in the
    confirmation box first; there is no undo, so export before you clear.

Built with Flask. Runs on Render's free plan.

---

## Read this before you deploy

**Render's free filesystem is erased every time the service redeploys,
restarts or wakes from sleep.** A log written to a plain file on the free plan
would disappear, usually within a day. That is why this app writes to a
database instead, and why `DATABASE_URL` matters.

The app picks its storage automatically:

| `DATABASE_URL` | Where the log is written | Use it for |
|---|---|---|
| set | PostgreSQL | **Production.** Survives restarts and redeploys. |
| not set | `data/visits.db` (SQLite) | Your own laptop, or a host with a real disk. |

Render's own free Postgres is deleted 30 days after you create it, so it is not
a good home for a log you want to keep all year. Use a free Postgres that does
not expire instead — **[Neon](https://neon.com)** is the one I would pick:
the free plan is permanent, gives 1 GB, and needs no card. **Supabase** works
the same way if you prefer it.

The export button always gives you a CSV, so you are never locked in.

---

## Deploy in about fifteen minutes

### 1. Create the database (Neon)

1. Sign up at [neon.com](https://neon.com) and create a project.
2. Copy the **pooled** connection string. It looks like
   `postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require`.
   Keep it somewhere safe — it is a password.

The app creates its own table on first boot; there is nothing to run by hand.

### 2. Put this folder on GitHub

```bash
cd tech-office-signin
git init
git add .
git commit -m "Tech office sign-in"
git branch -M main
git remote add origin https://github.com/YOUR-NAME/tech-office-signin.git
git push -u origin main
```

`.gitignore` already keeps `.env` and `data/` out of the repository. Never
commit your password or your database URL.

### 3. Create the Render service

In Render: **New > Web Service**, connect the repository, then:

- **Runtime**: Python
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 30`
- **Plan**: Free

(`render.yaml` is in the repo, so **New > Blueprint** does all of this for you.)

### 4. Set the environment variables

Open the service's **Environment** tab and add:

| Key | Value |
|---|---|
| `ADMIN_PASSWORD` | the password your tech will type. Make it long. |
| `SECRET_KEY` | run `python -c "import secrets; print(secrets.token_urlsafe(32))"` and paste the result |
| `DATABASE_URL` | the Neon connection string from step 1 |
| `TZ_NAME` | `America/New_York` |

`SECRET_KEY` signs the admin cookie. If it changes, everyone is logged out —
so set it once and leave it alone.

### 5. Open it

Render gives you a URL like `https://tech-office-signin.onrender.com`. Put that
on the kiosk computer, in full screen (`F11` in Chrome). The lock icon in the
corner is how you get to the log.

---

## The spin-down problem

A free Render service **sleeps after 15 minutes without traffic**, and the next
visitor waits about a minute for it to wake. For the first student of the
morning, that is a minute staring at a blank screen.

Three ways around it:

1. **Ping it.** Point a free uptime checker (UptimeRobot, Better Stack, Cron-job.org)
   at `https://your-app.onrender.com/healthz` every 10 minutes. The free plan
   gives 750 instance-hours a month, which is just over 31 days, so one service
   can stay awake full time — but only one.
2. **Pay $7/month** for Render's Starter plan, which never sleeps.
3. **Accept it** and have the kiosk browser open the page each morning.

Neon's free compute also suspends after 5 minutes idle, but it wakes in well
under a second, so you will not notice that one.

---

## Running it on your own machine

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in ADMIN_PASSWORD
export $(grep -v '^#' .env | xargs)
python app.py
```

Open <http://127.0.0.1:8000>. Without `DATABASE_URL`, entries go to
`data/visits.db`, which you can open with any SQLite browser.

---

## Changing the dropdown

The nine reasons live in one place — the `REASONS` list at the top of
`app.py` — and the server rejects anything that is not on it. If you add one
there, add the same text as an `<option>` in `static/signin.html`.

---

## What is stored, and who can read it

Each row holds a first name, a last name, a student ID, a reason, the time in
and the time out (empty until the student signs out). Two things worth knowing:

- The log is only as private as `ADMIN_PASSWORD`. Choose a long one, do not
  reuse it, and change it when someone leaves the role. Render serves the site
  over HTTPS, so the password is not sent in the clear.
- This is student data. Before it goes live, it is worth a word with whoever
  handles student records in the division about how long you keep the log and
  who may read it — the export button makes it easy to archive and clear.

The login throttles after 8 wrong passwords from one address and locks that
address out for 15 minutes. That counter lives in memory, so it resets if the
service restarts; it slows down guessing, it is not a substitute for a good
password.

---

## Files

```
app.py              Flask app: routes, validation, login, CSV export
storage.py          the log itself — Postgres or SQLite, same interface
static/home.html    the start screen: Arriving / Leaving
static/signin.html  the sign-in form + confirmation screen
static/signout.html the sign-out form (student ID only)
static/admin.html   the password screen + the log table
static/style.css    shared styles
requirements.txt    Flask, gunicorn, psycopg
render.yaml         Render blueprint
.env.example        the settings to fill in
```
