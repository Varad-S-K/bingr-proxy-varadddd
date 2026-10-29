from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import logging
import random
import re
import time

app = FastAPI()
logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.bingr.one/api"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://bingr.one",
    "Referer": "https://bingr.one/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
}

PROXIES = [
    "http://45.117.30.37:58989",
    "http://103.23.236.60:8080",
    "http://103.150.152.27:83",
    "http://152.67.191.232:6800",
    "http://45.194.91.7:8080",
    "http://160.19.41.52:80",
]

BINGR_SERVERS = {
    "s40": "Aphelion",
    "s70": "Polaris",
    "s80": "Moscow",
    "s81": "Novo",
    "s62": "Bastion",
    "s30": "Nova",
    "s61": "Corvus",
    "s31": "Orion",
}

CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 1000
cache = {}


def js_parse_int(value: str) -> int:
    match = re.match(r"\s*[+-]?\d+", value or "")
    return int(match.group()) if match else 0


def js_truthy(value) -> bool:
    if value is None or value is False or value == "" or value == 0:
        return False
    return True


async def request_through_proxies(method: str, url: str, **kwargs):
    failures = []
    for proxy_url in random.sample(PROXIES, len(PROXIES)):
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                follow_redirects=False,
                timeout=httpx.Timeout(40.0, connect=3.0),
            ) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=HEADERS,
                    **kwargs,
                )

            # A response is usable only when the upstream returns exactly 200.
            # Public proxies can be blocked independently, so try the remaining
            # proxies for every other status instead of caching it as no results.
            if response.status_code != 200:
                failure = f"HTTP {response.status_code} via {proxy_url}"
                failures.append(failure)
                logger.warning("Bingr request failed: %s", failure)
                await response.aclose()
                continue
            return response
        except httpx.RequestError as error:
            failure = f"{type(error).__name__} via {proxy_url}"
            failures.append(failure)
            logger.warning("Bingr request failed: %s (%s)", failure, error)

    detail = "; ".join(failures) if failures else "no proxies are configured"
    raise RuntimeError(f"No configured proxy returned HTTP 200: {detail}")


async def ask_bingr(srv: str, media_type: str, tmdb_id: str, query: dict):
    if srv == "s40" and media_type == "tv" and query.get("season") and query.get("episode"):
        url = (
            f"{API_BASE_URL}/stream/aphelion-tv/{tmdb_id}/"
            f"{query['season']}/{query['episode']}"
        )
        response = await request_through_proxies("GET", url)
    else:
        response = await request_through_proxies(
            "POST",
            f"{API_BASE_URL}/stream",
            json={"srv": srv, "t": media_type, "id": tmdb_id, "query": query},
        )

    try:
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected upstream HTTP {response.status_code}")

        try:
            data = response.json()
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        sources = []
        for source in data.get("sources") or []:
            if not isinstance(source, dict):
                continue
            source_url = source.get("url")
            if source_url and isinstance(source_url, str) and source_url.startswith("https:"):
                sources.append(
                    {
                        "url": source_url,
                        "quality": source.get("quality") if js_truthy(source.get("quality")) else "",
                        "language": source.get("language") if js_truthy(source.get("language")) else "",
                        "isMP4": js_truthy(source.get("isMP4")),
                    }
                )

        subtitles = []
        for subtitle in data.get("subtitles") or []:
            if not isinstance(subtitle, dict) or not js_truthy(subtitle.get("url")):
                continue
            subtitles.append(
                {
                    "url": subtitle["url"],
                    "lang": (
                        subtitle.get("lang")
                        if js_truthy(subtitle.get("lang"))
                        else subtitle.get("language")
                        if js_truthy(subtitle.get("language"))
                        else ""
                    ),
                    "label": subtitle.get("label") if js_truthy(subtitle.get("label")) else "",
                }
            )

        return {"sources": sources, "subtitles": subtitles}
    finally:
        await response.aclose()


@app.get("/{path:path}")
async def stream(request: Request, path: str):
    # Accept the root URL as well as /stream and Vercel's rewritten function
    # paths; all of them run the same stream lookup using URL query parameters.
    params = request.query_params
    srv = params.get("srv", "")
    t = params.get("t", "movie")
    id = params.get("id", "")
    title = params.get("title", "")
    year = params.get("year", "")
    season = params.get("season", "")
    episode = params.get("episode", "")

    srv = str(srv or "")
    media_type = "tv" if t == "tv" else "movie"
    tmdb_id = str(id or "")

    if srv not in BINGR_SERVERS:
        return JSONResponse(
            status_code=400,
            content={"error": "Unknown server", "sources": []},
        )
    if not re.fullmatch(r"\d{1,9}", tmdb_id):
        return JSONResponse(
            status_code=400,
            content={"error": "A TMDB id is required", "sources": []},
        )

    query = {}
    clean_title = str(title or "").strip()[:200]
    clean_year = str(year or "")[:4]
    if clean_title:
        query["title"] = clean_title
    if re.fullmatch(r"\d{4}", clean_year):
        query["year"] = clean_year
    if media_type == "tv":
        query["season"] = max(1, js_parse_int(str(season or "")))
        query["episode"] = max(1, js_parse_int(str(episode or "")))

    cache_key = (srv, media_type, tmdb_id, tuple(sorted(query.items())))
    cached = cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        result = await ask_bingr(srv, media_type, tmdb_id, query)
        value = {"name": BINGR_SERVERS[srv], **result}
        cache[cache_key] = (now, value)
        if len(cache) > CACHE_MAX_ENTRIES:
            cache.pop(next(iter(cache)))
        return value
    except Exception:
        return JSONResponse(
            status_code=502,
            content={
                "error": f"{BINGR_SERVERS[srv]} is not answering right now",
                "sources": [],
            },
        )
