#!/usr/bin/env python3
"""Blackout Beacon — briefing officer (the Gemini half of the pipeline).

WHILE the internet still works, this script asks Gemini 3.7 Flash to compile
structured local emergency knowledge ("readiness cards") for LOCATION. When
the grid dies, a local Gemma model on a MacBook serves neighbours' phones
grounded ONLY in these cards — so the cloud teaches the laptop before the
storm.

Key design points (this file is the proof of Gemini integration):

  * STRUCTURED OUTPUT — every Gemini call passes a Pydantic model as
    `response_schema`, so the SDK compiles the model into a JSON schema,
    Gemini's decoder is constrained to that shape, and `response.parsed`
    hands back validated `Card` objects. No regex, no "please output JSON"
    hope-and-pray parsing.
  * MODEL DISCOVERY — the Gemini 3.7 Flash model id is discovered at runtime
    via `client.models.list()` (prefer the stable, non-preview id), with a
    hardcoded fallback if listing is unavailable.
  * GRACEFUL FALLBACK — with no API key, a hand-authored seed card set is
    written instead so the beacon is never empty-handed.

Outputs: cards/<id>.json (one per card) + cards/meta.json.
Re-runnable: every run rewrites the cards/ directory from scratch.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

try:  # Literal is stdlib from 3.8; keep import explicit for clarity.
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LOCATION = "South Kensington, London, UK"
LANGS = ["en", "es", "fr", "ar", "zh", "pl", "ro"]

# MODEL is discovered at runtime (see discover_model()): we list the models
# available to this API key, pick the id matching MODEL_FAMILY (preferring a
# stable, non-preview id), and fall back to MODEL_FALLBACK if listing fails.
MODEL_FAMILY = "3.7-flash"
MODEL_FALLBACK = "gemini-3.7-flash"

PROJECT_ROOT = Path(__file__).resolve().parent
CARDS_DIR = PROJECT_ROOT / "cards"
ENV_FILE = PROJECT_ROOT / ".env"

LANG_NAMES = {
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "zh": "Chinese (Simplified)",
    "pl": "Polish",
    "ro": "Romanian",
}

# Card ids that MUST exist after a successful cloud briefing.
REQUIRED_CORE_IDS = [
    "emergency-numbers",
    "first-aid-basics",
    "choking-cpr",
    "safe-drinking-water",
    "power-cut-food-safety",
    "power-cut-medical-devices",
    "staying-warm-no-power",
    "comms-when-networks-down",
    "flood-safety",
    "local-help-points",
]
REQUIRED_PHRASE_IDS = ["phrases-" + code for code in LANGS if code != "en"]

KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# --------------------------------------------------------------------------
# The card schema — single source of truth.
#
# This Pydantic model does double duty:
#   1. It is passed to Gemini as `response_schema`, so the API *enforces*
#      this exact JSON shape (field names, enum values, array types) at
#      decode time — the model cannot drift from it.
#   2. It re-validates every JSON file after writing, so seed cards and
#      Gemini cards are held to the same contract the Gemma server reads.
#
# The Field descriptions are not decoration: the SDK forwards them inside
# the JSON schema, so they act as per-field instructions to Gemini.
# --------------------------------------------------------------------------

Category = Literal["water", "medical", "power", "shelter", "comms", "local", "language"]


class Card(BaseModel):
    id: str = Field(
        description=(
            "Stable kebab-case identifier, e.g. 'emergency-numbers'. "
            "Lowercase letters, digits and hyphens only."
        )
    )
    title: str = Field(description="Short human-readable card title.")
    category: Category = Field(
        description="One of: water, medical, power, shelter, comms, local, language."
    )
    summary: str = Field(description="One-line summary of what this card covers.")
    content: str = Field(
        description=(
            "200-400 words of practical, stepwise emergency guidance in plain "
            "language a stressed person can follow. Use short numbered steps "
            "where possible. Mark anything uncertain with 'verify locally'."
        )
    )
    keywords: List[str] = Field(
        description="Lowercase search keywords for offline retrieval (5-10 items)."
    )
    lang: str = Field(
        description=(
            "BCP-47 language code. 'en' for guidance cards; for phrasebook "
            "cards, the code of the language the phrases are translated into."
        )
    )


class CardSet(BaseModel):
    """Top-level structured-output wrapper: Gemini returns exactly this."""

    cards: List[Card] = Field(description="The generated readiness cards.")


# --------------------------------------------------------------------------
# API key loading: env var first, then a hand-parsed .env (KEY=VALUE lines).
# --------------------------------------------------------------------------

def load_api_key() -> Optional[str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            name, _, value = line.partition("=")
            if name.strip() == "GEMINI_API_KEY":
                value = value.strip().strip("'\"")
                if value:
                    return value
    return None


# --------------------------------------------------------------------------
# Model discovery
# --------------------------------------------------------------------------

def discover_model(client) -> str:
    """Pick the Gemini 3.7 Flash model id from the live model list.

    Prefers a stable id (no 'preview'/'exp' marker); among equals, the
    shortest name — which is how the plain alias sorts ahead of dated
    snapshots like ...-flash-001. Falls back to MODEL_FALLBACK if listing
    is unavailable.
    """
    try:
        names = []
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            actions = getattr(m, "supported_actions", None)
            if actions and "generateContent" not in actions:
                continue
            if MODEL_FAMILY in name:
                names.append(name)
        if names:
            def rank(n: str):
                is_preview = ("preview" in n) or ("exp" in n)
                return (is_preview, len(n), n)
            return min(names, key=rank)
        print(f"[briefing] No '{MODEL_FAMILY}' match in model list; "
              f"falling back to {MODEL_FALLBACK}")
    except Exception as exc:  # listing can fail on restricted keys
        print(f"[briefing] Model listing failed ({exc!r}); "
              f"falling back to {MODEL_FALLBACK}")
    return MODEL_FALLBACK


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTION = f"""\
You are a meticulous emergency-preparedness briefing officer compiling
offline "readiness cards" for residents of {LOCATION}, to be served by a
small offline model during a prolonged power/network outage.

