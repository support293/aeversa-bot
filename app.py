import os
import json
import time
import requests
import threading
import logging
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AE-Ace] %(levelname)s: %(message)s",
    force=True
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL      = "claude-sonnet-4-6"
ANTHROPIC_URL        = "https://api.anthropic.com/v1/messages"

# Twilio credentials — add these to Render environment variables
TWILIO_ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WA_NUMBER     = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# Email configuration — set RESEND_API_KEY in Render environment variables
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SUPPORT_EMAIL  = "support@aeversa.com"
FROM_EMAIL     = "AE Support Bot <onboarding@resend.dev>"

# ── Ampcontrol Configuration ──────────────────────────────────────────────────
# Multi-organization support: Aeversa has separate Ampcontrol Service
# Accounts per organization (there is no single account spanning all of
# them). Configure each org as AMPCONTROL_ORG_<n>_NAME / _CLIENT_ID /
# _CLIENT_SECRET in Render, numbered from 1 with no gaps. The bot searches
# every configured org in turn when identifying a charger.
AMPCONTROL_BASE       = "https://api.ampcontrol.io/v2"
AMPCONTROL_NETWORK_ID = os.environ.get("AMPCONTROL_NETWORK_ID", "")  # legacy single-org fallback only

def _load_ampcontrol_orgs() -> list[dict]:
    orgs = []
    i = 1
    while True:
        client_id     = os.environ.get(f"AMPCONTROL_ORG_{i}_CLIENT_ID")
        client_secret = os.environ.get(f"AMPCONTROL_ORG_{i}_CLIENT_SECRET")
        if not (client_id and client_secret):
            break
        name = os.environ.get(f"AMPCONTROL_ORG_{i}_NAME", f"Org {i}")
        orgs.append({"index": i, "name": name, "client_id": client_id, "client_secret": client_secret})
        i += 1

    if orgs:
        return orgs

    # Backward compatibility: no numbered orgs configured yet — fall back
    # to the original single-account env vars as "org 1" so the bot keeps
    # working for the Aeversa org while additional orgs are being set up.
    legacy_client_id     = os.environ.get("AMPCONTROL_CLIENT_ID", os.environ.get("AMPCONTROL_EMAIL", ""))
    legacy_client_secret = os.environ.get("AMPCONTROL_CLIENT_SECRET", os.environ.get("AMPCONTROL_PASSWORD", ""))
    if legacy_client_id and legacy_client_secret:
        orgs.append({"index": 1, "name": "Aeversa", "client_id": legacy_client_id, "client_secret": legacy_client_secret})
    return orgs

AMPCONTROL_ORGS = _load_ampcontrol_orgs()

def get_org_by_index(org_index) -> dict | None:
    """Looks up a configured org by its index, as stored in conversation state."""
    if org_index is None:
        return None
    for org in AMPCONTROL_ORGS:
        if org["index"] == org_index:
            return org
    return None

# Manual token override — only ever applies to org 1, for quick manual
# testing. Not meaningful across multiple orgs, so kept as a narrow
# legacy convenience rather than extended to every org.
AMPCONTROL_TOKEN_STORED = os.environ.get("AMPCONTROL_TOKEN", "")

# Token cache — one cached token per organization, keyed by org index
_ampcontrol_tokens: dict[int, dict] = {}   # {org_index: {"token": str, "expiry": float}}
_ampcontrol_lock = threading.Lock()

# ── Agent Configuration ────────────────────────────────────────────────────────
AGENT_NUMBERS = {
    "whatsapp:+27728472288": "Given",
    "whatsapp:+27670085445": "Thapelo",
    "whatsapp:+46704588801": "Mike",
}
PAUSE_DURATION_HOURS = 4

# ── Paused Customers ───────────────────────────────────────────────────────────
# {customer_whatsapp_number: pause_expiry_unix_timestamp}
paused_customers: dict[str, float] = {}

# ── Session Timeout Settings ──────────────────────────────────────────────────
ESCALATION_TIMEOUT_SECS = 600    # 10 minutes — notify agent if no response
SESSION_RESET_SECS      = 7200   # 2 hours   — reset session completely

# Steps where a customer is actively waiting for YES/NO — these trigger escalation
MID_FLOW_STEPS = {
    "opt1_key_removed", "opt1_replug_fixed", "opt1_removed_key_try",
    "opt1_error_check", "opt1_try_another_charger", "opt1_other_charger_working",
    "opt1_confirm_unplugged",
    "opt2_power_on_site", "opt2_another_charger", "opt2_other_charger_works",
    "opt3_restart_session", "opt3_still_slow", "opt3_wattspot_wifi",
    "opt3_wattspot_replug", "opt3_other_4g", "opt3_other_final_restart",
    "await_restart_result",
    "emergency_stop_check", "emergency_stop_replug_result",
    "issue_menu", "manual_issue_menu",
    "pre_escalate_site", "pre_escalate_charger_id",
    "something_else", "something_else_followup",
    "something_else_after_restart_result",
    "confirm_restart",
}

# Steps where the customer is specifically answering a YES/NO question
# (used to phrase the "let's continue where we left off" message correctly)
YES_NO_STEPS = [
    "opt1_key_removed", "opt1_replug_fixed", "opt1_removed_key_try",
    "opt1_error_check", "opt1_try_another_charger", "opt1_other_charger_working",
    "opt2_power_on_site", "opt2_another_charger", "opt2_other_charger_works",
    "opt3_restart_session", "opt3_still_slow", "opt3_wattspot_after_wait",
    "opt3_wattspot_replug", "opt3_other_final_restart",
    "await_restart_result",
    "emergency_stop_check", "emergency_stop_replug_result",
]

# ── Media Library ─────────────────────────────────────────────────────────────
# Media is served directly from the bot's own server (Render)
# This is more reliable than GitHub raw URLs for Twilio media messages
RENDER_URL     = os.environ.get("RENDER_URL", "https://aeversa-bot.onrender.com")
MEDIA_BASE     = f"{RENDER_URL}/media"

MEDIA = {
    # ── Images ────────────────────────────────────────────────────────────────
    "cable_plugin":         f"{MEDIA_BASE}/cable-plugin.mp4.mp4",
    "charger_id_northgate": f"{MEDIA_BASE}/charger-id-northgate.jpg.jpeg",
    "charger_id_other":     f"{MEDIA_BASE}/charger-id-other.jpg.jpeg",
    "stop_session":         f"{MEDIA_BASE}/stop-session.jpg.jpeg",
    "wifi_symbol_wattspot": f"{MEDIA_BASE}/wifi-symbol-wattspot.jpg.jpeg",
    "4g_symbol_other":      f"{MEDIA_BASE}/4g-symbol-other.jpeg",

    # ── Videos ────────────────────────────────────────────────────────────────
    "video_how_to_start":   f"{MEDIA_BASE}/how-to-start-session.mp4",
    "video_how_to_stop":    f"{MEDIA_BASE}/how-to-stop-session.mp4.mp4",
    "video_emergency_stop": f"{MEDIA_BASE}/emergency-stop-release.mp4.mp4",
}

# Set to False until media files are uploaded to GitHub
MEDIA_ENABLED = os.environ.get("MEDIA_ENABLED", "false").lower() == "true"


def get_media(key: str) -> str | None:
    """Returns the media URL for a given key, or None if media is disabled."""
    if not MEDIA_ENABLED:
        return None
    return MEDIA.get(key)

# ── Load Knowledge Base ────────────────────────────────────────────────────────
KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base.json")

def load_knowledge_base():
    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load knowledge base: {e}")
        return {"company_info": {}, "faqs": []}

KNOWLEDGE_BASE = load_knowledge_base()


def _send_email_worker(customer_number: str, fault_type: str,
                       site: str, charger_id: str,
                       error_code: str, extra_notes: str):
    """Runs in a background thread — sends escalation email via Resend HTTP API."""
    try:
        log.info(f"Sending escalation email | {customer_number} | {fault_type} | {site}")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rows = [("📱 Customer WhatsApp", customer_number),
                ("🕐 Time", timestamp),
                ("⚠️  Fault Type", fault_type or "Not specified")]
        if site:        rows.append(("📍 Site", site))
        if charger_id:
            charger_name_display = state.get("charger_name", "") if state else ""
            display = charger_name_display if charger_name_display and charger_name_display != "Unknown" else charger_id
            rows.append(("🔌 Charger", display))
        if error_code:  rows.append(("🔴 Error Code", error_code))
        if extra_notes: rows.append(("📋 Notes", extra_notes))

        html_rows = "".join(
            f"<tr>"
            f"<td style='padding:10px 12px;background:#f8f8f8;font-weight:bold;"
            f"width:170px;border-bottom:1px solid #eee'>{label}</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #eee'>{value}</td>"
            f"</tr>"
            for label, value in rows
        )

        html_body = f"""
        <html><body style='font-family:Arial,sans-serif;color:#333;margin:0;padding:0'>
          <div style='max-width:600px;margin:32px auto;border:1px solid #ddd;
                      border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>
            <div style='background:#0f172a;padding:20px 28px'>
              <h2 style='color:#fff;margin:0;font-size:20px'>⚡ AE Support Escalation</h2>
              <p style='color:#94a3b8;margin:6px 0 0;font-size:13px'>
                Aeversa (PTY) Ltd — Automated Support Alert</p>
            </div>
            <div style='padding:28px'>
              <p style='margin-top:0;font-size:15px'>
                A customer has been escalated and requires agent assistance.</p>
              <table style='width:100%;border-collapse:collapse;font-size:14px;
                            border:1px solid #eee;border-radius:6px;overflow:hidden'>
                {html_rows}
              </table>
              <div style='margin-top:24px;padding:16px;background:#fefce8;
                          border-left:4px solid #eab308;border-radius:4px;font-size:14px'>
                <strong>⚡ Action Required:</strong> Please respond to the customer on WhatsApp
                at <strong>{customer_number}</strong> as soon as possible.
              </div>
            </div>
            <div style='background:#f9fafb;padding:14px 28px;
                        font-size:12px;color:#9ca3af;border-top:1px solid #eee'>
              Automatically generated by AE-Ace — Aeversa WhatsApp Support Bot
            </div>
          </div>
        </body></html>
        """

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": FROM_EMAIL,
                "to": [SUPPORT_EMAIL],
                "subject": f"🔴 AE-Ace Escalation — {fault_type or 'Fault'} | {site or 'Unknown site'} | {timestamp}",
                "html": html_body
            },
            timeout=15
        )

        if response.status_code in (200, 201):
            log.info(f"✅ Escalation email sent to {SUPPORT_EMAIL}")
        else:
            log.error(f"❌ Resend API error {response.status_code}: {response.text}")

    except Exception as e:
        log.error(f"❌ Failed to send escalation email: {e}")


def send_escalation_email(customer_number: str, fault_type: str,
                          site: str = None, charger_id: str = None,
                          error_code: str = None, extra_notes: str = None):
    """Fires the email in a background thread so it never blocks the webhook."""
    if not RESEND_API_KEY:
        log.warning("Email not configured — RESEND_API_KEY missing from environment")
        return
    thread = threading.Thread(
        target=_send_email_worker,
        args=(customer_number, fault_type, site, charger_id, error_code, extra_notes),
        daemon=True
    )
    thread.start()
    log.info(f"📧 Email thread started for {customer_number}")


# ── Agent Notification ────────────────────────────────────────────────────────

