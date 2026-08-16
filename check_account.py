#!/usr/bin/env python3
"""
[MAINNET] Bybit AI Subaccount Unified Trading Account 잔고 및 포지션 연동 확인 도구
"""

import json
import time
import hmac
import hashlib
import urllib.request
import urllib.parse

CRED_PATH = "/home/jph/.bybit/oauth_token.json"

with open(CRED_PATH, "r") as f:
    cred = json.load(f)

ai_acc = cred.get("ai-account", {})
API_KEY = ai_acc.get("api_key")
API_SECRET = ai_acc.get("api_secret")

if not API_KEY or not API_SECRET:
    print("[!] AI Subaccount credentials missing")
    exit(1)

def signed_request(endpoint, params=None):
    if params is None:
        params = {}
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    query_string = urllib.parse.urlencode(params)
    
    # Bybit V5 HMAC SHA256 서명
    raw_sign = timestamp + API_KEY + recv_window + query_string
    signature = hmac.new(API_SECRET.encode('utf-8'), raw_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    url = f"https://api.bybit.com{endpoint}"
    if query_string:
        url += f"?{query_string}"

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "User-Agent": "BybitAI-Agent/1.0"
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode('utf-8'))

# 1. 지갑 잔고 조회 (Unified Account)
wallet_res = signed_request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
print("\n[MAINNET] Bybit AI Subaccount 지갑 잔고 조회 결과:")
print(json.dumps(wallet_res, indent=2))
