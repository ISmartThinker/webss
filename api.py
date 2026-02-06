import subprocess
import os
import platform
import shutil
from datetime import datetime
from contextlib import asynccontextmanager
import logging
import socket
import asyncio
from pathlib import Path
import time
import uuid
import tempfile
import traceback
import sys
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import uvloop
import cloudscraper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

SCREENSHOT_DIR = Path("/tmp/screenshots")
STORE = {}
CLEANUP_INTERVAL = 30
FILE_EXPIRY = 60

QUALITY_SETTINGS = {
    "low": {"width": 1280, "height": 720},
    "hd": {"width": 1920, "height": 1080},
    "fhd": {"width": 1920, "height": 1080},
    "wqhd": {"width": 2560, "height": 1440}
}

templates = Jinja2Templates(directory="templates")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.warning(f"Failed to get local IP: {str(e)}")
        return "127.0.0.1"

def install_browser():
    system = platform.system()
    logger.info(f"Attempting to install browser on {system}")
    
    try:
        if system == "Linux":
            distro_info = ""
            try:
                with open('/etc/os-release', 'r') as f:
                    distro_info = f.read().lower()
            except:
                pass
            
            if 'ubuntu' in distro_info or 'debian' in distro_info:
                logger.info("Installing Chrome on Debian/Ubuntu...")
                commands = [
                    "wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -",
                    "sh -c 'echo \"deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main\" >> /etc/apt/sources.list.d/google.list'",
                    "apt-get update -qq",
                    "apt-get install -y google-chrome-stable"
                ]
                for cmd in commands:
                    subprocess.run(cmd, shell=True, check=True, capture_output=True)
                logger.info("Chrome installed successfully")
                return "/usr/bin/google-chrome-stable"
                
            elif 'fedora' in distro_info or 'rhel' in distro_info or 'centos' in distro_info:
                logger.info("Installing Chrome on Fedora/RHEL/CentOS...")
                commands = [
                    "dnf install -y wget",
                    "wget https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm",
                    "dnf install -y ./google-chrome-stable_current_x86_64.rpm",
                    "rm google-chrome-stable_current_x86_64.rpm"
                ]
                for cmd in commands:
                    subprocess.run(cmd, shell=True, check=True, capture_output=True)
                logger.info("Chrome installed successfully")
                return "/usr/bin/google-chrome-stable"
                
            else:
                logger.info("Installing Chromium via package manager...")
                try:
                    subprocess.run("apt-get update -qq && apt-get install -y chromium-browser", shell=True, check=True, capture_output=True)
                    return "/usr/bin/chromium-browser"
                except:
                    subprocess.run("apt-get update -qq && apt-get install -y chromium", shell=True, check=True, capture_output=True)
                    return "/usr/bin/chromium"
                    
        elif system == "Darwin":
            logger.info("Installing Chrome on macOS...")
            commands = [
                "brew install --cask google-chrome"
            ]
            for cmd in commands:
                subprocess.run(cmd, shell=True, check=True, capture_output=True)
            logger.info("Chrome installed successfully")
            return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            
        elif system == "Windows":
            logger.info("Installing Chrome on Windows...")
            import urllib.request
            installer_path = "chrome_installer.exe"
            urllib.request.urlretrieve(
                "https://dl.google.com/chrome/install/latest/chrome_installer.exe",
                installer_path
            )
            subprocess.run([installer_path, "/silent", "/install"], check=True)
            os.remove(installer_path)
            logger.info("Chrome installed successfully")
            return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            
    except Exception as e:
        logger.error(f"Failed to install browser: {str(e)}")
        logger.error(traceback.format_exc())
        return None

def find_browser():
    system = platform.system()
    
    if system == "Windows":
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        ]
    elif system == "Darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        ]
    else:
        browsers = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge", "google-chrome-stable"]
        for browser in browsers:
            found = shutil.which(browser)
            if found:
                logger.info(f"Browser found via which: {found}")
                return found
        paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/bin/google-chrome-stable"
        ]
    
    for path in paths:
        if os.path.exists(path):
            logger.info(f"Browser found at path: {path}")
            return path
    
    logger.warning("No browser found, attempting auto-install...")
    installed_browser = install_browser()
    
    if installed_browser and os.path.exists(installed_browser):
        logger.info(f"Browser successfully installed at: {installed_browser}")
        return installed_browser
    
    logger.error("Failed to find or install browser")
    return None

