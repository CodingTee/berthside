"""Automated LLM API Connectivity Test Script.

Tests:
1. Alibaba DashScope (Qwen-Plus / Qwen-VL)
2. Google Gemini (Gemini 2.5 Flash)
3. Zhipu AI (GLM-4.6v-Flash & GLM-4.6v)

Run:
    python scripts/test_llm_connection.py
"""
import os
import sys
import json
import time
import urllib.request
from pathlib import Path

# Ensure utf-8 output in Windows PowerShell/CMD
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

backend_root = Path(__file__).resolve().parent.parent
env_file = backend_root / ".env"

# Read .env file directly
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'").strip('"')
            os.environ[k] = v  # update current process

def mask_key(k: str) -> str:
    if not k:
        return "[NOT SET]"
    if len(k) <= 10:
        return "***"
    return k[:6] + "..." + k[-4:]

def test_dashscope():
    key = os.getenv("DASHSCOPE_API_KEY")
    print(f"\n[1] Testing Alibaba DashScope (Qwen)... Key: {mask_key(key)}")
    if not key:
        print("    -> Skipped (DASHSCOPE_API_KEY not found in .env)")
        return
    try:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        payload = {
            "model": "qwen-plus",
            "messages": [{"role": "user", "content": "Respond with: OK"}],
            "max_tokens": 10
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.time() - t0
            reply = data["choices"][0]["message"]["content"].strip()
            print(f"    -> [SUCCESS] (took {elapsed:.2f}s) Model: qwen-plus | Reply: '{reply}'")
    except Exception as e:
        print(f"    -> [FAILED]: {e}")

def test_gemini():
    key = os.getenv("GEMINI_API_KEY")
    print(f"\n[2] Testing Google Gemini... Key: {mask_key(key)}")
    if not key:
        print("    -> Skipped (GEMINI_API_KEY not found in .env)")
        return
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        payload = {
            "contents": [{"parts": [{"text": "Respond with: OK"}]}]
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.time() - t0
            reply = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"    -> [SUCCESS] (took {elapsed:.2f}s) Model: gemini-2.5-flash | Reply: '{reply}'")
    except Exception as e:
        print(f"    -> [FAILED]: {e}")

def test_zhipu():
    key = os.getenv("ZHIPUAI_API_KEY")
    print(f"\n[3] Testing Zhipu AI (GLM)... Key: {mask_key(key)}")
    if not key:
        print("    -> Skipped (ZHIPUAI_API_KEY not found in .env)")
        return
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    
    # 3a. Small model: glm-4.6v-flash
    for model_name, label in [("glm-4.6v-flash", "Small Vision Model"), ("glm-4.6v", "Large Vision Model")]:
        try:
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "Respond with: OK"}],
                "max_tokens": 10
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json"
                }
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                elapsed = time.time() - t0
                reply = data["choices"][0]["message"]["content"].strip()
                print(f"    -> [SUCCESS] (took {elapsed:.2f}s) {label} ({model_name}) | Reply: '{reply}'")
        except Exception as e:
            print(f"    -> [FAILED] {label} ({model_name}): {e}")
        time.sleep(1)  # small pause to avoid concurrency rate-limits

if __name__ == "__main__":
    print("=" * 60)
    print("      SDOC LLM API Key Connectivity Test Suite")
    print(f"      Reading configuration from: {env_file}")
    print("=" * 60)
    test_dashscope()
    test_gemini()
    test_zhipu()
    print("\n" + "=" * 60)
    print("All API connectivity tests completed.")
    print("=" * 60)
