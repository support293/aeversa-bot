import os
import json
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ── Session State ─────────────────────────────────────────────────────────────
# Tracks conversation state per user phone number
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

# ── Claude AI – Intent Classification & General Q&A ──────────────────────────

AE_SYSTEM_PROMPT = """You are AE, the WhatsApp support assistant for Aeversa (PTY) Ltd, a South African EV charge point operator.

Your job right now is to read a customer's free-text WhatsApp message and decide what they need. You must respond with ONLY a JSON object (no other text, no markdown fences) in this exact format:

{"intent": "...", "reply": "..."}

Where "intent" is ONE of:
- "not_charging" — if the customer's vehicle is not charging
- "charger_off" — if the charger itself appears offline/off/dead/no screen
- "slow_charging" — if charging is happening but slower than expected
- "agent" — if the customer explicitly wants a human, or has a billing/account/complaint issue unrelated to a technical fault
- "general" — if it's a general question you can answer directly (e.g. "what are your hours", "where are your chargers", "how does charging work", "what is the price")
- "greeting" — if it's just a greeting with no specific issue
- "unclear" — if you genuinely cannot tell what they want

If intent is "not_charging", "charger_off", "slow_charging", or "agent", set "reply" to a short one-sentence acknowledgment (e.g. "It sounds like your vehicle isn't charging — let's sort this out.").

If intent is "general", set "reply" to a helpful, friendly, concise answer (2-4 sentences max) using ONLY this company info:
- Aeversa operates EV charge points across South Africa
- Support hours: Monday-Friday 07:00-19:00, Saturday 08:00-14:00
- Customers pay per kWh consumed, billed via the Aeversa app
- For account/billing/app issues, suggest emailing support@aeversa.co.za
- Do not invent specific prices, locations, or details you don't know — if unsure, say you'll connect them with a support agent who can confirm

If intent is "greeting", set "reply" to "" (empty string).
If intent is "unclear", set "reply" to "" (empty string).

Be warm, concise, and use a friendly South African tone. Never make up technical specifications, pricing, or site details you don't have."""


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
                "max_tokens": 300,
                "system": AE_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": message_text}],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        if "intent" in parsed:
            return parsed
        return None
    except Exception as e:
        print(f"Claude API error: {e}")
        return None


# ── State Machine ─────────────────────────────────────────────────────────────

