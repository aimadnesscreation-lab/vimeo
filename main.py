import os
import re
import httpx
import m3u8
import yt_dlp
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import Response, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import urllib.parse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage for stream URLs (for simplicity in this example)
# In production, you might want to use Redis or a cache with TTL
stream_cache = {}

# Global headers to mimic a browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://vimeo.com/"
}

# Global HTTPX client for efficiency
http_client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)

@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()

@app.get("/api/extract")
async def extract_vimeo(url: str):
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'user_agent': HEADERS["User-Agent"]
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Find the best HLS format (prefer master manifest or one with video)
            formats = info.get('formats', [])
            hls_url = None
            
            # 1. Try to find a master manifest (usually protocol is m3u8_native and no specific resolution)
            for f in formats:
                if f.get('protocol') == 'm3u8_native' or 'm3u8' in f.get('url', ''):
                    # For Vimeo, the master manifest is often in 'manifest_url'
                    if f.get('manifest_url'):
                        hls_url = f.get('manifest_url')
                        break
                    
                    if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                        hls_url = f.get('url')
                        if 'master.m3u8' in hls_url or 'manifest.m3u8' in hls_url or 'playlist.m3u8' in hls_url:
                            break
            
            # 2. Fallback to any HLS format that isn't audio-only
            if not hls_url:
                for f in formats:
                    if (f.get('protocol') == 'm3u8_native' or 'm3u8' in f.get('url', '')) and f.get('vcodec') != 'none':
                        if f.get('manifest_url'):
                            hls_url = f.get('manifest_url')
                            break
                        hls_url = f.get('url')
                        break
            
            # 3. Last resort fallback
            if not hls_url:
                hls_url = info.get('url')
            
            if not hls_url:
                raise HTTPException(status_code=404, detail="HLS stream not found")
            
            print(f"Selected HLS URL: {hls_url[:100]}...")
            video_id = info.get('id')
            stream_cache[video_id] = {
                "hls_url": hls_url,
                "original_url": url
            }
            
            return {
                "id": video_id,
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "proxy_url": f"/proxy/manifest/{video_id}"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def rewrite_url(url, is_manifest=False):
    if not url:
        return url
    encoded_url = urllib.parse.quote_plus(url)
    if is_manifest:
        return f"/proxy/raw_manifest?url={encoded_url}"
    else:
        return f"/proxy/segment?url={encoded_url}"

def rewrite_m3u8(m3u8_obj):
    # Master Playlist tags
    for playlist in m3u8_obj.playlists:
        playlist.uri = rewrite_url(playlist.absolute_uri, is_manifest=True)
    
    for media in m3u8_obj.media:
        if media.uri:
            media.uri = rewrite_url(media.absolute_uri, is_manifest=True)

    for iframe in m3u8_obj.iframe_playlists:
        iframe.uri = rewrite_url(iframe.absolute_uri, is_manifest=True)

    # Media Playlist tags
    if m3u8_obj.segment_map:
        for sm in m3u8_obj.segment_map:
            if hasattr(sm, 'uri') and sm.uri:
                sm.uri = rewrite_url(sm.absolute_uri)

    for key in m3u8_obj.keys:
        if key and hasattr(key, 'uri') and key.uri:
            key.uri = rewrite_url(key.absolute_uri)

    for segment in m3u8_obj.segments:
        segment.uri = rewrite_url(segment.absolute_uri)
        if segment.init_section:
            segment.init_section.uri = rewrite_url(segment.init_section.absolute_uri)
    
    # Low-latency HLS tags
    if hasattr(m3u8_obj, 'preload_hints'):
        for hint in m3u8_obj.preload_hints:
            if hasattr(hint, 'uri') and hint.uri:
                hint.uri = rewrite_url(hint.absolute_uri)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.get("/proxy/manifest/{video_id}")
async def proxy_manifest(video_id: str, request: Request):
    cached = stream_cache.get(video_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Stream expired or not found")
    
    hls_url = cached["hls_url"]
    headers = HEADERS.copy()
    headers["Referer"] = cached["original_url"]
    
    resp = await http_client.get(hls_url, headers=headers)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code)
    
    m3u8_obj = m3u8.loads(resp.text, uri=hls_url)
    rewrite_m3u8(m3u8_obj)
    
    return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/raw_manifest")
async def proxy_raw_manifest(url: str, request: Request):
    decoded_url = urllib.parse.unquote(url)
    resp = await http_client.get(decoded_url, headers=HEADERS)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code)
    
    m3u8_obj = m3u8.loads(resp.text, uri=decoded_url)
    rewrite_m3u8(m3u8_obj)
        
    return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/segment")
async def proxy_segment(url: str):
    decoded_url = urllib.parse.unquote(url)
    
    # We use a stream request to forward headers and data efficiently
    request = http_client.build_request("GET", decoded_url, headers=HEADERS)
    response = await http_client.send(request, stream=True)
    
    # Filter headers to avoid conflicts
    excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
    headers = {k: v for k, v in response.headers.items() if k.lower() not in excluded_headers}
    
    return StreamingResponse(
        response.aiter_bytes(),
        status_code=response.status_code,
        headers=headers,
        media_type=response.headers.get("Content-Type")
    )

# Static files for the frontend
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