async def bypass_cloudflare(url):
    try:
        logger.info(f"Starting Cloudflare bypass for: {url}")
        
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False
            }
        )
        
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: scraper.get(url, timeout=15)),
            timeout=20
        )
        
        if response.status_code == 200:
            temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, dir='/tmp', encoding='utf-8')
            temp_file.write(response.text)
            temp_file.close()
            logger.info(f"Cloudflare bypass successful, HTML saved to: {temp_file.name}")
            return temp_file.name
        else:
            logger.warning(f"Cloudflare bypass returned status {response.status_code}")
            return None
        
    except asyncio.TimeoutError:
        logger.error(f"Cloudflare bypass timeout for: {url}")
        return None
    except Exception as e:
        logger.error(f"Cloudflare bypass failed: {str(e)}")
        logger.debug(traceback.format_exc())
        return None

async def cleanup_expired_files():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            now = time.time()
            dead_keys = []
            
            for fid, data in STORE.items():
                if now > data["exp"]:
                    try:
                        if os.path.exists(data["path"]):
                            os.remove(data["path"])
                            logger.info(f"Deleted expired file: {os.path.basename(data['path'])}")
                    except Exception as e:
                        logger.error(f"Error deleting file {data['path']}: {str(e)}")
                    dead_keys.append(fid)
            
            for fid in dead_keys:
                STORE.pop(fid, None)
            
            if dead_keys:
                logger.info(f"Cleaned up {len(dead_keys)} expired files")
                
        except Exception as e:
            logger.error(f"Cleanup error: {str(e)}")
            logger.debug(traceback.format_exc())

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Starting Screenshot API Server")
    logger.info(f"Server running on http://{get_local_ip()}:3674")
    logger.info(f"Local access: http://127.0.0.1:3674")
    logger.info(f"Network access: http://0.0.0.0:3674")
    logger.info("=" * 60)
    
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Screenshot directory: {SCREENSHOT_DIR}")
    except Exception as e:
        logger.error(f"Failed to create screenshot directory: {str(e)}")
    
    browser = find_browser()
    if browser:
        logger.info(f"Browser ready: {browser}")
    else:
        logger.error("Failed to setup browser - screenshots will fail")
    
    logger.info("Cloudscraper enabled - Cloudflare bypass available")
    
    cleanup_task = asyncio.create_task(cleanup_expired_files())
    
    yield
    
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down Screenshot API Server")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    try:
        return FileResponse("templates/index.html")
    except Exception as e:
        logger.error(f"Error serving index: {str(e)}")
        logger.debug(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": "Index page not found",
            "message": str(e),
            "traceback": traceback.format_exc()
        }, status_code=500)