def send_whatsapp_message(to: str, message: str):
    """Sends a proactive WhatsApp message to a customer via Twilio REST API."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        log.warning("Twilio credentials not set — cannot send proactive message")
        return
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WA_NUMBER,
            to=to,
            body=message
        )
        log.info(f"✅ Proactive message sent to {to}")
    except Exception as e:
        log.error(f"❌ Failed to send proactive message to {to}: {e}")


def check_session_timeouts():
    """
    Checks all active sessions for timeouts:
    - 10 minutes mid-flow → notify agents + send customer reminder
    - 2 hours any session → reset session + send fresh greeting to customer
    """
    now = datetime.now().timestamp()
    for user_id, state in list(user_states.items()):

        if user_id in AGENT_NUMBERS:
            continue
        if is_paused(user_id):
            continue

        step          = state.get("step", "start")
        last_activity = state.get("last_activity", 0)

        if last_activity == 0:
            continue

        elapsed = now - last_activity

        # ── 2 Hour Session Reset ──────────────────────────────────────────────
        if elapsed > SESSION_RESET_SECS:
            log.info(f"🔄 Session reset for {user_id} — inactive {elapsed/3600:.1f}h")
            user_states[user_id] = {"step": "start", "last_activity": 0}
            # Send fresh greeting so customer knows to start again
            if step in MID_FLOW_STEPS:
                send_whatsapp_message(
                    user_id,
                    "👋 Your previous support session has ended after 2 hours of inactivity.\n\n"
                    "If you still need help, please send a photo of your Charger ID "
                    "sticker and I'll assist you right away! ⚡"
                )
            continue

        # ── 10 Minute Mid-Flow Escalation ────────────────────────────────────
        if (elapsed > ESCALATION_TIMEOUT_SECS
                and step in MID_FLOW_STEPS
                and not state.get("timeout_escalated", False)):

            log.info(f"⏰ Timeout escalation for {user_id} — {elapsed/60:.0f}min in '{step}'")

            user_states[user_id] = {**state, "timeout_escalated": True}

            # Notify agents
            notify_agents(user_id, {
                **state,
                "fault_type":  state.get("fault_type", "Unknown"),
                "extra_notes": f"⏰ No response for {elapsed/60:.0f} minutes — step: {step}"
            })

            # Send reminder to customer
            send_whatsapp_message(
                user_id,
                "⏰ Hi! Just checking in — it looks like you may still need help. 😊\n\n"
                "If your issue is *resolved*, no action needed! ✅\n\n"
                "If you still need assistance, please reply *YES* or *NO*, "
                "or type *MENU* to start a new request."
            )
            log.info(f"✅ Timeout notifications sent for {user_id}")


def start_timeout_checker():
    """Starts a background thread that checks session timeouts every 60 seconds."""
    def _run():
        log.info("⏱️ Session timeout checker started")
        while True:
            time.sleep(60)
            try:
                check_session_timeouts()
            except Exception as e:
                log.error(f"Timeout checker error: {e}")
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def notify_agents(customer_number: str, state: dict):
    """Sends WhatsApp notification to all agents when an escalation happens."""
    def _notify():
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            log.warning("Twilio credentials not set — agent WhatsApp notification skipped")
            return
        fault_type = state.get("fault_type", "Not specified")
        site       = state.get("site", "Not provided")
        charger_id   = state.get("charger_id") or state.get("charger_uuid") or "Not provided"
        charger_name = state.get("charger_name", "")
        charger_display = charger_name if charger_name and charger_name != "Unknown" else charger_id
        error_code = state.get("error_code", "")
        extra_notes = state.get("extra_notes", "")
        clean_num  = customer_number.replace("whatsapp:", "")
        error_line = f"🔴 *Error Code:* {error_code}\n" if error_code else ""
        notes_line = f"📋 *Notes:* {extra_notes}\n" if extra_notes else ""
        message = (
            f"🔴 *AE-Ace Escalation*\n\n"
            f"📱 *Customer:* {clean_num}\n"
            f"⚠️ *Fault:* {fault_type}\n"
            f"📍 *Site:* {site}\n"
            f"🔌 *Charger:* {charger_display}\n"
            f"{error_line}"
            f"{notes_line}\n"
            f"*To take over from AE-Ace:*\n"
            f"Reply: *PAUSE {clean_num}*\n"
            f"Then contact the customer directly on WhatsApp."
        )
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        for agent_wa, agent_name in AGENT_NUMBERS.items():
            try:
                client.messages.create(
                    from_=TWILIO_WA_NUMBER,
                    to=agent_wa,
                    body=message
                )
                log.info(f"✅ Agent notification sent to {agent_name}")
            except Exception as e:
                log.error(f"❌ Failed to notify {agent_name}: {e}")

    threading.Thread(target=_notify, daemon=True).start()


# ── Pause / Resume Helpers ────────────────────────────────────────────────────

def is_paused(customer_number: str) -> bool:
    """Returns True if the bot is currently paused for this customer."""
    expiry = paused_customers.get(customer_number)
    if expiry is None:
        return False
    if datetime.now().timestamp() > expiry:
        del paused_customers[customer_number]
        return False
    return True


def handle_agent_command(sender: str, command: str) -> str:
    """Processes commands sent by agents to control AE-Ace."""
    agent_name = AGENT_NUMBERS.get(sender, "Agent")
    cmd        = command.strip()
    cmd_upper  = cmd.upper()

    # ── PAUSE ──────────────────────────────────────────────────────────────────
    if cmd_upper.startswith("PAUSE"):
        parts = cmd.split()
        if len(parts) < 2:
            return "Usage: *PAUSE +27XXXXXXXXX*\nExample: PAUSE +27760260625"
        number = parts[1].strip()
        if not number.startswith("+"):
            number = f"+{number}"
        wa_number = f"whatsapp:{number}"
        expiry = datetime.now().timestamp() + (PAUSE_DURATION_HOURS * 3600)
        paused_customers[wa_number] = expiry
        resume_time = datetime.fromtimestamp(expiry).strftime("%H:%M")
        log.info(f"⏸️ Bot paused for {number} by {agent_name} until {resume_time}")
        return (
            f"⏸️ *AE-Ace paused for {number}*\n\n"
            f"The bot will not respond to this customer for {PAUSE_DURATION_HOURS} hours "
            f"(until {resume_time}).\n\n"
            f"You can now assist them directly on WhatsApp.\n"
            f"To resume early, reply: *RESUME {number}*"
        )

    # ── RESUME ─────────────────────────────────────────────────────────────────
    if cmd_upper.startswith("RESUME"):
        parts = cmd.split()
        if len(parts) < 2:
            return "Usage: *RESUME +27XXXXXXXXX*\nExample: RESUME +27760260625"
        number = parts[1].strip()
        if not number.startswith("+"):
            number = f"+{number}"
        wa_number = f"whatsapp:{number}"
        if wa_number in paused_customers:
            del paused_customers[wa_number]
            log.info(f"▶️ Bot resumed for {number} by {agent_name}")
            return (
                f"▶️ *AE-Ace resumed for {number}*\n\n"
                f"The bot will now respond to this customer's messages again."
            )
        return f"ℹ️ {number} is not currently paused."

    # ── STATUS ─────────────────────────────────────────────────────────────────
    if cmd_upper == "STATUS":
        # Clean expired pauses first
        now = datetime.now().timestamp()
        expired = [k for k, v in paused_customers.items() if now > v]
        for k in expired:
            del paused_customers[k]
        if not paused_customers:
            return "✅ *No customers currently paused.*\nAE-Ace is handling all conversations."
        lines = ["📋 *Currently paused customers:*\n"]
        for wa_num, expiry in paused_customers.items():
            remaining = int((expiry - now) / 60)
            clean = wa_num.replace("whatsapp:", "")
            lines.append(f"• *{clean}* — {remaining} mins remaining")
        return "\n".join(lines)

    # ── ACTIVE ─────────────────────────────────────────────────────────────────
    if cmd_upper == "ACTIVE":
        active = [
            (uid, s) for uid, s in user_states.items()
            if uid not in AGENT_NUMBERS and s.get("step", "start") != "start"
        ]
        if not active:
            return "ℹ️ *No customers currently in active support flows.*"
        lines = ["📊 *Active customer flows:*\n"]
        for uid, s in active:
            clean = uid.replace("whatsapp:", "")
            fault = s.get("fault_type", "Unknown")
            step  = s.get("step", "unknown")
            paused = " ⏸️ PAUSED" if is_paused(uid) else ""
            lines.append(f"• *{clean}*{paused}\n  Fault: {fault} | Step: {step}")
        return "\n".join(lines)

    # ── HELP / UNKNOWN ─────────────────────────────────────────────────────────
    return (
        f"🤖 *AE-Ace Agent Commands*\n\n"
        f"*PAUSE +27XXXXXXXXX*\n"
        f"Stop bot for a customer for {PAUSE_DURATION_HOURS} hours\n\n"
        f"*RESUME +27XXXXXXXXX*\n"
        f"Resume bot for a customer early\n\n"
        f"*STATUS*\n"
        f"See all paused customers\n\n"
        f"*ACTIVE*\n"
        f"See all customers in active flows"
    )


# ── Ampcontrol API ────────────────────────────────────────────────────────────

def get_ampcontrol_token(org: dict) -> str:
    """
    Returns a valid Ampcontrol bearer token for the given org, using
    Service Account authentication.
    Endpoint: POST /v2/service_accounts/token/
    Payload:  {"clientId": "...", "secret": "..."}
    Expiry:   1080 seconds (18 minutes) — auto-refreshed
    Cached per organization, since each org has its own credentials.
    """
    org_index = org["index"]
    with _ampcontrol_lock:
        # Priority 1: Pre-stored manual token — only ever applies to org 1
        if org_index == 1 and AMPCONTROL_TOKEN_STORED:
            log.info("Using pre-stored AMPCONTROL_TOKEN (org 1 only)")
            return AMPCONTROL_TOKEN_STORED

        # Priority 2: Cached token still valid (with 60 second buffer)
        cached = _ampcontrol_tokens.get(org_index)
        if cached and datetime.now().timestamp() < cached["expiry"] - 60:
            return cached["token"]

        client_id     = org.get("client_id", "")
        client_secret = org.get("client_secret", "")
        if not client_id or not client_secret:
            log.warning(f"Ampcontrol credentials missing for org '{org.get('name')}' (index {org_index})")
            return ""

        log.info(f"Refreshing Ampcontrol service account token for org '{org.get('name')}' ({client_id})")

        try:
            response = requests.post(
                "https://api.ampcontrol.io/v2/service_accounts/token/",
                json={
                    "clientId": client_id,
                    "secret":   client_secret
                },
                timeout=10
            )
            log.info(f"Ampcontrol token request for org '{org.get('name')}' → {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                token      = data["data"][0]["token"]
                expires_in = data["data"][0].get("expires_in", 1080)  # default 18 min
                _ampcontrol_tokens[org_index] = {
                    "token": token,
                    "expiry": datetime.now().timestamp() + expires_in
                }
                log.info(f"✅ Ampcontrol token obtained for org '{org.get('name')}' — expires in {expires_in}s")
                return token
            else:
                log.error(f"❌ Ampcontrol token request failed for org '{org.get('name')}': {response.status_code} — {response.text[:200]}")
                return ""

        except Exception as e:
            log.error(f"❌ Ampcontrol token exception for org '{org.get('name')}': {e}")
            return ""


def ampcontrol_get(endpoint: str, org: dict) -> dict | None:
    """Makes an authenticated GET request to Ampcontrol API for a specific org."""
    token = get_ampcontrol_token(org)
    if not token:
        return None
    try:
        response = requests.get(
            f"{AMPCONTROL_BASE}{endpoint}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        # Log the response body too — the status code alone ("422 Client
        # Error") doesn't say WHAT was invalid, but Ampcontrol's error
        # responses typically include that detail in the body.
        body = ""
        try:
            body = response.text[:500]
        except Exception:
            pass
        log.error(f"❌ Ampcontrol GET {endpoint} failed: {e} | Response body: {body}")
        return None
    except Exception as e:
        log.error(f"❌ Ampcontrol GET {endpoint} failed: {e}")
        return None


def ampcontrol_post(endpoint: str, payload: dict, org: dict) -> dict | None:
    """Makes an authenticated POST request to Ampcontrol API for a specific org."""
    token = get_ampcontrol_token(org)
    if not token:
        return None
    try:
        response = requests.post(
            f"{AMPCONTROL_BASE}{endpoint}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log.error(f"❌ Ampcontrol POST {endpoint} failed: {e}")
        return None


def get_charger_status(charger_uuid: str, org: dict) -> dict:
    """
    Fetches charger details from Ampcontrol, within a specific org.
    Endpoint: GET /v2/charge_points/{uuid}/
    Returns online=None (with name='Unknown') if the charger isn't found
    in this org, or if this org's API call failed — either way, the
    caller (find_charger_status_across_orgs) treats that as "try the
    next org" rather than distinguishing the two cases.
    """
    def parse_charger(charger: dict) -> dict:
        online_status = charger.get("onlineStatus", "").upper()
        ocpp_status   = charger.get("ocppStatus", "")
        name          = (charger.get("customName") or
                         charger.get("name") or
                         charger.get("ocppId") or
                         f"Charger ...{charger_uuid[-6:]}")
        is_online = online_status == "ONLINE"
        network_id   = charger.get("networkId", "")
        network_name = charger.get("networkName", "")
        log.info(f"Charger '{name}' (org '{org.get('name')}') → onlineStatus={online_status} ocppStatus={ocpp_status} networkId={network_id} networkName={network_name}")
        return {
            "online": is_online,
            "name":   name,
            "status": online_status,
            "ocpp":   ocpp_status,
            "network_id":   network_id,
            "network_name": network_name,
            "org_index":    org["index"],
            "org_name":     org.get("name", ""),
            "raw":    charger
        }

    # Use list search directly (direct UUID lookup returns 401 for service accounts)
    list_data = ampcontrol_get(f"/charge_points/?search={charger_uuid}", org)
    if list_data and list_data.get("data"):
        for charger in list_data["data"]:
            if charger.get("id", "").lower() == charger_uuid.lower():
                return parse_charger(charger)
        # Return first result if exact match not found
        if list_data["data"]:
            return parse_charger(list_data["data"][0])

    # Fallback: list all and search
    all_data = ampcontrol_get("/charge_points/", org)
    if all_data:
        count = all_data.get("total", 0)
        log.info(f"Org '{org.get('name')}' service account sees {count} chargers total")
        for charger in all_data.get("data", []):
            if charger.get("id", "").lower() == charger_uuid.lower():
                return parse_charger(charger)

    log.info(f"Charger {charger_uuid} not found in org '{org.get('name')}'")
    return {"online": None, "name": "Unknown", "status": "unknown", "raw": {}}


def find_charger_status_across_orgs(charger_uuid: str) -> dict:
    """
    Tries get_charger_status() against every configured org in turn,
    returning as soon as one of them has a definitive answer (online
    True or False). If no org has heard of this charger, or all of
    their API calls failed, returns the same 'unknown' shape as
    get_charger_status() did previously, so existing callers/fallback
    logic (manual troubleshooting flow) keep working unchanged.
    """
    if not AMPCONTROL_ORGS:
        log.warning("No Ampcontrol organizations configured")
        return {"online": None, "name": "Unknown", "status": "unknown", "raw": {}}

    for org in AMPCONTROL_ORGS:
        status = get_charger_status(charger_uuid, org)
        if status.get("online") is not None:
            return status

    log.warning(f"Charger {charger_uuid} not found in any of {len(AMPCONTROL_ORGS)} configured org(s)")
    return {"online": None, "name": "Unknown", "status": "unknown", "raw": {}}


# Alert names that are cosmetic/non-blocking — confirmed by the customer
# these don't stop the charger from operating and don't show up as real
# faults on the Ampcontrol dashboard's "Unresolved Alerts" widget. Excluded
# entirely from get_charger_alerts() so they never trigger an escalation.
IGNORABLE_ALERT_NAMES = {
    "METER_VALUE_LIMIT_VIOLATION",
    "VENDOR_ERROR_CODE",
}


def get_charger_alerts(charger_uuid: str, network_id: str, org: dict) -> list:
    """
    Fetches unresolved, non-ignorable alerts for a specific charger from
    Ampcontrol, within a specific org.
    Endpoint: GET /v2/alerts/?network={uuid}&charger={uuid}
    'network' is a REQUIRED parameter on this endpoint (confirmed against
    the API docs) — without it Ampcontrol returns 422 Unprocessable
    Content. 'charger' further filters to just this charger.

    Aeversa spans multiple organizations, each with their own networks,
    so both org (for credentials) and network_id (from the charger's own
    data) must be correct together — a charger's network_id is only
    meaningful within its own org.

    IMPORTANT: the 'active' field does NOT indicate whether an alert is
    still unresolved — confirmed against real data that a resolved alert
    can still have active=true (and 'end' can be null even when resolved,
    so that's not reliable either). The correct field is 'status'. We
    exclude the confirmed value "Resolved" rather than whitelisting a
    specific "unresolved" value, since the full set of non-resolved status
    strings (Active/Open/New/etc.) isn't confirmed against real data.

    Also excludes alert names in IGNORABLE_ALERT_NAMES — cosmetic alert
    types that don't actually affect charger operation.
    """
    if not org:
        log.warning(f"No org available for charger {charger_uuid} — skipping alert check")
        return []
    resolved_network_id = network_id or AMPCONTROL_NETWORK_ID
    if not resolved_network_id:
        log.warning(f"No network_id available for charger {charger_uuid} — skipping alert check")
        return []
    data = ampcontrol_get(f"/alerts/?network={resolved_network_id}&charger={charger_uuid}", org)
    if not data or not data.get("data"):
        return []
    unresolved_alerts = [
        a for a in data["data"]
        if a.get("status") != "Resolved" and a.get("name") not in IGNORABLE_ALERT_NAMES
    ]
    log.info(f"Charger {charger_uuid} (org '{org.get('name')}', network {resolved_network_id}) → {len(unresolved_alerts)} unresolved alert(s) found")
    return unresolved_alerts


def get_connector_numbers(charger_uuid: str, network_id: str, org: dict) -> dict:
    """
    Fetches the friendly connector numbers (1, 2, ...) for every
    connector on a charger, keyed by connector UUID.
    Endpoint: GET /v2/connectors/?network={uuid}&chargepoint={uuid}
    Confirmed via docs: the response's 'connectorId' field is the
    integer "Connector 1+" label matching the Ampcontrol dashboard
    directly (1-indexed, no offset needed) — separate from the
    connector's own 'id' (its UUID).
    Returns {connector_uuid: connector_number}. Returns an empty dict on
    any failure, so callers can gracefully fall back to showing the raw
    UUID rather than breaking.
    """
    if not org:
        return {}
    resolved_network_id = network_id or AMPCONTROL_NETWORK_ID
    if not resolved_network_id:
        return {}
    data = ampcontrol_get(f"/connectors/?network={resolved_network_id}&chargepoint={charger_uuid}", org)
    if not data or not data.get("data"):
        return {}
    mapping = {}
    for connector in data["data"]:
        connector_uuid = connector.get("id")
        connector_number = connector.get("connectorId")
        if connector_uuid is not None and connector_number is not None:
            mapping[connector_uuid] = connector_number
    return mapping


def get_charger_meter_values(charger_uuid: str, network_id: str, org: dict) -> list | None:
    """
    Fetches the most recent meter reading for EACH connector on a
    charger — specifically Current.Import (Amps) and Power.Active.Import
    (kW) — for attaching as diagnostic context on slow-charging
    escalations.
    Endpoint: GET /v2/meter_values/?network={uuid}&charger={uuid}
    At least one of network/charger/vehicle/evse/connector is required;
    network+charger together satisfies that, same pattern as alerts.

    Deliberately does NOT judge whether any reading counts as "slow" —
    just surfaces the real numbers for a human agent to interpret, since
    what counts as a meaningfully low reading depends on the vehicle and
    charge stage and isn't something to guess at here.

    IMPORTANT: a charger can have multiple connectors charging
    simultaneously (confirmed against real data — a 2-connector DC
    charger both actively charging different vehicles). Each top-level
    record from this endpoint is already scoped to one connectorId, so
    readings are kept separate per connector rather than blended into
    one "latest overall" value, which could otherwise silently reflect
    the wrong connector's numbers. Within each connector, tracks the
    most recent value for each measurand independently, since some
    meterValues submissions report only a subset of measurands (e.g. a
    SoC-only update) and requiring an exact shared timestamp would miss
    real readings that arrived slightly earlier.

    Returns a list of dicts — one per connector with any data —
    [{"connector_id": int|str, "current_a": float|None, "power_kw": float|None,
      "current_timestamp": str|None, "power_timestamp": str|None}, ...],
    or None if no meter data is available at all. connector_id is the
    friendly dashboard number (e.g. 1, 2) via get_connector_numbers()
    where available, falling back to the raw connector UUID otherwise.
    """
    if not org:
        log.warning(f"No org available for charger {charger_uuid} — skipping meter value check")
        return None
    resolved_network_id = network_id or AMPCONTROL_NETWORK_ID
    if not resolved_network_id:
        log.warning(f"No network_id available for charger {charger_uuid} — skipping meter value check")
        return None

    data = ampcontrol_get(f"/meter_values/?network={resolved_network_id}&charger={charger_uuid}", org)
    if not data or not data.get("data"):
        return None

    by_connector = {}  # connector_id -> reading dict

    for record in data["data"]:
        connector_id = record.get("connectorId") or "unknown"
        entry = by_connector.setdefault(connector_id, {
            "connector_id": connector_id,
            "current_a": None, "power_kw": None,
            "current_timestamp": None, "power_timestamp": None,
        })
        for mv in record.get("meterValues", []):
            ts = mv.get("timestamp")
            if not ts:
                continue
            for sample in mv.get("sampledValue", []):
                measurand = sample.get("measurand", "")
                value = sample.get("value")
                if value is None:
                    continue
                if measurand == "Current.Import" and (entry["current_timestamp"] is None or ts > entry["current_timestamp"]):
                    try:
                        entry["current_a"] = float(value)
                        entry["current_timestamp"] = ts
                    except (TypeError, ValueError):
                        pass
                elif measurand == "Power.Active.Import" and (entry["power_timestamp"] is None or ts > entry["power_timestamp"]):
                    try:
                        entry["power_kw"] = float(value)
                        entry["power_timestamp"] = ts
                    except (TypeError, ValueError):
                        pass

    readings = [r for r in by_connector.values() if r["current_a"] is not None or r["power_kw"] is not None]
    if not readings:
        return None

    # Swap raw connector UUIDs for the friendly dashboard number (e.g. "1",
    # "2") where we can — falls back to the raw UUID for any connector
    # not found in the mapping, rather than dropping the reading.
    connector_number_map = get_connector_numbers(charger_uuid, network_id, org)
    for r in readings:
        friendly_number = connector_number_map.get(r["connector_id"])
        if friendly_number is not None:
            r["connector_id"] = friendly_number

    log.info(
        f"Charger {charger_uuid} (org '{org.get('name')}') → meter readings for "
        f"{len(readings)} connector(s): " +
        "; ".join(f"{r['connector_id']}: {r['current_a']}A/{r['power_kw']}kW" for r in readings)
    )
    return readings


def format_meter_values_for_agent(readings: list | None) -> str:
    """Formats per-connector meter readings into a plain-text note for escalation."""
    if not readings:
        return ""
    lines = []
    for r in readings:
        parts = []
        if r.get("current_a") is not None:
            parts.append(f"{r['current_a']}A (as of {r.get('current_timestamp', 'unknown time')})")
        if r.get("power_kw") is not None:
            parts.append(f"{r['power_kw']}kW (as of {r.get('power_timestamp', 'unknown time')})")
        if parts:
            lines.append(f"Connector {r.get('connector_id', 'unknown')}: " + ", ".join(parts))
    if not lines:
        return ""
    label = "Latest meter reading:" if len(lines) == 1 else "Latest meter readings (multiple connectors):"
    return label + "\n" + "\n".join(lines)


def format_alerts_for_agent(alerts: list) -> str:
    """
    Formats Ampcontrol alerts into a plain-text summary for escalation
    notes. Deliberately does NOT try to interpret what any alert means —
    the real wording Ampcontrol uses for name/category/description isn't
    confirmed to be driver-friendly, so this just surfaces the raw fields
    for a human agent to read, rather than guess-matching against
    unconfirmed values or showing it to the customer directly.

    Dedupes identical alerts (Ampcontrol can log the same alert type more
    than once) and sorts by urgency so the agent sees the most severe
    issue first, regardless of the order Ampcontrol returned them in.
    """
    if not alerts:
        return ""

    # Confirmed real urgency values, most severe first
    URGENCY_ORDER = ["Very High", "High", "Medium", "Low", "Very Low"]

    seen = set()
    unique_alerts = []
    for alert in alerts:
        name        = alert.get("name") or "Unnamed alert"
        description = alert.get("description") or ""
        category    = alert.get("category") or []
        urgency     = alert.get("urgency") or ""
        category_str = ", ".join(category) if isinstance(category, list) else str(category)
        key = (name, description, category_str, urgency)
        if key in seen:
            continue
        seen.add(key)
        unique_alerts.append((name, description, category_str, urgency))

    def urgency_rank(entry):
        urgency = entry[3]
        return URGENCY_ORDER.index(urgency) if urgency in URGENCY_ORDER else len(URGENCY_ORDER)

    unique_alerts.sort(key=urgency_rank)

    lines = []
    for name, description, category_str, urgency in unique_alerts[:3]:
        parts = [name]
        if description:
            parts.append(description)
        if category_str:
            parts.append(f"category: {category_str}")
        if urgency:
            parts.append(f"urgency: {urgency}")
        lines.append(" — ".join(parts))
    if len(unique_alerts) > 3:
        lines.append(f"...and {len(unique_alerts) - 3} more active alert(s)")
    return "\n".join(lines)


def is_emergency_stop_alert(alert: dict) -> bool:
    """
    Detects the specific FAULTED alert caused by the physical emergency
    stop button being pressed on the charger — confirmed against real
    data as OCPP error code 258 appearing in the alert's description
    (e.g. "Reported errors: 258, OtherError"). Matches on a word boundary
    so it doesn't false-match a different code like "1258" or "2580".
    This is deliberately narrow — only this one confirmed code triggers
    the emergency-stop guidance; every other alert type still escalates
    normally.
    """
    description = alert.get("description", "") or ""
    return contains_phrase(description, "258")


def is_invalid_id_tag_alert(alert: dict) -> bool:
    """
    Detects the specific alert for a vehicle ID tag that's invalid,
    expired, or not registered to charge — confirmed against real data
    as name == "INVALID_ID_TAGS" (e.g. description: "Id tag for
    <020000000000> is either invalid, expired or blocked.").
    """
    return alert.get("name") == "INVALID_ID_TAGS"


def restart_charger(charger_uuid: str, org: dict) -> bool:
    """
    Sends a remote Soft Reset OCPP command to the charger via Ampcontrol,
    within a specific org.
    Returns True if the command was accepted.
    """
    import uuid as _uuid
    payload = {
        "chargePointId": charger_uuid,
        "body": [2, str(_uuid.uuid4())[:8], "Reset", {"type": "Soft"}],
        "source": "API",
        "sendToCharger": True,
        "operationType": "Reset",
        "protocol": "ocpp1.6"
    }
    result = ampcontrol_post("/ocpp_messages/", payload, org)
    success = result is not None and result.get("status") == "success"
    log.info(f"{'✅' if success else '❌'} Remote restart for {charger_uuid} (org '{org.get('name')}'): {success}")
    return success


def poll_charger_and_notify_online(user_id: str, charger_uuid: str, charger_name: str, org: dict,
                                    next_step: str = "await_restart_result",
                                    question: str = "Is your vehicle charging?",
                                    fault_type: str = "Vehicle not charging",
                                    action_line: str = "Please plug your vehicle back in now.",
                                    initial_delay_secs: int = 30,
                                    timeout_secs: int = 120, interval_secs: int = 10):
    """
    Runs in a background thread after a remote restart (used by the slow-
    charging, vehicle-not-charging, and stuck-cable flows). Waits
    initial_delay_secs before the first check — Ampcontrol can briefly
    still report the pre-restart status immediately after the OCPP reset
    command is sent, so checking too early risks a false positive. After
    that, polls every interval_secs and proactively messages the customer
    with the appropriate next action once it's confirmed back online. If
    it doesn't come back online within timeout_secs total, escalates to
    an agent automatically instead of leaving the customer waiting.
    """
    time.sleep(initial_delay_secs)
    elapsed = initial_delay_secs
    while elapsed < timeout_secs:
        status = get_charger_status(charger_uuid, org)
        if status.get("online") is True:
            log.info(f"✅ Charger {charger_name} back online after {elapsed}s — notifying {user_id}")
            send_whatsapp_message(
                user_id,
                f"✅ Good news — *{charger_name}* is back online! {action_line}\n\n"
                f"{question}\n\nReply *YES* or *NO*"
            )
            current = user_states.get(user_id, {})
            user_states[user_id] = {**current, "step": next_step}
            return
        time.sleep(interval_secs)
        elapsed += interval_secs

    # Timed out — still not back online after timeout_secs
    log.warning(f"⚠️ Charger {charger_name} did not come back online within {timeout_secs}s")
    current = user_states.get(user_id, {})
    timeout_fault = f"{fault_type} — restart timeout"
    send_whatsapp_message(
        user_id,
        f"⏳ This is taking longer than expected to bring *{charger_name}* back online.\n\n"
        "Let me connect you with our support team so they can look into it directly."
    )
    notify_agents(user_id, {
        **current,
        "fault_type": timeout_fault,
        "extra_notes": f"Charger did not come back online within {timeout_secs}s of remote restart"
    })
    send_escalation_email(
        customer_number=user_id.replace("whatsapp:", ""),
        fault_type=timeout_fault,
        site=current.get("site"),
        charger_id=current.get("charger_id") or current.get("charger_uuid"),
    )
    user_states[user_id] = {**current, "step": "start"}


def poll_emergency_stop_cleared(user_id: str, charger_uuid: str, network_id: str,
                                 charger_name: str, org: dict,
                                 timeout_secs: int = 60, interval_secs: int = 10):
    """
    Runs in a background thread after the customer confirms they released
    the emergency stop button. Polls Ampcontrol to confirm the emergency-
    stop alert (OCPP error 258) has actually cleared, rather than trusting
    the customer's self-report alone — mirrors the same real-status-check
    pattern used for the restart flows. If it doesn't clear within
    timeout_secs, escalates to an agent automatically.
    """
    elapsed = 0
    while elapsed < timeout_secs:
        time.sleep(interval_secs)
        elapsed += interval_secs
        alerts = get_charger_alerts(charger_uuid, network_id, org)
        still_present = any(is_emergency_stop_alert(a) for a in alerts)
        if not still_present:
            log.info(f"✅ Emergency stop alert cleared for {charger_uuid} after {elapsed}s — notifying {user_id}")
            send_whatsapp_message(
                user_id,
                "✅ Confirmed — the emergency stop alert has cleared!\n\n"
                "Since it was pressed, you'll need to *unplug your vehicle and "
                "plug it back in* to start a new charging session.\n\n"
                "Is it charging now?\n\nReply *YES* or *NO*"
            )
            current = user_states.get(user_id, {})
            user_states[user_id] = {**current, "step": "emergency_stop_replug_result"}
            return

    # Timed out — alert still showing as unresolved
    log.warning(f"⚠️ Emergency stop alert still present for {charger_uuid} after {timeout_secs}s")
    current = user_states.get(user_id, {})
    timeout_fault = "Emergency stop — alert did not clear after customer release"
    send_whatsapp_message(
        user_id,
        "⏳ I'm still seeing that alert flagged on our system.\n\n"
        "Let me connect you with our support team so they can look into it directly."
    )
    notify_agents(user_id, {
        **current,
        "fault_type": timeout_fault,
        "extra_notes": current.get("extra_notes", "")
    })
    send_escalation_email(
        customer_number=user_id.replace("whatsapp:", ""),
        fault_type=timeout_fault,
        site=current.get("site"),
        charger_id=current.get("charger_id") or current.get("charger_uuid"),
        extra_notes=current.get("extra_notes", ""),
    )
    user_states[user_id] = {**current, "step": "start"}


def extract_uuid_from_text(text: str) -> str | None:
    """
    Extracts a charger UUID from text.
    Only matches Ampcontrol URLs or properly formatted UUIDs.
    Never treats random words as a UUID.
    """
    import re
    # Try Ampcontrol URL pattern
    url_match = re.search(
        r"ampcontrol\.io/#/charger/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        text, re.IGNORECASE
    )
    if url_match:
        return url_match.group(1)
    # Try bare UUID pattern (strict format only)
    uuid_match = re.search(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
        text, re.IGNORECASE
    )
    return uuid_match.group(0) if uuid_match else None


def search_charger_by_name(name: str, org: dict) -> dict | None:
    """
    Searches Ampcontrol for a charger by name/custom name/ocppId, within
    a specific org. Ampcontrol's search endpoint matches broadly across
    account/site metadata, not just the charger's own name — so a
    generic term (e.g. the company name) can return unrelated chargers.
    We only accept a result if the search term genuinely appears in THAT
    charger's own customName, name, or ocppId; otherwise we treat it as
    not found rather than guessing.
    """
    log.info(f"Searching org '{org.get('name')}' for charger by name: {name}")
    data = ampcontrol_get(f"/charge_points/?search={name}", org)
    if not data or not data.get("data"):
        return None

    needle = name.strip().lower()
    if not needle:
        return None

    for charger in data["data"]:
        candidates = [
            charger.get("customName", ""),
            charger.get("name", ""),
            charger.get("ocppId", ""),
        ]
        if any(needle in c.lower() for c in candidates if c):
            charger["_matched_org_index"] = org["index"]
            charger["_matched_org_name"] = org.get("name", "")
            return charger

    log.warning(
        f"Ampcontrol search for '{name}' in org '{org.get('name')}' returned "
        f"{len(data['data'])} result(s) but none matched by charger name/ID — treating as not found"
    )
    return None


def search_charger_by_name_across_orgs(name: str) -> dict | None:
    """
    Tries search_charger_by_name() against every configured org in turn,
    returning as soon as one of them finds a genuine match. The returned
    charger dict carries _matched_org_index/_matched_org_name so the
    caller knows which org's credentials to keep using for this charger.
    """
    if not AMPCONTROL_ORGS:
        log.warning("No Ampcontrol organizations configured")
        return None
    for org in AMPCONTROL_ORGS:
        charger = search_charger_by_name(name, org)
        if charger:
            return charger
    return None


def read_text_from_image(image_url: str) -> str | None:
    """
    Uses Claude vision to read charger name/ID text from a sticker or label photo.
    Falls back gracefully if Claude is unavailable.
    """
    if not ANTHROPIC_API_KEY or not TWILIO_ACCOUNT_SID:
        return None
    try:
        import base64
        # Download image from Twilio (requires auth)
        img_response = requests.get(
            image_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=10
        )
        if img_response.status_code != 200:
            log.warning(f"Failed to download image for OCR: {img_response.status_code}")
            return None

        img_base64    = base64.b64encode(img_response.content).decode("utf-8")
        content_type  = img_response.headers.get("content-type", "image/jpeg")

        response = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 60,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content_type,
                                "data": img_base64
                            }
                        },
                        {
                            "type": "text",
                            "text": (
                                "This is a photo of an EV charger or its sticker/label. "
                                "What is the charger name, ID, or serial number shown on it? "
                                "Reply with ONLY the charger name or ID text you can see — "
                                "nothing else, no explanation. "
                                "If you cannot read any identifying text, reply with exactly: NONE"
                            )
                        }
                    ]
                }]
            },
            timeout=15
        )

        if response.status_code == 200:
            text = "".join(
                b.get("text", "") for b in response.json().get("content", [])
            ).strip()
            if text and text.upper() != "NONE":
                log.info(f"✅ Claude vision read charger text: '{text}'")
                return text
        return None

    except Exception as e:
        log.error(f"Image OCR error: {e}")
        return None


def read_qr_code(image_url: str) -> str | None:
    """
    Reads a QR code from an image URL using the free QR Server API.
    Returns the decoded text or None if it fails.
    """
    try:
        # Twilio media URLs require authentication to access
        # We pass the URL directly to the QR reading API
        account_sid  = TWILIO_ACCOUNT_SID
        auth_token   = TWILIO_AUTH_TOKEN

        # Download the image from Twilio first (requires auth)
        img_response = requests.get(
            image_url,
            auth=(account_sid, auth_token),
            timeout=10
        )
        if img_response.status_code != 200:
            log.warning(f"Failed to download image: {img_response.status_code}")
            return None

        # Send image bytes to QR Server API
        qr_response = requests.post(
            "https://api.qrserver.com/v1/read-qr-code/",
            files={"file": ("qr.jpg", img_response.content, "image/jpeg")},
            timeout=10
        )
        if qr_response.status_code != 200:
            log.warning(f"QR API error: {qr_response.status_code}")
            return None

        data = qr_response.json()
        decoded = data[0]["symbol"][0]["data"]
        if decoded:
            log.info(f"✅ QR code decoded: {decoded}")
            return decoded
        return None

    except Exception as e:
        log.error(f"QR read error: {e}")
        return None


def extract_charger_id_from_qr(qr_data: str) -> str | None:
    """
    Extracts Charger UUID from decoded QR code data.
    Only handles Ampcontrol URLs and bare UUID format.
    """
    import re
    # Match Ampcontrol URL
    match = re.search(
        r"ampcontrol\.io/#/charger/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        qr_data, re.IGNORECASE
    )
    if match:
        return match.group(1)
    # Match bare UUID format only
    uuid_match = re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
        qr_data.strip(), re.IGNORECASE
    )
    if uuid_match:
        return qr_data.strip()
    return None


def contains_phrase(text: str, phrase: str) -> bool:
    """
    True if `phrase` appears in `text` as a whole word/phrase — not merely
    as a substring. Prevents false positives like "hi" matching inside
    "this", or "no" matching inside "know". Punctuation-only phrases (e.g.
    "?") fall back to plain substring matching since word boundaries don't
    apply meaningfully to them.
    """
    import re as _re
    if not _re.search(r"\w", phrase):
        return phrase in text
    pattern = r"\b" + _re.escape(phrase) + r"\b"
    return _re.search(pattern, text) is not None


def extract_error_code(text: str) -> str | None:
    """Extracts a numeric error code from text like '76', 'error 76', 'error code 76'."""
    import re
    # Strip common prefixes and extract the number
    cleaned = text.strip().lower()
    cleaned = re.sub(r"(error\s*code|error|code|err)\s*", "", cleaned).strip()
    if re.fullmatch(r"\d+", cleaned):
        return cleaned
    return None


def lookup_error_code(code: str) -> dict | None:
    """Looks up an error code in the knowledge base. Returns the error dict or None."""
    for error in KNOWLEDGE_BASE.get("error_codes", []):
        if str(error.get("code", "")) == str(code).strip():
            return error
    return None


def error_code_response(error: dict) -> str:
    """Formats a customer-friendly error code response."""
    code = error.get("code", "")
    message = error.get("customer_message", "")
    self_service = error.get("self_service", False)

    response = f"🔴 *Error Code {code}*\n\n{message}"

    if not self_service:
        response += f"\n\n{AGENT_INTRO}"
    else:
        response += (
            "\n\nIf you have tried the steps above and the issue persists, "
            "type *AGENT* to speak to a support agent or type *MENU* to start again."
        )
    return response


def build_kb_text(kb: dict) -> str:
    """Formats the knowledge base into plain text for Claude's system prompt."""
    lines = []
    info = kb.get("company_info", {})
    if info:
        lines.append(f"Company: {info.get('name', '')}")
        lines.append(f"Support Hours: {info.get('support_hours', '')}")
        lines.append(f"Support Email: {info.get('support_email', '')}")
        lines.append("")

    lines.append("FAQ KNOWLEDGE BASE (use this to answer general questions accurately):")
    current_category = None
    for item in kb.get("faqs", []):
        category = item.get("category", "General")
        if category != current_category:
            lines.append(f"\n## {category}")
            current_category = category
        lines.append(f"Q: {item.get('question', '')}")
        lines.append(f"A: {item.get('answer', '')}")
    return "\n".join(lines)


KB_TEXT = build_kb_text(KNOWLEDGE_BASE)
SALES_INFO = KNOWLEDGE_BASE.get("company_info", {}).get("sales_contact", {})

# ── Session State ─────────────────────────────────────────────────────────────
user_states = {}

# ── Messages ──────────────────────────────────────────────────────────────────

GREETING = (
    "👋 Hello, welcome to the Aeversa helpdesk!\n\n"
    "My name is *AE-Ace* and I am here to get you charged up. ⚡\n\n"
    "To get started, please send me a photo of the *Charger ID sticker*, "
    "or type the *Charger ID*.\n\n"
    "📸 See reference image below for where to find it.\n\n"
    "I will read it and check your charger's status immediately!"
)

GREAT_NEWS = (
    "🎉 *Great news! Glad we got you sorted and back on the road!* ⚡\n\n"
    "If you need anything else, type *MENU* to start again."
)

AGENT_INTRO = (
    "👤 *Connecting you to a support agent...*\n\n"
    "Our team will be with you shortly.\n\n"
    "🕗 *Support Hours:*\n"
    "Monday – Friday: 07:00 – 19:00\n"
    "Saturday: 08:00 – 14:00\n\n"
    "For urgent faults outside these hours, please email:\n"
    "📧 *support@aeversa.com*"
)

FALLBACK = (
    "🤔 I didn't quite understand that.\n\n"
    "Please type *MENU* to see the options again, or type *AGENT* to speak to a support agent."
)


def issue_menu(charger_name: str, confirmed_online: bool = True) -> str:
    status_word = "online" if confirmed_online else "offline"
    intro = f"I can see that you are at charger, *{charger_name}*, and the charger is currently {status_word}. 😊\n\n"
    return (
        f"{intro}"
        "What issue are you experiencing? Please reply with *1*, *2*, or *3*:\n\n"
        "🔴 *1* – My vehicle is not charging\n"
        "🐢 *2* – The charging speed is slow\n"
        "❓ *3* – Something else"
    )


def offline_message(charger_name: str) -> str:
    return (
        f"I can see that you are at charger, *{charger_name}*, and the charger is currently offline. 😔\n\n"
        "Our technical team has been notified and will investigate immediately.\n\n"
    )


# ── Smart Response Interpretation ────────────────────────────────────────────

POSITIVE_PHRASES = [
    "charging now", "it's charging", "its charging", "is charging",
    "working now", "it works", "it's working", "its working",
    "started", "fixed", "sorted", "resolved", "all good", "good now",
    "faster now", "normal now", "charging fine", "it charged",
    "began charging", "started charging", "yes it is", "yes it's",
    "seems to be working", "looks good", "working again", "charges now",
]

NEGATIVE_PHRASES = [
    "still not", "not working", "doesn't work", "does not work",
    "wont work", "won't work", "still broken", "same issue",
    "same problem", "nothing changed", "no change", "not fixed",
    "still happening", "still the same", "tried that", "already tried",
    "already did", "multiple times", "5 times", "several times",
    "3 times", "4 times", "many times", "keeps happening",
    "still showing", "still offline", "still slow", "still not charging",
    "not helping", "didn't help", "did not help", "no luck",
    "tried again", "tried it again", "not resolved", "not sorted",
]

NEW_ISSUE_PHRASES = [
    "my charger is", "charger is broken", "charger is not working",
    "charger is off", "charger not working", "charger won't work",
    "my vehicle is", "vehicle is not", "different problem",
    "new problem", "another issue", "something else",
    "actually my", "actually the",
]

CONFUSION_PHRASES = [
    "where", "how do", "what is", "what does", "find", "locate", "?",
    "can i find", "can you", "don't know", "dont know",
    "not sure", "unsure", "no idea", "which", "help me",
    "show me", "dont understand", "don't understand", "i dont understand",
    "i don't understand", "confused", "lost", "what do you mean",
    "unclear", "explain", "i need help", "instructions",
]

# Words that plausibly indicate an actual fault description, as opposed to
# a short ambiguous token (e.g. "JAC" or "JAC DBN") that just failed to
# match a charger name. Used to stop Claude's intent classifier from
# over-triggering a fault intent on inputs that don't actually contain
# any fault language.
FAULT_DESCRIPTION_INDICATORS = [
    "not", "isn't", "isnt", "wont", "won't", "doesn't", "doesnt",
    "dont", "don't", "cant", "can't", "cannot", "unable", "no",
    "slow", "off", "broken", "stuck", "error", "issue", "problem",
    "wrong", "fault", "fail", "failed", "failing", "stopped", "stop",
    "blank", "dead", "dark", "flashing", "won't start", "not working",
]


def looks_like_fault_description(text: str) -> bool:
    """
    Heuristic check for whether a message plausibly describes an actual
    fault, versus being a short ambiguous token that just failed to match
    a charger name (e.g. "JAC", "JAC DBN", "STC-01"). A genuine fault
    description almost always contains at least one word like "not",
    "slow", "off", "broken", etc. — a bare attempted charger name doesn't.
    """
    msg_lower = text.lower()
    return any(contains_phrase(msg_lower, w) for w in FAULT_DESCRIPTION_INDICATORS)


def interpret_response(msg: str, context: str) -> str:
    """
    Interprets a free-text response to a YES/NO question.
    Returns: 'yes', 'no', 'new_issue', 'confused', 'unclear'
    """
    msg_lower = msg.lower().strip()

    if msg_lower in ["yes", "y", "yep", "yeah", "yup", "ja", "correct", "affirmative", "fine", "ok", "okay"]:
        return "yes"
    if msg_lower in ["no", "n", "nope", "nah", "negative", "nee", "nada"]:
        return "no"

    # Check negative phrases FIRST — a negation like "not resolved" or
    # "not fixed" contains a positive word as a substring ("resolved",
    # "fixed"), so it must be checked before the positive-phrase pass
    # or the negation gets silently missed.
    if any(p in msg_lower for p in NEGATIVE_PHRASES):
        return "no"
    if any(p in msg_lower for p in POSITIVE_PHRASES):
        return "yes"
    if any(p in msg_lower for p in NEW_ISSUE_PHRASES):
        return "new_issue"
    if any(p in msg_lower for p in CONFUSION_PHRASES):
        return "confused"

    if not ANTHROPIC_API_KEY:
        return "unclear"

    try:
        response = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 10,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Support bot asked: \"{context}\"\n"
                        f"Customer replied: \"{msg}\"\n\n"
                        f"Reply ONE word only: yes / no / new_issue / confused / unclear"
                    )
                }],
            },
            timeout=8,
        )
        result = "".join(
            b.get("text", "") for b in response.json().get("content", [])
        ).strip().lower().split()[0]
        return result if result in ["yes", "no", "new_issue", "confused", "unclear"] else "unclear"
    except Exception as e:
        log.error(f"interpret_response error: {e}")
        return "unclear"


