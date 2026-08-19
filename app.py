import os
import json
import requests
import threading
import logging
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AE] %(levelname)s: %(message)s",
    force=True
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Email configuration — set RESEND_API_KEY in Render environment variables
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SUPPORT_EMAIL  = "support@aeversa.com"
FROM_EMAIL     = "AE Support Bot <onboarding@resend.dev>"  # Update to your domain once verified

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
        if charger_id:  rows.append(("🔌 Charger ID", charger_id))
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
              Automatically generated by AE — Aeversa WhatsApp Support Bot
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
                "subject": f"🔴 AE Escalation — {fault_type or 'Fault'} | {site or 'Unknown site'} | {timestamp}",
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
    "My name is *AE* and I am here to get you charged up. ⚡\n\n"
    "To help you get back on the road, please type one of the options below, "
    "or just describe your problem in your own words and I'll help you out:\n\n"
    "🔴 *1* – My vehicle is not charging\n"
    "⚫ *2* – The charger is off\n"
    "🐢 *3* – The charging speed is slow\n"
    "👤 *4* – Speak to a support agent\n\n"
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
    "📧 *support@aeversa.co.za*"
)

FALLBACK = (
    "🤔 I didn't quite understand that.\n\n"
    "Please type *MENU* to see the options again, or type *AGENT* to speak to a support agent."
)

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

AE_SYSTEM_PROMPT = f"""You are AE, the WhatsApp support assistant for Aeversa (PTY) Ltd, a South African EV fleet charge point operator.

Your job is to read a customer's free-text WhatsApp message and decide what they need. You must respond with ONLY a JSON object (no other text, no markdown fences) in this exact format:

{{"intent": "...", "reply": "..."}}

Where "intent" is ONE of:
- "not_charging" — ONLY use this if the vehicle is plugged in but the charging SESSION is not starting or the vehicle is not receiving charge. Do NOT use this for stuck cables, error messages, or how-to questions.
- "charger_off" — if the charger screen is blank, off, or the unit appears to have no power
- "slow_charging" — if a charging session IS active but the speed is slower than expected
- "charger_fault" — if the customer says the charger is not working, broken, faulty, has a problem, or is giving issues in a general sense without specifying vehicle not charging, screen off, or slow speed
- "agent" — if the customer explicitly wants a human agent, or has an account/complaint issue that cannot be resolved with FAQ information
- "sales" — if the customer is asking about new charger installations, fleet expansion, partnerships, or business pricing
- "general" — use this for ANY question that can be answered using the FAQ knowledge base below, including: how to start a session, how to stop a session, stuck cables, error messages on screen, VIN start process, red tick questions, how to register a vehicle, session reports, and any other how-to or informational question
- "greeting" — if it is purely a greeting with no specific issue mentioned
- "unclear" — only if you genuinely cannot determine what the customer needs after careful reading

IMPORTANT ROUTING RULES:
- "Charger is not working" / "charger is broken" / "charger has a problem" / "charger giving issues" = "charger_fault"
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

If intent is "agent", set "reply" to a short one-sentence acknowledgment.

If intent is "sales", set "reply" to "" (empty string).

If intent is "general", set "reply" to a helpful, friendly, concise answer (2-4 sentences max) using ONLY the FAQ knowledge base below. If the knowledge base does not contain the answer, do NOT make one up — set intent to "agent" instead and reply with a short acknowledgment that you will connect them to someone who can help.

If intent is "greeting" or "unclear", set "reply" to "" (empty string).

Be warm, concise, and professional. Use a friendly South African tone. Never invent technical details not found in the knowledge base.

─────────────────────────────
{KB_TEXT}
─────────────────────────────
"""


def ask_claude(message_text: str):
    """Calls Claude to classify intent and optionally generate a reply.
    Returns dict: {"intent": str, "reply": str} or None on failure."""
    if not ANTHROPIC_API_KEY:
        return None

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
                "messages": [{"role": "user", "content": message_text}],
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