@app.get("/web/ss")
async def screenshot_endpoint(url: str, quality: str = "hd", bypass: bool = False):
    start_time = time.time()
    temp_html_file = None
    output_path = None
    
    try:
        logger.info(f"Screenshot request - URL: {url}, Quality: {quality}, Bypass: {bypass}")
        
        if not url:
            logger.error("URL parameter missing")
            raise HTTPException(status_code=400, detail="URL parameter is required")
        
        quality = quality.lower()
        if quality not in QUALITY_SETTINGS:
            logger.error(f"Invalid quality: {quality}")
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid quality. Choose from: {', '.join(QUALITY_SETTINGS.keys())}"
            )
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            logger.info(f"Added https:// prefix: {url}")
        
        browser = find_browser()
        if not browser:
            logger.error("No browser executable found even after auto-install attempt")
            raise HTTPException(status_code=500, detail="No browser found on system - auto-install failed")
        
        target_url = url
        
        if bypass:
            logger.info("Attempting Cloudflare bypass")
            temp_html_file = await bypass_cloudflare(url)
            if temp_html_file:
                target_url = f"file://{temp_html_file}"
                logger.info(f"Using bypassed HTML: {target_url}")
            else:
                logger.warning("Cloudflare bypass failed, using original URL")
        
        dimensions = QUALITY_SETTINGS[quality]
        width = dimensions["width"]
        height = dimensions["height"]
        
        fid = uuid.uuid4().hex
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        try:
            domain = url.split('//')[-1].split('/')[0].replace('www.', '').replace('.', '_')
        except Exception as e:
            logger.warning(f"Failed to extract domain: {str(e)}")
            domain = "unknown"
        
        filename = f"{fid}_{domain}_{quality}_{timestamp}.png"
        output_path = SCREENSHOT_DIR / filename
        
        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            f"--screenshot={str(output_path)}",
            f"--window-size={width},{height}",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--metrics-recording-only",
            "--mute-audio",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--ignore-certificate-errors",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            target_url
        ]
        
        logger.info(f"Executing: {' '.join(cmd[:10])}... {target_url}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
            
            if stdout:
                logger.debug(f"Browser stdout: {stdout.decode()[:500]}")
            if stderr:
                stderr_text = stderr.decode()
                if stderr_text and not all(x in stderr_text.lower() for x in ['devtools', 'extensions']):
                    logger.warning(f"Browser stderr: {stderr_text[:500]}")
                
        except asyncio.TimeoutError:
            logger.error(f"Screenshot timeout for {url}")
            process.kill()
            try:
                await process.wait()
            except:
                pass
            raise HTTPException(status_code=504, detail="Screenshot generation timeout after 45 seconds")
        
        if temp_html_file:
            try:
                if os.path.exists(temp_html_file):
                    os.remove(temp_html_file)
                    logger.debug(f"Cleaned up temp HTML: {temp_html_file}")
            except Exception as e:
                logger.warning(f"Failed to remove temp HTML: {str(e)}")
        
        if not output_path.exists():
            logger.error(f"Screenshot file not created: {output_path}")
            logger.error(f"Browser exit code: {process.returncode}")
            raise HTTPException(status_code=500, detail=f"Failed to generate screenshot - file not created (browser exit code: {process.returncode})")
        
        file_size = output_path.stat().st_size
        
        if file_size < 100:
            logger.error(f"Screenshot file too small: {file_size} bytes")
            try:
                os.remove(output_path)
            except:
                pass
            raise HTTPException(status_code=500, detail="Screenshot generated but file appears corrupted (too small)")
        
        expiry = time.time() + FILE_EXPIRY
        
        STORE[fid] = {
            "path": str(output_path),
            "exp": expiry,
            "filename": filename
        }
        
        server_ip = get_local_ip()
        file_url = f"http://{server_ip}:3674/file/{fid}"
        
        elapsed = time.time() - start_time
        
        logger.info(f"SUCCESS - Screenshot: {filename} ({file_size} bytes) in {elapsed:.2f}s")
        
        return JSONResponse({
            "success": True,
            "url": url,
            "quality": quality.upper(),
            "resolution": f"{width}x{height}",
            "screenshot": file_url,
            "file_id": fid,
            "filename": filename,
            "size": file_size,
            "cloudflare_bypass": bypass and temp_html_file is not None,
            "expires_in": f"{FILE_EXPIRY} seconds",
            "timestamp": datetime.now().isoformat(),
            "processing_time": f"{elapsed:.2f}s",
            "dev": "@ISmartCoder",
            "updates": "@abirxdhackz"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}")
        logger.error(traceback.format_exc())
        
        if temp_html_file:
            try:
                if os.path.exists(temp_html_file):
                    os.remove(temp_html_file)
            except:
                pass
        
        if output_path and output_path.exists():
            try:
                os.remove(output_path)
            except:
                pass
        
        return JSONResponse({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "url": url if 'url' in locals() else None,
            "traceback": traceback.format_exc(),
            "timestamp": datetime.now().isoformat(),
            "dev": "@ISmartCoder",
            "updates": "@abirxdhackz"
        }, status_code=500)

@app.get("/file/{fid}")
async def get_file(fid: str):
    try:
        logger.info(f"File request: {fid}")
        
        if fid not in STORE:
            logger.warning(f"File ID not found: {fid}")
            raise HTTPException(status_code=404, detail="File not found or expired")
        
        data = STORE[fid]
        
        if time.time() > data["exp"]:
            logger.info(f"File expired: {fid}")
            try:
                if os.path.exists(data["path"]):
                    os.remove(data["path"])
                    logger.info(f"Deleted expired file: {os.path.basename(data['path'])}")
            except Exception as e:
                logger.error(f"Error deleting expired file: {str(e)}")
            STORE.pop(fid, None)
            raise HTTPException(status_code=404, detail="File expired")
        
        if not os.path.exists(data["path"]):
            logger.error(f"File not found on disk: {data['path']}")
            STORE.pop(fid, None)
            raise HTTPException(status_code=404, detail="File not found on disk")
        
        logger.info(f"Serving file: {data['filename']}")
        
        return FileResponse(
            data["path"],
            media_type="image/png",
            filename=data["filename"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving file {fid}: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(),
            "file_id": fid
        }, status_code=500)

@app.get("/health")
async def health_check():
    try:
        browser = find_browser()
        return JSONResponse({
            "success": True,
            "status": "healthy",
            "browser_available": browser is not None,
            "browser_path": browser,
            "cloudscraper_available": True,
            "screenshot_dir": str(SCREENSHOT_DIR),
            "active_files": len(STORE),
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }, status_code=500)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=3674,
        loop="uvloop",
        log_level="info",
        access_log=True
    )