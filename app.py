import os
import logging
import httpx
from fastapi import FastAPI, Request, Query, Response, BackgroundTasks
from supabase import create_client, Client
from dotenv import load_dotenv

# 1. Configuration & Logging
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="DiveIn Automation Engine v2.0")

# 2. Infrastructure Clients
# Note: Use the same Supabase credentials from your first app
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN") # Set this in Render Env

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ------------------------------------------------------------------
# PHASE 2: WEBHOOK HANDSHAKE (GET)
# ------------------------------------------------------------------
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """Proves to Meta that this server is yours."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("✅ Webhook Handshake Successful")
        # CRITICAL: Must return as plain text, not JSON
        return Response(content=hub_challenge, media_type="text/plain")
    
    logger.warning("❌ Webhook Handshake Failed: Invalid Token")
    return Response(content="Forbidden", status_code=403)

# ------------------------------------------------------------------
# PHASE 3: AUTOMATION RECEIVER (POST)
# ------------------------------------------------------------------
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives comment data and processes in the background."""
    payload = await request.json()
    
    # Use BackgroundTasks so Meta receives 200 OK immediately (preventing timeouts)
    background_tasks.add_task(process_comment_logic, payload)
    return {"status": "accepted"}

async def process_comment_logic(payload: dict):
    """High-speed routing: Webhook -> Supabase -> Meta DM."""
    try:
        for entry in payload.get("entry", []):
            creator_biz_id = entry.get("id")
            
            for change in entry.get("changes", []):
                if change.get("field") == "comments":
                    value = change.get("value", {})
                    comment_text = value.get("text", "").upper()
                    comment_id = value.get("id")

                    # KEYWORD TRIGGER
                    if "LINK" in comment_text:
                        logger.info(f"🚀 Trigger 'LINK' for Creator {creator_biz_id}")
                        
                        # Fetch the creator's token from YOUR EXISTING TABLE
                        res = supabase.table("instagram_consents") \
                            .select("user_access_token") \
                            .eq("instagram_business_id", creator_biz_id) \
                            .single().execute()
                        
                        if res.data and res.data.get("user_access_token"):
                            token = res.data["user_access_token"]
                            await execute_dm_reply(comment_id, token)
                        else:
                            logger.error(f"❌ Token missing for Creator {creator_biz_id}")

    except Exception as e:
        logger.error(f"🔥 Error in Automation Loop: {str(e)}")

async def execute_dm_reply(comment_id: str, token: str):
    """The final API call to send the DM."""
    url = f"https://graph.facebook.com/v21.0/{comment_id}/private_replies"
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={
            "message": "Thanks for your comment! Here is the link: https://diveinmedia.in",
            "access_token": token
        })
        
        if response.status_code == 200:
            logger.info(f"✅ DM Sent for comment {comment_id}")
        else:
            logger.error(f"❌ DM Failed: {response.text}")

@app.get("/")
def health_check():
    return {"status": "Automation Engine is Healthy"}