Non-negotiable rules — ACCURACY OVER CREATIVITY:
1. Use only real, current UK emergency information: 999 and 112 (emergency),
   NHS 111 (urgent medical advice), 105 (power cuts), 0800 111 999 (gas
   emergency). Never invent phone numbers.
2. First-aid guidance must align with mainstream NHS / St John Ambulance /
   British Red Cross advice. No folk remedies.
3. Never invent street addresses, opening hours, or small local venues. Only
   name major, well-known institutions (e.g. large hospitals), and append
   "(verify locally)" to any address, distance, or detail that could age or
   be wrong.
4. If you are not confident something is correct, either omit it or mark it
   with "verify locally". Prefer omission over speculation.
5. Content must be practical and stepwise: short numbered steps, plain
   words, readable by a stressed person on a phone screen. 200-400 words
   per card.
6. Keywords: 5-10 lowercase retrieval keywords per card, including likely
   panic phrasings (e.g. "no power", "fridge", "insulin").
"""

CORE_CARDS_PROMPT = f"""\
Generate the core readiness card set for {LOCATION}. Produce EXACTLY one
card for each of these ids, with id copied verbatim:

- "emergency-numbers" (category comms): every key UK number — 999/112 and
  when to use them (free, work without credit or signal on any network),
  Silent Solution (55), NHS 111 (phone and 111.nhs.uk), 105 power-cut line
  (UK Power Networks covers London), 0800 111 999 gas emergency, 101
  non-emergency police, Floodline 0345 988 1188, emergencySMS registration
  for deaf users (text 'register' to 999 in advance).
- "first-aid-basics" (category medical): DR ABC primary survey, recovery
  position, severe bleeding, burns (20 minutes cool running water), shock,
  seizures — when to call 999 vs 111.
- "choking-cpr" (category medical): adult choking (5 back blows / 5
  abdominal thrusts), infant differences, adult CPR (30:2 at 100-120/min,
  5-6 cm), hands-only option, using a public defibrillator (AED).
- "safe-drinking-water" (category water): stored water per person per day,
  boiling (rolling boil 1 minute), emergency disinfection with plain
  unscented household bleach, what never to drink, water company bottled
  water stations (Thames Water serves this area — verify locally).
- "power-cut-food-safety" (category power): keep doors shut, fridge ~4 h,
  full freezer ~48 h / half-full ~24 h, what to discard, UK FSA guidance on
  refreezing food that still holds ice crystals, outdoor-only cooking and
  carbon monoxide danger.
- "power-cut-medical-devices" (category power): Priority Services Register
  (via 105 / UK Power Networks), battery-backup planning for ventilators,
  home oxygen, dialysis, CPAP, powered wheelchairs; INSULIN refrigeration
  guidance: unopened insulin 2-8 C, in-use insulin is fine at room
  temperature (below 25-30 C) for up to 28 days, keep the fridge shut, use
  a cool bag but never let insulin freeze — discard insulin that has frozen;
  when to call 999.
