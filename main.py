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

@app.get("/api/extract")
async def extract_vimeo(url: str):
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'quiet': True,
        'no_warnings': True,
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
                    # Prefer manifests with better quality if needed, 
                    # but usually yt-dlp returns the master playlist
                    break
            
            if not hls_url:
                raise HTTPException(status_code=404, detail="HLS stream not found")
            
            video_id = info.get('id')
            stream_cache[video_id] = hls_url
            
            return {
                "id": video_id,
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "proxy_url": f"/proxy/manifest/{video_id}"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def rewrite_m3u8(m3u8_obj, base_proxy_url):
    if m3u8_obj.is_variant:
        for playlist in m3u8_obj.playlists:
            original_uri = playlist.absolute_uri
            encoded_uri = urllib.parse.quote_plus(original_uri)
            playlist.uri = f"{base_proxy_url}/proxy/raw_manifest?url={encoded_uri}"
        
        for media in m3u8_obj.media:
            if media.uri:
                original_uri = media.absolute_uri
                encoded_uri = urllib.parse.quote_plus(original_uri)
                media.uri = f"{base_proxy_url}/proxy/raw_manifest?url={encoded_uri}"
    else:
        # Handle Initialization Segment (EXT-X-MAP)
        if m3u8_obj.segment_map:
            for segment_map in m3u8_obj.segment_map:
                if segment_map.uri:
                    original_uri = segment_map.absolute_uri
                    encoded_uri = urllib.parse.quote_plus(original_uri)
                    segment_map.uri = f"{base_proxy_url}/proxy/segment?url={encoded_uri}"

        for segment in m3u8_obj.segments:
            original_uri = segment.absolute_uri
            encoded_uri = urllib.parse.quote_plus(original_uri)
            segment.uri = f"{base_proxy_url}/proxy/segment?url={encoded_uri}"

@app.get("/proxy/manifest/{video_id}")
async def proxy_manifest(video_id: str, request: Request):
    hls_url = stream_cache.get(video_id)
    if not hls_url:
        raise HTTPException(status_code=404, detail="Stream expired or not found")
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(hls_url, follow_redirects=True)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code)
        
        m3u8_obj = m3u8.loads(resp.text, uri=hls_url)
        base_proxy_url = str(request.base_url).rstrip('/')
        rewrite_m3u8(m3u8_obj, base_proxy_url)
        
        return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/raw_manifest")
async def proxy_raw_manifest(url: str, request: Request):
    decoded_url = urllib.parse.unquote(url)
    async with httpx.AsyncClient() as client:
        resp = await client.get(decoded_url, follow_redirects=True)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code)
        
        m3u8_obj = m3u8.loads(resp.text, uri=decoded_url)
        base_proxy_url = str(request.base_url).rstrip('/')
        rewrite_m3u8(m3u8_obj, base_proxy_url)
            
        return Response(content=m3u8_obj.dumps(), media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/segment")
async def proxy_segment(url: str):
    decoded_url = urllib.parse.unquote(url)
    
    async def stream_video():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", decoded_url, follow_redirects=True) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_video(), media_type="video/MP2T")

# Static files for the frontend
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
