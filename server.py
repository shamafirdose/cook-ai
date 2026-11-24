"""Cook AI - Modern FastAPI/ASGI Backend Server.

Provides REST endpoints for kitchen management, recipe matching with Python guardrails,
Gemini multimodal vision scanner, Pexels food photography, and zero-waste savings tracking.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import database as db
import gemini_client as ai
import logic as L
import photos
import shelf_life

# Initialize database schema
db.initialize_database()

DAILY_LIMIT = int(L.get_secret("DAILY_SUGGESTIONS", "10") or 10)
DEV_USER_ID = "default_cook_user"


# --------------------------------------------------------------------------
# Security & Cryptographic Session Helpers
# --------------------------------------------------------------------------
def _get_signing_key() -> bytes:
    """Retrieve or derive persistent secret key for signing session tokens."""
    key = (
        L.get_secret("SESSION_SECRET")
        or os.getenv("SESSION_SECRET")
        or L.get_secret("client_id")
        or "cook-ai-default-signing-salt-key-928471"
    )
    return key.encode("utf-8")


def sign_session(payload: dict) -> str:
    """Create a tamper-proof HMAC-SHA256 signed session token."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_get_signing_key(), raw, hashlib.sha256).digest()
    token = (
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    )
    return token


def verify_session(token: str) -> dict | None:
    """Verify HMAC signature and expiration on a session token."""
    if not token or "." not in token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        raw_b64, sig_b64 = parts[0], parts[1]
        raw_padded = raw_b64 + "=" * ((4 - len(raw_b64) % 4) % 4)
        sig_padded = sig_b64 + "=" * ((4 - len(sig_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(raw_padded)
        expected_sig = hmac.new(_get_signing_key(), raw, hashlib.sha256).digest()
        actual_sig = base64.urlsafe_b64decode(sig_padded)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("exp") and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


def _verify_google_credential(credential: str) -> dict:
    """Validate Google ID token JWT claims and authenticity."""
    parts = credential.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))

    # Verify issuer
    issuer = payload.get("iss", "")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("Invalid token issuer")

    # Verify expiration
    exp = payload.get("exp")
    if not exp or float(exp) < time.time():
        raise ValueError("Token has expired")

    # Verify audience if configured
    expected_client_id = L.get_secret("client_id") or os.getenv("GOOGLE_CLIENT_ID")
    if expected_client_id:
        token_aud = payload.get("aud")
        if token_aud != expected_client_id:
            raise ValueError("Token audience mismatch")

    # Verify with Google endpoint if network is reachable
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}",
            headers={"User-Agent": "CookAI-Auth/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                google_verified = json.loads(resp.read().decode("utf-8"))
                return google_verified
    except urllib.error.HTTPError as exc:
        raise ValueError("Google rejected token signature") from exc
    except Exception:
        # Offline / local environment without external network
        pass

    return payload


def _extract_session(request) -> dict | None:
    """Extract and verify session from Authorization header or cook_ai_user cookie."""
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    if not token:
        token = request.cookies.get("cook_ai_user", "").strip()

    if not token:
        return None

    # First check signed session token
    verified = verify_session(token)
    if verified:
        return verified

    # Fallback only for non-production local development
    is_prod = bool(os.getenv("RENDER") or os.getenv("ENV") == "production")
    if not is_prod:
        if token == DEV_USER_ID:
            return {"uid": DEV_USER_ID, "name": "Guest Cook", "picture": ""}
        if token.startswith("usr_") or token.startswith("dev_"):
            return {"uid": token, "name": "Cook", "picture": ""}

    return None


def _get_user_id(request) -> str:
    """Extract user_id from verified session, or fallback to guest user."""
    session = _extract_session(request)
    if session and session.get("uid"):
        return str(session["uid"])
    return DEV_USER_ID


def _require_auth(request) -> str | None:
    """Return authenticated user_id, or None if guest/unauthenticated."""
    session = _extract_session(request)
    if session and session.get("uid") and session["uid"] != DEV_USER_ID:
        return str(session["uid"])
    return None


def _auth_required_response() -> JSONResponse:
    return _json(
        {
            "error": "auth_required",
            "message": "Sign in with Google is required before making any changes.",
        },
        401,
    )


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


# --------------------------------------------------------------------------
# Endpoints: System & Auth
# --------------------------------------------------------------------------
async def get_config(request):
    client_id = L.get_secret("client_id") or ""
    return _json({"google_client_id": client_id})


async def auth_google(request):
    body = await request.json()
    credential = body.get("credential", "")
    if not credential:
        return _json({"error": "No credential provided"}, 400)
    try:
        payload = _verify_google_credential(credential)
        sub = str(payload.get("sub") or "").strip()
        if not sub:
            return _json({"error": "No account id in Google token"}, 400)

        name = payload.get("name") or "Cook"
        picture = payload.get("picture") or ""

        # Generate secure signed session
        session_token = sign_session(
            {
                "uid": sub,
                "name": name,
                "picture": picture,
                "exp": int(time.time()) + 60 * 60 * 24 * 90,
            }
        )

        # Do NOT leak raw user_id or email address to the client response
        response = JSONResponse(
            {
                "status": "ok",
                "name": name,
                "picture": picture,
                "is_logged_in": True,
            }
        )

        is_https = request.url.scheme == "https" or os.getenv("RENDER") is not None
        response.set_cookie(
            key="cook_ai_user",
            value=session_token,
            max_age=60 * 60 * 24 * 90,
            httponly=True,
            samesite="lax",
            secure=is_https,
        )
        return response
    except Exception:
        return _json({"error": "Google sign-in verification failed"}, 401)


async def auth_dev(request):
    """Instant sign-in for testing/local development."""
    is_prod = bool(os.getenv("RENDER") or os.getenv("ENV") == "production")
    dev_key = os.getenv("DEV_ACCESS_KEY")
    if is_prod:
        provided_key = request.headers.get("X-Dev-Key") or ""
        if not dev_key or provided_key != dev_key:
            return _json({"error": "Development login disabled in production."}, 403)

    body = await request.json()
    email = body.get("email", "").strip()
    name = body.get("name", "").strip()
    if not name and email:
        name = email.split("@")[0].capitalize()
    if not name:
        name = "Local Chef"

    # Hash user identifier so internal email is never used directly as raw user ID
    if email:
        uid = f"usr_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
    else:
        uid = f"usr_{hashlib.sha256(name.encode()).hexdigest()[:16]}"

    session_token = sign_session(
        {
            "uid": uid,
            "name": name,
            "picture": "",
            "exp": int(time.time()) + 60 * 60 * 24 * 30,
        }
    )

    response = JSONResponse(
        {
            "status": "ok",
            "name": name,
            "picture": "",
            "is_logged_in": True,
        }
    )
    is_https = request.url.scheme == "https" or os.getenv("RENDER") is not None
    response.set_cookie(
        key="cook_ai_user",
        value=session_token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return response


async def auth_logout(request):
    response = JSONResponse({"status": "ok", "message": "Signed out"})
    response.delete_cookie("cook_ai_user")
    return response


async def health(request):
    ok, msg = db.check_connection()
    return _json(
        {
            "status": "ok" if ok else "degraded",
            "app": "Cook AI",
            "storage": db.storage_mode(),
            "database": msg,
            "ai_ready": ai.ai_configured(),
            "photos_ready": photos.photos_configured(),
        }
    )


async def get_user_profile(request):
    """Return user profile for UI header. Never leaks raw email or internal user ID."""
    session = _extract_session(request)
    if session and session.get("uid") and session["uid"] != DEV_USER_ID:
        return _json(
            {
                "name": session.get("name", "Cook"),
                "picture": session.get("picture", ""),
                "is_logged_in": True,
            }
        )
    return _json(
        {
            "name": "Guest Cook",
            "picture": "",
            "is_logged_in": False,
        }
    )


# --------------------------------------------------------------------------
# Endpoints: Kitchen & Inventory Management
# --------------------------------------------------------------------------
async def get_kitchen(request):
    uid = _get_user_id(request)
    data = db.load_all(uid)

    # Calculate expiring soon items (< 4 days) for gentle zero-waste suggestion
    expiring_soon = []
    for item in data["pantry"]:
        exp = item.get("expires_at")
        days = shelf_life.days_until(exp)
        if item.get("use_soon") or (days is not None and days <= 4):
            expiring_soon.append({**item, "days_left": days})

    return _json(
        {
            **data,
            "expiring_soon": expiring_soon,
            "daily_limit": DAILY_LIMIT,
            "remaining_today": max(0, DAILY_LIMIT - data["usage_today"]),
        }
    )


async def set_diet(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    diet = body.get("diet", "both")
    if diet not in L.DIETS:
        return _json({"error": f"Invalid diet. Must be one of {L.DIETS}"}, 400)
    db.set_diet(uid, diet)
    return _json({"status": "ok", "diet": diet})


async def add_pantry_items(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    raw_items = body.get("items", [])
    if isinstance(raw_items, list) and raw_items and isinstance(raw_items[0], dict):
        result = db.add_pantry_items_with_expiry(uid, raw_items)
    else:
        names = body.get("names") or raw_items
        result = db.add_many(uid, "pantry", names)
    return _json(result)


async def update_pantry_item(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    item_id = int(request.path_params["id"])
    body = await request.json()
    if "name" in body and str(body["name"]).strip():
        db.rename_item(uid, "pantry", item_id, str(body["name"]).strip())
    if "use_soon" in body:
        db.set_use_soon(uid, item_id, bool(body["use_soon"]))
    if "expires_at" in body:
        db.set_item_expiry(uid, item_id, body["expires_at"] or None)
    return _json({"status": "ok"})


async def delete_pantry_items(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    ids = body.get("ids", [])
    db.delete_items(uid, "pantry", ids)
    return _json({"status": "ok", "deleted": len(ids)})


async def add_essentials(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    names = body.get("names", [])
    result = db.add_many(uid, "essential", names)
    return _json(result)


async def delete_essentials(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    ids = body.get("ids", [])
    db.delete_items(uid, "essential", ids)
    return _json({"status": "ok", "deleted": len(ids)})


async def add_restricted(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    names = body.get("names", [])
    result = db.add_many(uid, "prohibited", names)
    return _json(result)


async def delete_restricted(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    ids = body.get("ids", [])
    db.delete_items(uid, "prohibited", ids)
    return _json({"status": "ok", "deleted": len(ids)})


# --------------------------------------------------------------------------
# Endpoints: Home Recipes (Dishes)
# --------------------------------------------------------------------------
async def propose_dish_ingredients(request):
    body = await request.json()
    name = body.get("name", "")
    if not name.strip():
        return _json({"ok": False, "message": "Type a dish name first."}, 400)
    proposal = ai.propose_dish_ingredients(name)
    return _json(proposal)


async def add_dish(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    name = body.get("name", "")
    ingredients = body.get("ingredients", [])
    status = db.add_dish(uid, name, ingredients)
    return _json({"status": status})


async def update_dish(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    dish_id = int(request.path_params["id"])
    body = await request.json()
    name = body.get("name", "").strip()
    ingredients = body.get("ingredients")
    status = db.update_dish(uid, dish_id, name, ingredients)
    return _json({"status": status})


async def delete_dish(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    dish_id = int(request.path_params["id"])
    db.delete_dish(uid, dish_id)
    return _json({"status": "ok"})


# --------------------------------------------------------------------------
# Endpoints: Shopping List
# --------------------------------------------------------------------------
async def add_shopping_items(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    names = body.get("names", [])
    result = db.add_many(uid, "shopping", names)
    return _json(result)


async def toggle_shopping_bought(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    item_id = int(request.path_params["id"])
    body = await request.json()
    bought = bool(body.get("bought", False))
    db.set_bought(uid, item_id, bought)
    return _json({"status": "ok", "bought": bought})


async def move_shopping_to_pantry(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    items = db.get_items(uid, "shopping")
    bought = [i for i in items if i.get("bought")]
    moved = 0
    for item in bought:
        status = db.move_shopping_to_pantry(uid, item["id"])
        if status in ("ok", "exists"):
            moved += 1
    return _json({"status": "ok", "moved": moved})


async def clear_bought_shopping(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    items = db.get_items(uid, "shopping")
    bought_ids = [i["id"] for i in items if i.get("bought")]
    if bought_ids:
        db.delete_items(uid, "shopping", bought_ids)
    return _json({"status": "ok", "cleared": len(bought_ids)})


# --------------------------------------------------------------------------
# Endpoints: Recipe Suggestions Engine (Core)
# --------------------------------------------------------------------------
def _enrich_recipe(r: dict) -> dict:
    """Add external search and high-resolution food photo links to recipe dict."""
    name = r["name"]
    photo = photos.get_photo(name)
    return {
        **r,
        "photo_url": photo["url"] if photo else None,
        "youtube_url": L.youtube_url(name),
        "recipe_url": L.recipe_url(name),
    }


async def suggest_recipes(request):
    uid = _get_user_id(request)
    body = await request.json()
    meal = str(body.get("meal", "Any"))[:30]
    prompt = str(body.get("prompt", ""))[:500]
    try:
        servings = max(1, min(20, int(body.get("servings", 2))))
    except (ValueError, TypeError):
        servings = 2
    spice_level = str(body.get("spice_level", "Medium"))[:20]
    kid_friendly = bool(body.get("kid_friendly", False))

    data = db.load_all(uid)
    inventory = [i["name"] for i in data["pantry"] + data["essential"]]
    if not data["pantry"]:
        return _json(
            {
                "status": "empty",
                "message": "Add what you have at home first (Pantry).",
                "favourites": [],
                "alternatives": [],
                "near": None,
                "notes": [],
            }
        )

    allowed, used_after = db.consume_suggestion(uid, DAILY_LIMIT)
    if not allowed:
        return _json(
            {
                "status": "limit",
                "message": f"You have used all {DAILY_LIMIT} suggestions for today. Come back tomorrow!",
                "favourites": [],
                "alternatives": [],
                "near": None,
                "notes": [],
            }
        )

    prohibited_keys = [i["key"] for i in data["prohibited"]]
    use_soon = [i["key"] for i in data["pantry"] if i.get("use_soon")]

    cookable, near = L.classify_dishes(
        data["dishes"], L.key_set(inventory), prohibited_keys, data["diet"], use_soon
    )

    try:
        history = [h["name"] for h in db.get_recent_history(uid, 15)]
    except Exception:
        history = []

    res = ai.generate_suggestions(
        inventory_names=inventory,
        prohibited_names=[i["name"] for i in data["prohibited"]],
        diet=data["diet"],
        meal=meal,
        user_prompt=prompt,
        cookable=cookable,
        near=near,
        favourite_names=[d["name"] for d in data["dishes"]],
        history_names=history,
        servings=servings,
        spice_level=spice_level,
        kid_friendly=kid_friendly,
    )

    # Prefetch photos for all returned recipes concurrently
    all_names = [
        r["name"]
        for r in res["favourites"]
        + res["alternatives"]
        + ([res["near"]] if res["near"] else [])
    ]
    photos.prefetch(all_names)

    enriched_favs = [_enrich_recipe(r) for r in res["favourites"]]
    enriched_alts = [_enrich_recipe(r) for r in res["alternatives"]]
    enriched_near = _enrich_recipe(res["near"]) if res["near"] else None

    return _json(
        {
            "status": res["status"],
            "message": res["message"],
            "favourites": enriched_favs,
            "alternatives": enriched_alts,
            "near": enriched_near,
            "notes": res["notes"],
            "usage_today": used_after,
            "remaining_today": max(0, DAILY_LIMIT - used_after),
        }
    )


# --------------------------------------------------------------------------
# Endpoints: Multimodal Vision Scanner (Fridge Photo or Bill/Receipt)
# --------------------------------------------------------------------------
async def scan_groceries(request):
    form = await request.form()
    file = form.get("file")
    if not file:
        return _json({"ok": False, "items": [], "message": "No photo was uploaded."}, 400)

    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        return _json({"ok": False, "items": [], "message": "Photo is too large (max 10MB)."}, 400)

    mime_type = file.content_type or "image/jpeg"
    result = ai.scan_groceries_image(image_bytes, mime_type=mime_type)
    return _json(result)


# --------------------------------------------------------------------------
# Endpoints: Cook History & Waste Stats
# --------------------------------------------------------------------------
async def record_cooked(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    body = await request.json()
    dish_name = body.get("dish_name", "")
    source = body.get("source", "favourite")
    used_item_ids = body.get("used_item_ids", [])

    result = db.add_history(uid, dish_name, source, used_item_ids=used_item_ids)
    stats = db.get_waste_stats(uid)
    return _json({**result, "stats": stats})


async def get_history(request):
    uid = _get_user_id(request)
    history = db.get_recent_history(uid, 50)
    stats = db.get_waste_stats(uid)
    return _json({"history": history, "stats": stats})


async def delete_user_account(request):
    uid = _require_auth(request)
    if not uid:
        return _auth_required_response()
    db.delete_all_user_data(uid)
    response = JSONResponse({"status": "ok", "message": "All user data deleted."})
    response.delete_cookie("cook_ai_user")
    return response


# --------------------------------------------------------------------------
# Endpoints: Developer & User Feedback
# --------------------------------------------------------------------------
async def submit_feedback(request):
    uid = _get_user_id(request)
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return _json({"error": "Message is required"}, 400)
    if len(message) > 2000:
        return _json({"error": "Message is too long (max 2000 characters)"}, 400)

    email = body.get("email", "").strip()[:100]
    feedback_type = body.get("type", "feedback").strip()[:50]
    rating = body.get("rating")
    if rating is not None:
        try:
            rating = max(1, min(5, int(rating)))
        except (ValueError, TypeError):
            rating = None

    fid = db.add_feedback(
        uid, message, email=email, feedback_type=feedback_type, rating=rating
    )
    print("\n" + "=" * 50)
    print("📢 [NEW USER FEEDBACK RECEIVED]")
    print(f"From: {email or uid} | Type: {feedback_type} | Rating: {rating or 'N/A'}")
    print(f"Message: {message}")
    print("=" * 50 + "\n")
    return _json(
        {
            "status": "ok",
            "feedback_id": fid,
            "message": "Thank you! Your feedback has been passed directly to the developer.",
        }
    )


async def list_feedback(request):
    """Admin-only endpoint to view user feedback. Requires ADMIN_API_KEY."""
    admin_key = L.get_secret("ADMIN_API_KEY") or os.getenv("ADMIN_API_KEY")
    auth_header = request.headers.get("Authorization", "")
    api_key = request.headers.get("X-Admin-Key", "")

    is_authorized = False
    if admin_key:
        if auth_header == f"Bearer {admin_key}" or api_key == admin_key:
            is_authorized = True

    if not is_authorized:
        return _json({"error": "Forbidden: Admin credentials required."}, 403)

    feedbacks = db.get_all_feedback(limit=100)
    return _json({"feedback": feedbacks})


async def serve_landing(request):
    return FileResponse("static/index.html")


async def serve_app(request):
    return FileResponse("static/app.html")


# --------------------------------------------------------------------------
# Security Headers Middleware
# --------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response


# --------------------------------------------------------------------------
# Application Routing & Middleware Setup
# --------------------------------------------------------------------------
routes = [
    Route("/", serve_landing, methods=["GET"]),
    Route("/app", serve_app, methods=["GET"]),
    Route("/api/health", health, methods=["GET"]),
    Route("/api/config", get_config, methods=["GET"]),
    Route("/api/user", get_user_profile, methods=["GET"]),
    Route("/api/auth/google", auth_google, methods=["POST"]),
    Route("/api/auth/dev", auth_dev, methods=["POST"]),
    Route("/api/auth/logout", auth_logout, methods=["POST"]),
    Route("/api/kitchen", get_kitchen, methods=["GET"]),
    Route("/api/diet", set_diet, methods=["POST"]),
    Route("/api/pantry", add_pantry_items, methods=["POST"]),
    Route("/api/pantry/{id:int}", update_pantry_item, methods=["PUT"]),
    Route("/api/pantry", delete_pantry_items, methods=["DELETE"]),
    Route("/api/essentials", add_essentials, methods=["POST"]),
    Route("/api/essentials", delete_essentials, methods=["DELETE"]),
    Route("/api/restricted", add_restricted, methods=["POST"]),
    Route("/api/restricted", delete_restricted, methods=["DELETE"]),
    Route("/api/dishes/propose", propose_dish_ingredients, methods=["POST"]),
    Route("/api/dishes", add_dish, methods=["POST"]),
    Route("/api/dishes/{id:int}", update_dish, methods=["PUT"]),
    Route("/api/dishes/{id:int}", delete_dish, methods=["DELETE"]),
    Route("/api/shopping", add_shopping_items, methods=["POST"]),
    Route("/api/shopping/{id:int}/toggle", toggle_shopping_bought, methods=["PUT"]),
    Route("/api/shopping/to-pantry", move_shopping_to_pantry, methods=["POST"]),
    Route("/api/shopping/clear", clear_bought_shopping, methods=["DELETE"]),
    Route("/api/suggest", suggest_recipes, methods=["POST"]),
    Route("/api/scan", scan_groceries, methods=["POST"]),
    Route("/api/history/cooked", record_cooked, methods=["POST"]),
    Route("/api/history", get_history, methods=["GET"]),
    Route("/api/feedback", submit_feedback, methods=["POST"]),
    Route("/api/feedback", list_feedback, methods=["GET"]),
    Route("/api/account/data", delete_user_account, methods=["DELETE"]),
]

os.makedirs("static", exist_ok=True)
routes.append(Mount("/", app=StaticFiles(directory="static", html=True), name="static"))

# Whitelist allowed origins for CORS
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
if not allowed_origins:
    allowed_origins = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        allowed_origins.append(render_url)

middleware = [
    Middleware(SecurityHeadersMiddleware),
    Middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Admin-Key", "X-Dev-Key"],
        allow_credentials=True,
    ),
]

app = Starlette(routes=routes, middleware=middleware)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