def handle_message(user_id: str, msg_raw: str, has_media: bool = False) -> tuple[str, str | None]:
    msg = msg_raw.strip().lower()
    state = user_states.get(user_id, {"step": "start"})
    step = state.get("step", "start")

    # ── Global Commands — these work from ANY step ────────────────────────────
    # Greetings always reset the conversation regardless of current state
    if msg in ["menu", "start", "hi", "hello", "hey", "hiya", "howzit",
               "good morning", "good afternoon", "good evening", "good day"]:
        user_states[user_id] = {"step": "start"}
        return GREETING

    # Agent request works from any step
    if msg in ["agent", "human", "person", "speak to someone", "4"]:
        user_states[user_id] = {"step": "start"}
        return AGENT_INTRO

    # Sales request works from any step
    if msg in ["sales", "sales rep", "sales agent"]:
        user_states[user_id] = {"step": "start"}
        return sales_redirect_message()

    # ── MENU / START STEP ─────────────────────────────────────────────────────
    if step == "start":
        if msg == "1":
            user_states[user_id] = {**state, "step": "opt1_key_removed", "fault_type": "Vehicle not charging"}
            return (
                "🔴 *Vehicle Not Charging*\n\n"
                "Let's get this sorted! First things first:\n\n"
                "Is your vehicle switched off and the key removed from the ignition?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "2":
            user_states[user_id] = {**state, "step": "opt2_power_on_site", "fault_type": "Charger is off"}
            return (
                "⚫ *Charger is Off*\n\n"
                "Let's investigate! 🔍\n\n"
                "Is there power on site? (e.g. are lights or other appliances working?)\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "3":
            user_states[user_id] = {**state, "step": "opt3_restart_session", "fault_type": "Slow charging"}
            return (
                "🐢 *Slow Charging*\n\n"
                "Let's get your speed up! ⚡\n\n"
                "Can you stop the charging session and start it again?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "4":
            user_states[user_id] = {"step": "start"}
            return AGENT_INTRO

        # ── Direct error code lookup ──────────────────────────────────────────
        # Handles: "76", "error 76", "error code 76", "err 76"
        extracted_code = extract_error_code(msg.strip())
        if extracted_code:
            error = lookup_error_code(extracted_code)
            if error:
                user_states[user_id] = {"step": "start"}
                return error_code_response(error)

        # ── Free text — ask Claude to understand intent ─────────────────────
        ai_result = ask_claude(msg_raw)

        if ai_result is None:
            # Claude unavailable or timed out — show menu with helpful message
            return (
                "👋 Hi! I'm AE, the Aeversa support assistant.\n\n"
                "Please select one of the options below and I'll get you sorted:\n\n"
                "🔴 *1* – My vehicle is not charging\n"
                "⚫ *2* – The charger is off\n"
                "🐢 *3* – The charging speed is slow\n"
                "👤 *4* – Speak to a support agent\n\n"
                "Simply type the number of your issue."
            )

        intent = ai_result.get("intent", "unclear")
        ai_reply = ai_result.get("reply", "")

        if intent == "not_charging":
            user_states[user_id] = {**state, "step": "opt1_key_removed"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}🔴 *Vehicle Not Charging*\n\n"
                "Is your vehicle switched off and the key removed from the ignition?\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "charger_off":
            user_states[user_id] = {**state, "step": "opt2_power_on_site"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}⚫ *Charger is Off*\n\n"
                "Is there power on site? (e.g. are lights or other appliances working?)\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "slow_charging":
            user_states[user_id] = {**state, "step": "opt3_restart_session"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}🐢 *Slow Charging*\n\n"
                "Can you stop the charging session and start it again?\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "charger_fault":
            user_states[user_id] = {**state, "step": "charger_fault_site"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}⚠️ *Charger Fault Reported*\n\n"
                "I'm sorry to hear that. Let me gather some information so our support team can assist you.\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        elif intent == "agent":
            user_states[user_id] = {"step": "start"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return f"{prefix}{AGENT_INTRO}"
        elif intent == "sales":
            user_states[user_id] = {"step": "start"}
            return sales_redirect_message()
        elif intent == "general":
            user_states[user_id] = {"step": "start"}
            return (
                f"{ai_reply}\n\n"
                "Is there anything else I can help with? Type *MENU* to see support options."
            )
        elif intent == "greeting":
            user_states[user_id] = {"step": "start"}
            return GREETING
        else:
            return FALLBACK

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 1 – VEHICLE NOT CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt1_key_removed":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt1_replug_fixed"}
            return (
                "✅ Good.\n\n"
                "Please *unplug the charging cable*, wait 5 seconds and plug it back in firmly into the vehicle. Please see the video below.\n\n"
                "Has this fixed the issue? Is your vehicle now charging?\n\n"
                "Reply *YES* or *NO*",
                get_media("cable_plugin")
            )
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_removed_key_try"}
            return (
                "No problem!\n\n"
                "Please *remove the key from the ignition*, make sure the vehicle is switched off, "
                "and then try to charge again.\n\n"
                "Is your vehicle now charging?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle switched off and key removed from the ignition?"

    if step == "opt1_replug_fixed":
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_error_check"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Is there an *error message* on the charger screen?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            # Customer typed something other than YES/NO — check with Claude
            ai_result = ask_claude(msg_raw)
            if ai_result and ai_result.get("intent") == "general":
                user_states[user_id] = {"step": "start"}
                return (
                    f"{ai_result.get('reply', '')}\n\n"
                    "Type *MENU* to go back to the main options."
                )
            return "Please reply *YES* or *NO*. Is your vehicle now charging?"

    if step == "opt1_removed_key_try":
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_error_check"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Is there an *error message* on the charger screen?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            ai_result = ask_claude(msg_raw)
            if ai_result and ai_result.get("intent") == "general":
                user_states[user_id] = {"step": "start"}
                return (
                    f"{ai_result.get('reply', '')}\n\n"
                    "Type *MENU* to go back to the main options."
                )
            return "Please reply *YES* or *NO*. Is your vehicle now charging?"

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
        return (
            f"Thank you for that information. I have logged the error: *\"{error_msg}\"*\n\n"
            f"{AGENT_INTRO}"
        )

    if step == "opt1_try_another_charger":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt1_other_charger_working"}
            return (
                "Great! Please try the other charger and let us know:\n\n"
                "Is your vehicle now charging on the other charger?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {"step": "start"}
            return (
                "No problem, we will get an agent to assist you right away.\n\n"
                f"{AGENT_INTRO}"
            )
        else:
            return "Please reply *YES* or *NO*. Is there another charger available at this location?"

    if step == "opt1_other_charger_working":
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "opt1_site_name"}
            return (
                "I'm sorry to hear that. 😔\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle charging on the other charger?"

    if step == "opt1_site_name":
        site = msg_raw.strip()
        user_states[user_id] = {**state, "step": "start", "site": site}
        return (
            f"Thank you. I have logged your location as *\"{site}\"*.\n\n"
            f"{AGENT_INTRO}"
        )

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
        user_states[user_id] = {**state, "step": "start", "site": site}
        return (
            f"Thank you. I have logged your location as *\"{site}\"*.\n\n"
            f"{AGENT_INTRO}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 3 – SLOW CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt3_restart_session":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "opt3_still_slow"}
            return (
                "Great! Please stop the session and start it again.\n\n"
                "🎥 Watch the short video above if you need help stopping the session.\n\n"
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
        if msg == "no":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "yes":
            user_states[user_id] = {**state, "step": "opt3_which_site"}
            return (
                "Let's dig deeper. 🔍\n\n"
                "Which *site* are you calling from?\n\n"
                "Reply *1* for Wattspot or *2* for Other"
            )
        else:
            # Check if customer is saying it's now working
            positive_phrases = ["charging now", "its charging", "it's charging", "working now",
                                 "it works", "its working", "it's working", "working", "sorted",
                                 "fixed", "resolved", "charging fine", "all good", "good now",
                                 "faster now", "speed is fine", "normal now"]
            if any(phrase in msg for phrase in positive_phrases):
                user_states[user_id] = {"step": "start"}
                return GREAT_NEWS
            return "Please reply *YES* or *NO*. Is the charging speed still slow?"

    if step == "opt3_which_site":
        if msg in ["1", "wattspot"]:
            user_states[user_id] = {**state, "step": "opt3_wattspot_wifi"}
            return (
                "📍 *Wattspot Site*\n\n"
                "Please check the *WiFi symbol at the top of the charger.*\n\n"
                "Is the WiFi symbol *White* or *Red*?\n\n"
                "Reply *WHITE* or *RED*",
                get_media("wifi_symbol_wattspot")
            )
        elif msg in ["2", "other"]:
            user_states[user_id] = {**state, "step": "opt3_other_4g"}
            return (
                "📍 *Other Site*\n\n"
                "Please check the *bottom left of the charger screen.*\n\n"
                "📸 Please send a photo of the charger screen.\n\n"
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
                "Is your vehicle now charging?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "red":
            user_states[user_id] = {**state, "step": "start"}
            return (
                "🔴 The charger is *offline* (WiFi is red).\n\n"
                "Our support team will investigate this unit immediately.\n\n"
                f"{AGENT_INTRO}"
            )
        else:
            return "Please reply *WHITE* or *RED*. What colour is the WiFi symbol on the charger?"

    if step == "opt3_wattspot_replug":
        if msg == "yes":
            user_states[user_id] = {**state, "step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {**state, "step": "start"}
            return (
                "I'm sorry the issue persists. 😔\n\n"
                "Our support team will investigate further.\n\n"
                f"{AGENT_INTRO}"
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle now charging?"

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
            user_states[user_id] = {"step": "start"}
            return (
                "⚠️ The *4G symbol is greyed out* — the charger is offline.\n\n"
                "Please wait while we investigate.\n\n"
                f"{AGENT_INTRO}"
            )
        elif msg == "2":
            user_states[user_id] = {"step": "start"}
            return (
                "⚠️ A *red cross* on the symbol indicates the charger is offline.\n\n"
                f"{AGENT_INTRO}"
            )
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
            user_states[user_id] = {"step": "start"}
            return (
                "We are sorry the issue persists. 😔\n\n"
                f"{AGENT_INTRO}"
            )
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
            f"Please type the *Charger ID* once you have it. Please see the image below.",
            get_media(media_key)
        )

    if step == "charger_fault_id":
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
    incoming  = request.form.get("Body", "").strip()
    sender    = request.form.get("From", "unknown")
    has_media = request.form.get("NumMedia", "0") != "0"

    result = handle_message(sender, incoming, has_media=has_media)

    # handle_message returns either a string or a (text, media_url) tuple
    if isinstance(result, tuple):
        response_text, media_url = result
    else:
        response_text, media_url = result, None

    # ── Auto-detect escalation and send support email ─────────────────────────
    if "Connecting you to a support agent" in response_text:
        state = user_states.get(sender, {})
        send_escalation_email(
            customer_number = sender.replace("whatsapp:", ""),
            fault_type      = state.get("fault_type", "Not specified"),
            site            = state.get("site"),
            charger_id      = state.get("charger_id"),
            error_code      = state.get("error_code"),
            extra_notes     = state.get("extra_notes")
        )

    # ── Build TwiML response ──────────────────────────────────────────────────
    resp = MessagingResponse()

    if media_url:
        # Send text instruction first, then media as a separate message
        # This ensures both are clearly visible on WhatsApp
        resp.message(response_text)
        media_msg = resp.message("👆 *Watch the guide above*")
        media_msg.media(media_url)
        log.info(f"📎 Sending media: {media_url}")
    else:
        resp.message(response_text)

    return str(resp)


@app.route("/", methods=["GET"])
def health_check():
    ai_status    = "configured" if ANTHROPIC_API_KEY else "NOT configured"
    email_status = "configured" if RESEND_API_KEY else "NOT configured"
    kb_count = len(KNOWLEDGE_BASE.get("faqs", []))
    return (f"AE Bot is running. Claude AI: {ai_status}. "
            f"Email: {email_status}. Knowledge base: {kb_count} FAQs loaded.")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
