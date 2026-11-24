"""Security audit and regression test suite for Cook AI."""
import json
import os
import sys
import time

from starlette.testclient import TestClient

import server
from server import app, sign_session, verify_session

client = TestClient(app)

def test_security_patches():
    print("--- 1. Testing GET /api/user (No PII / user_id leak) ---")
    # Unauthenticated
    r = client.get("/api/user")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_logged_in"] is False
    assert "user_id" not in data, "VULNERABILITY: user_id leaked in /api/user"
    assert "email" not in data, "VULNERABILITY: email leaked in /api/user"
    assert data["name"] == "Guest Cook"
    print("✓ Unauthenticated /api/user is safe and exposes no PII.")

    # Authenticated via signed session
    test_token = sign_session({
        "uid": "google_sub_123456789",
        "name": "Jane Doe",
        "picture": "https://example.com/avatar.jpg",
        "exp": int(time.time()) + 3600
    })
    client.cookies.set("cook_ai_user", test_token)
    r = client.get("/api/user")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_logged_in"] is True
    assert data["name"] == "Jane Doe"
    assert data["picture"] == "https://example.com/avatar.jpg"
    assert "user_id" not in data, "VULNERABILITY: user_id leaked in /api/user for authenticated user"
    assert "email" not in data, "VULNERABILITY: email leaked in /api/user for authenticated user"
    assert "123456789" not in json.dumps(data), "VULNERABILITY: Google sub ID leaked in response"
    print("✓ Authenticated /api/user returns display profile only, zero PII/ID leaked.")

    print("\n--- 2. Testing Insecure Cookie Settings ---")
    # Check Set-Cookie headers on auth_dev
    client.cookies.clear()
    r = client.post("/api/auth/dev", json={"name": "Alice", "email": "alice@example.com"})
    assert r.status_code == 200, r.text
    dev_data = r.json()
    assert "user_id" not in dev_data, "VULNERABILITY: user_id leaked in /api/auth/dev"
    assert "email" not in dev_data, "VULNERABILITY: email leaked in /api/auth/dev"
    assert dev_data["name"] == "Alice"

    # Inspect the cookie attribute
    set_cookie_header = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header or "httponly" in set_cookie_header, "VULNERABILITY: cook_ai_user cookie is missing HttpOnly"
    assert "SameSite=lax" in set_cookie_header or "samesite=lax" in set_cookie_header, "VULNERABILITY: cook_ai_user cookie is missing SameSite=lax"
    print("✓ Auth cookies are hardened with HttpOnly and SameSite=lax.")

    print("\n--- 3. Testing GET /api/feedback (Access Control & Anti-Data Dump) ---")
    # Unauthenticated access to feedback must be forbidden
    client.cookies.clear()
    r = client.get("/api/feedback")
    assert r.status_code == 403, f"Expected 403 Forbidden, got {r.status_code}: {r.text}"
    print("✓ Public unauthenticated request to /api/feedback is blocked with 403 Forbidden.")

    # Access with invalid admin key must be forbidden
    r = client.get("/api/feedback", headers={"X-Admin-Key": "wrong_key"})
    assert r.status_code == 403
    print("✓ Invalid admin key to /api/feedback is blocked.")

    # Access with valid admin key
    os.environ["ADMIN_API_KEY"] = "super_secret_admin_test_key"
    r = client.get("/api/feedback", headers={"X-Admin-Key": "super_secret_admin_test_key"})
    assert r.status_code == 200, r.text
    assert "feedback" in r.json()
    print("✓ Authorized developer access to /api/feedback functions as expected.")

    print("\n--- 4. Testing Dev Auth Lockdown in Production ---")
    os.environ["RENDER"] = "true"
    r = client.post("/api/auth/dev", json={"name": "Attacker"})
    assert r.status_code == 403, f"Expected 403 in production mode, got {r.status_code}"
    print("✓ Dev auth endpoint is strictly disabled in production environment.")
    del os.environ["RENDER"]

    print("\n--- 5. Testing Google JWT Validation ---")
    # Forged base64 token without Google signature
    fake_header = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    fake_payload = "eyJzdWIiOiIxMjM0NTYiLCJlbWFpbCI6ImhhY2tlckBldmlsLmNvbSIsIm5hbWUiOiJIYWNrZXIifQ"
    fake_token = f"{fake_header}.{fake_payload}.fakesig"
    r = client.post("/api/auth/google", json={"credential": fake_token})
    assert r.status_code == 401, f"Expected 401 for forged token, got {r.status_code}: {r.text}"
    print("✓ Forged Google ID tokens are rejected with 401.")

    print("\n--- 6. Testing Security Headers ---")
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    print("✓ HTTP Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy) are present.")

    print("\n--- 7. Testing Core Kitchen & CRUD Safety ---")
    # Login as Alice
    r = client.post("/api/auth/dev", json={"name": "Alice"})
    assert r.status_code == 200
    # Add pantry item
    r = client.post("/api/pantry", json={"items": [{"name": "Organic Tomatoes"}]})
    assert r.status_code == 200, r.text
    # Get kitchen
    r = client.get("/api/kitchen")
    assert r.status_code == 200, r.text
    k_data = r.json()
    assert any(i["name"] == "Organic Tomatoes" for i in k_data["pantry"])
    assert "user_id" not in k_data, "VULNERABILITY: user_id leaked in /api/kitchen"
    print("✓ Kitchen workflows operate normally and securely.")

    print("\n==========================================")
    print("🎉 ALL SECURITY CHECKS PASSED SUCCESSFULLY!")
    print("==========================================")

if __name__ == "__main__":
    test_security_patches()
