# BMS Show Tracker

A 24/7 BookMyShow watcher that pings your phone (Telegram or ntfy) the moment:

- 🎬 a movie gets **listed** in your city, including new languages/formats (3D, IMAX, Telugu…)
- 📅 a new **date opens** for booking in the city
- 🏛 a specific **theatre opens** bookings (e.g. Prasads, Allu Cinemas)
- 🆕 **new shows** appear on a specific **screen** (PCX, Dolby Cinema, IMAX…)
- 💺 a new **seat class** shows up on an existing show, or a sold-out class comes back
- 🔓 **rows get unlocked** in the seat map, or a batch of seats is released

It only reads what the BookMyShow website shows. It never books, holds or locks seats.

Built on [aviiciii/bms-ticket-notifier](https://github.com/aviiciii/bms-ticket-notifier), the most-starred
BMS notifier on GitHub, with the seat-map reader from
[karanloyola/bms-telegram-tracker](https://github.com/karanloyola/bms-telegram-tracker). Both are MIT; see [NOTICE.md](NOTICE.md).

## How it works

```
config.yaml ──► Tracker loop (every ~3 min + jitter)
                 │
                 ├─ listings  : city now-showing + upcoming pages → movie ET codes (every 20 min)
                 ├─ showtimes : showtimes-by-event API, every open date, every format/language
                 │              (one fetch per movie per pass, shared by all watches)
                 ├─ filter    : theatre keywords → screen/format keywords → exclusions
                 ├─ seat maps : per matching show, free seats per row (every 2nd pass)
                 ├─ diff      : against state.json → alerts
                 └─ notify    : Telegram and/or ntfy
                errors / 403 → exponential backoff (up to 30 min) + one "struggling" alert
                daily "still alive" heartbeat
```

## Setup (10 minutes)

```bash
git clone https://github.com/Ronitreddy10/bms-tracker.git && cd bms-tracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env            # put your Telegram token + chat id here
.venv/bin/python -m bms_tracker test-notify      # you should get a message
.venv/bin/python -m bms_tracker discover "avengers doomsday"
```

`discover` prints every theatre showing the movie with its exact screen and seat-class names
(e.g. how Prasads labels the PCX screen). Use those words in `formats:` to make the watches exact.
If the movie isn't listed yet, that's fine: leave `movie:` in the config and you'll get an alert when it is.

Then start it:

```bash
.venv/bin/python -m bms_tracker once    # one check: sends "Now watching…" per watch
.venv/bin/python -m bms_tracker run     # the 24/7 loop
```

### Telegram bot in 2 minutes

1. In Telegram, open **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and copy `"chat":{"id": …}` into `TELEGRAM_CHAT_ID`.

**ntfy alternative:** install the ntfy app, subscribe to a long random topic name, put it in `NTFY_TOPIC`.
Urgent alerts (new shows, rows unlocked) come through at max priority.

## Running it 24/7

BookMyShow serves India only and **blocks datacenter and foreign IPs** (GitHub Actions runners get HTTP 403).
So it has to run from an Indian internet connection:

| Option | Cost | Notes |
|---|---|---|
| **Your Mac** (launchd) | free | `deploy/com.bmstracker.plist` starts it at login, restarts on crash and keeps the Mac awake with `caffeinate`. Keep it plugged in with the lid open (or use an external display). |
| **Old Android phone** (Termux) | free | Most reliable "always on" box at home. `pkg install python git`, clone, same setup, run under `termux-wake-lock`. |
| **Raspberry Pi / any Linux box at home** | ~₹4k one-time | `deploy/bms-tracker.service` (systemd). |
| Oracle Cloud Always-Free VM, Hyderabad or Mumbai region | free | Indian IP, but it's a datacenter range and may be blocked. Worth a try; the tracker will alert you if it keeps getting 403s. |

Docker works anywhere: `docker build -t bms-tracker . && docker run -d --restart=always -v $PWD:/data bms-tracker`.

## Config reference

See `config.example.yaml`. Per watch:

| key | meaning |
|---|---|
| `movie` / `event_codes` | name to search in city listings, and/or pinned BMS `ET…` codes |
| `venues` | theatre-name keywords or venue codes (empty = all theatres) |
| `formats` / `exclude_formats` | screen keywords matched against theatre name, screen attributes and seat-class names |
| `seats` | read seat maps to catch rows unlocking / seats released |
| `rows`, `seat_classes` | only care about e.g. rows `G-N` or class `recliner` |
| `seat_release_threshold` | how many seats must free up at once to alert (default 4) |
| `city_wide_alerts` | also alert when a date opens anywhere in the city |
| `disabled: true` | keep a watch in the file without running it |

## Be gentle

This uses BookMyShow's website endpoints, not a public API, and BMS's terms don't allow automated
access. Keep it personal and low-frequency. The defaults (one pass every ~3 min, 2.5 s+ between
requests, seat maps every ~6 min, backoff on errors) are deliberately polite. Don't lower them a lot,
and don't rotate IPs or proxies to get around a block; if you get 403s, slow down.

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest -q
```