- "staying-warm-no-power" (category shelter): one warm room strategy,
  layers, closing curtains/doors, safe candle use, NEVER using gas
  ovens/BBQs/camping stoves indoors (carbon monoxide), hot water bottles,
  hypothermia warning signs, checking on vulnerable neighbours.
- "comms-when-networks-down" (category comms): battery discipline (low
  power mode, one phone on at a time), SMS more resilient than calls, 999
  roams onto any available network, car charging safely, battery/wind-up
  radio and useful stations (BBC Radio London 94.9 FM — verify locally),
  pre-agreed family meeting points, paper copies of key numbers, note that
  modern digital-voice landlines die without mains power.
- "flood-safety" (category shelter): Floodline and flood warnings, never
  walk or drive through flood water (30 cm floats a car), electricity and
  flood water, moving upstairs, turning off utilities if safe, avoiding
  contact with flood water (sewage), what to do after.
- "local-help-points" (category local): nearest major hospitals with 24 h
  A&E for South Kensington — Chelsea and Westminster Hospital (369 Fulham
  Road, SW10) and St Mary's Hospital (Praed Street, Paddington, W2) — plus
  St Thomas' Hospital and Charing Cross Hospital (Fulham Palace Road, W6)
  as alternates; note pharmacies and larger supermarkets as
  information/water points. EVERY address line must end with
  "(verify locally)". Do not invent smaller venues.

Every card: lang "en". Follow the system rules strictly.
"""


def phrasebook_prompt(code: str) -> str:
    return f"""\
Generate ONE phrasebook readiness card for {LANG_NAMES[code]} speakers in
{LOCATION}, for use during an emergency when residents and responders may
not share a language.

Requirements:
- id EXACTLY "phrases-{code}", category "language", lang "{code}".
- title like "Emergency Phrases: English <-> {LANG_NAMES[code]}".
- content: a short intro line (one sentence, in English, saying the card
  can be shown on screen and pointed at), then about 10 numbered essential
  emergency phrases, each on the pattern:
    n. EN: <English phrase> | {LANG_NAMES[code].upper()[:2]}: <accurate translation>
  Include a romanized pronunciation in parentheses after the translation
  when the script is non-Latin (Arabic, Chinese).
- Cover at least: calling for help, requesting an ambulance/police, "I am
  hurt/injured", a medical condition (diabetic/heart), "where is the
  nearest hospital?", needing water/food, "the power is out", "is the water
  safe to drink?", "I need my medication", "do you speak English?".
- Translations must be natural and accurate — this is safety-critical.
- Aim for 200-400 words total; add a closing tip (in English) on speaking
  slowly and using gestures if needed.
"""


# --------------------------------------------------------------------------
# Gemini generation
# --------------------------------------------------------------------------

def generate_cards(client, model_id: str, prompt: str, attempts: int = 2) -> List[Card]:
    """One structured-output call: Gemini is constrained to the CardSet schema.

    `response_schema=CardSet` + `response_mime_type="application/json"` makes
    the SDK send our Pydantic schema with the request; the API enforces the
    shape server-side and `response.parsed` returns a validated CardSet.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=CardSet,          # <- the SDK enforces the JSON shape
        temperature=0.2,                  # accuracy over creativity
    )
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.models.generate_content(
                model=model_id, contents=prompt, config=config
            )
            parsed = response.parsed  # CardSet instance (or None on failure)
            if parsed is None:        # belt-and-braces: parse the raw text
                parsed = CardSet.model_validate_json(response.text)
            if parsed.cards:
                return parsed.cards
            raise ValueError("Gemini returned an empty card list")
        except Exception as exc:
            last_exc = exc
            print(f"[briefing] generation attempt {attempt}/{attempts} failed: {exc!r}")
    raise RuntimeError(f"structured generation failed after {attempts} attempts") from last_exc