def handle_message(user_id: str, msg_raw: str) -> str:
    msg = msg_raw.strip().lower()
    state = user_states.get(user_id, {"step": "start"})
    step = state.get("step", "start")

    # ── Global Commands ───────────────────────────────────────────────────────
    if msg in ["menu", "start"]:
        user_states[user_id] = {"step": "start"}
        return GREETING

    if msg in ["agent", "human", "person", "speak to someone"] and step == "start":
        user_states[user_id] = {"step": "start"}
        return AGENT_INTRO

    # ── MENU / START STEP ─────────────────────────────────────────────────────
    if step == "start":
        # Direct number selections (fast path, no AI needed)
        if msg == "1":
            user_states[user_id] = {"step": "opt1_key_removed"}
            return (
                "🔴 *Vehicle Not Charging*\n\n"
                "Let's get this sorted! First things first:\n\n"
                "Is your vehicle switched off and the key removed from the ignition?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "2":
            user_states[user_id] = {"step": "opt2_power_on_site"}
            return (
                "⚫ *Charger is Off*\n\n"
                "Let's investigate! 🔍\n\n"
                "Is there power on site? (e.g. are lights or other appliances working?)\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "3":
            user_states[user_id] = {"step": "opt3_restart_session"}
            return (
                "🐢 *Slow Charging*\n\n"
                "Let's get your speed up! ⚡\n\n"
                "Can you stop the charging session and start it again?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "4":
            user_states[user_id] = {"step": "start"}
            return AGENT_INTRO

        # Greetings — quick local match, no need to call AI
        if msg in ["hi", "hello", "hey", "hiya", "howzit", "good morning", "good afternoon", "good evening"]:
            user_states[user_id] = {"step": "start"}
            return GREETING

        # ── Free text — ask Claude to understand intent ─────────────────────
        ai_result = ask_claude(msg_raw)

        if ai_result is None:
            # AI unavailable or failed — fall back gracefully
            return GREETING

        intent = ai_result.get("intent", "unclear")
        ai_reply = ai_result.get("reply", "")

        if intent == "not_charging":
            user_states[user_id] = {"step": "opt1_key_removed"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}🔴 *Vehicle Not Charging*\n\n"
                "Is your vehicle switched off and the key removed from the ignition?\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "charger_off":
            user_states[user_id] = {"step": "opt2_power_on_site"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}⚫ *Charger is Off*\n\n"
                "Is there power on site? (e.g. are lights or other appliances working?)\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "slow_charging":
            user_states[user_id] = {"step": "opt3_restart_session"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return (
                f"{prefix}🐢 *Slow Charging*\n\n"
                "Can you stop the charging session and start it again?\n\n"
                "Reply *YES* or *NO*"
            )
        elif intent == "agent":
            user_states[user_id] = {"step": "start"}
            prefix = f"{ai_reply}\n\n" if ai_reply else ""
            return f"{prefix}{AGENT_INTRO}"
        elif intent == "general":
            user_states[user_id] = {"step": "start"}
            return (
                f"{ai_reply}\n\n"
                "Is there anything else I can help with? Type *MENU* to see support options."
            )
        elif intent == "greeting":
            user_states[user_id] = {"step": "start"}
            return GREETING
        else:  # unclear
            return FALLBACK

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 1 – VEHICLE NOT CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt1_key_removed":
        if msg == "yes":
            user_states[user_id] = {"step": "opt1_replug_fixed"}
            return (
                "✅ Good.\n\n"
                "Please *unplug the charging cable* and plug it back in firmly on both ends.\n\n"
                "Has this fixed the issue? Is your vehicle now charging?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {"step": "opt1_removed_key_try"}
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
            user_states[user_id] = {"step": "opt1_error_check"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Is there an *error message* on the charger screen?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle now charging?"

    if step == "opt1_removed_key_try":
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "no":
            user_states[user_id] = {"step": "opt1_error_check"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Is there an *error message* on the charger screen?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle now charging?"

    if step == "opt1_error_check":
        if msg == "yes":
            user_states[user_id] = {"step": "opt1_error_detail"}
            return (
                "📋 What does the *error message* say?\n\n"
                "Please type the error message exactly as it appears on the screen."
            )
        elif msg == "no":
            user_states[user_id] = {"step": "opt1_try_another_charger"}
            return (
                "No error message — understood.\n\n"
                "Is there *another charger* available at this location that you could try?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *YES* or *NO*. Is there an error message on the charger screen?"

    if step == "opt1_error_detail":
        error_msg = msg_raw.strip()
        user_states[user_id] = {"step": "start"}
        return (
            f"Thank you for that information. I have logged the error: *\"{error_msg}\"*\n\n"
            f"{AGENT_INTRO}"
        )

    if step == "opt1_try_another_charger":
        if msg == "yes":
            user_states[user_id] = {"step": "opt1_other_charger_working"}
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
            user_states[user_id] = {"step": "opt1_site_name"}
            return (
                "I'm sorry to hear that. 😔\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is your vehicle charging on the other charger?"

    if step == "opt1_site_name":
        site = msg_raw.strip()
        user_states[user_id] = {"step": "start"}
        return (
            f"Thank you. I have logged your location as *\"{site}\"*.\n\n"
            f"{AGENT_INTRO}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 2 – CHARGER IS OFF
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt2_power_on_site":
        if msg == "yes":
            user_states[user_id] = {"step": "opt2_another_charger"}
            return (
                "Okay, there is power on site. 🔍\n\n"
                "📸 *Please send a photo of the charger screen* so we can see what is displayed.\n\n"
                "Is there *another charger* available on site?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {"step": "opt2_no_power_site_name"}
            return (
                "It seems there may be a power outage on site. ⚠️\n\n"
                "📸 *Please send a photo of the charger screen* so we can confirm.\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is there power on site?"

    if step == "opt2_another_charger":
        if msg == "yes":
            user_states[user_id] = {"step": "opt2_other_charger_works"}
            return (
                "Great! Please try the other charger.\n\n"
                "📸 *Please also send a photo of that charger screen.*\n\n"
                "Is the other charger working?\n\n"
                "Reply *YES* or *NO*"
            )
        elif msg == "no":
            user_states[user_id] = {"step": "opt2_site_name_escalate"}
            return (
                "Understood, no other charger available.\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is there another charger available on site?"

    if step == "opt2_other_charger_works":
        if msg == "yes":
            user_states[user_id] = {"step": "start"}
            return (
                "🎉 *Great, glad we got you sorted!*\n\n"
                "We will log a fault on the affected unit and have it investigated. ⚡\n\n"
                "Type *MENU* if you need anything else."
            )
        elif msg == "no":
            user_states[user_id] = {"step": "opt2_site_name_escalate"}
            return (
                "Sorry to hear that. 😔\n\n"
                "Which *site* are you calling from? Please type the site name."
            )
        else:
            return "Please reply *YES* or *NO*. Is the other charger working?"

    if step in ["opt2_site_name_escalate", "opt2_no_power_site_name"]:
        site = msg_raw.strip()
        user_states[user_id] = {"step": "start"}
        return (
            f"Thank you. I have logged your location as *\"{site}\"*.\n\n"
            f"{AGENT_INTRO}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # OPTION 3 – SLOW CHARGING
    # ══════════════════════════════════════════════════════════════════════════

    if step == "opt3_restart_session":
        if msg in ["yes", "no"]:
            user_states[user_id] = {"step": "opt3_still_slow"}
            if msg == "yes":
                return (
                    "Great! Please stop the session and start it again.\n\n"
                    "Is the charging speed *still slow* after restarting?\n\n"
                    "Reply *YES* or *NO*"
                )
            else:
                return (
                    "No problem.\n\n"
                    "Is the charging speed *still slow*?\n\n"
                    "Reply *YES* or *NO*"
                )
        else:
            return "Please reply *YES* or *NO*. Can you stop the charging session and start it again?"

    if step == "opt3_still_slow":
        if msg == "no":
            user_states[user_id] = {"step": "start"}
            return GREAT_NEWS
        elif msg == "yes":
            user_states[user_id] = {"step": "opt3_which_site"}
            return (
                "Let's dig deeper. 🔍\n\n"
                "Which *site* are you calling from?\n\n"
                "Reply *1* for Wattspot or *2* for Other"
            )
        else:
            return "Please reply *YES* or *NO*. Is the charging speed still slow?"

    if step == "opt3_which_site":
        if msg in ["1", "wattspot"]:
            user_states[user_id] = {"step": "opt3_wattspot_wifi"}
            return (
                "📍 *Wattspot Site*\n\n"
                "Please check the *WiFi symbol at the top of the charger.*\n\n"
                "📸 Please send a photo of the charger screen.\n\n"
                "Is the WiFi symbol *White* or *Red*?\n\n"
                "Reply *WHITE* or *RED*"
            )
        elif msg in ["2", "other"]:
            user_states[user_id] = {"step": "opt3_other_4g"}
            return (
                "📍 *Other Site*\n\n"
                "Please check the *bottom left of the charger screen.*\n\n"
                "📸 Please send a photo of the charger screen.\n\n"
                "What do you see?\n\n"
                "Reply:\n"
                "*1* – 4G symbol is greyed out\n"
                "*2* – There is a red cross on a symbol\n"
                "*3* – Everything looks normal"
            )
        else:
            return "Please reply *1* for Wattspot or *2* for Other."

    # ── Wattspot Flow ─────────────────────────────────────────────────────────

    if step == "opt3_wattspot_wifi":
        if msg == "white":
            user_states[user_id] = {"step": "start"}
            return (
                "✅ The charger is *online* (WiFi is white).\n\n"
                "Thank you for the photo. We are escalating this to our technical team.\n\n"
                f"{AGENT_INTRO}"
            )
        elif msg == "red":
            user_states[user_id] = {"step": "opt3_wattspot_after_wait"}
            return (
                "🔴 The charger appears to be *offline* (WiFi is red).\n\n"
                "Please *wait 5 minutes* and check the WiFi symbol again.\n\n"
                "After 5 minutes, is the WiFi symbol now *White* or still *Red*?\n\n"
                "Reply *WHITE* or *RED*"
            )
        else:
            return "Please reply *WHITE* or *RED*. What colour is the WiFi symbol on the charger?"

    if step == "opt3_wattspot_after_wait":
        if msg == "red":
            user_states[user_id] = {"step": "start"}
            return (
                "🔴 The charger is still offline.\n\n"
                "Please *contact us again* once the WiFi signal changes to white, "
                "or our team will investigate the unit.\n\n"
                f"{AGENT_INTRO}"
            )
        elif msg == "white":
            user_states[user_id] = {"step": "opt3_wattspot_final_restart"}
            return (
                "✅ Great, the charger is back online!\n\n"
                "Please *stop the charging session and start it again.*\n\n"
                "Is the charging speed still slow?\n\n"
                "Reply *YES* or *NO*"
            )
        else:
            return "Please reply *WHITE* or *RED*. What colour is the WiFi symbol now?"

    if step == "opt3_wattspot_final_restart":
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
            return "Please reply *YES* or *NO*. Is the charging speed still slow?"

    # ── Other Site Flow ───────────────────────────────────────────────────────

    if step == "opt3_other_4g":
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
            user_states[user_id] = {"step": "opt3_other_final_restart"}
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

    # ── Default Fallback ──────────────────────────────────────────────────────
    return FALLBACK


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    sender = request.form.get("From", "unknown")
    resp = MessagingResponse()
    resp.message(handle_message(sender, incoming))
    return str(resp)


@app.route("/", methods=["GET"])
def health_check():
    ai_status = "configured" if ANTHROPIC_API_KEY else "NOT configured"
    return f"AE Bot is running. Claude AI: {ai_status}"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
