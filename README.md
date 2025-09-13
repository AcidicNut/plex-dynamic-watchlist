# 📺 Dynamic Plex Watchlist

This script automatically updates your **Plex watchlist** with the latest trending **movies** and **TV shows** from [TMDb](https://www.themoviedb.org/).

It fetches trending titles, filters them to recent releases, finds the best match in Plex Discover, and adds them to your watchlist.

---

## ⚙️ Features

- Fetches **trending movies & shows** from TMDb (`week` by default).
- Filters results to releases within the **last year**.
- Matches Plex Discover items using:
  1. Exact TMDb GUID
  2. Title + year (+ fuzzy similarity)
  3. Best fallback result
- Skips items already in your Plex watchlist.
- Logs activity to both **stdout** and a rotating log file.
- Uses `.env` for **secure configuration**.

---

## 📦 Requirements

- Python **3.10+**
- Plex account with watchlist enabled
- TMDb API key

---

## 🔧 Installation

Clone or copy this script into your environment, then install dependencies:

```bash
pip install requests plexapi python-dotenv
````

---

## 📄 Configuration

Create a `.env` file in the same directory. There's a sample called `.env.example` you can copy.

---

## ▶️ Usage

Run manually:

```bash
python dynamic_watchlist.py
```

Schedule it via `cron` (e.g., run every night at 2 AM):

```cron
0 2 * * * /usr/bin/python3 /path/to/dynamic_watchlist.py >> /var/log/dynamic_watchlist_cron.log 2>&1
```

---

## 📚 Logging

* Console output always enabled
* File logging rotates **daily** and keeps the last **7 logs**

Default log path: `/scripts/arr/dynamic_watchlist.log`
(can be overridden in `.env`)

---

## 🚀 Future Enhancements

* Configurable **time window** (`day` or `week`) for TMDb
* Customizable **recency filter** (`days` parameter)
* Dry-run mode to preview items before adding
