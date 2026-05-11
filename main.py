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
            
            # Find HLS format
            formats = info.get('formats', [])
            hls_url = None
            for f in formats:
                if f.get('protocol') == 'm3u8_native' or f.get('ext') == 'mp4' and 'm3u8' in f.get('url', ''):
                    hls_url = f.get('url')
                    break
            
            if not hls_url:
                raise HTTPException(status_code=404, detail="HLS stream not found")
            
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

def rewrite_m3u8(m3u8_obj):
    # Base rewrite for all types of manifests
    if m3u8_obj.is_variant:
        for playlist in m3u8_obj.playlists:
            original_uri = playlist.absolute_uri
            encoded_uri = urllib.parse.quote_plus(original_uri)
            playlist.uri = f"/proxy/raw_manifest?url={encoded_uri}"
        
        for media in m3u8_obj.media:
            if media.uri:
                original_uri = media.absolute_uri
                encoded_uri = urllib.parse.quote_plus(original_uri)
                media.uri = f"/proxy/raw_manifest?url={encoded_uri}"
    
    # Always check for segments and map, even in variants just in case
    # Handle Initialization Segment (EXT-X-MAP)
    if m3u8_obj.segment_map:
        maps = m3u8_obj.segment_map if isinstance(m3u8_obj.segment_map, list) else [m3u8_obj.segment_map]
        for sm in maps:
            if hasattr(sm, 'uri') and sm.uri:
                # Use absolute_uri to get the full URL before quoting
                original_uri = sm.absolute_uri
                encoded_uri = urllib.parse.quote_plus(original_uri)
                sm.uri = f"/proxy/segment?url={encoded_uri}"

    # Handle Keys (EXT-X-KEY)
    if m3u8_obj.keys:
        for key in m3u8_obj.keys:
            if key and hasattr(key, 'uri') and key.uri:
                original_uri = key.absolute_uri
                encoded_uri = urllib.parse.quote_plus(original_uri)
                key.uri = f"/proxy/segment?url={encoded_uri}"

    # Handle Segments
    for segment in m3u8_obj.segments:
        original_uri = segment.absolute_uri
        encoded_uri = urllib.parse.quote_plus(original_uri)
        segment.uri = f"/proxy/segment?url={encoded_uri}"

@app.get("/proxy/manifest/{video_id}")
async def proxy_manifest(video_id: str, request: Request):
    cached = stream_cache.get(video_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Stream expired or not found")
    
    hls_url = cached["hls_url"]
    headers = HEADERS.copy()
    headers["Referer"] = cached["original_url"]
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(hls_url, headers=headers, follow_redirects=True)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code)
        
        m3u8_obj = m3u8.loads(resp.text, uri=hls_url)
        rewrite_m3u8(m3u8_obj)
        
        return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/raw_manifest")
async def proxy_raw_manifest(url: str, request: Request):
    decoded_url = urllib.parse.unquote(url)
    async with httpx.AsyncClient() as client:
        resp = await client.get(decoded_url, headers=HEADERS, follow_redirects=True)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code)
        
        m3u8_obj = m3u8.loads(resp.text, uri=decoded_url)
        rewrite_m3u8(m3u8_obj)
            
        return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/segment")
async def proxy_segment(url: str):
    decoded_url = urllib.parse.unquote(url)
    
    async def stream_video():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", decoded_url, headers=HEADERS, follow_redirects=True) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_video(), media_type="video/MP2T")

# Static files for the frontend
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
