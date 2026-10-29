from fastapi import FastAPI, Request, Response
import httpx
import random
import re

app = FastAPI()

SITE_BASE_URL = "https://bingr.one"
API_BASE_URL = "https://api.bingr.one"
API_PREFIX = "__api"

# Use only the supplied proxies. They are rotated for each incoming request,
# with the remaining proxies tried if a proxy cannot connect.
PROXIES = [
    "http://45.117.30.37:58989",
    "http://103.23.236.60:8080",
    "http://103.150.152.27:83",
    "http://152.67.191.232:6800",
    "http://45.194.91.7:8080",
    "http://160.19.41.52:80",
]

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def public_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def rewrite_upstream_urls(value: str, origin: str) -> str:
    """Point first-party site and API URLs back through this hosted proxy."""
    replacements = (
        ("https://api.bingr.one", f"{origin}/{API_PREFIX}"),
        ("http://api.bingr.one", f"{origin}/{API_PREFIX}"),
        ("//api.bingr.one", f"{origin}/{API_PREFIX}"),
        ("https://bingr.one", origin),
        ("http://bingr.one", origin),
        ("//bingr.one", origin),
        ("https://www.bingr.one", origin),
        ("http://www.bingr.one", origin),
        ("//www.bingr.one", origin),
    )
    for source, destination in replacements:
        value = value.replace(source, destination)
    return value


def rewrite_body(body: bytes, content_type: str, origin: str) -> bytes:
    media_type = content_type.lower()
    if not any(
        kind in media_type
        for kind in ("text/", "javascript", "json", "xml", "svg")
    ):
        return body

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    return rewrite_upstream_urls(text, origin).encode("utf-8")


def upstream_url(path: str, is_api: bool) -> str:
    base_url = API_BASE_URL if is_api else SITE_BASE_URL
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


@app.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(path: str, request: Request):
    is_api = path == API_PREFIX or path.startswith(f"{API_PREFIX}/")
    target_path = path[len(API_PREFIX):] if is_api else path
    url = upstream_url(target_path, is_api)
    body = await request.body()

    headers = dict(request.headers)
    connection_headers = {
        token.strip().lower()
        for token in headers.get("connection", "").split(",")
    }
    for name in list(headers):
        if name.lower() in HOP_BY_HOP_HEADERS | connection_headers:
            headers.pop(name, None)
    headers.pop("host", None)
    headers.pop("content-length", None)
    # Let httpx and Starlette work with the uncompressed upstream body.
    headers["accept-encoding"] = "identity"
    # The page and its API share the original site's first-party origin.
    headers["origin"] = SITE_BASE_URL
    headers["referer"] = f"{SITE_BASE_URL}/"

    proxy_order = random.sample(PROXIES, len(PROXIES))
    upstream_response = None
    last_error = None

    for selected_proxy in proxy_order:
        try:
            async with httpx.AsyncClient(
                proxy=selected_proxy,
                follow_redirects=False,
                timeout=httpx.Timeout(5.0, connect=2.5),
            ) as client:
                candidate = await client.request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    params=request.query_params,
                    content=body,
                )

            # These responses commonly mean the proxy itself is unavailable.
            if candidate.status_code in {407, 502, 503, 504}:
                last_error = f"proxy returned HTTP {candidate.status_code}"
                await candidate.aclose()
                continue

            upstream_response = candidate
            break
        except httpx.RequestError as error:
            last_error = str(error)

    if upstream_response is None:
        return Response(
            content=f"All configured proxies failed: {last_error or 'unknown error'}",
            status_code=502,
            media_type="text/plain",
        )

    origin = public_origin(request)
    response_body = rewrite_body(
        upstream_response.content,
        upstream_response.headers.get("content-type", ""),
        origin,
    )

    excluded_headers = HOP_BY_HOP_HEADERS | {
        "content-encoding",
        "content-length",
        "set-cookie",
    }
    response_headers = {}
    for name, value in upstream_response.headers.items():
        if name.lower() in excluded_headers:
            continue
        rewritten_value = rewrite_upstream_urls(value, origin)
        if is_api and name.lower() == "location" and value.startswith("/"):
            rewritten_value = f"/{API_PREFIX}{value}"
        response_headers[name] = rewritten_value

    result = Response(
        content=response_body,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )

    # Host-only cookies will be sent to the deployed proxy domain by browsers.
    for cookie in upstream_response.headers.get_list("set-cookie"):
        local_cookie = re.sub(
            r";\s*domain=(?:\.?bingr\.one|\.?api\.bingr\.one)",
            "",
            cookie,
            flags=re.IGNORECASE,
        )
        result.headers.append("set-cookie", local_cookie)

    await upstream_response.aclose()
    return result
