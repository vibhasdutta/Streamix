import base64, json, gzip, httpx, os, re

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Streamix API", version="2.0")

# --- Security Configuration ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",")
API_KEY_NAME = "x-api-key"
VALID_API_KEY = os.getenv("API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"DEBUG: Incoming {request.method} {request.url.path}")
    response = await call_next(request)
    return response

MIRURO_DOMAINS = ["miruro.to", "miruro.tv", "miruro.bz", "miruro.online"]
ANILIST_URL = "https://graphql.anilist.co"

async def _fetch_pipe(encoded_req: str) -> httpx.Response:
    last_status = 502
    async with httpx.AsyncClient(timeout=10.0) as client:
        for domain in MIRURO_DOMAINS:
            url = f"https://www.{domain}/api/secure/pipe"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Referer": f"https://www.{domain}/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            try:
                res = await client.get(f"{url}?e={encoded_req}", headers=headers)
                if res.status_code == 200:
                    return res
                last_status = res.status_code
            except httpx.RequestError:
                continue
    raise HTTPException(status_code=last_status, detail="All Miruro pipe domains failed")

def _proxy_img(url: str) -> str:
    # Proxy removed — return original image URL
    return url


def _proxy_deep_images(obj):
    # Proxy removed — return data unchanged
    return obj

def _inject_source_slugs(data: dict, anilist_id: int):
    """Transform episode IDs into simplified path-based slugs: watch/PROV/ALID/CAT/PREFIX-NUMBER"""
    providers = data.get("providers", {})
    for provider_name, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue
        episodes = provider_data.get("episodes", {})
        if not isinstance(episodes, dict):
            # Some providers return a flat list — wrap it
            if isinstance(episodes, list):
                provider_data["episodes"] = {"sub": episodes}
                episodes = provider_data["episodes"]
            else:
                continue
        for category, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                if "id" in ep and "number" in ep:
                    orig_id = ep["id"]
                    prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
                    ep["id"] = f"watch/{provider_name}/{anilist_id}/{category}/{prefix}-{ep['number']}"
    return data

async def _fetch_raw_episodes(anilist_id: int) -> dict:
    """Internal helper to fetch raw, decoded episode data from Miruro pipe."""
    payload = {
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    res = await _fetch_pipe(encoded_req)
    data = _decode_pipe_response(res.text.strip())
    _deep_translate(data)
    return data

# ─── Shared GraphQL Fragments ────────────────────────────────────────────────

MEDIA_LIST_FIELDS = """
    id
    title { romaji english native }
    coverImage { large extraLarge }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    genres
    source
    countryOfOrigin
    isAdult
    studios(isMain: true) { nodes { name isAnimationStudio } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
"""

MEDIA_FULL_FIELDS = """
    id
    idMal
    title { romaji english native }
    description(asHtml: false)
    coverImage { large extraLarge color }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    trending
    genres
    tags { name rank isMediaSpoiler }
    source
    countryOfOrigin
    isAdult
    hashtag
    synonyms
    siteUrl
    trailer { id site thumbnail }
    studios { nodes { id name isAnimationStudio siteUrl } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
            voiceActors(language: JAPANESE) { id name { full native } image { large } languageV2 }
        }
    }
    staff(sort: RELEVANCE, perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
        }
    }
    relations {
        edges {
            relationType(version: 2)
            node {
                id
                title { romaji english native }
                coverImage { large }
                format
                type
                status
                episodes
                meanScore
                relations {
                    edges {
                        relationType(version: 2)
                        node {
                            id
                            title { romaji english native }
                            type
                            format
                            status
                            episodes
                            meanScore
                        }
                    }
                }
            }
        }
    }
    recommendations(sort: RATING_DESC, perPage: 10) {
        nodes {
            rating
            mediaRecommendation {
                id
                title { romaji english native }
                coverImage { large }
                format
                episodes
                status
                meanScore
                averageScore
            }
        }
    }
    externalLinks { url site type }
    streamingEpisodes { title thumbnail url site }
    stats {
        scoreDistribution { score amount }
        statusDistribution { status amount }
    }
"""

# ─── Utility Functions ───────────────────────────────────────────────────────

def _translate_id(encoded_id: str) -> str:
    """Decode a base64-encoded episode ID back to plain text."""
    try:
        decoded = base64.urlsafe_b64decode(encoded_id + '=' * (4 - len(encoded_id) % 4)).decode()
        if ':' in decoded:
            return decoded
        return encoded_id
    except Exception:
        return encoded_id


def _deep_translate(obj):
    """Recursively walk a JSON structure and decode any base64 'id' fields."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'id' and isinstance(value, str):
                obj[key] = _translate_id(value)
            elif isinstance(value, (dict, list)):
                _deep_translate(value)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _deep_translate(item)


def _decode_pipe_response(encoded_str: str) -> dict:
    """Decode a base64+gzip pipe response into a plain dict."""
    try:
        encoded_str += '=' * (4 - len(encoded_str) % 4)
        compressed = base64.urlsafe_b64decode(encoded_str)
        return json.loads(gzip.decompress(compressed).decode('utf-8'))
    except Exception:
        raise ValueError("Failed to decode pipe response")


def _encode_pipe_request(payload: dict) -> str:
    """Encode a dict into the base64 format expected by the pipe endpoint."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')


async def _anilist_query(query: str, variables: dict = None):
    """Execute an AniList GraphQL query and return the data."""
    body = {"query": query}
    if variables:
        body["variables"] = variables
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(ANILIST_URL, json=body)
        if res.status_code != 200:
            raise HTTPException(status_code=500, detail="AniList query failed")
        return res.json().get("data", {})


# ─── Homepage ────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def home():
    """Redirect home to the auto-generated Swagger UI docs."""
    return RedirectResponse(url="/docs")


# ─── Search & Suggestions ───────────────────────────────────────────────────

@app.get("/search")
async def search_anime(
    query: str,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    """Search for anime by name via AniList GraphQL — returns full metadata."""
    gql = f"""
    query ($search: String, $page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"search": query, "page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)


@app.get("/suggestions")
async def search_suggestions(
    query: str = Query(..., min_length=1, description="Search query for autocomplete"),
):
    """Lightweight search for dropdown autocomplete — returns minimal data fast."""
    gql = """
    query ($search: String) {
        Page(page: 1, perPage: 8) {
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
                id
                title { romaji english }
                coverImage { large }
                format
                status
                startDate { year }
                episodes
            }
        }
    }
    """
    data = await _anilist_query(gql, {"search": query})
    results = []
    for item in data.get("Page", {}).get("media", []):
        results.append({
            "id": item["id"],
            "title": item["title"].get("english") or item["title"].get("romaji"),
            "title_romaji": item["title"].get("romaji"),
            "poster": item["coverImage"]["large"],
            "format": item.get("format"),
            "status": item.get("status"),
            "year": (item.get("startDate") or {}).get("year"),
            "episodes": item.get("episodes"),
        })
    return _proxy_deep_images({"suggestions": results})


# ─── Collection Endpoints (with pagination) ─────────────────────────────────

async def _fetch_collection(sort_type: str, status: str = None, page: int = 1, per_page: int = 20):
    """Internal helper for fetching collections like trending, popular, etc."""
    status_filter = f", status: {status}" if status else ""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(type: ANIME, sort: [{sort_type}]{status_filter}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)


@app.get("/spotlight")
async def get_spotlight():
    """Get the spotlight anime – high-priority trending and popular titles."""
    gql = f"""
    query {{
        Page(page: 1, perPage: 10) {{
            media(sort: [TRENDING_DESC, POPULARITY_DESC], type: ANIME) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql)
    media = data.get("Page", {}).get("media", [])
    return _proxy_deep_images({"results": media})


@app.get("/trending")
async def get_trending(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get trending anime with full metadata and pagination."""
    return await _fetch_collection("TRENDING_DESC", page=page, per_page=per_page)


@app.get("/popular")
async def get_popular(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get most popular anime of all time with full metadata and pagination."""
    return await _fetch_collection("POPULARITY_DESC", page=page, per_page=per_page)


@app.get("/upcoming")
async def get_upcoming(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get upcoming anime with full metadata and pagination."""
    return await _fetch_collection("POPULARITY_DESC", "NOT_YET_RELEASED", page=page, per_page=per_page)


@app.get("/recent")
async def get_recent(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get currently airing anime with full metadata and pagination."""
    return await _fetch_collection("START_DATE_DESC", "RELEASING", page=page, per_page=per_page)


@app.get("/schedule")
async def get_schedule(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get upcoming airing schedule with UNIX timestamps and full anime metadata."""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            airingSchedules(notYetAired: true, sort: TIME) {{
                episode
                airingAt
                timeUntilAiring
                media {{
                    {MEDIA_LIST_FIELDS}
                }}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    results = []
    for item in page_data.get("airingSchedules", []):
        entry = item.get("media", {})
        entry["next_episode"] = item.get("episode")
        entry["airingAt"] = item.get("airingAt")
        entry["timeUntilAiring"] = item.get("timeUntilAiring")
        results.append(entry)
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": results,
    }
    return _proxy_deep_images(response)


# ─── Advanced Filter ─────────────────────────────────────────────────────────

SORT_MAP = {
    "SCORE_DESC": "SCORE_DESC",
    "POPULARITY_DESC": "POPULARITY_DESC",
    "TRENDING_DESC": "TRENDING_DESC",
    "START_DATE_DESC": "START_DATE_DESC",
    "FAVOURITES_DESC": "FAVOURITES_DESC",
    "UPDATED_AT_DESC": "UPDATED_AT_DESC",
}

@app.get("/filter")
async def filter_anime(
    genre: Optional[str] = Query(None, description="Genre name, e.g. Action, Romance"),
    tag: Optional[str] = Query(None, description="Tag name, e.g. Isekai, Time Skip"),
    year: Optional[int] = Query(None, description="Season year, e.g. 2025"),
    season: Optional[str] = Query(None, description="WINTER, SPRING, SUMMER, or FALL"),
    format: Optional[str] = Query(None, description="TV, MOVIE, OVA, ONA, SPECIAL, MUSIC"),
    status: Optional[str] = Query(None, description="RELEASING, FINISHED, NOT_YET_RELEASED, CANCELLED, HIATUS"),
    sort: str = Query("POPULARITY_DESC", description="Sort order"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Advanced anime filter with genre, tag, year, season, format, status, and sort."""
    # Build dynamic argument string
    args = ["type: ANIME", f"sort: [{SORT_MAP.get(sort, 'POPULARITY_DESC')}]"]
    variables = {"page": page, "perPage": per_page}

    if genre:
        args.append("genre: $genre")
        variables["genre"] = genre
    if tag:
        args.append("tag: $tag")
        variables["tag"] = tag
    if year:
        args.append("seasonYear: $seasonYear")
        variables["seasonYear"] = year
    if season:
        args.append("season: $season")
        variables["season"] = season.upper()
    if format:
        args.append("format: $format")
        variables["format"] = format.upper()
    if status:
        args.append("status: $status")
        variables["status"] = status.upper()

    # Build variable type declarations
    var_types = ["$page: Int", "$perPage: Int"]
    if genre:
        var_types.append("$genre: String")
    if tag:
        var_types.append("$tag: String")
    if year:
        var_types.append("$seasonYear: Int")
    if season:
        var_types.append("$season: MediaSeason")
    if format:
        var_types.append("$format: MediaFormat")
    if status:
        var_types.append("$status: MediaStatus")

    gql = f"""
    query ({', '.join(var_types)}) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media({', '.join(args)}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, variables)
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)

# ─── Anime Details (Metadata Extensions) ────────────────────────────────────

@app.get("/anime/{anilist_id}/characters")
async def get_anime_characters(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=50),
):
    """Get paginated character list with voice actors for an anime."""
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            characters(sort: [ROLE, RELEVANCE], page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                edges {
                    role
                    node {
                        id
                        name { full native userPreferred }
                        image { large medium }
                        description
                        gender
                        dateOfBirth { year month day }
                        age
                        favourites
                        siteUrl
                    }
                    voiceActors(language: JAPANESE) {
                        id
                        name { full native }
                        image { large }
                        languageV2
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    chars = media.get("characters", {})
    page_info = chars.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "characters": chars.get("edges", []),
    }
    return _proxy_deep_images(response)


@app.get("/anime/{anilist_id}/relations")
async def get_anime_relations(anilist_id: int):
    """Get all related anime/manga for an anime (sequels, prequels, side stories, etc.)."""
    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            relations {
                edges {
                    relationType(version: 2)
                    node {
                        id
                        title { romaji english native }
                        coverImage { large }
                        bannerImage
                        format
                        type
                        status
                        episodes
                        chapters
                        meanScore
                        averageScore
                        popularity
                        startDate { year month day }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    response = {
        "id": media["id"],
        "title": media["title"],
        "relations": media.get("relations", {}).get("edges", []),
    }
    return _proxy_deep_images(response)


@app.get("/anime/{anilist_id}/recommendations")
async def get_anime_recommendations(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=25),
):
    """Get paginated community recommendations for an anime."""
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            recommendations(sort: RATING_DESC, page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                nodes {
                    rating
                    mediaRecommendation {
                        id
                        title { romaji english native }
                        coverImage { large extraLarge }
                        bannerImage
                        format
                        episodes
                        status
                        meanScore
                        averageScore
                        popularity
                        genres
                        startDate { year }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    recs = media.get("recommendations", {})
    page_info = recs.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "recommendations": recs.get("nodes", []),
    }
    return _proxy_deep_images(response)

# ─── Anime Details ───────────────────────────────────────────────────────────

@app.get("/info/{anilist_id}")
async def get_anime_info(anilist_id: int):
    """Get complete anime page data — everything AniList has to offer."""
    gql = f"""
    query ($id: Int) {{
        Media(id: $id, type: ANIME) {{
            {MEDIA_FULL_FIELDS}
        }}
    }}
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    return _proxy_deep_images(media)


# ─── Streaming (Pipe-based — unchanged logic) ───────────────────────────────

@app.get("/episodes/{anilist_id}")
async def get_episodes(anilist_id: int):
    """Get the episode list for an anime, with slugified source IDs."""
    data = await _fetch_raw_episodes(anilist_id)
    return _proxy_deep_images(_inject_source_slugs(data, anilist_id))


@app.get("/sources")
async def get_sources(
    episodeId: str = Query(..., description="Plain-text episode ID from /episodes response"),
    provider: str = Query(..., description="Provider name, e.g. kiwi, arc, telli"),
    anilistId: int = Query(..., description="AniList anime ID"),
    category: str = Query("sub", description="sub or dub"),
):
    """Get M3U8 streaming sources for a specific episode."""
    enc_id = base64.urlsafe_b64encode(episodeId.encode()).decode().rstrip('=')
    payload = {
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": enc_id,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    res = await _fetch_pipe(encoded_req)
    return _proxy_deep_images(_decode_pipe_response(res.text.strip()))

@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def get_watch_sources(provider: str, anilist_id: int, category: str, slug: str):
    """The super simple sources endpoint resolving slugs (prefix-number) back to provider IDs."""
    data = await _fetch_raw_episodes(anilist_id)
    prov_data = data.get("providers", {}).get(provider, {})
    ep_list = prov_data.get("episodes", {}).get(category, [])
    
    # Resolve the slug back to the original ID
    target_id = None
    for ep in ep_list:
        orig_id = ep.get("id", "")
        prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
        generated = f"{prefix}-{ep.get('number')}"
        if generated == slug:
            target_id = orig_id
            break
            
    if not target_id:
        raise HTTPException(status_code=404, detail=f"Episode slug '{slug}' not found for provider {provider}")
        
    return await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