def run_cloud_briefing(client, model_id: str) -> List[Card]:
    """Full briefing: core guidance cards + one phrasebook per non-EN language."""
    print(f"[briefing] Generating core cards with {model_id} ...")
    cards = generate_cards(client, model_id, CORE_CARDS_PROMPT)

    # One focused call per language keeps each translation task small and
    # lets a single bad language be retried without regenerating everything.
    for code in [c for c in LANGS if c != "en"]:
        print(f"[briefing] Generating phrasebook: {LANG_NAMES[code]} ({code}) ...")
        phrase_cards = generate_cards(client, model_id, phrasebook_prompt(code))
        card = phrase_cards[0]
        card.id = f"phrases-{code}"  # pin the id and lang regardless of model drift
        card.lang = code
        card.category = "language"
        cards.append(card)

    # Retry any missing required core card individually (targeted, cheap).
    have = {c.id for c in cards}
    for missing in [i for i in REQUIRED_CORE_IDS if i not in have]:
        print(f"[briefing] Required card '{missing}' missing; regenerating it alone ...")
        retry = generate_cards(
            client, model_id,
            CORE_CARDS_PROMPT + f"\n\nGenerate ONLY the single card with id \"{missing}\".",
        )
        for c in retry:
            if c.id == missing:
                cards.append(c)
                break
    return cards


# --------------------------------------------------------------------------
# Validation, writing, reporting
# --------------------------------------------------------------------------

def validate_cards(cards: List[Card], require_all: bool) -> List[str]:
    """Return a list of human-readable problems (empty list = all good)."""
    problems = []
    seen = set()
    for c in cards:
        if not KEBAB_RE.match(c.id):
            problems.append(f"id not kebab-case: {c.id!r}")
        if c.id in seen:
            problems.append(f"duplicate id: {c.id}")
        seen.add(c.id)
        words = len(c.content.split())
        if not (150 <= words <= 450):  # tolerance band around the 200-400 target
            print(f"[briefing] note: '{c.id}' content is {words} words "
                  f"(target 200-400)")
    if require_all:
        for rid in REQUIRED_CORE_IDS + REQUIRED_PHRASE_IDS:
            if rid not in seen:
                problems.append(f"required card missing: {rid}")
    return problems


