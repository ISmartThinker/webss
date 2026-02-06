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
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import uvloop
import cloudscraper

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeout = Exception

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

try:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except:
    logger.warning("uvloop not available, using default event loop")

SCREENSHOT_DIR = Path("/tmp/screenshots")
STORE = {}
CLEANUP_INTERVAL = 30
FILE_EXPIRY = 120

QUALITY_SETTINGS = {
    "low": {"width": 1280, "height": 720},
    "hd": {"width": 1920, "height": 1080},
    "fhd": {"width": 1920, "height": 1080},
    "wqhd": {"width": 2560, "height": 1440}
}

IS_VERCEL = os.environ.get('VERCEL') == '1' or os.environ.get('AWS_LAMBDA_FUNCTION_NAME') is not None
IS_SERVERLESS = IS_VERCEL or os.environ.get('AWS_EXECUTION_ENV') is not None

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

async def install_playwright_browsers():
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright not installed!")
        return False
    
    try:
        logger.info("Installing Playwright Chromium browser...")
        
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        
        if process.returncode == 0:
            logger.info("Playwright Chromium installed successfully")
            return True
        else:
            logger.error(f"Playwright install failed: {stderr.decode()}")
            return False
            
    except asyncio.TimeoutError:
        logger.error("Playwright installation timeout")
        return False
    except Exception as e:
        logger.error(f"Error installing Playwright: {str(e)}")
        logger.error(traceback.format_exc())
        return False

async def screenshot_with_playwright(url, output_path, width, height, use_mobile=False):
    logger.info(f"Using Playwright for screenshot: {url}")
    
    browser = None
    context = None
    page = None
    
    try:
        async with async_playwright() as p:
            launch_options = {
                'headless': True,
                'args': [
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-extensions',
                    '--disable-web-security',
                    '--ignore-certificate-errors',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=IsolateOrigins,site-per-process',
                ]
            }
            
            browser = await p.chromium.launch(**launch_options)
            
            context_options = {
                'viewport': {'width': width, 'height': height},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'ignore_https_errors': True,
            }
            
            if use_mobile:
                context_options['is_mobile'] = True
                context_options['has_touch'] = True
            
            context = await browser.new_context(**context_options)
            
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            page = await context.new_page()
            
            await page.set_extra_http_headers({
                'Accept-Language': 'en-US,en;q=0.9',
            })
            
            try:
                await page.goto(url, wait_until='networkidle', timeout=40000)
            except PlaywrightTimeout:
                logger.warning("Network idle timeout, trying domcontentloaded")
                await page.goto(url, wait_until='domcontentloaded', timeout=40000)
            
            await asyncio.sleep(1)
            
            await page.screenshot(path=str(output_path), full_page=False)
            logger.info(f"Playwright screenshot saved: {output_path}")
            
            return True
            
    except Exception as e:
        logger.error(f"Playwright screenshot failed: {str(e)}")
        logger.error(traceback.format_exc())
        return False
    finally:
        if page:
            try:
                await page.close()
            except:
                pass
        if context:
            try:
                await context.close()
            except:
                pass
        if browser:
            try:
                await browser.close()
            except:
                pass

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
            loop.run_in_executor(None, lambda: scraper.get(url, timeout=20)),
            timeout=25
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
    logger.info("=" * 80)
    logger.info("Starting Screenshot API Server")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Platform: {platform.platform()}")
    logger.info(f"Server running on http://{get_local_ip()}:3674")
    logger.info(f"Local access: http://127.0.0.1:3674")
    logger.info(f"Network access: http://0.0.0.0:3674")
    logger.info(f"Running on Vercel: {IS_VERCEL}")
    logger.info(f"Serverless mode: {IS_SERVERLESS}")
    logger.info("=" * 80)
    
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Screenshot directory: {SCREENSHOT_DIR}")
    except Exception as e:
        logger.error(f"Failed to create screenshot directory: {str(e)}")
    
    logger.info(f"Playwright available: {PLAYWRIGHT_AVAILABLE}")
    
    if PLAYWRIGHT_AVAILABLE:
        if IS_SERVERLESS:
            logger.info("Serverless environment detected - checking Playwright installation")
            browsers_installed = await install_playwright_browsers()
            if browsers_installed:
                logger.info("Playwright browsers ready")
            else:
                logger.warning("Playwright browsers installation had issues")
        else:
            logger.info("Non-serverless environment - Playwright should be pre-installed")
    else:
        logger.error("Playwright NOT available - install with: pip install playwright")
    
    logger.info("Cloudscraper enabled - Cloudflare bypass available")
    
    cleanup_task = asyncio.create_task(cleanup_expired_files())
    
    yield
    
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down Screenshot API Server")

app = FastAPI(
    title="Screenshot API",
    description="High-performance screenshot API with Cloudflare bypass",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def index():
    return JSONResponse({
        "success": True,
        "service": "Screenshot API",
        "version": "1.0.0",
        "endpoints": {
            "screenshot": "/web/ss?url=<URL>&quality=<hd|fhd|wqhd|low>&bypass=<true|false>",
            "file": "/file/<file_id>",
            "health": "/health"
        },
        "qualities": list(QUALITY_SETTINGS.keys()),
        "dev": "@ISmartCoder",
        "updates": "@abirxdhackz"
    })

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
        
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright not available")
            raise HTTPException(
                status_code=500,
                detail="Playwright not installed. Install with: pip install playwright && playwright install chromium"
            )
        
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
        
        screenshot_success = await screenshot_with_playwright(target_url, output_path, width, height)
        
        if not screenshot_success:
            raise HTTPException(
                status_code=500,
                detail="Screenshot generation failed with Playwright"
            )
        
        if temp_html_file:
            try:
                if os.path.exists(temp_html_file):
                    os.remove(temp_html_file)
                    logger.debug(f"Cleaned up temp HTML: {temp_html_file}")
            except Exception as e:
                logger.warning(f"Failed to remove temp HTML: {str(e)}")
        
        if not output_path.exists():
            logger.error(f"Screenshot file not created: {output_path}")
            raise HTTPException(status_code=500, detail="Failed to generate screenshot - file not created")
        
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
        
        if IS_VERCEL:
            base_url = os.environ.get('VERCEL_URL', get_local_ip())
            if not base_url.startswith('http'):
                base_url = f"https://{base_url}"
            file_url = f"{base_url}/file/{fid}"
        else:
            server_ip = get_local_ip()
            file_url = f"http://{server_ip}:3674/file/{fid}"
        
        elapsed = time.time() - start_time
        
        logger.info(f"SUCCESS - Screenshot: {filename} ({file_size} bytes) in {elapsed:.2f}s via Playwright")
        
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
            "method": "Playwright",
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
            filename=data["filename"],
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": f'inline; filename="{data["filename"]}"'
            }
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
        return JSONResponse({
            "success": True,
            "status": "healthy",
            "playwright_available": PLAYWRIGHT_AVAILABLE,
            "cloudscraper_available": True,
            "screenshot_dir": str(SCREENSHOT_DIR),
            "active_files": len(STORE),
            "is_vercel": IS_VERCEL,
            "is_serverless": IS_SERVERLESS,
            "python_version": sys.version,
            "platform": platform.platform(),
            "timestamp": datetime.now().isoformat(),
            "dev": "@ISmartCoder",
            "updates": "@abirxdhackz"
        })
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }, status_code=500)

handler = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3674))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True
    )
