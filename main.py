import os
import logging
import httpx
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, Request, Query, Response, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from supabase import create_client, Client
from urllib.parse import urlencode
from dotenv import load_dotenv

# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="DiveIn Instagram Automation Pipeline")

# Credentials
APP_ID = os.getenv("META_APP_ID")
APP_SECRET = os.getenv("META_APP_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
REDIRECT_URI = os.getenv("REDIRECT_URI")
# Secret for Webhook verification (Set this in Render)
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN") 

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ENDPOINTS
FB_GRAPH_URL = "https://graph.facebook.com/v19.0"
FACEBOOK_AUTH_URL = "https://www.facebook.com/v19.0/dialog/oauth"

# UPDATED SCOPES: Added 'instagram_manage_messages' for Auto-DM capability
SCOPES = "instagram_basic,instagram_manage_insights,pages_show_list,pages_read_engagement,business_management,instagram_manage_messages"

class ConsentResponse(BaseModel):
    consent_link: str
    oauth_state: str
    status: str

class CallbackResponse(BaseModel):
    status: str
    ig_handle: Optional[str] = None

# ------------------------------------------------------------------
# 1. WEBHOOK HANDSHAKE & RECEIVER
# ------------------------------------------------------------------

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """Handshake required by Meta to verify your server."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("✅ Webhook Handshake Successful")
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Forbidden", status_code=403)

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives comment data from Instagram."""
    payload = await request.json()
    # Process in background so we can respond to Meta immediately
    background_tasks.add_task(process_instagram_event, payload)
    return {"status": "ok"}

async def process_instagram_event(payload: dict):
    """Parses comments and sends DMs if keywords match."""
    for entry in payload.get("entry", []):
        creator_id = entry.get("id")
        for change in entry.get("changes", []):
            if change.get("field") == "comments":
                comment = change["value"]
                text = comment.get("text", "").upper()
                comment_id = comment.get("id")

                # Keyword Trigger Logic
                if "LINK" in text:
                    # Look up the creator's token in Supabase
                    res = supabase.table("instagram_consents") \
                        .select("user_access_token") \
                        .eq("instagram_business_id", creator_id) \
                        .single().execute()
                    
                    if res.data:
                        await send_auto_dm(comment_id, res.data["user_access_token"])

async def send_auto_dm(comment_id: str, token: str):
    """Sends the actual Private Reply (DM) to the user."""
    url = f"{FB_GRAPH_URL}/{comment_id}/private_replies"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={
            "message": "Thanks for your comment! Here is the link you requested: https://diveinmedia.in",
            "access_token": token
        })
        logger.info(f"DM Sent for {comment_id} | Status: {response.status_code}")

# ------------------------------------------------------------------
# 2. OAUTH FLOW (GENERATE CONSENT + CALLBACK)
# ------------------------------------------------------------------

@app.post("/generate-consent", response_model=ConsentResponse)
def generate_consent():
    oauth_state = secrets.token_urlsafe(32)
    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
        "state": oauth_state,
    }
    consent_link = f"{FACEBOOK_AUTH_URL}?{urlencode(params)}"

    return {
        "consent_link": consent_link,
        "status": "LINK_GENERATED",
        "oauth_state": oauth_state,
    }

@app.get("/auth/meta/callback", response_model=CallbackResponse)
async def oauth_callback(code: str, state: str):
    record = supabase.table("instagram_consents").select("*").eq("oauth_state", state).single().execute().data

    if not record:
        raise HTTPException(400, "Invalid or expired state")
    if record["status"] == "TOKEN_ISSUED":
        raise HTTPException(400, "Consent already completed")

    async with httpx.AsyncClient() as client:
        # Exchange code for LONG-LIVED USER TOKEN
        token_res = await client.get(
            f"{FB_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            }
        )

        if token_res.status_code != 200:
            raise HTTPException(400, "Token exchange failed")

        token_data = token_res.json()
        access_token = token_data["access_token"]
        issued_at = datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(days=60)

        # Discover Business Account ID
        accounts_res = await client.get(
            f"{FB_GRAPH_URL}/me/accounts",
            params={"access_token": access_token, "fields": "instagram_business_account{id,username}"}
        )
        pages = accounts_res.json().get("data", [])
        ig_id, ig_username = None, None

        for page in pages:
            ig = page.get("instagram_business_account")
            if ig:
                ig_id, ig_username = ig["id"], ig.get("username")
                break

        if not ig_id:
            raise HTTPException(400, "No Instagram Business account linked")

    # Save everything to Supabase
    supabase.table("instagram_consents").update({
        "status": "TOKEN_ISSUED",
        "user_access_token": access_token,
        "token_type": "USER",
        "token_issued_at": issued_at.isoformat(),
        "token_expires_at": expires_at.isoformat(),
        "last_refreshed_at": issued_at.isoformat(),
        "token_valid": True,
        "instagram_business_id": ig_id,
        "profile_username": ig_username,
        "oauth_state": None,
    }).eq("id", record["id"]).execute()

    return CallbackResponse(status="TOKEN_ISSUED", ig_handle=ig_username)

# ------------------------------------------------------------------
# 3. LEGAL PAGES (Privacy & Terms - Required by Meta)
# ------------------------------------------------------------------

@app.get("/privacy-policy", response_class=HTMLResponse)
def privacy_policy():
    return "<h1>Privacy Policy</h1><p>DiveIn Media uses your Instagram data solely for comment automation.</p>"

@app.get("/terms-of-service", response_class=HTMLResponse)
def terms_of_service():
    return "<h1>Terms of Service</h1><p>By connecting your account, you authorize automated replies to your comments.</p>"

@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion_instructions():
    return "<h1>Data Deletion</h1><p>To delete your data, revoke app access in your Facebook settings.</p>"