def smart_yes_no(user_id: str, state: dict, msg: str,
                  question: str, yes_response, no_response) -> str:
    """
    Handles any free-text YES/NO response intelligently.
    yes_response / no_response can be strings or callables.

    IMPORTANT: yes_response/no_response are only invoked lazily, inside
    the branch that actually needs them. They frequently have side
    effects (setting state, triggering escalations, sending WhatsApp
    notifications) — calling both unconditionally on every invocation,
    regardless of the interpreted result, would fire those side effects
    even when the customer's answer was neither a yes nor a no.
    """
    result = interpret_response(msg, question)

    if result == "yes":
        return yes_response() if callable(yes_response) else yes_response
    elif result == "no":
        return no_response() if callable(no_response) else no_response
    elif result == "new_issue":
        user_states[user_id] = {**state, "step": "confirm_restart"}
        return (
            "⚠️ It sounds like you may have a different issue.\n\n"
            "Would you like to *start a new support request*?\n\n"
            "Reply *YES* to start fresh or *NO* to continue with your current issue."
        )
    elif result == "confused":
        return f"No problem! 😊\n\n{question}\n\nPlease reply *YES* or *NO*."
    else:
        retries = state.get("retries", 0) + 1
        user_states[user_id] = {**state, "retries": retries}
        if retries >= 2:
            user_states[user_id] = {**state, "step": "start", "retries": 0}
            return (
                "I'm having a little trouble understanding — let me connect you "
                "with a support agent who can help directly. 😊\n\n"
                f"{AGENT_INTRO}"
            )
        return (
            f"Sorry, I didn't quite catch that! 😊\n\n"
            f"{question}\n\nPlease reply *YES* or *NO*."
        )


