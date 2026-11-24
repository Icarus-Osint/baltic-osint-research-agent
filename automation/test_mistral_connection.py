"""Minimal single-request Mistral API connectivity test.

Reads MISTRAL_API_KEY from automation/.env without exposing it, sends exactly one
chat completion request to ministral-3b-latest (no retries, no model fallback),
and prints the HTTP status, model, response, and token usage.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

AUTOMATION_DIR = Path(__file__).resolve().parent
ENV_FILE = AUTOMATION_DIR / ".env"
ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
REQUESTED_MODEL = "ministral-3b-latest"
TEST_PROMPT = "Reply with exactly: MISTRAL_OK"
MAX_TOKENS = 16


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def main() -> int:
    if not ENV_FILE.exists():
        print(f"ERROR: Missing environment file at {ENV_FILE}")
        return 2

    load_dotenv(ENV_FILE)
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MISTRAL_API_KEY is missing or empty in automation/.env")
        return 2

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": REQUESTED_MODEL,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }

    try:
        resp = requests.post(ENDPOINT, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        err_msg = redact(f"{type(exc).__name__}: {exc}", api_key)
        print("HTTP_STATUS: CONNECTION_ERROR")
        print(f"REQUESTED_MODEL: {REQUESTED_MODEL}")
        print(f"ERROR: {err_msg}")
        return 1

    print(f"HTTP_STATUS: {resp.status_code}")
    print(f"REQUESTED_MODEL: {REQUESTED_MODEL}")

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            print(f"ERROR: Invalid JSON response ({exc})")
            return 1
        actual_model = data.get("model", "")
        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "").strip()
        usage = data.get("usage") or {}
        print(f"ACTUAL_MODEL: {actual_model}")
        print(f"RESPONSE_TEXT: {content}")
        print(f"PROMPT_TOKENS: {usage.get('prompt_tokens', 'N/A')}")
        print(f"COMPLETION_TOKENS: {usage.get('completion_tokens', 'N/A')}")
        print(f"TOTAL_TOKENS: {usage.get('total_tokens', 'N/A')}")
        return 0

    body = redact(resp.text[:600].replace("\n", " "), api_key)
    print(f"ERROR_BODY: {body}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
