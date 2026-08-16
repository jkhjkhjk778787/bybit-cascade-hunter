#!/usr/bin/env python3
"""
Bybit AI Agent OAuth PKCE 인증 리스너 및 자동 연동 도구 (내장 urllib 사용)
"""

import os
import sys
import json
import base64
import hashlib
import secrets
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 9876
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
CLIENT_ID = "ai-agent"
SCOPE = "ai-account"
BYBIT_AUTH_BASE = "https://www.bybit.com/oauth/v1/authorize"
BYBIT_API_BASE = "https://api2.bybit.com"

# 1. PKCE Code Verifier & Challenge 생성
def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(hashed).decode('ascii').rstrip('=')
    state = secrets.token_hex(16)
    return code_verifier, code_challenge, state

code_verifier, code_challenge, state = generate_pkce()

# OAuth URL 생성
params = {
    "client_id": CLIENT_ID,
    "scope": SCOPE,
    "code_challenge_method": "S256",
    "code_challenge": code_challenge,
    "state": state,
    "redirect_uri": REDIRECT_URI
}
auth_url = f"{BYBIT_AUTH_BASE}?{urllib.parse.urlencode(params)}"

auth_result = {}

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [None])[0]
            returned_state = query.get("state", [None])[0]
            error = query.get("error", [None])[0]

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()

            if error:
                auth_result["error"] = error
                self.wfile.write(f"<h1>Authorization Failed: {error}</h1>".encode("utf-8"))
            elif code:
                auth_result["code"] = code
                auth_result["state"] = returned_state
                html = """
                <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                    <h1 style="color: #2e7d32;">Bybit AI Agent 연결 승인 완료!</h1>
                    <p>인증 코드가 정상적으로 수신되었습니다. 이 브라우저 창을 닫고 터미널/채팅으로 돌아가세요.</p>
                </body>
                </html>
                """
                self.wfile.write(html.encode("utf-8"))
            else:
                self.wfile.write(b"<h1>Invalid Request</h1>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 조용히 처리

def http_post(url, data_dict):
    json_bytes = json.dumps(data_dict).encode('utf-8')
    req = urllib.request.Request(url, data=json_bytes, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))

def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))

def run_server():
    print(f"AUTH_URL_START:{auth_url}:AUTH_URL_END", flush=True)
    server = HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    server.timeout = 300  # 5분 대기
    
    while "code" not in auth_result and "error" not in auth_result:
        server.handle_request()

    if "code" in auth_result:
        auth_code = auth_result["code"]
        print(f"\n[+] Authorization Code 수신 성공: {auth_code[:10]}...", flush=True)
        
        # Access Token 교환
        token_url = f"{BYBIT_API_BASE}/oauth/v1/public/access_token"
        payload = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": auth_code,
            "code_verifier": code_verifier,
            "redirect_uri": REDIRECT_URI
        }
        
        try:
            token_data = http_post(token_url, payload)
            print(f"[+] Token Response: {json.dumps(token_data)}", flush=True)
            
            access_token = token_data.get("result", {}).get("access_token") or token_data.get("access_token")
            if access_token:
                # AI Subaccount API 키 조회
                acc_url = f"{BYBIT_API_BASE}/oauth/v1/resource/restrict/ai_accounts"
                headers = {"Authorization": f"Bearer {access_token}"}
                acc_data = http_get(acc_url, headers)
                print(f"[+] AI Accounts: {json.dumps(acc_data)}", flush=True)
                
                # 저장
                with open("/home/jph/bybit_trade_collector/bybit_credentials.json", "w") as f:
                    json.dump({"token": token_data, "accounts": acc_data}, f, indent=2)
                print("[SUCCESS] Bybit AI Subaccount API 연동 완료 및 저장 완료!", flush=True)
        except Exception as e:
            print(f"[!] Token Exchange Error: {e}", flush=True)
    elif "error" in auth_result:
        print(f"[!] Auth Error: {auth_result['error']}", flush=True)

if __name__ == "__main__":
    run_server()