def start_escalation(user_id: str, state: dict, context_msg: str = "") -> str:
    """
    Routes to pre-escalation info gathering before connecting to agent.
    If charger was already identified via Ampcontrol, goes straight to agent.
    """
    prefix = f"{context_msg}\n\n" if context_msg else ""
    has_charger_id = bool(state.get("charger_id") or state.get("charger_uuid"))
    has_site       = bool(state.get("site"))

    # If we already identified the charger via Ampcontrol, go straight to agent
    # We have everything we need — no need to ask for site or charger ID again
    if has_charger_id:
        user_states[user_id] = {**state, "step": "start"}
        return f"{prefix}{AGENT_INTRO}"

    # Need site name
    if not has_site:
        user_states[user_id] = {**state, "step": "pre_escalate_site"}
        return (
            f"{prefix}Before I connect you to an agent, I need a couple of quick details.\n\n"
            "Which *site* are you calling from? Please type the site name."
        )

    # Have site but need charger ID — unless we already have an unconfirmed
    # attempt from an earlier failed identification try, in which case
    # asking again would just repeat a question they already couldn't answer
    if state.get("attempted_charger_id"):
        user_states[user_id] = {**state, "step": "start"}
        return f"{prefix}{AGENT_INTRO}"

    user_states[user_id] = {**state, "step": "pre_escalate_charger_id"}
    return (
        f"{prefix}Before I connect you to an agent, I just need one more detail.\n\n"
        "What is the *Charger ID*?\n\n"
        "📍 The sticker is on the *front of the charger, underneath the screen.*\n\n"
        "Please type it below."
    )


def escalate_slow_charging(user_id: str, state: dict, description: str = "") -> str:
    """
    For slow-charging reports: pulls the charger's live meter reading and
    escalates immediately with it attached, rather than attempting a
    remote restart first. This is deliberate for now — until real-world
    'normal vs slow' thresholds are defined, every slow-charging report
    goes straight to a human with the actual Amp/kW numbers attached, so
    an agent can judge it rather than the bot guessing.
    """
    charger_uuid = state.get("charger_uuid", "")
    network_id = state.get("network_id", "")
    org = get_org_by_index(state.get("org_index"))
    meter_readings = get_charger_meter_values(charger_uuid, network_id, org) if org else None
    meter_note = format_meter_values_for_agent(meter_readings)

    notes_parts = [n for n in [description, meter_note] if n]
    combined_notes = "\n".join(notes_parts)

    escalate_state = {**state, "fault_type": "Slow charging"}
    if combined_notes:
        escalate_state["extra_notes"] = combined_notes

    return start_escalation(user_id, escalate_state,
        "Thanks for letting me know! 🐢 I'm checking your charger's live "
        "data now — let me connect you with our support team who can "
        "review this properly.")

def sales_redirect_message() -> str:
    name = SALES_INFO.get("name", "our sales team")
    email = SALES_INFO.get("email", "")
    phone = SALES_INFO.get("phone", "")
    return (
        "💼 That sounds like a great opportunity!\n\n"
        "I'm focused on technical charger support, so for sales, new installations, "
        "fleet solutions, or partnership enquiries, please reach out to:\n\n"
        f"👤 *{name}*\n"
        f"📧 {email}\n"
        f"📞 {phone}\n\n"
        "They'll be happy to assist you! Is there anything else I can help with today?"
    )


# ── Claude AI – Intent Classification & Knowledge-Grounded Q&A ───────────────

