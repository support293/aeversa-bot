from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ── FAQ Knowledge Base ──────────────────────────────────────────────────────
FAQ = {
    "not charging": {
        "keywords": ["not charging", "won't charge", "wont charge", "not working", "no power", "vehicle not charging", "car not charging"],
        "response": (
            "⚡ *My vehicle is not charging – here's what to try:*\n\n"
            "1️⃣ *Check the connector* – Make sure the cable is firmly plugged into both the charger and your vehicle.\n"
            "2️⃣ *Check your vehicle* – Ensure your car's charge port is open and not locked.\n"
            "3️⃣ *Restart the session* – Stop the session on the app or charger, wait 30 seconds, then start again.\n"
            "4️⃣ *Check your account* – Make sure you have sufficient balance or an active subscription.\n"
            "5️⃣ *Try a different connector* – If available, try another connector on the same unit.\n\n"
            "Still not working? Reply *AGENT* to speak to a support agent. 🙏"
        )
    },
    "charger off": {
        "keywords": ["charger off", "charger is off", "unit off", "screen off", "charger not on", "charger dead", "no screen", "offline"],
        "response": (
            "🔌 *The charger appears to be off – here's what to do:*\n\n"
            "1️⃣ *Check for a power outage* – Look for any load shedding or local power issues in the area.\n"
            "2️⃣ *Check the charger screen* – If the screen is completely blank, the unit may have lost power.\n"
            "3️⃣ *Wait 2–3 minutes* – Some units automatically reboot after a power interruption.\n"
            "4️⃣ *Check our status page* – Visit *aeversa.co.za/status* for any known outages.\n"
            "5️⃣ *Report the fault* – Let us know the *charger ID* (found on the unit sticker) so we can investigate.\n\n"
            "Reply with your *Charger ID* and we will log a fault immediately. 🙏"
        )
    },
    "slow charging": {
        "keywords": ["slow", "slow charging", "charging slowly", "taking long", "low speed", "trickle", "not fast", "speed"],
        "response": (
            "🐢 *Charging speed is slow – here's why and what to do:*\n\n"
            "1️⃣ *Check the charger type* – AC chargers (7–22kW) are slower than DC fast chargers (50kW+). Check the label on the unit.\n"
            "2️⃣ *Check your vehicle settings* – Some vehicles limit charging speed. Check your car's charge settings.\n"
            "3️⃣ *Battery temperature* – If it's very hot or cold, your vehicle may automatically reduce charging speed to protect the battery.\n"
            "4️⃣ *Peak hours* – During peak times, some networks manage load which can affect speed.\n"
            "5️⃣ *Cable quality* – Ensure you are using the correct cable rated for the charger's output.\n\n"
            "If the speed seems abnormally low, reply *AGENT* and share your *Charger ID* and we will investigate. 🙏"
        )
    }
}

GREETING_KEYWORDS = ["hi", "hello", "hey", "good morning", "good afternoon", "good evening", "howzit", "hiya"]
AGENT_KEYWORDS = ["agent", "human", "person", "help", "support", "speak to someone"]

GREETING = (
    "👋 Hello! Welcome to *Aeversa (PTY) Ltd* EV Charging Support.\n\n"
    "I'm your virtual assistant. How can I help you today?\n\n"
    "Please describe your issue or choose from the options below:\n\n"
    "🔴 *1* – My vehicle is not charging\n"
    "⚫ *2* – The charger is off\n"
    "🐢 *3* – The charging speed is slow\n"
    "👤 *4* – Speak to a support agent\n\n"
    "Simply type the number or describe your problem in your own words."
)

AGENT_MSG = (
    "👤 *Connecting you to a support agent...*\n\n"
    "Our team will be with you shortly. Our support hours are:\n"
    "🕗 *Monday – Friday: 07:00 – 19:00*\n"
    "🕗 *Saturday: 08:00 – 14:00*\n\n"
    "For urgent faults outside these hours, please email:\n"
    "📧 *support@aeversa.co.za*\n\n"
    "Please share your *Charger ID* and *vehicle model* so we can assist you faster. 🙏"
)

FALLBACK = (
    "🤔 I'm sorry, I didn't quite understand that.\n\n"
    "Please choose one of the following:\n\n"
    "🔴 *1* – My vehicle is not charging\n"
    "⚫ *2* – The charger is off\n"
    "🐢 *3* – The charging speed is slow\n"
    "👤 *4* – Speak to a support agent\n\n"
    "Or type *MENU* to see options again."
)

# ── Message Handler ──────────────────────────────────────────────────────────
def get_response(message: str) -> str:
    msg = message.strip().lower()

    # Greetings
    if any(greet in msg for greet in GREETING_KEYWORDS):
        return GREETING

    # Menu shortcuts
    if msg in ["1", "not charging"]:
        return FAQ["not charging"]["response"]
    if msg in ["2", "charger off"]:
        return FAQ["charger off"]["response"]
    if msg in ["3", "slow charging"]:
        return FAQ["slow charging"]["response"]
    if msg in ["4", "menu"]:
        return GREETING

    # Agent request
    if any(word in msg for word in AGENT_KEYWORDS):
        return AGENT_MSG

    # Keyword matching across FAQs
    for faq in FAQ.values():
        if any(keyword in msg for keyword in faq["keywords"]):
            return faq["response"]

    # Fallback
    return FALLBACK


# ── Webhook Route ────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    resp = MessagingResponse()
    resp.message(get_response(incoming))
    return str(resp)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
