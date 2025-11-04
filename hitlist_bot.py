#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord Bot for tracking Epic Games account status.
- Saves accounts to a "hit list" for monitoring.
- Periodically checks if accounts become inactive.
- Notifies with a custom message when an account is "hit".
- Optimized for deployment on Render with persistent storage.
Last updated: 2025-10-25
"""

import os
import json
import time
import logging
import re
import asyncio
import sys
import random               # <-- required for shuffle / choice
import threading
import urllib.parse
from datetime import datetime

import requests
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button

# --- CONFIGURATION ---
# The bot token is now read from an environment variable for security.
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Bot version info
LAST_UPDATED = "2025-10-25 04:40:34"
BOT_USER = "gregergrgergeg"

# --- SERVER AND USER RESTRICTIONS ---
ALLOWED_SERVERS = [1427741533876125708, 1429638051482566708]
RESTRICTED_SERVER_ID = 1429638051482566708
ALLOWED_USER_ID = 851862667823415347

# --- RENDER PERSISTENT DISK CONFIG ---
DISK_PATH = "/etc/render/disk"
HITLIST_FILE_PATH = os.path.join(DISK_PATH, "hitlist.json")


# Epic API base URL
API_BASE = "https://api.proswapper.xyz/external"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")

# Headers for API requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# List of proxies to use for API lookups
PROXIES = [
    "45.89.53.245:3128", "66.36.234.130:1339", "45.167.126.1:8080",
    "190.242.157.215:8080", "154.62.226.126:8888", "51.159.159.73:80",
    "176.126.103.194:44214", "185.191.236.162:3128", "157.180.121.252:35993",
    "157.180.121.252:16621", "157.180.121.252:55503", "157.180.121.252:53919",
    "175.118.246.102:3128", "64.92.82.61:8081", "132.145.75.68:5457",
    "157.180.121.252:35519", "77.110.114.116:8081"
]

# Random "hit" messages
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
logger = logging.getLogger("hitlist_bot")
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True # Required for on_guild_join
bot = commands.Bot(command_prefix='!', intents=intents)

# --- GLOBAL VARIABLES & LOCKS ---
hitlist = {}
proxy_lock = threading.Lock()
current_proxy = None
proxy_last_checked = 0
proxy_check_interval = 60

# --- PROXY MANAGEMENT ---
def test_proxy(proxy, timeout=3):
    proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
    try:
        # Using a known valid endpoint for testing proxies might be more reliable
        response = requests.get(f"{API_BASE}/name/epic", proxies=proxy_dict, timeout=timeout, headers=HEADERS)
        return response.status_code in [200, 404] # 404 is also a valid response from the API
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
        logger.warning("No working proxy found. Trying direct connection.")
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
    """
    Ensure we return a consistent dict containing at least:
    - id
    - displayName
    - externalAuths (dict or {})
    This handles variations in the API's returned structure.
    """
    if not isinstance(obj, dict):
        return {}
    normalized = {}
    # id
    normalized['id'] = obj.get('id') or obj.get('accountId') or obj.get('account_id') or obj.get('epicId')
    # displayName variants
    normalized['displayName'] = obj.get('displayName') or obj.get('displayname') or obj.get('display_name') or obj.get('name')
    # external auths variants
    external = obj.get('externalAuths') or obj.get('external_auths') or obj.get('external') or obj.get('linkedAccounts')
    normalized['externalAuths'] = external if isinstance(external, dict) else {}
    # copy original keys through for any other usage
    normalized['_raw'] = obj
    return normalized

def epic_lookup_by_name(username):
    """Looks up an Epic account by display name using the /name/{name} endpoint."""
    encoded_username = urllib.parse.quote(username)
    url = f"{API_BASE}/name/{encoded_username}" # CORRECTED URL
    response = get_api_response(url)
    if response is None:
        return {"status": "ERROR", "message": "API request failed."}
    if response.status_code == 404:
        return {"status": "INACTIVE", "message": f"User '{username}' not found."}
    if response.status_code == 200:
        try:
            data = response.json()
            # If API returns a list, try to find exact match, otherwise take the first entry
            if isinstance(data, list):
                if not data:
                    return {"status": "INACTIVE", "message": f"User '{username}' not found."}
                # try exact match on displayName (case-insensitive)
                for user in data:
                    dn = (user.get('displayName') or user.get('displayname') or user.get('name') or "").lower()
                    if dn == username.lower():
                        return {"status": "ACTIVE", "data": _normalize_account_object(user)}
                # fallback to first list element if exact match not found
                return {"status": "ACTIVE", "data": _normalize_account_object(data[0])}
            elif isinstance(data, dict):
                # direct object - normalize and return
                return {"status": "ACTIVE", "data": _normalize_account_object(data)}
            else:
                return {"status": "INACTIVE", "message": f"User '{username}' not found."}
        except json.JSONDecodeError:
            return {"status": "ERROR", "message": "Failed to decode API response."}
    return {"status": "ERROR", "message": f"API returned unexpected status code: {response.status_code}"}


# --- DATA PERSISTENCE ---
def save_hitlist():
    try:
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
    print(f"Bot logged in as {bot.user.name}")
    print(f"User: {BOT_USER}")
    print(f"Last Updated: {LAST_UPDATED}")
    print("---------------------------------")
    # Leave any servers that are not in the allowed list
    for guild in bot.guilds:
        if guild.id not in ALLOWED_SERVERS:
            logger.warning(f"Leaving unauthorized server: {guild.name} ({guild.id})")
            await guild.leave()
    load_hitlist()
    find_working_proxy(force_check=True)
    account_monitor.start()

@bot.event
async def on_guild_join(guild):
    """Leaves a server if it is not on the allowed list."""
    if guild.id not in ALLOWED_SERVERS:
        logger.warning(f"Joined and leaving unauthorized server: {guild.name} ({guild.id})")
        await guild.leave()

@bot.before_invoke
async def check_permissions(ctx):
    """Global check to enforce server and user restrictions."""
    if not ctx.guild: # Ignore DMs for command checks
        raise commands.CheckFailure("Commands cannot be used in DMs.")
    if ctx.guild.id not in ALLOWED_SERVERS:
        logger.warning(f"Command '{ctx.command}' blocked in unauthorized server: {ctx.guild.name} ({ctx.guild.id})")
        raise commands.CheckFailure("This bot is not authorized for this server.")
    if ctx.guild.id == RESTRICTED_SERVER_ID and ctx.author.id != ALLOWED_USER_ID:
        logger.warning(f"User {ctx.author} ({ctx.author.id}) blocked from using '{ctx.command}' in restricted server.")
        raise commands.CheckFailure("You do not have permission to use this command.")
    return True

@bot.event
async def on_command_error(ctx, error):
    """Silently handle check failures to prevent bot from replying."""
    if isinstance(error, commands.CheckFailure):
        # Errors from check_permissions are logged there, so we just pass.
        pass
    elif isinstance(error, commands.CommandNotFound):
        pass # Ignore unknown commands
    else:
        logger.error(f"An error occurred with command '{ctx.command}': {error}")
        # Optionally, send a generic error message to the user
        # await ctx.send("An unexpected error occurred. Please try again later.")

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
                else: logger.warning(f"Could not find channel with ID {channel_id}.")
            if user_id:
                try:
                    user = await bot.fetch_user(user_id)
                    if user:
                        await user.send(embed=embed)
                        logger.info(f"Successfully sent DM to user {user.name} ({user_id}).")
                except discord.Forbidden: logger.warning(f"Failed to send DM to user {user_id}. They may have DMs disabled.")
                except Exception as e: logger.error(f"An unexpected error occurred when trying to DM user {user_id}: {e}")
            del hitlist[account_id]
            save_hitlist()
            logger.info(f"Removed {username} from the hitlist.")
        await asyncio.sleep(2)

# --- UI COMPONENTS FOR COMMANDS ---

class ConfirmationView(View):
    def __init__(self, account_data, original_author):
        super().__init__(timeout=60)  # Buttons will be disabled after 60 seconds
        self.account_data = account_data
        self.original_author = original_author
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow the original command author to interact
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("You cannot interact with this.", ephemeral=True)
            return False
        return True

    async def disable_buttons(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: Button):
        await self.disable_buttons()
        
        display_name = self.account_data.get("displayName", "N/A")
        account_id = self.account_data.get("id", "N/A")
        external_auths = self.account_data.get("externalAuths", {})

        format_str = f"my ID: {account_id}\nmy epic: {display_name}\n"
        
        # Check for xbox and psn in external_auths, which are dictionaries themselves
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
    """Looks up an Epic Games account by username or ID."""
    msg = await ctx.send(f"🔍 Searching for `{identifier}`...")
    
    # Determine if the identifier is an account ID or a username
    if _HEX32.match(identifier):
        result = await bot.loop.run_in_executor(None, epic_lookup_by_id, identifier)
    else:
        result = await bot.loop.run_in_executor(None, epic_lookup_by_name, identifier)

    if result["status"] == "ACTIVE":
        account_data = result["data"]
        # account_data may already be normalized by epic_lookup_by_name; if it's raw object, normalize it
        if isinstance(account_data, dict) and ('displayName' not in account_data and '_raw' not in account_data):
            account_data = _normalize_account_object(account_data)
        elif isinstance(account_data, dict) and '_raw' in account_data:
            # already normalized by lookup function
            pass
        elif isinstance(account_data, list) and account_data:
            account_data = _normalize_account_object(account_data[0])

        # Final fallbacks for name and id
        display_name = account_data.get("displayName") or (account_data.get('_raw') or {}).get('displayName') or (account_data.get('_raw') or {}).get('name') or "N/A"
        account_id = account_data.get("id") or (account_data.get('_raw') or {}).get('id') or "N/A"
        
        embed = discord.Embed(
            title=f"✅ Account Found: {display_name}",
            color=discord.Color.green()
        )
        embed.add_field(name="Status", value="🟢 **ACTIVE**", inline=False)
        embed.add_field(name="Account ID", value=account_id, inline=False)
        
        # Format linked accounts (use normalized externalAuths or fallbacks)
        external_auths = account_data.get("externalAuths") or (account_data.get('_raw') or {}).get('externalAuths') or {}
        if external_auths:
            linked_accounts_text = ""
            for platform, details in external_auths.items():
                if isinstance(details, dict):
                    name = details.get('externalDisplayName') or details.get('displayName') or details.get('name') or 'N/A'
                    linked_accounts_text += f"**{platform.capitalize()}:** {name}\n"
                else:
                    linked_accounts_text += f"**{platform.capitalize()}:** {details}\n"
            embed.add_field(name="🔗 Linked Accounts", value=linked_accounts_text, inline=False)
        else:
            embed.add_field(name="🔗 Linked Accounts", value="No external accounts linked.", inline=False)
            
        await msg.edit(content=None, embed=embed)

        # Create the confirmation view and send it
        view = ConfirmationView({"displayName": display_name, "id": account_id, "externalAuths": external_auths}, ctx.author)
        confirmation_msg = "Do you want to build a format for this user?"
        view.message = await ctx.send(confirmation_msg, view=view)

    elif result["status"] == "INACTIVE":
        embed = discord.Embed(
            title="❌ Account Not Found",
            color=discord.Color.red()
        )
        embed.add_field(name="Status", value="🔴 **INACTIVE**", inline=False)
        embed.add_field(name="Identifier Searched", value=identifier, inline=False)
        embed.add_field(name="Reason", value=result.get("message", "The account is inactive or does not exist."), inline=False)
        await msg.edit(content=None, embed=embed)

    else: # Handle ERROR or INVALID
        embed = discord.Embed(
            title="⚠️ Lookup Failed",
            description=result.get("message", "An unknown error occurred."),
            color=discord.Color.orange()
        )
        await msg.edit(content=None, embed=embed)


@bot.command(name='save')
async def save_account(ctx, *, identifier: str):
    """Saves an account to the hitlist by username or account ID."""
    if not identifier:
        await ctx.send("❌ **Error:** You must provide a username or account ID. Use `!save <USERNAME_OR_ID>`."); return

    msg = await ctx.send(f"🔍 Verifying account `{identifier}`...")
    
    # Determine if identifier is an ID or username
    if _HEX32.match(identifier):
        result = await bot.loop.run_in_executor(None, epic_lookup_by_id, identifier)
    else:
        result = await bot.loop.run_in_executor(None, epic_lookup_by_name, identifier)

    if result["status"] == "ACTIVE":
        account_data = result["data"]
        # ensure normalized
        if isinstance(account_data, dict) and '_raw' not in account_data:
            account_data = _normalize_account_object(account_data)
        account_id = account_data.get("id") or (account_data.get('_raw') or {}).get('id')
        username = account_data.get("displayName") or (account_data.get('_raw') or {}).get('displayName')

        if not account_id or not username:
            await msg.edit(content="❌ **Failed:** Could not retrieve essential account details (ID or Username)."); return

        if account_id in hitlist:
            await msg.edit(content=f"⚠️ **Notice:** Account `{username}` (ID: `{account_id}`) is already on the hitlist."); return

        hitlist[account_id] = {"username": username, "channel_id": ctx.channel.id, "user_id": ctx.author.id}
        save_hitlist()
        await msg.edit(content=f"✅ **Success!** `{username}` has been added to the hit list. Notifications will be sent to this channel and your DMs.")
    else:
        await msg.edit(content=f"❌ **Failed:** Could not verify `{identifier}` as an active account. Reason: {result.get('message')}")

@bot.command(name='unsave')
async def unsave_account(ctx, *, identifier: str):
    """Unsaves an account from the hitlist by username or account ID."""
    if not identifier:
        await ctx.send("❌ **Error:** You must provide a username or account ID. Use `!unsave <USERNAME_OR_ID>`."); return
        
    account_id_to_remove = None
    username_to_remove = None

    if _HEX32.match(identifier):
        account_id_to_remove = identifier
        if account_id_to_remove in hitlist:
            username_to_remove = hitlist[account_id_to_remove]['username']
    else:
        # Search for the username in the hitlist
        for acc_id, data in hitlist.items():
            if data['username'].lower() == identifier.lower():
                account_id_to_remove = acc_id
                username_to_remove = data['username']
                break
    
    if account_id_to_remove:
        del hitlist[account_id_to_remove]
        save_hitlist()
        await ctx.send(f"🗑️ **Success!** `{username_to_remove}` has been removed from the hit list.")
    else:
        await ctx.send(f"❌ **Error:** `{identifier}` was not found on the hit list.")

@bot.command(name='hitlist')
async def show_hitlist(ctx):
    """Displays all the accounts currently on the hit list."""
    if not hitlist:
        await ctx.send("The hit list is currently empty."); return
    embed = discord.Embed(title="Current Hit List", color=discord.Color.blue())
    description = ""
    for i, (account_id, data) in enumerate(hitlist.items(), 1):
        channel_mention = f"<#{data.get('channel_id')}>" if data.get('channel_id') else "N/A"
        user_mention = f"<@{data.get('user_id')}>" if data.get('user_id') else "N/A"
        description += f"**{i}. {data['username']}**\n   - ID: `{account_id}`\n   - Channel: {channel_mention}\n   - Saved by: {user_mention}\n"
    embed.description = description
    embed.set_footer(text=f"Monitoring {len(hitlist)} accounts.")
    await ctx.send(embed=embed)

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: Bot token not found. Make sure the DISCORD_BOT_TOKEN environment variable is set on Render.")
        sys.exit(1)
    print("Starting bot...")
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("ERROR: Invalid bot token provided in environment variables.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to start the bot: {e}")
        sys.exit(1)