AE_SYSTEM_PROMPT = f"""You are AE-Ace, the WhatsApp support assistant for Aeversa (PTY) Ltd, a South African EV fleet charge point operator.

Your job is to read a customer's free-text WhatsApp message and decide what they need. You must respond with ONLY a JSON object (no other text, no markdown fences) in this exact format:

{{"intent": "...", "reply": "..."}}

Where "intent" is ONE of:
- "not_charging" — ONLY use this if the vehicle is plugged in but the charging SESSION is not starting or the vehicle is not receiving charge. Do NOT use this for stuck cables, error messages, or how-to questions.
- "charger_off" — if the charger screen is blank, off, or the unit appears to have no power
- "slow_charging" — if a charging session IS active but the speed is slower than expected
- "charger_fault" — if the customer says the charger is not working, broken, faulty, has a problem, or is giving issues in a general sense without specifying vehicle not charging, screen off, or slow speed
- "no_charger_id" — if the customer says there is no Charger ID sticker, no visible label, or they cannot find any identifying marking on the charger at all (e.g. "there's no sticker", "I can't find a Charger ID")
- "agent" — if the customer explicitly wants a human agent, or has an account/complaint issue that cannot be resolved with FAQ information
- "sales" — if the customer is asking about new charger installations, fleet expansion, partnerships, or business pricing
- "general" — use this for ANY question that can be answered using the FAQ knowledge base below, including: how to start a session, how to stop a session, stuck cables, error messages on screen, VIN start process, red tick questions, how to register a vehicle, session reports, and any other how-to or informational question
- "greeting" — if it is purely a greeting with no specific issue mentioned
- "unclear" — only if you genuinely cannot determine what the customer needs after careful reading

IMPORTANT ROUTING RULES:
- "Charger is not working" / "charger is broken" / "charger has a problem" / "charger giving issues" = "charger_fault"
- "There's no sticker" / "no Charger ID on it" / "can't find a sticker" = "no_charger_id" (NOT "general" — this needs a state change, not just an FAQ answer)
- "Cable is stuck" or "cannot unplug" = "general" (answer from KB, NOT "not_charging")
- "Error message on screen" = "general" (answer from KB, NOT "not_charging")
- "How do I start charging" = "general" (answer from KB)
- "How do I stop charging" = "general" (answer from KB)
- "Red tick" questions = "general" (answer from KB)
- "VIN" questions = "general" (answer from KB)
- "Session not starting" or "vehicle won't charge" = "not_charging" (needs diagnostic flow)
- "Charger screen is off/blank" = "charger_off" (needs diagnostic flow)
- "Charging is slow" = "slow_charging" (needs diagnostic flow)

If intent is "not_charging", "charger_off", or "slow_charging", set "reply" to a short one-sentence warm acknowledgment of their specific issue.

If intent is "agent" or "no_charger_id", set "reply" to a short one-sentence acknowledgment.

If intent is "sales", set "reply" to "" (empty string).

If intent is "general", set "reply" to a helpful answer using ONLY the FAQ knowledge base below. Keep it short and simple — 1-2 short sentences where possible, plain everyday words, no long or complex sentences. Drivers are reading this quickly on their phone, often mid-task, so clarity and brevity matter more than completeness. If the knowledge base does not contain the answer, do NOT make one up — set intent to "agent" instead and reply with a short acknowledgment that you will connect them to someone who can help.

If the "general" answer involves the customer tapping the charger screen twice and pressing Stop — whether they asked how to stop a session, or the cable is stuck — also include a "media" field set to exactly "video_how_to_stop" (a short demo video showing this action). For every other question, omit the "media" field entirely.

If intent is "greeting" or "unclear", set "reply" to "" (empty string).

Be warm, concise, and professional. Use a friendly South African tone. Never invent technical details not found in the knowledge base.

─────────────────────────────
{KB_TEXT}
─────────────────────────────
"""


def ask_claude(message_text: str, context_hint: str = ""):
    """Calls Claude to classify intent and optionally generate a reply.
    context_hint briefly describes what the bot last asked the customer,
    so short follow-ups like "where is it?" can be resolved correctly.
    Returns dict: {"intent": str, "reply": str} or None on failure."""
    if not ANTHROPIC_API_KEY:
        return None

    user_content = message_text
    if context_hint:
        user_content = (
            f"[Context: {context_hint}]\n\n"
            f"Customer message: {message_text}"
        )

    try:
        response = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "system": AE_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=10,  # Strict 10s — Twilio cancels the whole request at 15s
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        if "intent" in parsed:
            return parsed
        return None
    except requests.exceptions.Timeout:
        print("Claude API timed out — falling back to menu")
        return None
    except Exception as e:
        print(f"Claude API error: {e}")
        return None


# ── State Machine ─────────────────────────────────────────────────────────────

def handle_qr_or_image(user_id: str, state: dict,
                        image_url: str, msg_raw: str) -> tuple[str, str | None]:
    """
    Handles an image sent by a customer:
    1. Try QR code decode → get UUID → look up charger
    2. Try Claude vision OCR → read sticker text → search Ampcontrol by name
    3. Fall back to asking for manual input
    """
    # Step 1: Try QR code decode
    qr_data = read_qr_code(image_url)
    if qr_data:
        charger_uuid = extract_charger_id_from_qr(qr_data)
        if charger_uuid:
            log.info(f"QR decoded → UUID: {charger_uuid}")
            return lookup_charger_and_respond(user_id, state, charger_uuid)

    # Step 2: Try Claude vision OCR to read sticker text
    log.info("QR decode failed — trying Claude vision OCR on image")
    sticker_text = read_text_from_image(image_url)
    if sticker_text:
        # Check if it's a UUID or URL first
        charger_uuid = extract_uuid_from_text(sticker_text)
        if charger_uuid:
            log.info(f"OCR found UUID: {charger_uuid}")
            return lookup_charger_and_respond(user_id, state, charger_uuid)

        # Search Ampcontrol by the text read from sticker
        log.info(f"OCR read '{sticker_text}' — searching Ampcontrol by name")
        charger = search_charger_by_name_across_orgs(sticker_text)
        if charger:
            charger_uuid = charger.get("id", "")
            charger_name = charger.get("customName") or charger.get("name") or sticker_text
            matched_org = get_org_by_index(charger.get("_matched_org_index"))
            log.info(f"✅ Found charger '{charger_name}' via OCR name search")
            return lookup_charger_and_respond(user_id, state, charger_uuid, matched_org)

        # OCR read text but charger not found in Ampcontrol
        user_states[user_id] = {**state, "step": "await_charger_id"}
        return (
            f"📷 I can see *\"{sticker_text}\"* on your charger, "
            f"but I couldn't find it in our system.\n\n"
            f"Please check the name and try typing it, or send a clearer "
            f"photo of the *Charger ID sticker*. 😊"
        )

    # Step 3: Both QR and OCR failed (QR is attempted silently, not advertised)
    user_states[user_id] = {**state, "step": "await_charger_id"}
    return (
        "📷 I received your image but couldn't read any charger information from it.\n\n"
        "Please try one of these:\n\n"
        "📷 Send a clearer photo of the *Charger ID sticker*\n"
        "✍️ Type the *Charger ID* as shown on the unit\n\n"
        "💡 _Tip: Make sure the image is well-lit and in focus._\n\n"
        "📸 See an example reference photo below.",
        get_media("charger_id_northgate")
    )


def lookup_charger_and_respond(user_id: str, state: dict,
                                charger_uuid: str, org: dict = None) -> tuple[str, str | None]:
    """
    Looks up charger status on Ampcontrol and routes accordingly.
    If org is not provided (e.g. charger_uuid came from a QR code or a
    pasted UUID, rather than a name search that already matched a
    specific org), searches across every configured org to find which
    one has this charger.
    """
    log.info(f"Looking up charger {charger_uuid} on Ampcontrol")
    if org:
        charger = get_charger_status(charger_uuid, org)
    else:
        charger = find_charger_status_across_orgs(charger_uuid)

    matched_org_index = charger.get("org_index")
    matched_org = get_org_by_index(matched_org_index) if matched_org_index else org

    # Build a friendly charger name
    raw_name = charger.get("name", "")
    site      = state.get("site", "")
    if raw_name and raw_name != charger_uuid[:8]:
        friendly_name = raw_name
    elif site:
        friendly_name = f"{site} charger"
    else:
        friendly_name = f"Charger ...{charger_uuid[-6:]}"

    base_state = {
        **state,
        "charger_uuid":  charger_uuid,
        "charger_id":    charger_uuid,   # also store as charger_id for escalation
        "charger_name":  friendly_name,
        "network_id":    charger.get("network_id", ""),
        "org_index":     matched_org_index,
        "site":          state.get("site") or charger.get("network_name", ""),
    }

    if charger["online"] is True:
        active_alerts = get_charger_alerts(charger_uuid, base_state.get("network_id", ""), matched_org) if matched_org else []
        if active_alerts:
            alert_summary = format_alerts_for_agent(active_alerts)
            if any(is_emergency_stop_alert(a) for a in active_alerts):
                user_states[user_id] = {**base_state, "step": "emergency_stop_check",
                                         "fault_type": "Possible emergency stop pressed",
                                         "extra_notes": alert_summary}
                return (
                    f"I can see that you are at charger, *{friendly_name}*, and it's "
                    "online — but it looks like the *emergency stop* button may have "
                    "been pressed on this charger. 🛑\n\n"
                    "Could you please check the charger for a red emergency stop "
                    "button, and if it's pressed in, twist or pull it to release it? "
                    "🎥 See the video below for how.\n\n"
                    "Reply *YES* once you've released it, or *NO* if you can't find one.",
                    get_media("video_emergency_stop")
                )
            if any(is_invalid_id_tag_alert(a) for a in active_alerts):
                escalate_state = {**base_state, "fault_type": "Invalid vehicle ID tag",
                                   "extra_notes": alert_summary}
                return (
                    start_escalation(
                        user_id, escalate_state,
                        f"I can see that you are at charger, *{friendly_name}*. Your "
                        "*Vehicle ID Tag* doesn't seem to be registered to charge on "
                        "this system. 🪪\n\n"
                        "I'm putting you in touch with our support team who can help "
                        "get this sorted."
                    ),
                    None
                )
            escalate_state = {**base_state, "fault_type": "Active charger alert",
                               "extra_notes": alert_summary}
            return (
                start_escalation(
                    user_id, escalate_state,
                    f"I can see that you are at charger, *{friendly_name}*, and it's "
                    "online — but there's an active alert flagged on it. 🔎"
                ),
                None
            )
        user_states[user_id] = {**base_state, "step": "issue_menu"}
        return (issue_menu(friendly_name, confirmed_online=True), None)

    elif charger["online"] is False:
        user_states[user_id] = {**base_state,
                                  "step": "start",
                                  "fault_type": "Charger offline"}
        return (f"{offline_message(friendly_name)}{AGENT_INTRO}", None)

    else:
        # Ampcontrol unavailable — fall back to manual diagnostic flow
        # NEVER attempt restart when we can't reach Ampcontrol
        user_states[user_id] = {**base_state, "step": "manual_issue_menu"}
        log.warning("Ampcontrol unavailable — falling back to manual diagnostic flow")
        return (
            "⚠️ I couldn't check your charger's live status right now.\n\n"
            "I'll guide you through some manual troubleshooting steps.\n\n"
            "What issue are you experiencing? Please reply with *1*, *2*, or *3*:\n\n"
            "🔴 *1* – My vehicle is not charging\n"
            "🐢 *2* – The charging speed is slow\n"
            "❓ *3* – Something else",
            None
        )


