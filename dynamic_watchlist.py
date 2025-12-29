import logging
import time
from datetime import datetime, timedelta
import requests
from plexapi.exceptions import BadRequest
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
import re, unicodedata
from difflib import SequenceMatcher
from logging.handlers import TimedRotatingFileHandler
from dotenv import load_dotenv
import os

# --- 1. CONFIGURATION ---
EXCLUDED_LANGS = {"ko", "zh"}
EXCLUDED_COUNTRIES = {"KR", "CN", "TW", "HK"}

load_dotenv()  # load .env

API_KEY = os.getenv("TMDB_API_KEY")
MOVIE_URL = f"https://api.themoviedb.org/3/trending/movie/week?api_key={API_KEY}"
TV_URL = f"https://api.themoviedb.org/3/trending/tv/week?api_key={API_KEY}"

PLEX_BASEURL = os.getenv("PLEX_BASEURL", "http://192.168.68.100:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN")

LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "/scripts/arr/dynamic_watchlist.log")

logger = logging.getLogger('dynamic_watchlist')
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Handler for writing to the console (stdout)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
logger.addHandler(stream_handler)

# Handler for writing to the file
try:
    file_handler = TimedRotatingFileHandler(
        LOG_FILE_PATH, when='midnight', interval=1, backupCount=7
    )
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    logger.info(f"File logging enabled to: {LOG_FILE_PATH}")
except PermissionError:
    logger.warning(f"Permission denied to write to '{LOG_FILE_PATH}'. File logging is disabled.")
except Exception as e:
    logger.warning(f"An unexpected error occurred while setting up file logging: {e}. File logging is disabled.")

def is_excluded_tmdb_item(item: dict) -> bool:
    # Language-based exclusion (movies + TV)
    lang = item.get("original_language")
    if lang in EXCLUDED_LANGS:
        return True

    # Country-based exclusion (mostly TV shows)
    countries = set(item.get("origin_country", []))
    if countries & EXCLUDED_COUNTRIES:
        return True

    return False

def norm_title(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^a-z0-9]+', ' ', s.lower())
    return s.strip()

def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()

def year_from(date_str: str) -> int | None:
    if not date_str: return None
    m = re.match(r'(\d{4})', date_str)
    return int(m.group(1)) if m else None

def tmdb_id_from_guids(plex_obj) -> int | None:
    # plex_obj.guids is a list of Guid objects or strings like 'tmdb://12345?lang=en'
    for g in getattr(plex_obj, 'guids', []) or []:
        gid = getattr(g, 'id', g)  # Guid.id or raw string
        m = re.search(r'tmdb://(\d+)', str(gid))
        if m:
            return int(m.group(1))
    return None

def titles_from_tmdb_item(item: dict) -> list[str]:
    # De-duped title candidates from TMDb payload
    cand = [item.get(k) for k in ('name','original_name','title','original_title') if item.get(k)]
    seen, out = set(), []
    for t in cand:
        nt = norm_title(t)
        if nt and nt not in seen:
            seen.add(nt); out.append(t)
    return out

def watchlist_signatures(account, libtype: str):
    """Return (tmdb_ids_in_watchlist, (norm_title, year) pairs) for quick membership tests."""
    tmdb_ids, title_years = set(), set()
    try:
        for w in account.watchlist(libtype=libtype) or []:
            tid = tmdb_id_from_guids(w)
            if tid: tmdb_ids.add(tid)
            y = getattr(w, 'year', None)
            t = getattr(w, 'title', None)
            if t and y: title_years.add((norm_title(t), int(y)))
    except Exception as e:
        logger.warning(f"Failed to read watchlist for '{libtype}': {e}")
    return tmdb_ids, title_years

def discover_best_match(account, media_type: str, titles: list[str], year: int | None, tmdb_id: int | None):
    """
    Use Plex Discover to find the exact item to add.
    Preference order:
    1) result whose guids include tmdb://<id>
    2) title-year match (±1 year) with high similarity
    3) otherwise the first sane result
    """
    guid = f"tmdb://{tmdb_id}"
    try:
        plex_items = account.searchDiscover(guid, libtype=media_type)
        if plex_items and plex_items[0]:
            logger.debug(f"Matched via TMDB GUID: {plex_item.title} ({plex_item.year})")
            return plex_items[0]
    except Exception as e:
        logger.debug(f"No match via GUID {guid}: {e}")

    queries = []
    for t in titles:
        if year: queries.append(f"{t} {year}")
        queries.append(t)


    best_by_similarity = (None, 0.0)
    for q in queries:
        try:
            results = account.searchDiscover(query=q, libtype=media_type) or []
        except Exception as e:
            logger.debug(f"Discover search failed for '{q}': {e}")
            continue

        # 1) Exact TMDb GUID match
        if tmdb_id:
            for r in results:
                rid = tmdb_id_from_guids(r)
                if rid and rid == tmdb_id:
                    return r

        # 2) Year + fuzzy title
        filtered = []
        if year:
            for r in results:
                ry = getattr(r, 'year', None)
                if isinstance(ry, int) and abs(ry - year) <= 1:
                    filtered.append(r)
        ranked = filtered or results

        # keep the most similar title as a fallback candidate
        for r in ranked:
            sim = similar(getattr(r, 'title', ''), titles[0])
            if sim > best_by_similarity[1]:
                best_by_similarity = (r, sim)

        # If we have a very strong match, take it
        if best_by_similarity[0] and best_by_similarity[1] >= 0.92:
            return best_by_similarity[0]

    # Fallback to the best we saw at all (if any)
    return best_by_similarity[0]

