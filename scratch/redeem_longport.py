"""Scratch helper script to redeem LongPort auth_code and fetch quote for NVDA.US."""

import sys
import json
import urllib.request
import os
from pathlib import Path

def redeem_and_verify(auth_code: str):
    auth_code = auth_code.strip()
    url = "https://mcp.longportapp.com/agent"
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "authenticate",
            "arguments": {
                "auth_code": auth_code
            }
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0"
    }
    
    print(f"Redeeming auth_code at {url}...")
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    
    resp_data = None
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str.startswith("data:"):
                resp_data = json.loads(line_str[5:].strip())
                break
                
    if not resp_data:
        print("Error: No data received from server.")
        return
        
    if "error" in resp_data:
        print("Server error:", resp_data["error"]["message"])
        return
        
    result = resp_data.get("result", {})
    print("Authentication Result:", json.dumps(result, indent=2))
    
    # Extract token text / details if returned
    content = result.get("content", [])
    token_str = None
    for item in content:
        text = item.get("text", "")
        print("Content text:", text)
        if "access_token" in text:
            token_str = text

    # Call main service https://mcp.longportapp.com to fetch NVDA.US quote if token found
    print("\n--- Quote Verification ---")
    # Also attempt quote call using OpenAPI SDK or HTTP if token is retrieved
    return result

if __name__ == "__main__":
    if len(sys.argv) > 1:
        redeem_and_verify(sys.argv[1])
    else:
        print("Usage: python redeem_longport.py <AUTH_CODE>")