def handle_message(user_id: str, msg_raw: str, has_media: bool = False, received_media: str = "") -> tuple[str, str | None]:
    msg = msg_raw.strip().lower()
    state = user_states.get(user_id, {"step": "start"})
    step  = state.get("step", "start")

    # ── Record activity timestamp + clear timeout flag on any response ────────
    now = datetime.now().timestamp()
    last_activity = state.get("last_activity", 0)

    # ── Timestamp-based session reset (belt & suspenders with background checker)
    # If more than 2 hours have passed since last activity, reset the session
    if last_activity > 0 and (now - last_activity) > SESSION_RESET_SECS:
        log.info(f"Session reset on message receipt for {user_id} — inactive for {(now - last_activity)/3600:.1f}h")
        user_states[user_id] = {"step": "start", "last_activity": now}
        state = user_states[user_id]
        step  = "start"

    user_states[user_id] = {
        **state,
        "last_activity":     now,
        "timeout_escalated": False,
    }
    state = user_states[user_id]

    # ── Global Commands — these work from ANY step ────────────────────────────
    # "menu"/"start" are explicit, unambiguous reset commands — they always
    # restart immediately, with no confirmation step. Softer greetings like
    # "hi" are ambiguous (could just be a casual check-in mid-flow), so
    # those still go through the confirm_restart prompt.
    EXPLICIT_RESET_WORDS = {"menu", "start"}
    SOFT_GREETING_WORDS = {"hi", "hello", "hey", "hiya", "howzit",
                            "yo", "sup", "good morning", "good afternoon", "good evening",
                            "good day", "morning", "afternoon"}

    if msg in EXPLICIT_RESET_WORDS:
        user_states[user_id] = {"step": "await_qr", "last_activity": now}
        return (GREETING, get_media("charger_id_northgate"))

    if msg in SOFT_GREETING_WORDS:
        if step in ["await_qr", "await_charger_id"]:
            # Already greeted — just remind them what to send
            return (
                "😊 I'm still waiting for your charger details!\n\n"
                "Please send me:\n"
                "📷 A photo of the *Charger ID sticker*\n\n"
                "Or simply type the Charger ID."
            )
        elif step not in ["start"]:
            # Mid-flow — offer to restart
            user_states[user_id] = {**state, "step": "confirm_restart", "prev_step": step}
            return (
                "👋 Hi! Would you like to *start a new support request*?\n\n"
                "Reply *YES* to start fresh or *NO* to continue where we left off."
            )
        else:
            # Fresh start
            user_states[user_id] = {"step": "await_qr", "last_activity": now}
            return (GREETING, get_media("charger_id_northgate"))

    # Agent request works from any step
    if msg in ["agent", "human", "person", "speak to someone"]:
        return start_escalation(user_id, state)

    # Sales request works from any step
    if msg in ["sales", "sales rep", "sales agent"]:
        user_states[user_id] = {"step": "start", "last_activity": now}
        return sales_redirect_message()

    # ── Confirm restart step ──────────────────────────────────────────────────
    if step == "confirm_restart":
        if msg in ["yes", "y", "yeah", "yep"]:
            user_states[user_id] = {"step": "start"}
            return (GREETING, get_media("charger_id_northgate"))
        elif msg in ["no", "n", "nope"]:
            prev_step = state.get("prev_step", "start")
            user_states[user_id] = {**state, "step": prev_step}
            if prev_step in YES_NO_STEPS:
                return (
                    "No problem! Let's continue where we left off.\n\n"
                    "Please reply *YES* or *NO* to my previous question, "
                    "or type *MENU* to start a new request."
                )
            return (
                "No problem! Let's continue where we left off.\n\n"
                "Please reply to my previous message above, "
                "or type *MENU* to start a new request."
            )
        else:
            return (
                "Would you like to start a new support request?\n\n"
                "Reply *YES* to start fresh or *NO* to continue."
            )

    # ── Mid-flow new issue detection ──────────────────────────────────────────
    if step in YES_NO_STEPS and any(p in msg for p in NEW_ISSUE_PHRASES):
        user_states[user_id] = {**state, "step": "confirm_restart", "prev_step": step}
        return (
            "⚠️ It sounds like you may have a different issue.\n\n"
            "Would you like to *start a new support request*?\n\n"
            "Reply *YES* to start fresh or *NO* to continue with your current issue."
        )

    # ── START STEP — always ask for QR code first ─────────────────────────────
    if step == "start":
        # QR code or image sent
        if has_media and received_media:
            return handle_qr_or_image(user_id, state, received_media, msg_raw)

        # Agent / Sales shortcuts
        if msg == "4":
            return start_escalation(user_id, state)

        # Error code typed directly
        extracted_code = extract_error_code(msg.strip())
        if extracted_code:
            error = lookup_error_code(extracted_code)
            if error:
                user_states[user_id] = {"step": "start"}
                return error_code_response(error)

        # UUID or Ampcontrol URL pasted
        charger_uuid = extract_uuid_from_text(msg_raw)
        if charger_uuid:
            return lookup_charger_and_respond(user_id, state, charger_uuid)

        # Try Ampcontrol name search for typed text
        GREETING_WORDS = {"hi", "hello", "hey", "yes", "no", "ok", "okay",
                           "thanks", "thank you", "yo", "sup", "help"}
        is_greeting = msg.strip(' .!?').lower() in GREETING_WORDS
        is_confused = any(contains_phrase(msg, p) for p in CONFUSION_PHRASES)

        if len(msg_raw.strip()) >= 3 and not is_greeting:
            # Only try the Ampcontrol name search if this doesn't read as a
            # question — no point searching "where do i find X" as a charger name
            if not is_confused:
                charger = search_charger_by_name_across_orgs(msg_raw.strip())
                if charger:
                    charger_uuid = charger.get("id", "")
                    matched_org = get_org_by_index(charger.get("_matched_org_index"))
                    log.info(f"Found charger by typed name at start: {charger.get('customName') or charger.get('name')}")
                    return lookup_charger_and_respond(user_id, state, charger_uuid, matched_org)

            # Not found in Ampcontrol, or looked like a question — check the KB
            ai_result = ask_claude(msg_raw, context_hint=(
                "The bot's last message asked the customer to send a photo of "
                "their Charger ID sticker, or type the Charger ID."
            ))
            intent = ai_result.get("intent", "unclear") if ai_result else "unclear"
            if intent == "general" and ai_result:
                ai_reply = ai_result.get("reply", "")
                user_states[user_id] = {**state, "step": "await_qr", "kb_answered": True}
                media_key = ai_result.get("media")
                video_note = "\n\n🎥 See the video below." if media_key else ""
                return (
                    f"{ai_reply}{video_note}\n\n"
                    "---\n"
                    "Did that answer your question? 😊 If you still need help with a specific charger, "
                    "just send me a photo of the Charger ID sticker, or type the Charger ID — "
                    "otherwise you're all set!",
                    get_media(media_key) if media_key else None
                )

            if intent == "agent" and ai_result:
                ai_reply = ai_result.get("reply", "")
                return start_escalation(user_id, state, ai_reply)

            if intent == "no_charger_id" and ai_result:
                # Customer has no sticker/QR to identify the charger with —
                # move into the site-collection flow so their next message
                # (e.g. a site name) is actually captured, instead of being
                # re-checked as if it were a charger name/ID again.
                user_states[user_id] = {**state, "step": "pre_escalate_site"}
                return (
                    "No worries! Just let me know which *site or depot* you're at "
                    "and I'll get our team to help identify the correct charger for you. 😊"
                )

        # EVERYTHING ELSE — show greeting and move to await_qr
        user_states[user_id] = {"step": "await_qr"}
        return (GREETING, get_media("charger_id_northgate"))

    # ── Waiting for QR code / charger name ───────────────────────────────────
    if step == "await_qr":
        if has_media and received_media:
            return handle_qr_or_image(user_id, state, received_media, msg_raw)

        # Check for UUID or Ampcontrol URL
        charger_uuid = extract_uuid_from_text(msg_raw)
        if charger_uuid:
            return lookup_charger_and_respond(user_id, state, charger_uuid)

        GREETING_WORDS = {"hi", "hello", "hey", "yes", "no", "ok", "okay",
                           "thanks", "thank you", "yo", "sup", "help"}
        is_greeting = msg.strip(' .!?').lower() in GREETING_WORDS
        is_confused = any(contains_phrase(msg, p) for p in CONFUSION_PHRASES)

        if len(msg_raw.strip()) >= 3 and not is_greeting:

            # Only try the Ampcontrol name search if this doesn't read as a
            # question — no point searching "where do i find X" as a charger name
            if not is_confused:
                charger = search_charger_by_name_across_orgs(msg_raw.strip())
                if charger:
                    charger_uuid = charger.get("id", "")
                    matched_org = get_org_by_index(charger.get("_matched_org_index"))
                    log.info(f"Found charger by typed name: {charger.get('customName') or charger.get('name')}")
                    return lookup_charger_and_respond(user_id, state, charger_uuid, matched_org)

            # Not found in Ampcontrol, or looked like a question — check intent
            ai_result = ask_claude(msg_raw, context_hint=(
                "The bot's last message asked the customer to send a photo of "
                "their Charger ID sticker, or type the Charger ID."
            ))
            intent = ai_result.get("intent", "unclear") if ai_result else "unclear"

            if intent == "general" and ai_result:
                # KB can answer — reply and note that advice was given
                ai_reply = ai_result.get("reply", "")
                user_states[user_id] = {**state, "kb_answered": True}
                media_key = ai_result.get("media")
                video_note = "\n\n🎥 See the video below." if media_key else ""
                return (
                    f"{ai_reply}{video_note}\n\n"
                    "---\n"
                    "Did that answer your question? 😊 If you still need help with a specific charger, "
                    "just send me a photo of the Charger ID sticker, or type the Charger ID — "
                    "otherwise you're all set!",
                    get_media(media_key) if media_key else None
                )

            if intent == "agent" and ai_result:
                ai_reply = ai_result.get("reply", "")
                return start_escalation(user_id, state, ai_reply)

            if intent == "no_charger_id" and ai_result:
                # Customer has no sticker/QR to identify the charger with —
                # move into the site-collection flow so their next message
                # (e.g. a site name) is actually captured, instead of being
                # re-checked as if it were a charger name/ID again.
                user_states[user_id] = {**state, "step": "pre_escalate_site"}
                return (
                    "No worries! Just let me know which *site or depot* you're at "
                    "and I'll get our team to help identify the correct charger for you. 😊"
                )

            if intent in ["not_charging", "charger_fault", "charger_off", "slow_charging"] \
                    and looks_like_fault_description(msg_raw):
                # Customer is describing a problem — not a charger name
                if state.get("kb_answered", False):
                    # Already gave advice and it didn't help → escalate
                    return start_escalation(user_id, state,
                        "I understand the issue is still ongoing. 😔\n\n"
                        "Let me connect you with a support agent who can help directly.")
                else:
                    # First time — guide them to identify charger so we can help properly
                    user_states[user_id] = {**state, "fault_hint": intent}
                    return (
                        "Got it — to look into this properly, I'll need to check "
                        "your charger's status.\n\n"
                        "Please send me:\n"
                        "📷 A photo of the *Charger ID sticker*\n"
                        "✍️ Or type the *Charger ID* as shown on the unit"
                    )

            # Phrases indicating KB advice already failed → escalate
            advice_failed = ["still stuck", "still not", "tried that", "didnt work",
                             "didn't work", "i tried", "still happening", "already tried",
                             "not working", "i just tried", "same problem"]
            if state.get("kb_answered", False) and any(p in msg for p in advice_failed):
                return start_escalation(user_id, state,
                    "I understand the advice didn't resolve your issue. 😔\n\n"
                    "Let me connect you with a support agent who can help directly.")

        # Track failed identification attempts (KB answers don't count)
        attempts = state.get("unrecognized_attempts", 0) + 1
        user_states[user_id] = {**state, "step": "await_qr",
                                  "unrecognized_attempts": attempts}

        if attempts >= 3:
            attempted_id = msg_raw.strip()
            escalate_state = {
                **state, "step": "start", "unrecognized_attempts": 0,
                "attempted_charger_id": attempted_id,
                "extra_notes": f"Customer's last identification attempt (unconfirmed): \"{attempted_id}\""
            }
            user_states[user_id] = escalate_state
            return start_escalation(user_id, escalate_state,
                "I'm having trouble identifying your charger. 😔\n\n"
                "Let me connect you with a support agent who can help directly.")

        return (
            f"I couldn't find a charger matching that. 😔 "
            f"({3 - attempts} attempt{'s' if 3 - attempts != 1 else ''} remaining)\n\n"
            "Please try:\n"
            "📷 Send a *photo of the Charger ID sticker* on the charger\n"
            "✍️ Type the *exact Charger ID* as shown on the unit\n\n"
            "Or type *AGENT* to speak to someone directly."
        )

    # ── Waiting for Charger UUID after initial identification failed ─────────
    if step == "await_charger_id":
        if has_media and received_media:
            return handle_qr_or_image(user_id, state, received_media, msg_raw)

        # Check for UUID or Ampcontrol URL
        charger_uuid = extract_uuid_from_text(msg_raw)
        if charger_uuid:
            return lookup_charger_and_respond(user_id, state, charger_uuid)

        # Search Ampcontrol by typed charger name
        # Only if not a common greeting/question phrase
        GREETING_WORDS = {"hi", "hello", "hey", "yes", "no", "ok", "okay",
                           "thanks", "thank you"}
        is_greeting = msg.strip(' .!?').lower() in GREETING_WORDS
        is_confused = any(contains_phrase(msg, p) for p in CONFUSION_PHRASES)

        if len(msg_raw.strip()) >= 3 and not is_greeting:
            # Only try the Ampcontrol name search if this doesn't read as a question
            if not is_confused:
                charger = search_charger_by_name_across_orgs(msg_raw.strip())
                if charger:
                    charger_uuid = charger.get("id", "")
                    matched_org = get_org_by_index(charger.get("_matched_org_index"))
                    log.info(f"Found charger by name: {charger.get('customName') or charger.get('name')}")
                    return lookup_charger_and_respond(user_id, state, charger_uuid, matched_org)

            # Not found in Ampcontrol, or looked like a question — check the KB
            ai_result = ask_claude(msg_raw, context_hint=(
                "The bot's last message asked the customer to send a photo of "
                "their Charger ID sticker, or type the Charger ID."
            ))
            intent = ai_result.get("intent", "unclear") if ai_result else "unclear"
            if intent == "general" and ai_result:
                ai_reply = ai_result.get("reply", "")
                user_states[user_id] = {**state, "kb_answered": True}
                media_key = ai_result.get("media")
                video_note = "\n\n🎥 See the video below." if media_key else ""
                return (
                    f"{ai_reply}{video_note}\n\n"
                    "---\n"
                    "Did that answer your question? 😊 If you still need help with a specific charger, "
                    "just send me a photo of the Charger ID sticker, or type the Charger ID — "
                    "otherwise you're all set!",
                    get_media(media_key) if media_key else None
                )

            if intent == "agent" and ai_result:
                ai_reply = ai_result.get("reply", "")
                return start_escalation(user_id, state, ai_reply)

            if intent == "no_charger_id" and ai_result:
                # Customer has no sticker/QR to identify the charger with —
                # move into the site-collection flow so their next message
                # (e.g. a site name) is actually captured, instead of being
                # re-checked as if it were a charger name/ID again.
                user_states[user_id] = {**state, "step": "pre_escalate_site"}
                return (
                    "No worries! Just let me know which *site or depot* you're at "
                    "and I'll get our team to help identify the correct charger for you. 😊"
                )

            if not is_confused:
                return (
                    f"I searched for *\"{msg_raw.strip()}\"* but couldn't find a matching charger. 😔\n\n"
                    "Please try:\n"
                    "📷 Send a photo of the *Charger ID sticker*\n"
                    "✍️ Type the exact Charger ID as shown on the unit\n\n"
                    "Or type *AGENT* to speak to someone directly."
                )

        return (
            "Please send me a photo of the charger or type the Charger ID. 😊\n\n"
            "📷 Photo of *Charger ID sticker* — I'll read the text\n"
            "✍️ Type the *Charger ID* as shown on the unit\n\n"
            "Or type *AGENT* to speak to someone directly."
        )

    # ── Manual issue menu — used when Ampcontrol is unavailable ──────────────
    if step == "manual_issue_menu":
        charger_uuid = state.get("charger_uuid", "")
        if msg == "1":
            user_states[user_id] = {**state, "step": "opt1_key_removed",
                                     "fault_type": "Vehicle not charging"}
            return (
                "🔴 *Vehicle Not Charging*\n\n"
                "Let's get this sorted! First things first:\n\n"
                "Is your vehicle switched off and the key removed from the ignition?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "2":
            user_states[user_id] = {**state, "step": "opt3_restart_session",
                                     "fault_type": "Slow charging"}
            return (
                "🐢 *Slow Charging*\n\n"
                "Let's get your speed up! ⚡\n\n"
                "Can you stop the charging session and start it again?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "3":
            user_states[user_id] = {**state, "step": "something_else",
                                     "fault_type": "Other issue"}
            return "Please describe the issue you are experiencing and I will get our support team to help. 📋"
        elif msg == "4":
            return start_escalation(user_id, state)
        else:
            return (
                "What issue are you experiencing? Please reply with *1*, *2*, or *3*:\n\n"
                "🔴 *1* – My vehicle is not charging\n"
                "🐢 *2* – The charging speed is slow\n"
                "❓ *3* – Something else"
            )

    # ── Issue menu — shown after charger confirmed online ─────────────────────
    if step == "issue_menu":
        charger_name = state.get("charger_name", "your charger")
        charger_uuid = state.get("charger_uuid", "")

        if msg == "1":
            # Vehicle not charging — check for active alerts before trying a restart
            active_alerts = get_charger_alerts(charger_uuid, state.get("network_id", ""), get_org_by_index(state.get("org_index")))
            if active_alerts:
                alert_summary = format_alerts_for_agent(active_alerts)
                if any(is_emergency_stop_alert(a) for a in active_alerts):
                    user_states[user_id] = {**state, "step": "emergency_stop_check",
                                             "fault_type": "Possible emergency stop pressed",
                                             "extra_notes": alert_summary}
                    return (
                        "It looks like the *emergency stop* button may have been "
                        "pressed on this charger. 🛑\n\n"
                        "Could you please check the charger for a red emergency stop "
                        "button, and if it's pressed in, twist or pull it to release it? "
                        "🎥 See the video below for how.\n\n"
                        "Reply *YES* once you've released it, or *NO* if you can't find one.",
                        get_media("video_emergency_stop")
                    )
                if any(is_invalid_id_tag_alert(a) for a in active_alerts):
                    return start_escalation(
                        user_id,
                        {**state, "fault_type": "Invalid vehicle ID tag", "extra_notes": alert_summary},
                        "Your *Vehicle ID Tag* doesn't seem to be registered to charge "
                        "on this system. 🪪\n\n"
                        "I'm putting you in touch with our support team who can help "
                        "get this sorted."
                    )
                return start_escalation(
                    user_id,
                    {**state, "fault_type": "Vehicle not charging", "extra_notes": alert_summary},
                    "I can see there's an active alert on this charger. 🔎\n\n"
                    "Let me connect you with our support team so they can look "
                    "into this properly rather than trying a remote restart."
                )
            # Vehicle not charging — get a description first, THEN unplug/restart/poll
            user_states[user_id] = {**state, "step": "opt1_awaiting_description",
                                     "fault_type": "Vehicle not charging"}
            return (
                "🔴 Sorry to hear that!\n\n"
                "Can you briefly describe what's happening? (e.g. no lights on "
                "the charger, an error message, nothing happens when you plug in, etc.)"
            )
        elif msg == "2":
            # Slow charging — get a description first, THEN stop/unplug/restart/poll
            user_states[user_id] = {**state, "step": "opt2_awaiting_description",
                                     "fault_type": "Slow charging"}
            return (
                "🐢 Sorry to hear that!\n\n"
                "Can you briefly describe what's happening? (e.g. how slow it is, "
                "when it started, any error messages, etc.)"
            )
        elif msg == "3":
            user_states[user_id] = {**state, "step": "something_else",
                                     "fault_type": "Other issue"}
            return (
                "Please describe the issue you are experiencing and I will "
                "make sure our support team has all the details. 📋"
            )
        elif msg == "4":
            return start_escalation(user_id, state)
        else:
            # Customer typed free text instead of a number
            # Use Claude to understand intent and route automatically
            ai_result = ask_claude(msg_raw, context_hint=(
                "The bot's last message asked the customer to choose an issue "
                "from a numbered menu: 1) vehicle is not charging, "
                "2) charging speed is slow, 3) something else, "
                "4) speak to a support agent."
            ))
            intent = ai_result.get("intent", "unclear") if ai_result else "unclear"

            if intent in ["not_charging", "charger_fault", "charger_off"] \
                    and looks_like_fault_description(msg_raw):
                active_alerts = get_charger_alerts(charger_uuid, state.get("network_id", ""), get_org_by_index(state.get("org_index")))
                if active_alerts:
                    alert_summary = format_alerts_for_agent(active_alerts)
                    if any(is_emergency_stop_alert(a) for a in active_alerts):
                        user_states[user_id] = {**state, "step": "emergency_stop_check",
                                                 "fault_type": "Possible emergency stop pressed",
                                                 "extra_notes": alert_summary}
                        return (
                            "It looks like the *emergency stop* button may have been "
                            "pressed on this charger. 🛑\n\n"
                            "Could you please check the charger for a red emergency "
                            "stop button, and if it's pressed in, twist or pull it to "
                            "release it? 🎥 See the video below for how.\n\n"
                            "Reply *YES* once you've released it, or *NO* if you can't find one.",
                            get_media("video_emergency_stop")
                        )
                    if any(is_invalid_id_tag_alert(a) for a in active_alerts):
                        return start_escalation(
                            user_id,
                            {**state, "fault_type": "Invalid vehicle ID tag", "extra_notes": alert_summary},
                            "Your *Vehicle ID Tag* doesn't seem to be registered to "
                            "charge on this system. 🪪\n\n"
                            "I'm putting you in touch with our support team who can "
                            "help get this sorted."
                        )
                    return start_escalation(
                        user_id,
                        {**state, "fault_type": "Vehicle not charging", "extra_notes": alert_summary},
                        "I can see there's an active alert on this charger. 🔎\n\n"
                        "Let me connect you with our support team so they can look "
                        "into this properly rather than trying a remote restart."
                    )
                user_states[user_id] = {**state, "step": "opt1_confirm_unplugged",
                                         "fault_type": "Vehicle not charging"}
                return (
                    "🔴 Let's get you charging!\n\n"
                    "Could you please *unplug the charging cable* from your vehicle? "
                    "Once it's unplugged, just send me a 👍 or let me know."
                )
            elif intent == "slow_charging":
                return escalate_slow_charging(user_id, state, msg_raw.strip())
            elif intent == "agent":
                return start_escalation(user_id, state)
            elif intent == "general":
                ai_reply = ai_result.get("reply", "")
                media_key = ai_result.get("media")
                video_note = "\n\n🎥 See the video below." if media_key else ""
                return (
                    f"{ai_reply}{video_note}\n\n"
                    "---\n"
                    "Did that help? 😊 If not, just tell me a bit more and I'll keep helping.",
                    get_media(media_key) if media_key else None
                )
            else:
                # Capture description and escalate
                user_states[user_id] = {**state, "fault_type": "Other issue",
                                         "extra_notes": msg_raw.strip()}
                return start_escalation(
                    user_id,
                    {**state, "fault_type": "Other issue", "extra_notes": msg_raw.strip()},
                    f"I've noted your issue: *\"{msg_raw.strip()}\"*\n\n"
                    "Let me connect you with a support agent."
                )

    # ── Vehicle not charging — capturing the customer's description ──────────
    if step == "opt1_awaiting_description":
        description = msg_raw.strip()
        user_states[user_id] = {**state, "step": "opt1_confirm_unplugged",
                                 "extra_notes": description}
        return (
            "Thanks for letting me know! 📋\n\n"
            "Let's get you charging!\n\n"
            "Could you please *unplug the charging cable* from your vehicle? "
            "Once it's unplugged, just send me a 👍 or let me know."
        )

    # ── Vehicle not charging — waiting for customer to confirm unplugged ─────
    if step == "opt1_confirm_unplugged":
        confirm_phrases = ["done", "unplugged", "out", "removed", "yes", "ok", "okay", "finished", "ready"]
        if "👍" in msg_raw or any(contains_phrase(msg, p) for p in confirm_phrases):
            charger_uuid = state.get("charger_uuid", "")
            charger_name = state.get("charger_name", "your charger")
            org = get_org_by_index(state.get("org_index"))
            user_states[user_id] = {**state, "step": "opt1_restarting"}
            if org:
                threading.Thread(
                    target=lambda: restart_charger(charger_uuid, org), daemon=True
                ).start()
                threading.Thread(
                    target=lambda: poll_charger_and_notify_online(
                        user_id, charger_uuid, charger_name, org,
                        next_step="await_restart_result",
                        question="Is your vehicle charging?",
                        fault_type="Vehicle not charging"
                    ),
                    daemon=True
                ).start()
            else:
                log.warning(f"No org on record for charger {charger_uuid} — cannot restart")
            return (
                f"Thank you! I'm restarting *{charger_name}* now — please hold on "
                "for a moment while I check that it's back online. ⏳"
            )
        else:
            return (
                "Just let me know once you've *unplugged the cable* from your "
                "vehicle — you can reply with a 👍, or just tell me when you're done."
            )

    # ── Vehicle not charging — restart in progress, polling in the background ─
    if step == "opt1_restarting":
        return (
            "⏳ Still checking on the charger's status — I'll message you as soon as it's "
            "back online and ready to plug back in. Thanks for your patience!"
        )

    # ── Emergency stop button check (specific to OCPP error 258) ─────────────
    if step == "emergency_stop_check":
        QUESTION = "Have you released the emergency stop button?"
        def yes_fn():
            charger_uuid = state.get("charger_uuid", "")
            network_id = state.get("network_id", "")
            charger_name = state.get("charger_name", "your charger")
            org = get_org_by_index(state.get("org_index"))
            user_states[user_id] = {**state, "step": "emergency_stop_verifying"}
            if org:
                threading.Thread(
                    target=lambda: poll_emergency_stop_cleared(
                        user_id, charger_uuid, network_id, charger_name, org
                    ),
                    daemon=True
                ).start()
            else:
                log.warning(f"No org on record for charger {charger_uuid} — cannot verify emergency stop")
            return "Thanks! Let me just confirm that on our system... ⏳"
        def no_fn():
            return start_escalation(user_id, state,
                "No problem, let me connect you with a support agent who can help "
                "directly. 😊")
        if msg == "yes":
            return yes_fn()
        elif msg == "no":
            return no_fn()
        else:
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    # ── Emergency stop — verifying the alert actually cleared (polling) ──────
    if step == "emergency_stop_verifying":
        return (
            "⏳ Still checking on the charger's status — I'll message you shortly. "
            "Thanks for your patience!"
        )

    # ── After releasing emergency stop — confirm charging actually resumed ────
    if step == "emergency_stop_replug_result":
        QUESTION = "Is it charging now?"
        def yes_fn():
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        def no_fn():
            return start_escalation(user_id, state,
                "I'm sorry that didn't resolve it. 😔\n\n"
                "Our support team will take over from here.")
        if msg == "yes":
            return yes_fn()
        elif msg == "no":
            return no_fn()
        else:
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    # ── After remote restart — vehicle not charging ───────────────────────────
    if step == "await_restart_result":
        # Ignore images at this step — just re-ask the question
        if has_media and not msg_raw.strip():
            return ("Is your vehicle now charging after the restart?\n\nReply *YES* or *NO*", None)
        charger_name = state.get("charger_name", "your charger")
        QUESTION = "Is your vehicle charging?"
        def yes_fn():
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        def no_fn():
            return start_escalation(user_id, state,
                f"I'm sorry the restart didn't resolve the issue. 😔\n\n"
                f"Our support team will take over from here.")
        if msg == "yes":
            return yes_fn()
        elif msg == "no":
            return no_fn()
        else:
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    # ── Slow charging — capturing the customer's description ─────────────────
    if step == "opt2_awaiting_description":
        description = msg_raw.strip()
        return escalate_slow_charging(user_id, state, description)

    # ── Something else ────────────────────────────────────────────────────────
    if step == "something_else":
        description  = msg_raw.strip()
        charger_name = state.get("charger_name", "")
        charger_info = f" at *{charger_name}*" if charger_name else ""

        # Check knowledge base first before escalating
        ai_result = ask_claude(description, context_hint=(
            "The bot asked the customer to describe, in their own words, "
            "the issue they are experiencing with their charger or vehicle."
        ))
        intent    = ai_result.get("intent", "unclear") if ai_result else "unclear"

        if intent == "general" and ai_result:
            ai_reply = ai_result.get("reply", "")
            media_key = ai_result.get("media")
            user_states[user_id] = {**state, "step": "something_else_followup",
                                     "extra_notes": description, "media_topic": media_key}
            video_note = "\n\n🎥 See the video below." if media_key else ""
            return (
                f"{ai_reply}{video_note}\n\n"
                "---\n"
                "Has this fixed your problem?\n\n"
                "Reply *YES* or *NO*",
                get_media(media_key) if media_key else None
            )

        # KB can't answer — escalate with description captured
        user_states[user_id] = {**state, "step": "start", "extra_notes": description}
        return start_escalation(
            user_id,
            {**state, "fault_type": "Other issue", "extra_notes": description},
            f"Thank you for describing the issue{charger_info}.\n\n"
            f"I have noted: *\"{description}\"*\n\n"
            "Let me connect you with a support agent who can help."
        )

    if step == "something_else_followup":
        QUESTION = "Has this fixed your problem?"
        def yes_fn():
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        def no_fn():
            charger_uuid = state.get("charger_uuid", "")
            charger_name = state.get("charger_name", "your charger")
            org = get_org_by_index(state.get("org_index"))
            # If this was a stuck-cable/stop-session issue, a remote restart
            # is genuinely worth trying before escalating — the same thing
            # an agent would do.
            if state.get("media_topic") == "video_how_to_stop" and charger_uuid and org:
                user_states[user_id] = {**state, "step": "something_else_restarting"}
                threading.Thread(
                    target=lambda: restart_charger(charger_uuid, org), daemon=True
                ).start()
                threading.Thread(
                    target=lambda: poll_charger_and_notify_online(
                        user_id, charger_uuid, charger_name, org,
                        next_step="something_else_after_restart_result",
                        question="Is the cable free and is your issue resolved now?",
                        fault_type="Other issue — restart attempted",
                        action_line="Please try the cable now — it should be free."
                    ),
                    daemon=True
                ).start()
                return (
                    f"No problem — let me try restarting *{charger_name}* remotely, "
                    "this often releases a stuck cable. 🔄\n\n"
                    "Please hold on for a moment while I check that it's back online. ⏳"
                )
            return start_escalation(user_id, state,
                "No problem, let me get an agent to assist you. 😊")
        if msg in ["resolved", "yes"]:
            return yes_fn()
        elif msg in ["agent", "no"]:
            return no_fn()
        else:
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    # ── Cable/stop issue — restart in progress, polling in the background ────
    if step == "something_else_restarting":
        return (
            "⏳ Still checking on the charger's status — I'll message you as soon as "
            "it's ready. Thanks for your patience!"
        )

    # ── After remote restart — cable/stop issue ───────────────────────────────
    if step == "something_else_after_restart_result":
        QUESTION = "Is the cable free and is your issue resolved now?"
        def yes_fn():
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        def no_fn():
            return start_escalation(user_id, state,
                "I'm sorry the restart didn't resolve the issue. 😔\n\n"
                "Our support team will take over from here.")
        if msg == "yes":
            return yes_fn()
        elif msg == "no":
            return no_fn()
        else:
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)



    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 1 – VEHICLE NOT CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt1_key_removed":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt1_replug_fixed"}
            return (
                "✅ Good.\n\n"
                "Please *unplug the charging cable*, wait 5 seconds and plug it back in firmly into the vehicle. Please see the video below.\n\n"
                "Has this fixed the issue? Is your vehicle charging?\n\n"
                "Reply *YES* or *NO*",
                get_media("cable_plugin")
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_removed_key_try"}
            return (
                "No problem!\n\n"
                "Please *remove the key from the ignition*, make sure the vehicle is switched off, "
                "and then try to charge again.\n\n"
                "Is your vehicle charging?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle switched off and key removed from the ignition?"

    if step == "opt1_replug_fixed":
        QUESTION = "Has this fixed the issue? Is your vehicle charging?"
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_error_check", "retries": 0}
            return (
                "Sorry to hear that. 😔\n\n"
                "Is there an *error message* on the charger screen?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            def yes_fn():
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            def no_fn():
                user_states[user_id] = {**state, "step": "opt1_error_check", "retries": 0}
                return "Sorry to hear that. 😔\n\nIs there an *error message* on the charger screen?\n\nReply *YES* or *NO*"
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    if step == "opt1_removed_key_try":
        QUESTION = "Is your vehicle charging?"
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_error_check", "retries": 0}
            return "Sorry to hear that. 😔\n\nIs there an *error message* on the charger screen?\n\nReply *YES* or *NO*"
        else:
            def yes_fn():
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            def no_fn():
                user_states[user_id] = {**state, "step": "opt1_error_check", "retries": 0}
                return "Sorry to hear that. 😔\n\nIs there an *error message* on the charger screen?\n\nReply *YES* or *NO*"
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    if step == "opt1_error_check":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt1_error_detail"}
            return (
                "📋 What does the *error message* say?\n\n"
                "Please type the error message exactly as it appears on the screen."
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_try_another_charger"}
            return (
                "No error message — understood.\n\n"
                "Is there *another charger* available at this location that you could try?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is there an error message on the charger screen?"

    if step == "opt1_error_detail":
        error_msg = msg_raw.strip()
        # Store error code in state for email notification
        state["error_code"] = error_msg
        user_states[user_id] = {**state, "step": "start"}
        # Try to extract and match a known error code (handles "76", "error 76", "error code 76")
        extracted = extract_error_code(error_msg)
        error = lookup_error_code(extracted) if extracted else lookup_error_code(error_msg)
        if error:
            return error_code_response(error)
        # Unknown error code — log and escalate
        return start_escalation(user_id, state,
            f"Thank you for that information. I have logged the error: *\"{error_msg}\"*")

    if step == "opt1_try_another_charger":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt1_other_charger_working"}
            return (
                "Great! Please try the other charger and let us know:\n\n"
                "Is your vehicle now charging on the other charger?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            return start_escalation(user_id, state,
                "No problem, we will get an agent to assist you right away.")
        else:
            return "Please reply *YES* or *NO*. Is there another charger available at this location?"

    if step == "opt1_other_charger_working":
        QUESTION = "Is your vehicle now charging on the other charger?"
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_site_name"}
            return "I'm sorry to hear that. 😔\n\nWhich *site* are you calling from? Please type the site name."
        else:
            def yes_fn():
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            def no_fn():
                user_states[user_id] = {**state, "step": "opt1_site_name"}
                return "I'm sorry to hear that. 😔\n\nWhich *site* are you calling from? Please type the site name."
            return smart_yes_no(user_id, state, msg_raw, QUESTION, yes_fn, no_fn)

    if step == "opt1_site_name":
        site = msg_raw.strip()
        updated_state = {**state, "site": site}
        user_states[user_id] = updated_state
        return start_escalation(user_id, updated_state)

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 2 – CHARGER IS OFF
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt2_power_on_site":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt2_another_charger"}
            return (
                "Okay, there is power on site. 🔍\n\n"
                "📸 *Please send a photo of the charger screen* so we can see what is displayed.\n\n"
                "Is there *another charger* available on site?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt2_no_power_site_name"}
            return (
                "It seems there may be a power outage on site. ⚠️\n\n"
                "📸 *Please send a photo of the charger screen* so we can confirm.\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is there power on site?"

    if step == "opt2_another_charger":
        # If customer sent a photo, acknowledge it first
        if has_media and not msg:
            return (
                "📸 *Thank you, we have received your photo!*\n\n"
                "Is there *another charger* available on site?\n\n"
                "Reply *YES* or *NO*"
            )
        # If photo sent with a caption like "yes" or "no", treat caption as answer
        if has_media and msg:
            pass  # falls through to YES/NO handling below
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt2_other_charger_works"}
            return (
                "Great! Please try the other charger.\n\n"
                "📸 *Please also send a photo of that charger screen.*\n\n"
                "Is the other charger working?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt2_site_name_escalate"}
            return (
                "Understood, no other charger available.\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is there another charger available on site?"

    if step == "opt2_other_charger_works":
        if has_media and not msg:
            return (
                "📸 *Thank you, we have received your photo!*\n\n"
                "Is the other charger working?\n\n"
                "Reply *YES* or *NO*"
            )
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return (
                "🎉 *Great, glad we got you sorted!*\n\n"
                "We will log a fault on the affected unit and have it investigated. ⚡\n\n"
                "Type *MENU* if you need anything else."
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt2_site_name_escalate"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is the other charger working?"

    if step in ["opt2_site_name_escalate", "opt2_no_power_site_name"]:
        site = msg_raw.strip()
        updated_state = {**state, "site": site}
        user_states[user_id] = updated_state
        return start_escalation(user_id, updated_state)

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 3 – SLOW CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt3_restart_session":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt3_still_slow"}
            return (
                "Great! Please stop the session and start it again.\n\n"
                "🎥 Watch the short video below if you need help stopping the session.\n\n"
                "Is the charging speed *still slow* after restarting?\n\n"
                "Reply *YES* or *NO*",
                get_media("video_how_to_stop")
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt3_still_slow"}
            return (
                "No problem.\n\n"
                "Is the charging speed *still slow*?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            # Check if customer is saying it's now working
            positive_phrases = ["charging now", "its charging", "it's charging", "working now",
                                 "it works", "its working", "it's working", "working", "sorted",
                                 "fixed", "resolved", "charging fine", "all good", "good now"]
            if any(phrase in msg for phrase in positive_phrases):
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            return "Please reply *YES* or *NO*. Can you stop the charging session and start it again?"

    if step == "opt3_still_slow":
        QUESTION = "Is the charging speed still slow?"
        if msg == "no":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "yes":
            user_states[user_id] = {**state, "step": "opt3_which_site"}
            return "Let's dig deeper. 🔍\n\nWhich *site* are you calling from?\n\nReply *1* for Wattspot or *2* for Other"
        else:
            positive_phrases = ["charging now", "its charging", "it's charging", "working now",
                                 "it works", "its working", "it's working", "working", "sorted",
                                 "fixed", "resolved", "charging fine", "all good", "good now",
                                 "faster now", "speed is fine", "normal now"]
            if any(phrase in msg for phrase in positive_phrases):
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            def yes_fn():
                user_states[user_id] = {**state, "step": "opt3_which_site"}
                return "Let's dig deeper. 🔍\n\nWhich *site* are you calling from?\n\nReply *1* for Wattspot or *2* for Other"
            def no_fn():
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            return smart_yes_no(user_id, state, msg_raw, QUESTION, no_fn, yes_fn)

    if step == "opt3_which_site":
        if msg in ["1", "wattspot"]:
            user_states[user_id] = {**state, "step": "opt3_wattspot_wifi"}
            return (
                "📍 *Wattspot Site*\n\n"
                "Please check the *WiFi symbol at the top of the charger* "
                "(see reference image below).\n\n"
                "Is the WiFi symbol *White* or *Red*?\n\n"
                "Reply *WHITE* or *RED*",
                get_media("wifi_symbol_wattspot")
            )
        elif msg in ["2", "other"]:
            user_states[user_id] = {**state, "step": "opt3_other_4g"}
            return (
                "📍 *Other Site*\n\n"
                "Please check the *bottom left of the charger screen* "
                "(see reference image below for what to look for).\n\n"
                "📸 Please also send a photo of your charger screen.\n\n"
                "What do you see?\n\n"
                "Reply:\n"
                "*1* – 4G symbol is greyed out\n"
                "*2* – There is a red cross on a symbol\n"
                "*3* – Everything looks normal",
                get_media("4g_symbol_other")
            )
        else:
            return "Please reply *1* for Wattspot or *2* for Other."

    # ── Wattspot Flow ─────────────────────────────────────────────────────────

    if step == "opt3_wattspot_wifi":
        if has_media and not msg:
            return (
                "Is the WiFi symbol at the top of the charger *White* or *Red*?\n\n"
                "Reply *WHITE* or *RED*"
            )
        if msg == "white":
            user_states[user_id] = {**state, "step": "opt3_wattspot_replug"}
            return (
                "✅ The charger is *online* (WiFi is white).\n\n"
                "Please *unplug the charging cable* from the vehicle, "
                "wait 30 seconds, and plug it back in firmly.\n\n"
                "Is your vehicle charging?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "red":
            return start_escalation(user_id, state,
                "🔴 The charger is *offline* (WiFi is red).\n\nOur support team will investigate this unit immediately.")
        else:
            return "Please reply *WHITE* or *RED*. What colour is the WiFi symbol on the charger?"

    if step == "opt3_wattspot_replug":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            return start_escalation(user_id, state,
                "I'm sorry the issue persists. 😔\n\nOur support team will investigate further.")
        else:
            return "Please reply *YES* or *NO*. Is your vehicle charging?"

    # ── Other Site Flow ───────────────────────────────────────────────────────

    if step == "opt3_other_4g":
        if has_media and not msg:
            return (
                "📸 *Thank you, we have received your photo!*\n\n"
                "What do you see at the bottom left of the charger screen?\n\n"
                "Reply:\n"
                "*1* – 4G symbol is greyed out\n"
                "*2* – There is a red cross on a symbol\n"
                "*3* – Everything looks normal"
            )
        if msg == "1":
            return start_escalation(user_id, state,
                "⚠️ The *4G symbol is greyed out* — the charger is offline.\n\nPlease wait while we investigate.")
        elif msg == "2":
            return start_escalation(user_id, state,
                "⚠️ A *red cross* on the symbol indicates the charger is offline.")
        elif msg == "3":
            user_states[user_id] = {**state, "step": "opt3_other_final_restart"}
            return (
                "✅ The charger appears connected.\n\n"
                "Please *restart the charging session* now.\n\n"
                "Is the charging speed still slow after restarting?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return (
                "Please reply:\n"
                "*1* – 4G symbol is greyed out\n"
                "*2* – There is a red cross on a symbol\n"
                "*3* – Everything looks normal"
            )

    if step == "opt3_other_final_restart":
        if msg == "no":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "yes":
            return start_escalation(user_id, state,
                "We are sorry the issue persists. 😔")
        else:
            return "Please reply *YES* or *NO*. Is the charging speed still slow after restarting?"

    # ══════════════════════════════════════════════════════════════════════════
    # CHARGER FAULT — gather site and charger ID then escalate
    # ══════════════════════════════════════════════════════════════════════════

    if step == "charger_fault_site":
        site = msg_raw.strip()
        site_lower = site.lower()
        user_states[user_id] = {**state, "step": "charger_fault_id",
                                 "site": site, "fault_type": "Charger not working"}

        # Give charger-type-specific instructions with matching image
        wattspot_sites = ["northgate", "fourways", "wynberg"]
        if any(s in site_lower for s in wattspot_sites):
            charger_id_hint = (
                "📍 The Charger ID sticker is on the *front of the charger, "
                "underneath the screen*. Please see the image below for reference."
            )
            media_key = "charger_id_northgate"
        else:
            charger_id_hint = (
                "📍 The Charger ID sticker is on the *front of the charger, "
                "underneath the screen*. Please see the image below for reference."
            )
            media_key = "charger_id_other"

        return (
            f"Thank you — noted that you are at *{site}*.\n\n"
            f"Which *charger* are you having issues with?\n\n"
            f"{charger_id_hint}\n\n"
            f"Please type the *Charger ID* once you have it.",
            get_media(media_key)
        )

    if step == "charger_fault_id":
        # ── QR Code Detection ─────────────────────────────────────────────────
        if has_media and received_media:
            log.info(f"📷 QR image received in charger fault ID step")
            qr_data = read_qr_code(received_media)
            if qr_data:
                charger_id = extract_charger_id_from_qr(qr_data)
                if charger_id:
                    site = state.get("site", "Unknown site")
                    user_states[user_id] = {**state, "step": "start", "charger_id": charger_id}
                    return (
                        f"✅ *QR code scanned successfully!*\n\n"
                        f"I have identified your charger:\n\n"
                        f"📍 *Site:* {site}\n"
                        f"🔌 *Charger ID:* `{charger_id}`\n\n"
                        f"Our support team will investigate immediately.\n\n"
                        f"{AGENT_INTRO}"
                    )
            return (
                "📷 I received your image but couldn't identify the charger from it.\n\n"
                "Please type the *Charger ID* manually.\n\n"
                "📍 The sticker is on the *front of the charger, underneath the screen* "
                "— see reference image below.",
                get_media("charger_id_northgate")
            )
        charger_id = msg_raw.strip()
        site = state.get("site", "Unknown site")
        user_states[user_id] = {**state, "step": "start", "charger_id": charger_id}
        return (
            f"Thank you. I have logged the following fault:\n\n"
            f"📍 *Site:* {site}\n"
            f"🔌 *Charger ID:* {charger_id}\n\n"
            f"Our support team will investigate immediately.\n\n"
            f"{AGENT_INTRO}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # PRE-ESCALATION — collect site and charger ID before connecting agent
    # ══════════════════════════════════════════════════════════════════════════

    if step == "pre_escalate_site":
        # If they send a photo here (QR/sticker), try to identify the charger
        # directly first — if it works, they can skip escalation entirely
        if has_media and received_media:
            log.info("📷 Image received while asking for site — trying QR/OCR first")
            qr_data = read_qr_code(received_media)
            if qr_data:
                charger_uuid = extract_charger_id_from_qr(qr_data)
                if charger_uuid:
                    return lookup_charger_and_respond(user_id, state, charger_uuid)
            sticker_text = read_text_from_image(received_media)
            if sticker_text:
                charger = search_charger_by_name_across_orgs(sticker_text)
                if charger:
                    charger_uuid = charger.get("id", "")
                    matched_org = get_org_by_index(charger.get("_matched_org_index"))
                    return lookup_charger_and_respond(user_id, state, charger_uuid, matched_org)
            # Couldn't read it — fall back to asking for the site as text
            return (
                "📷 I received your photo but couldn't identify the "
                "charger from it. 😔\n\n"
                "No problem — which *site or depot* are you at? I'll get our "
                "team to help identify the correct charger."
            )

        # Handle "I don't know" responses
        unknown_phrases = ["dont know", "don't know", "not sure", "unsure",
                           "no idea", "unknown", "i dont", "no clue", "cant tell"]
        if any(phrase in msg for phrase in unknown_phrases):
            if state.get("attempted_charger_id"):
                # Already have an unconfirmed attempt from earlier — don't ask again
                user_states[user_id] = {**state, "site": "Unknown", "step": "start"}
                return (
                    "No problem! 😊\n\n"
                    f"{AGENT_INTRO}"
                )
            user_states[user_id] = {**state, "step": "pre_escalate_charger_id",
                                     "site": "Unknown"}
            return (
                "No problem! 😊\n\n"
                "Can you find the *Charger ID* on the unit?\n\n"
                "📍 The sticker is on the *front of the charger, underneath the screen* "
                "— see reference image below.\n\n"
                "Please type it or send a photo of the sticker.",
                get_media("charger_id_northgate")
            )
        site = msg_raw.strip()
        if state.get("attempted_charger_id"):
            # Already have an unconfirmed attempt from earlier — don't ask again
            user_states[user_id] = {**state, "site": site, "step": "start"}
            return (
                f"Thank you — noted that you are at *{site}*.\n\n"
                f"{AGENT_INTRO}"
            )
        user_states[user_id] = {**state, "step": "pre_escalate_charger_id", "site": site}
        return (
            f"Thank you — noted that you are at *{site}*.\n\n"
            "What is the *Charger ID*?\n\n"
            "📍 The sticker is on the *front of the charger, underneath the screen* "
            "— see reference image below.\n\n"
            "Please type it or send a photo of the sticker.",
            get_media("charger_id_northgate")
        )

    if step == "pre_escalate_charger_id":
        # ── Handle images — try QR then Claude OCR ────────────────────────────
        if has_media and received_media:
            log.info("📷 Image received in charger ID step — trying QR then OCR")
            # Try QR first
            qr_data = read_qr_code(received_media)
            if qr_data:
                charger_id = extract_charger_id_from_qr(qr_data)
                if charger_id:
                    user_states[user_id] = {**state, "step": "start", "charger_id": charger_id}
                    return (
                        f"✅ *QR code scanned!*\n\n"
                        f"🔌 *Charger:* `{charger_id}`\n\n"
                        f"Connecting you to our support team now.\n\n{AGENT_INTRO}"
                    )
            # Try Claude OCR on sticker
            sticker_text = read_text_from_image(received_media)
            if sticker_text:
                # Search Ampcontrol by sticker text
                charger = search_charger_by_name_across_orgs(sticker_text)
                if charger:
                    charger_id = charger.get("id", sticker_text)
                    charger_name = charger.get("customName") or charger.get("name") or sticker_text
                    matched_org_index = charger.get("_matched_org_index")
                    user_states[user_id] = {**state, "step": "start",
                                             "charger_id": charger_id,
                                             "charger_name": charger_name,
                                             "org_index": matched_org_index}
                    return (
                        f"✅ *I found your charger from the sticker!*\n\n"
                        f"🔌 *Charger:* {charger_name}\n\n"
                        f"Connecting you to our support team now.\n\n{AGENT_INTRO}"
                    )
                # OCR read text but not found in Ampcontrol — use as charger ID
                user_states[user_id] = {**state, "step": "start", "charger_id": sticker_text}
                return (
                    f"Thank you. I read *\"{sticker_text}\"* from your image.\n\n"
                    f"Connecting you to our support team now.\n\n{AGENT_INTRO}"
                )
            # Both failed
            return (
                "📷 I received your image but couldn't read the charger details from it.\n\n"
                "Please *type the Charger ID* as shown on the sticker, or type *AGENT* "
                "to connect directly."
            )

        # ── Handle "I don't know" for charger ID ─────────────────────────────
        unknown_phrases = ["dont know", "don't know", "not sure", "unsure",
                           "no idea", "unknown", "i dont", "cant find", "no sticker"]
        if any(phrase in msg for phrase in unknown_phrases):
            user_states[user_id] = {**state, "step": "start", "charger_id": "Unknown"}
            return start_escalation(user_id, {**state, "charger_id": "Unknown"},
                "No problem! Our agent will help identify the charger. 😊")

        # ── Question/confusion detection ──────────────────────────────────────
        if any(phrase in msg for phrase in CONFUSION_PHRASES):
            return (
                "📍 The *Charger ID* sticker is on the *front of the charger, "
                "underneath the screen* — see reference image below.\n\n"
                "It is a combination of letters and numbers.\n\n"
                "💡 You can also send a *photo of the sticker* — I'll read it! 📷\n\n"
                "Or type *AGENT* to skip this and speak to someone directly.",
                get_media("charger_id_northgate")
            )

        charger_id = msg_raw.strip()
        site       = state.get("site", "Not provided")
        fault_type = state.get("fault_type", "Not specified")
        error_code = state.get("error_code", "")
        user_states[user_id] = {**state, "step": "start", "charger_id": charger_id}
        error_line = f"🔴 *Error Code:* {error_code}\n" if error_code else ""
        return (
            f"Thank you. I have logged the following details:\n\n"
            f"📍 *Site:* {site}\n"
            f"🔌 *Charger ID:* {charger_id}\n"
            f"⚠️ *Fault:* {fault_type}\n"
            f"{error_line}\n"
            f"Connecting you to our support team now.\n\n"
            f"{AGENT_INTRO}"
        )

    # ── Default Fallback ──────────────────────────────────────────────────────
    return FALLBACK


# ── Media File Server ─────────────────────────────────────────────────────────
import mimetypes
from pathlib import Path

MEDIA_DIR = Path(__file__).parent / "media"

@app.route("/media/<path:filename>")
def serve_media(filename):
    """Serves media files directly from the media folder."""
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        log.warning(f"Media file not found: {filename}")
        return "File not found", 404
    mime_type, _ = mimetypes.guess_type(str(file_path))
    mime_type = mime_type or "application/octet-stream"
    log.info(f"Serving media: {filename} ({mime_type})")
    with open(file_path, "rb") as f:
        from flask import Response
        return Response(f.read(), mimetype=mime_type)


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming        = request.form.get("Body", "").strip()
    sender          = request.form.get("From", "unknown")
    num_media       = int(request.form.get("NumMedia", "0"))
    has_media       = num_media > 0
    received_media  = request.form.get("MediaUrl0", "") if has_media else ""

    # ── Agent command handling ────────────────────────────────────────────────
    if sender in AGENT_NUMBERS:
        log.info(f"Agent command from {AGENT_NUMBERS[sender]}: {incoming}")
        response_text = handle_agent_command(sender, incoming)
        resp = MessagingResponse()
        resp.message(response_text)
        return str(resp)

    # ── Paused customer — bot stays silent ───────────────────────────────────
    if is_paused(sender):
        log.info(f"Bot paused for {sender} — message ignored")
        return str(MessagingResponse())  # Empty response — bot stays silent

    # ── Normal customer flow ──────────────────────────────────────────────────
    result = handle_message(sender, incoming, has_media=has_media, received_media=received_media)

    if isinstance(result, tuple):
        response_text, media_url = result
    else:
        response_text, media_url = result, None

    # ── Escalation detected — notify agents ──────────────────────────────────
    if "Connecting you to a support agent" in response_text:
        state = user_states.get(sender, {})
        # Send email notification
        send_escalation_email(
            customer_number = sender.replace("whatsapp:", ""),
            fault_type      = state.get("fault_type", "Not specified"),
            site            = state.get("site"),
            charger_id      = state.get("charger_id") or state.get("charger_uuid"),
            error_code      = state.get("error_code"),
            extra_notes     = state.get("extra_notes")
        )
        # Send WhatsApp notification to all agents
        notify_agents(sender, state)

    # ── Build TwiML response ──────────────────────────────────────────────────
    resp = MessagingResponse()
    resp.message(response_text)
    if media_url:
        media_msg = resp.message()
        media_msg.media(media_url)
        is_video = media_url.lower().endswith((".mp4", ".mp4.mp4", ".mov"))
        log.info(f"📎 Sending {'video' if is_video else 'image'} (no caption): {media_url}")

    return str(resp)


@app.route("/", methods=["GET"])
def health_check():
    ai_status     = "configured" if ANTHROPIC_API_KEY else "NOT configured"
    email_status  = "configured" if RESEND_API_KEY else "NOT configured"
    twilio_status = "configured" if TWILIO_ACCOUNT_SID else "NOT configured"
    kb_count      = len(KNOWLEDGE_BASE.get("faqs", []))
    agent_count   = len(AGENT_NUMBERS)
    paused_count  = len(paused_customers)
    return (
        f"AE-Ace Bot is running ✅\n"
        f"Claude AI: {ai_status} | Email: {email_status} | "
        f"Twilio Agents: {twilio_status} | KB: {kb_count} FAQs | "
        f"Agents: {agent_count} | Paused customers: {paused_count}"
    )



# Start session timeout checker (runs on both Render and local)
start_timeout_checker()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
