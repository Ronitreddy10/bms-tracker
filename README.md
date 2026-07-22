# BookMyShow High-Speed Ticket & Row Unblock Tracker 🍿

Automated high-speed tracker for [BookMyShow](https://in.bookmyshow.com) ticket releases, new showtimes, and row unblock alerts sent directly to Telegram.

## Features 🚀

- **Instant Row Unblock Detection**: Sends an immediate alert when blocked rows (e.g. Row D, Recliner, Executive) switch from unavailable to open.
- **New Showtime & Theatre Alerts**: Instant notifications when new venues or showtimes appear.
- **High-Speed Polling**: Performs fast (1-2 second) checks using lightweight HTTP session keep-alives.
- **24/7 Cloud Support**: Pre-configured GitHub Actions workflow runs every 5 minutes automatically in the cloud.
- **Telegram Integration**: Beautifully formatted HTML messages with direct movie booking links.

## How It Works

1. Queries BookMyShow's dynamic showtimes API.
2. Compares the current venue, showtime, and seat category status against `bms_state.json`.
3. Dispatches a formatted HTML alert via Telegram whenever new showtimes or unblocked rows/categories are detected.

## Local Usage 💻

```bash
# Install dependencies
pip install requests playwright

# Run continuously in fast mode (1-2 second polling)
python3 bms_tracker.py

# Run a single check (used by GitHub Actions)
python3 bms_tracker.py --once
```

## 24/7 Cloud Deployment (GitHub Actions) ☁️

Add the following Secrets to your GitHub repository under **Settings → Secrets and variables → Actions**:

| Secret Name | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Your Telegram Chat ID |
| `BMS_URL` | The BookMyShow buytickets movie URL |

GitHub Actions will automatically run the checker every 5 minutes in the cloud 24/7.
