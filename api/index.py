from fastapi import FastAPI, Request, Response
import httpx
import random
import time

app = FastAPI()

TARGET_BASE_URL = "https://api.bingr.one"
PROXY_LIST_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/by-country/in/http.txt"

HEADERS = {
    'Content-Type': 'application/json',
    'Origin': 'https://bingr.one',
    'Referer': 'https://bingr.one/',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
}

_proxy_cache = {
    "proxies": [],
    "last_fetched": 0
}
CACHE_DURATION = 300  # Refresh proxy list every 5 minutes

async def get_working_proxy():
    now = time.time()
    if not _proxy_cache["proxies"] or (now - _proxy_cache["last_fetched"]) > CACHE_DURATION:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(PROXY_LIST_URL, timeout=5.0)
                if resp.status_code == 200:
                    lines = resp.text.splitlines()
                    proxies = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            if not line.startswith("http"):
                                line = f"http://{line}"
                            proxies.append(line)
                    if proxies:
                        _proxy_cache["proxies"] = proxies
                        _proxy_cache["last_fetched"] = now
        except Exception as e:
            print(f"Failed to fetch proxy list: {e}")
            
    proxies = _proxy_cache["proxies"]
    return random.choice(proxies) if proxies else None

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    url = f"{TARGET_BASE_URL}/{path}"
    body = await request.body()
    
    # Prepare headers and mask Vercel origin headers
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers.update(HEADERS)
    
    selected_proxy = await get_working_proxy()
    
    client_kwargs = {"follow_redirects": True}
    if selected_proxy:
        client_kwargs["proxy"] = selected_proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                params=request.query_params,
                content=body,
                timeout=12.0
            )
    except Exception:
        # Fallback to direct connection if proxy fails or times out
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    params=request.query_params,
                    content=body,
                    timeout=10.0
                )
        except Exception as err:
            return Response(content=f"Proxy and fallback failed: {str(err)}", status_code=502)
        
    # Exclude headers that cause transport conflicts
    excluded_headers = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    response_headers = {
        k: v for k, v in response.headers.items() 
        if k.lower() not in excluded_headers
    }
        
    # KEY FIX: Use response.text so httpx automatically decompresses 
    # the payload into clean, readable text/JSON instead of raw binary bytes.
    return Response(
        content=response.text,
        status_code=response.status_code,
        headers=response_headers
    )