def fetch_trending_data(url):
    """Fetches trending data from TMDb."""
    try:
        logger.debug(f"Fetching data from {url}")
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Fetched {len(data['results'])} items from TMDb")
        return data['results']
    except requests.RequestException as e:
        logger.error(f"Error fetching data from TMDb: {e}")
        raise

def filter_items(items, date_key, days=365):
    """Filters items based on their date, returning those within the last 'days' days."""
    cutoff_date = datetime.now() - timedelta(days=days)
    filtered_items = [item for item in items if datetime.strptime(item[date_key], '%Y-%m-%d') >= cutoff_date][:10]
    logger.info(f"Filtered items: {len(filtered_items)} items within the last {days} days")
    return filtered_items

def add_to_plex_watchlist(account, items):
    """Adds items to the Plex watchlist."""
    try:
        account.addToWatchlist(items)
        for item in items:
            logger.info(f"Added {item.title} to Plex watchlist.")
    except BadRequest as e:
        logger.error(f"Error adding items to watchlist: {e}")

def get_watchlist(account, libtype):
    """Retrieves the current watchlist items from Plex."""
    try:
        watchlist_items = account.watchlist(libtype=libtype)
        logger.info(f"Retrieved {len(watchlist_items)} items from Plex watchlist of type '{libtype}'")
        return {item.title: item for item in watchlist_items}
    except Exception as e:
        logger.error(f"Error retrieving watchlist: {e}")
        return {}

def process_media_items(trending_items, plex, account, media_type):
    """
    Build a list of Plex *catalog* items (not from your server) that correspond to TMDb trending items
    and are not already in your Plex watchlist.
    media_type: 'movie' or 'show'
    """
    items_to_add = []
    tmdb_in_wl, titleyear_in_wl = watchlist_signatures(account, media_type)
    logger.info(f"Processing {len(trending_items)} trending items for media type '{media_type}'")

    date_key = 'first_air_date' if media_type == 'show' else 'release_date'

    for entry in trending_items:
        if is_excluded_tmdb_item(entry):
            logger.info(
                f"Skipping excluded language/country item: "
                f"{entry.get('title') or entry.get('name')} "
                f"(lang={entry.get('original_language')}, "
                f"country={entry.get('origin_country')})"
            )
            continue
        tmdb_id = entry.get('id')
        year = year_from(entry.get(date_key, ''))
        titles = titles_from_tmdb_item(entry)
        if not titles:
            logger.debug("No titles found in TMDb entry; skipping")
            continue

        # Skip if already in watchlist by TMDb id or title+year
        if tmdb_id and tmdb_id in tmdb_in_wl:
            logger.info(f"{titles[0]} ({year}) already in watchlist; skipping")
            continue
        if year and any((norm_title(t), year) in titleyear_in_wl for t in titles):
            logger.info(f"{titles[0]} ({year}) already in watchlist; skipping")
            continue

        match = discover_best_match(account, media_type, titles, year, tmdb_id)
        if not match:
            logger.info(f"No Discover match for {titles[0]} ({year or 'n/a'})")
            continue

        # extra safety: don't queue duplicates
        if match not in items_to_add:
            items_to_add.append(match)
            logger.info(f"Queued '{getattr(match,'title','?')}' ({getattr(match,'year','?')}) for watchlist addition")

    logger.info(f"Total new '{media_type}' items to add: {len(items_to_add)}")
    return items_to_add


def dynamic_watchlist():
    """Fetches, processes, and updates the Plex watchlist with trending items."""
    try:
        # Fetch and filter trending TV shows and movies
        logger.info("Starting script execution")
        top_tv_shows = filter_items(fetch_trending_data(TV_URL), 'first_air_date')
        top_movies = filter_items(fetch_trending_data(MOVIE_URL), 'release_date')

        # Log into Plex
        logger.info("Logging into Plex")
        account = MyPlexAccount(token=PLEX_TOKEN)
        plex_server = PlexServer(PLEX_BASEURL, PLEX_TOKEN)

        # Process and add TV shows and movies to watchlist
        tv_shows_to_add = process_media_items(top_tv_shows, plex_server, account, 'show')
        movies_to_add = process_media_items(top_movies, plex_server, account, 'movie')

        all_items_to_add = tv_shows_to_add + movies_to_add

        if all_items_to_add:
            logger.info(f"Adding {len(all_items_to_add)} items to Plex watchlist")
            add_to_plex_watchlist(account, all_items_to_add)
            logger.info("Finished adding items to watchlist.")
        else:
            logger.info("No new items to add to the watchlist after processing and filtering.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")

if __name__ == '__main__':
    dynamic_watchlist()