def write_cards(card_dicts: List[dict], briefing_model: str,
                briefed_at: Optional[str]) -> None:
    """Idempotent overwrite: clear stale cards, write fresh set + meta.json."""
    CARDS_DIR.mkdir(exist_ok=True)
    for stale in CARDS_DIR.glob("*.json"):
        stale.unlink()
    for card in card_dicts:
        path = CARDS_DIR / f"{card['id']}.json"
        path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    meta = {
        "briefed_at": briefed_at,
        "location": LOCATION,
        "briefing_model": briefing_model,
        "card_count": len(card_dicts),
    }
    (CARDS_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def verify_written_files() -> int:
    """Re-read every card file and re-validate against the Card schema."""
    count = 0
    for path in sorted(CARDS_DIR.glob("*.json")):
        if path.name == "meta.json":
            json.loads(path.read_text(encoding="utf-8"))
            continue
        Card.model_validate(json.loads(path.read_text(encoding="utf-8")))
        count += 1
    return count


def print_summary(card_dicts: List[dict], briefing_model: str) -> None:
    print()
    print("=" * 78)
    print(f"  Blackout Beacon briefing — {LOCATION}")
    print(f"  model: {briefing_model}   cards: {len(card_dicts)}")
    print("=" * 78)
    print(f"  {'id':<28} {'category':<10} {'lang':<5} {'words':>5}  title")
    print("  " + "-" * 74)
    for c in sorted(card_dicts, key=lambda d: (d["category"], d["id"])):
        words = len(c["content"].split())
        print(f"  {c['id']:<28} {c['category']:<10} {c['lang']:<5} {words:>5}  "
              f"{c['title'][:28]}")
    print("=" * 78)


# --------------------------------------------------------------------------
# Seed fallback — hand-authored cards used when no API key is available.
# Accuracy matters here too: this content follows NHS / St John Ambulance /
# UK Food Standards Agency public guidance.
# --------------------------------------------------------------------------

SEED_CARDS: List[dict] = [
    {
        "id": "emergency-numbers",
        "title": "UK Emergency Numbers",
        "category": "comms",
        "summary": "Every number you need in a UK emergency, and when to use each one.",
        "content": (
            "In a life-threatening emergency — someone unconscious, not breathing, "
            "severe bleeding, chest pain, fire — call 999 immediately. 112 reaches the "
            "same operators and also works across Europe. Both are free from any phone, "
            "work without credit, and a mobile will use ANY network with signal, even if "
            "your own has none. Speak clearly: what happened, where you are, how many "
            "people are hurt. Stay on the line and follow instructions.\n\n"
            "If you cannot speak safely, call 999, listen, then press 55 when prompted "
            "(the Silent Solution) so the operator knows it is a real emergency. If you "
            "are deaf or speech-impaired, the emergencySMS service lets you text 999 — "
            "but you must register in advance: text the word 'register' to 999 and "
            "follow the reply.\n\n"
            "Other key numbers:\n"
            "1. NHS 111 — urgent medical help that is not life-threatening. Call 111 or "
            "use 111.nhs.uk online. Free, 24 hours.\n"
            "2. 105 — free national power-cut line. It connects you to your local "
            "electricity network operator (UK Power Networks for London) to report a "
            "cut and hear restoration updates.\n"
            "3. 0800 111 999 — National Gas Emergency line. If you smell gas: open "
            "windows and doors, do not touch light switches or naked flames, get "
            "outside, then call.\n"
            "4. 101 — police non-emergency.\n"
            "5. Floodline 0345 988 1188 — flood warnings and advice.\n"
            "6. Thames Water 0800 316 9800 for water supply problems in this area "
            "(verify locally).\n\n"
            "Write these numbers on paper now, while you can. Phone batteries die; "
            "paper does not. Keep a copy by the door and one in your wallet."
        ),
        "keywords": ["999", "112", "111", "105", "emergency", "police", "ambulance",
                     "gas leak", "power cut", "phone numbers"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "first-aid-basics",
        "title": "First Aid Basics",
        "category": "medical",
        "summary": "The DR ABC primary survey plus bleeding, burns, shock and seizures.",
        "content": (
            "For any collapsed or injured person, run DR ABC:\n"
            "1. Danger — check the scene is safe for YOU before approaching.\n"
            "2. Response — speak loudly, gently shake their shoulders: 'Can you hear me?'\n"
            "3. Airway — if unresponsive, tilt the head back gently and lift the chin.\n"
            "4. Breathing — look, listen and feel for normal breathing for up to 10 "
            "seconds. Not breathing normally? Call 999 and start CPR (see the choking "
            "and CPR card).\n"
            "5. Circulation — check for and control severe bleeding.\n\n"
            "Unresponsive but breathing: roll them onto their side into the recovery "
            "position, head tilted back so the airway stays open, and monitor until "
            "help arrives.\n\n"
            "Severe bleeding: press hard on the wound with a clean cloth or dressing "
            "and keep pressing. Do not remove a soaked dressing — add more layers on "
            "top. Lie the person down, keep them warm, call 999. If an object is "
            "embedded, press around it, never pull it out.\n\n"
            "Burns: cool the burn under cool running water for a full 20 minutes. "
            "Remove rings and loose clothing near the burn unless stuck to skin. Cover "
            "loosely with cling film. Never use ice, butter or creams. Large, deep, or "
            "face/hand burns need 999.\n\n"
            "Shock (pale, cold, clammy, fast breathing after injury): lie them down, "
            "raise their legs, keep them warm, call 999.\n\n"
            "Seizure: do not restrain them or put anything in their mouth. Cushion the "
            "head, time the seizure, clear hard objects away. Afterwards put them in "
            "the recovery position. Call 999 if it lasts over 5 minutes, repeats, or "
            "is their first seizure.\n\n"
            "When unsure and phones work, NHS 111 can advise. If in doubt about life "
            "or limb, always choose 999."
        ),
        "keywords": ["first aid", "bleeding", "burns", "recovery position", "shock",
                     "seizure", "unconscious", "dr abc", "injury"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "choking-cpr",
        "title": "Choking and CPR",
        "category": "medical",
        "summary": "Step-by-step choking response and adult CPR, with baby differences.",
        "content": (
            "CHOKING (adult or child over 1): if they can cough, encourage coughing. "
            "If they cannot breathe, cough or speak:\n"
            "1. Give up to 5 back blows — lean them forward, strike hard between the "
            "shoulder blades with the heel of your hand.\n"
            "2. If that fails, give up to 5 abdominal thrusts — stand behind them, "
            "fist above the belly button, grasp with the other hand, pull sharply "
            "inwards and upwards.\n"
            "3. Alternate 5 back blows and 5 thrusts. If the blockage does not clear, "
            "call 999 and continue until help arrives or they become unresponsive — "
            "then start CPR.\n"
            "Babies under 1: lay the baby face-down along your forearm, head low, give "
            "5 firm back blows; if needed, turn face-up and give 5 chest thrusts with "
            "two fingers. NEVER do abdominal thrusts on a baby.\n\n"
            "CPR (adult, not breathing normally):\n"
            "1. Call 999 and put it on speaker. Send someone for a defibrillator (AED).\n"
            "2. Kneel beside them, heel of one hand on the centre of the chest, other "
            "hand on top, arms straight.\n"
            "3. Push hard and fast: 5-6 cm deep, 100-120 pushes per minute — the beat "
            "of 'Stayin' Alive'. Let the chest come fully back up each time.\n"
            "4. If trained, give 2 rescue breaths after every 30 compressions. If not, "
            "hands-only CPR is effective — just keep pushing.\n"
            "5. Do not stop until help arrives, an AED tells you to, or they breathe.\n\n"
            "AEDs are on many streets, stations and building lobbies (verify locally). "
            "Open it and follow the voice instructions — it will not shock anyone who "
            "does not need it. The 999 operator can talk you through everything."
        ),
        "keywords": ["choking", "cpr", "heart attack", "not breathing", "back blows",
                     "abdominal thrusts", "defibrillator", "aed", "baby choking"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "safe-drinking-water",
        "title": "Safe Drinking Water",
        "category": "water",
        "summary": "How much water to store and how to make questionable water safe.",
        "content": (
            "Plan on at least 2.5 to 3 litres of drinking water per person per day — "
            "more in heat or for pregnant women — plus extra for basic hygiene. If a "
            "storm or outage is forecast and the taps still run, fill clean bottles, "
            "pans and the bathtub NOW.\n\n"
            "If mains water fails, your water company must provide bottled water at "
            "collection stations; Thames Water serves this area — listen to local "
            "radio or check their site for locations (verify locally). Priority "
            "deliveries exist for medically vulnerable customers who register in "
            "advance.\n\n"
            "Making questionable water safe:\n"
            "1. Filter cloudy water first through a clean cloth or coffee filter and "
            "let it settle.\n"
            "2. Best option — boil: bring water to a full rolling boil for at least "
            "one minute, then let it cool covered. Boiling kills bacteria, viruses "
            "and parasites.\n"
            "3. If you cannot boil: use plain, unscented household bleach (4-6% sodium "
            "hypochlorite). Add about 2 drops per litre of clear water, stir, and wait "
            "30 minutes. It should smell very faintly of chlorine; if not, repeat once "
            "and wait again. Double the dose for cloudy water. Never use scented, "
            "'thick', or colour-safe bleach.\n\n"
            "Emergency sources inside the home: the hot water tank and the toilet "
            "CISTERN (the upper tank — never the bowl) can be used after boiling or "
            "disinfecting, if no chemical blocks are used in the cistern.\n\n"
            "Never drink flood water, river water, or radiator water under any "
            "circumstances. Do not ration water to the point of dehydration — drink "
            "what you need today and solve tomorrow's supply tomorrow. Babies' feeds "
            "need boiled (then cooled) water only."
        ),
        "keywords": ["water", "drinking water", "boil", "purify", "bleach",
                     "bottled water", "no water", "storage"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "power-cut-food-safety",
        "title": "Food Safety in a Power Cut",
        "category": "power",
        "summary": "What is still safe to eat from the fridge and freezer, and when.",
        "content": (
            "The single most important rule: KEEP THE DOORS SHUT. An unopened fridge "
            "keeps food safely cold for about 4 hours. A full, unopened freezer holds "
            "safe temperatures for roughly 48 hours; a half-full one about 24 hours. "
            "Every door opening costs you cold air, so decide what you want before "
            "you open.\n\n"
            "Plan of action:\n"
            "1. Note the time the power failed — write it on the fridge door.\n"
            "2. Eat fresh and perishable food first, then fridge food, then freezer "
            "food, keeping tins and dry goods for last.\n"
            "3. Group frozen items together so they keep each other cold; if you have "
            "cool boxes and ice packs, move high-risk items (milk, meat, fish) into "
            "them, packed tight.\n"
            "4. After power returns, check the freezer: food that still contains ice "
            "crystals and feels refrigerator-cold can be refrozen (UK Food Standards "
            "Agency guidance), though quality may suffer. Fully thawed meat or fish "
            "should be cooked promptly if still cold, or discarded.\n"
            "5. Discard high-risk chilled food — meat, fish, dairy, cooked rice, "
            "ready meals — that has spent more than 4 hours above fridge temperature. "
            "When in doubt, throw it out: you cannot see or smell the bacteria that "
            "cause food poisoning, and a blackout is the worst time to get ill.\n\n"
            "Cooking without power: barbecues, camping stoves and gas burners are for "
            "OUTDOOR use only — indoors they produce carbon monoxide, which is "
            "invisible, odourless and lethal. Cook outside in open air, away from "
            "windows. Food in a chest freezer left shut survives longest — resist "
            "checking it."
        ),
        "keywords": ["food", "fridge", "freezer", "power cut", "food safety",
                     "refreeze", "spoiled", "carbon monoxide", "cooking"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "power-cut-medical-devices",
        "title": "Medical Devices and Medicines Without Power",
        "category": "power",
        "summary": "Keeping powered medical equipment and refrigerated medicines (incl. insulin) safe.",
        "content": (
            "If anyone in your home depends on powered medical equipment — home "
            "oxygen, a ventilator, dialysis, a CPAP machine, hoists, a powered "
            "wheelchair — register NOW with the free Priority Services Register via "
            "105 or UK Power Networks. Registered households get advance warning of "
            "planned works, priority updates and extra support in an outage.\n\n"
            "During a cut:\n"
            "1. Switch life-supporting devices to battery backup and note the runtime "
            "you have. Call your equipment supplier's emergency line for spare "
            "batteries or cylinders.\n"
            "2. Home oxygen users: keep backup cylinders accessible and call your "
            "supplier early.\n"
            "3. If a life-sustaining device is failing and you have no backup, call "
            "999 — hospitals have power.\n"
            "4. NHS 111 can advise on missed dialysis or treatment.\n\n"
            "INSULIN — the key facts: unopened insulin belongs at 2-8 C in the "
            "fridge, but insulin in current use is fine at room temperature (below "
            "about 25-30 C) for up to 28 days. So in a typical outage:\n"
            "1. Keep spare insulin in the closed fridge — it will stay cool for "
            "hours if you do not open the door.\n"
            "2. For longer outages, move it to a cool bag or a wide-necked vacuum "
            "flask. Wrap it so it never touches ice packs directly.\n"
            "3. NEVER let insulin freeze. Frozen (or previously frozen) insulin must "
            "be discarded — it stops working even after thawing.\n"
            "4. Keep using your current pen or vial as normal.\n\n"
            "Other refrigerated medicines (some eye drops, liquid antibiotics, "
            "biologics): keep them in the shut fridge, and ask a pharmacist or NHS "
            "111 how long yours lasts warm (verify locally). Keep at least one "
            "phone charged for medical calls."
        ),
        "keywords": ["insulin", "diabetes", "medical devices", "oxygen", "cpap",
                     "dialysis", "priority services register", "medication",
                     "fridge medicine", "power cut"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "staying-warm-no-power",
        "title": "Staying Warm Without Power",
        "category": "shelter",
        "summary": "Heat one room, layer up, and avoid the deadly indoor-heating mistakes.",
        "content": (
            "Cold kills quietly, so plan for warmth before you are cold.\n\n"
            "1. Choose ONE room to live in — small, ideally sun-facing, with few "
            "outside walls. Shut its door and close off the rest of the home.\n"
            "2. Trap heat: close curtains and blinds (open sun-facing ones during "
            "the day), and lay towels or blankets along door draughts. Hang a "
            "blanket over large windows at night.\n"
            "3. Dress in LAYERS — several thin layers beat one thick one, because "
            "they trap air. Wear a hat, socks and gloves indoors; you lose serious "
            "heat from an uncovered head. Get into a sleeping bag or under duvets "
            "together — shared body heat works.\n"
            "4. Eat and drink warm things regularly if you can heat them safely, and "
            "keep eating — digestion generates heat. Hot water bottles are excellent; "
            "fill them before the power dies if warned.\n"
            "5. Move around every hour, but avoid sweating — damp clothes chill you.\n\n"
            "NEVER heat a room with a gas oven, barbecue, patio heater or camping "
            "stove. Indoors they release carbon monoxide — invisible, odourless and "
            "deadly. Headache, dizziness and nausea in several people at once is a "
            "CO red flag: get outside and call 999. Use candles carefully — on a "
            "plate, away from fabric, never left burning while you sleep.\n\n"
            "Watch for hypothermia, especially in babies and older people: constant "
            "shivering (or shivering that STOPS), slurred speech, drowsiness, "
            "confusion, cold pale skin. Warm the person gradually with dry layers "
            "and warm (not hot) drinks and call 999 if symptoms are severe.\n\n"
            "Check on elderly or disabled neighbours at least daily — a knock on the "
            "door can save a life."
        ),
        "keywords": ["cold", "warm", "heating", "no power", "hypothermia",
                     "carbon monoxide", "blankets", "winter", "one warm room"],
        "lang": "en",
        "source": "seed",
    },
    {
        "id": "comms-when-networks-down",
        "title": "Communicating When Networks Are Down",
        "category": "comms",
        "summary": "Stretching phone batteries, reaching 999, and getting news without internet.",
        "content": (
            "Phone networks and broadband often limp or die in a long power cut — "
            "mobile masts have only hours of battery backup. Plan around it.\n\n"
            "1. Battery discipline: switch phones to low-power mode, drop screen "
            "brightness, and turn off Wi-Fi/Bluetooth scanning. In a household, keep "
            "ONE phone on and switch the rest off, rotating daily. A charged power "
            "bank is the single best cheap preparation.\n"
            "2. Texts beat calls: SMS uses far less network capacity and will often "
            "get through when calls fail. Agree short check-in texts with family.\n"
            "3. 999 is special: an emergency call will use ANY network with signal, "
            "not just your own. If your phone shows 'emergency calls only', 999 "
            "still works.\n"
            "4. Charging: a car can charge phones — run it OUTDOORS in open air, "
            "never in a garage (carbon monoxide). Laptop batteries can also refill "
            "a phone over USB.\n"
            "5. News without internet: a battery or wind-up radio is gold. Try BBC "
            "Radio London 94.9 FM or BBC Radio 4 on 93.5 FM (verify locally) for "
            "official updates. Car radios work too.\n"
            "6. Landlines: old corded phones on traditional copper lines may work "
            "in a power cut, but modern 'digital voice' landlines run through your "
            "powered router and will NOT.\n"
            "7. Go analogue: write key numbers on paper, agree a family meeting "
            "point in advance (e.g. a named neighbour's home), and leave notes on "
            "doors if you move. Knock on neighbours' doors — in a real blackout, "
            "the street itself is the best network you have.\n\n"
            "This beacon you are reading is served locally and keeps working "
            "without internet — check back for updates."
        ),
        "keywords": ["phone", "signal", "no internet", "battery", "radio", "sms",
                     "999", "landline", "power bank", "network down"],
        "lang": "en",
        "source": "seed",
    },
]


def run_seed_fallback(reason: str) -> None:
    """Write the hand-authored seed set so the beacon is never empty-handed."""
    banner = "!" * 78
    print(banner)
    print("!!  NO GEMINI BRIEFING PERFORMED — WRITING SEED CARDS ONLY")
    print(f"!!  Reason: {reason}")
    print("!!  Set GEMINI_API_KEY (env or .env) and re-run briefing.py for the")
    print("!!  full location-tailored, multilingual card set from Gemini.")
    print(banner)
    write_cards(SEED_CARDS, briefing_model="seed", briefed_at=None)
    n = verify_written_files()
    print(f"[briefing] Verified {n} seed card files against the Card schema.")
    print_summary(SEED_CARDS, briefing_model="seed")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    api_key = load_api_key()
    if not api_key:
        run_seed_fallback("GEMINI_API_KEY not found in environment or .env")
        return 0  # deliberate: seed fallback is a successful (degraded) run

    from google import genai  # imported late so seed mode never needs network

    client = genai.Client(api_key=api_key)
    model_id = discover_model(client)
    print(f"[briefing] Using model: {model_id}")

    try:
        cards = run_cloud_briefing(client, model_id)
    except Exception as exc:
        print(f"[briefing] Cloud briefing failed: {exc!r}")
        run_seed_fallback(f"Gemini call failed: {exc}")
        return 1

    problems = validate_cards(cards, require_all=True)
    if problems:
        print("[briefing] Validation problems:")
        for p in problems:
            print(f"  - {p}")
        if any(p.startswith("required card missing") for p in problems):
            run_seed_fallback("Gemini output missing required cards")
            return 1

    card_dicts = [dict(c.model_dump(), source="gemini") for c in cards]
    briefed_at = datetime.now(timezone.utc).isoformat()
    write_cards(card_dicts, briefing_model=model_id, briefed_at=briefed_at)
    n = verify_written_files()
    print(f"[briefing] Verified {n} card files against the Card schema.")
    print_summary(card_dicts, briefing_model=model_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
