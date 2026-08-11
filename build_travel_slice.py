#!/usr/bin/env python3
"""Build the travel-anchored SimpleQA slice (TODOS.md #97) — the beachhead
metric filter. Transparent, rule-based, pinned with a seed for reproducibility.

Runs over the FULL 4,326-row dataset (datasets/simple_qa_test_set.csv), not
just the pre-labeled "Geography" topic (424 rows / 9.8%) — a travel-place
entity can appear in a Science/Politics/Art/Sports/etc. question too (e.g.
"Which museum in Florence holds ..." is topic=Art, not Geography).

RULE (applied to TEXT = problem + " " + answer, raw case preserved):

  Row is INCLUDED if TEXT contains, as a whole-word/phrase case-insensitive
  match, any of:

    (A) a country name from COUNTRIES (196 short-form UN/ISO names)
    (B) a city name from CITIES (~230 national capitals + major world cities)
    (C) a capitalized place-head phrase (1-4 Title-Case words, allowing the
        lowercase connectors "of/the/de/la/du/van/al/da/dos/e") immediately
        followed by a travel-place-class suffix word — PLACE_SUFFIX_WORDS
        (Bridge, Museum, National Park, Airport, Hotel, Station, ... — see
        list below). This is the "landmark/museum/national park/airport/
        hotel/transit" entity classes the task calls out, keyed off a real
        capitalized proper noun so a bare generic mention ("airport security
        policy") does not qualify.
    (D) a travel-place-class PREFIX word (Mount, Lake, Cape, Port, Fort, Sea,
        Gulf, Bay, Strait, Isle) immediately followed by a Title-Case proper
        noun ("Mount Everest", "Lake Victoria", "Cape Town", "Fort Worth").

  This is a RECALL-oriented heuristic, not NER — it will over-include some
  false positives (country names that double as common words/surnames:
  Georgia, Chad, Jordan, Turkey) and under-include travel entities phrased
  without a recognizable suffix/prefix or gazetteer hit. Both failure modes
  are disclosed via the printed counts and the sample audit this script
  prints, rather than hidden.

Usage:
    python3 build_travel_slice.py
        -> datasets/travel_slice_20260730.jsonl (question, answer, topic,
           matched_rule, matched_span) + prints counts to stdout.
"""
from __future__ import annotations

import ast
import csv
import json
import random
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent
DATASET = REPO / "datasets" / "simple_qa_test_set.csv"
OUT = REPO / "datasets" / "travel_slice_20260730.jsonl"
SEED = 20260730  # pinned — reused downstream for any random cut (2h stop condition)

# ── (A) Countries — common short-form English names, UN/ISO-ish list ───────
COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola",
    "Antigua and Barbuda", "Argentina", "Armenia", "Australia", "Austria",
    "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus",
    "Belgium", "Belize", "Benin", "Bhutan", "Bolivia",
    "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon",
    "Canada", "Central African Republic", "Chad", "Chile", "China",
    "Colombia", "Comoros", "Costa Rica", "Croatia", "Cuba", "Cyprus",
    "Czechia", "Czech Republic", "Denmark", "Djibouti", "Dominica",
    "Dominican Republic", "Ecuador", "Egypt", "El Salvador",
    "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia",
    "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany",
    "Ghana", "Greece", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau",
    "Guyana", "Haiti", "Honduras", "Hungary", "Iceland", "India",
    "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy",
    "Ivory Coast", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya",
    "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos", "Latvia",
    "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania",
    "Luxembourg", "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali",
    "Malta", "Marshall Islands", "Mauritania", "Mauritius", "Mexico",
    "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco",
    "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands",
    "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea",
    "North Macedonia", "Norway", "Oman", "Pakistan", "Palau", "Palestine",
    "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines",
    "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent",
    "Samoa", "San Marino", "Sao Tome and Principe", "Saudi Arabia",
    "Senegal", "Serbia", "Seychelles", "Sierra Leone", "Singapore",
    "Slovakia", "Slovenia", "Solomon Islands", "Somalia", "South Africa",
    "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan",
    "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan",
    "Tanzania", "Thailand", "Timor-Leste", "Togo", "Tonga",
    "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican",
    "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
    "England", "Scotland", "Wales", "Northern Ireland", "Hong Kong", "Macau",
]

# ── (B) Major world cities/capitals ─────────────────────────────────────────
CITIES = [
    "Kabul", "Tirana", "Algiers", "Luanda", "Buenos Aires", "Yerevan",
    "Canberra", "Sydney", "Melbourne", "Vienna", "Baku", "Nassau",
    "Manama", "Dhaka", "Minsk", "Brussels", "Belmopan", "Porto-Novo",
    "Thimphu", "La Paz", "Sarajevo", "Gaborone", "Brasilia", "Rio de Janeiro",
    "Sao Paulo", "Bandar Seri Begawan", "Sofia", "Ouagadougou", "Bujumbura",
    "Phnom Penh", "Yaounde", "Ottawa", "Toronto", "Vancouver", "Montreal",
    "Bangui", "N'Djamena", "Santiago", "Beijing", "Shanghai", "Hong Kong",
    "Bogota", "Moroni", "San Jose", "Zagreb", "Havana", "Nicosia", "Prague",
    "Copenhagen", "Djibouti City", "Roseau", "Santo Domingo", "Quito",
    "Cairo", "San Salvador", "Malabo", "Asmara", "Tallinn", "Mbabane",
    "Addis Ababa", "Suva", "Helsinki", "Paris", "Nice", "Marseille",
    "Libreville", "Banjul", "Tbilisi", "Berlin", "Munich", "Hamburg",
    "Frankfurt", "Accra", "Athens", "Thessaloniki", "St. George's",
    "Guatemala City", "Conakry", "Bissau", "Georgetown", "Port-au-Prince",
    "Tegucigalpa", "Budapest", "Reykjavik", "New Delhi", "Mumbai",
    "Bangalore", "Chennai", "Kolkata", "Jakarta", "Bali", "Tehran",
    "Baghdad", "Dublin", "Jerusalem", "Tel Aviv", "Rome", "Milan",
    "Venice", "Florence", "Naples", "Turin", "Kingston", "Tokyo", "Osaka",
    "Kyoto", "Amman", "Nur-Sultan", "Astana", "Almaty", "Nairobi",
    "Mombasa", "Tarawa", "Pristina", "Kuwait City", "Bishkek", "Vientiane",
    "Riga", "Beirut", "Maseru", "Monrovia", "Tripoli", "Vaduz", "Vilnius",
    "Luxembourg City", "Antananarivo", "Lilongwe", "Kuala Lumpur",
    "George Town", "Male", "Bamako", "Valletta", "Nouakchott",
    "Port Louis", "Mexico City", "Cancun", "Palikir", "Chisinau", "Monaco",
    "Ulaanbaatar", "Podgorica", "Rabat", "Marrakech", "Casablanca",
    "Maputo", "Yangon", "Windhoek", "Kathmandu", "Amsterdam", "Rotterdam",
    "The Hague", "Wellington", "Auckland", "Managua", "Niamey", "Abuja",
    "Lagos", "Pyongyang", "Skopje", "Oslo", "Muscat", "Islamabad",
    "Karachi", "Lahore", "Ramallah", "Panama City", "Port Moresby",
    "Asuncion", "Lima", "Cusco", "Manila", "Warsaw", "Krakow", "Lisbon",
    "Porto", "Doha", "Bucharest", "Moscow", "St. Petersburg", "Kigali",
    "Apia", "San Marino", "Riyadh", "Jeddah", "Dakar", "Belgrade",
    "Victoria", "Freetown", "Singapore", "Bratislava", "Ljubljana",
    "Honiara", "Mogadishu", "Cape Town", "Johannesburg", "Durban",
    "Pretoria", "Seoul", "Busan", "Juba", "Madrid", "Barcelona", "Seville",
    "Valencia", "Colombo", "Khartoum", "Paramaribo", "Stockholm",
    "Gothenburg", "Bern", "Zurich", "Geneva", "Damascus", "Taipei",
    "Dushanbe", "Dodoma", "Dar es Salaam", "Zanzibar", "Bangkok",
    "Phuket", "Chiang Mai", "Dili", "Lome", "Nuku'alofa", "Port of Spain",
    "Tunis", "Ankara", "Istanbul", "Antalya", "Ashgabat", "Kampala",
    "Kyiv", "Abu Dhabi", "Dubai", "London", "Edinburgh", "Manchester",
    "Liverpool", "Glasgow", "New York", "Los Angeles", "Chicago",
    "San Francisco", "Las Vegas", "Miami", "Boston", "Seattle",
    "Washington", "Montevideo", "Tashkent", "Port Vila", "Caracas",
    "Hanoi", "Ho Chi Minh City", "Sanaa", "Lusaka", "Harare",
]

# ── (C) Suffix words that mark a preceding capitalized phrase as a
# travel-place entity: landmark / museum / national park / airport / hotel /
# transit classes named in the task. Multi-word suffixes checked first.
PLACE_SUFFIX_WORDS = [
    "National Park", "State Park", "World Heritage Site", "Opera House",
    "Railway Station", "Metro Station", "Subway Station", "Bus Station",
    "International Airport", "Airport", "Station", "Terminal", "Hotel",
    "Resort", "Bridge", "Tower", "Museum", "Cathedral", "Basilica",
    "Temple", "Mosque", "Church", "Synagogue", "Shrine", "Palace",
    "Castle", "Fort", "Fortress", "Citadel", "Stadium", "Arena", "Zoo",
    "Aquarium", "Park", "Square", "Plaza", "Harbour", "Harbor", "Canal",
    "Dam", "Cave", "Caves", "Ruins", "Pyramid", "Pyramids", "Lighthouse",
    "Pier", "Wharf", "Island", "Islands", "Mountain", "Mountains",
    "Falls", "Bay", "Lake", "River", "Sea", "Ocean", "Desert", "Valley",
    "Canyon", "Peak", "Strait", "Peninsula", "Gulf", "Reef", "Glacier",
    "Volcano", "Province", "County", "Region", "District", "Monastery",
    "Cemetery", "Memorial", "Monument", "Botanical Garden", "Garden",
]

# ── (D) Prefix words that mark a following capitalized proper noun as a
# travel-place entity.
PLACE_PREFIX_WORDS = ["Mount", "Mt", "Mt.", "Lake", "Cape", "Port", "Fort",
                       "Sea", "Gulf", "Bay", "Strait", "Isle", "Lago"]

_CONNECTOR = r"(?:of|the|de|la|du|van|al|da|dos|e|di|del)"
_HEAD = rf"[A-Z][A-Za-z.\'’-]*(?:\s(?:{_CONNECTOR}\s)?[A-Z][A-Za-z.\'’-]*){{0,3}}"

_suffix_alt = "|".join(sorted((re.escape(w) for w in PLACE_SUFFIX_WORDS), key=len, reverse=True))
_prefix_alt = "|".join(re.escape(w) for w in PLACE_PREFIX_WORDS)

RULE_C_RE = re.compile(rf"\b({_HEAD})\s+({_suffix_alt})\b")
RULE_D_RE = re.compile(rf"\b({_prefix_alt})\s+([A-Z][A-Za-z.\'’-]*(?:\s[A-Z][A-Za-z.\'’-]*){{0,2}})\b")


def _word_re(name: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(name) + r"\b")


COUNTRY_RES = [(c, _word_re(c)) for c in COUNTRIES]
CITY_RES = [(c, _word_re(c)) for c in CITIES]

# Rule A/B PRECISION GUARDS — first pass (no guards) over-matched 698/1173 rows
# via bare country mentions, most incidental ("Officer of the Order of
# Canada", "Mali G52 MC2" GPU, "Victoria Villarruel" a person). Two guards,
# both applied only to gazetteer hits (A/B), never to C/D (already anchored
# on a real proper-noun+place-suffix pattern so they don't need this):
#
#   NEG_PRECEDING — institutional/honorific phrases that place a country or
#   city name right after "of"/"to" without the row being ABOUT that place.
NEG_PRECEDING = [
    "order of", "university of", "bank of", "embassy of", "consulate of",
    "ambassador to", "college of", "duke of", "duchess of", "king of",
    "queen of", "prince of", "princess of", "minister of", "ministry of",
    "department of", "board of", "institute of", "academy of",
    "chamber of", "court of", "diocese of", "treaty of", "company of",
    "governor of", "parliament of", "senate of", "congress of",
    "central bank of", "team of", "republic of",  # "Republic of X" is the
    # formal COUNTRY name (keep matching the country) -- excluded from the
    # guard by being listed but never firing since the country regex already
    # matches the bare name "X", and "Republic of X" preceding "X" would
    # false-negative a legitimate hit; kept here as documentation, removed
    # from the active check below.
]
NEG_PRECEDING = [p for p in NEG_PRECEDING if p != "republic of"]

# LOCATIONAL_CUE — a preposition/travel verb within 3 tokens before the match
# is evidence the row is actually ABOUT the place (not just naming it).
LOCATIONAL_CUE_RE = re.compile(
    r"\b(in|at|from|to|near|across|throughout|within|visit|visiting|"
    r"travel(?:ed|ing|s)?|trip|vacation|tour(?:ed|ing|s)?|border(?:s|ing)?|"
    r"coast(?:al)?|capital|city|cities|region|province|located|based in|"
    r"citizen of|born in|nationality|resident of|country of)\s+"
    r"(?:the\s+)?$",
    re.IGNORECASE,
)


def _guarded_gazetteer_hit(text: str, name: str, rx: re.Pattern, gold: str) -> bool:
    """True if ANY occurrence of `name` in `text` survives NEG_PRECEDING and
    is licensed by a LOCATIONAL_CUE nearby OR the gold answer IS the place
    itself (a place-identification question, e.g. 'In which country is X
    found?' -> 'New Guinea')."""
    gold_norm = re.sub(r"^\s*the\s+", "", gold.strip(), flags=re.IGNORECASE).strip()
    if gold_norm.lower() == name.lower():
        return True
    for m in rx.finditer(text):
        before = text[max(0, m.start() - 30):m.start()].lower()
        if any(before.rstrip().endswith(neg) for neg in NEG_PRECEDING):
            continue
        window = text[max(0, m.start() - 30):m.start()]
        if LOCATIONAL_CUE_RE.search(window):
            return True
    return False


def classify(text: str, gold: str) -> tuple[bool, str, str]:
    """-> (matched, rule, matched_span). Checks C/D (anchored, no guard
    needed) first, then guarded A/B."""
    m = RULE_C_RE.search(text)
    if m:
        return True, "C_place_suffix", m.group(0)
    m = RULE_D_RE.search(text)
    if m:
        return True, "D_place_prefix", m.group(0)
    for name, rx in CITY_RES:
        if rx.search(text) and _guarded_gazetteer_hit(text, name, rx, gold):
            return True, "B_city", name
    for name, rx in COUNTRY_RES:
        if rx.search(text) and _guarded_gazetteer_hit(text, name, rx, gold):
            return True, "A_country", name
    return False, "", ""


def main() -> None:
    rows = []
    with open(DATASET, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            meta = ast.literal_eval(r["metadata"])
            rows.append({"q": r["problem"], "gold": r["answer"], "topic": meta.get("topic", "")})

    print(f"full dataset: {len(rows)} rows")

    matched = []
    for r in rows:
        text = f'{r["q"]} {r["gold"]}'
        ok, rule, span = classify(text, r["gold"])
        if ok:
            r2 = dict(r, matched_rule=rule, matched_span=span)
            matched.append(r2)

    print(f"travel-place-anchored slice: {len(matched)} rows "
          f"({len(matched) / len(rows) * 100:.1f}% of full 4,326)")

    by_topic = {}
    by_rule = {}
    for r in matched:
        by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
        by_rule[r["matched_rule"]] = by_rule.get(r["matched_rule"], 0) + 1
    print("\nby original topic:")
    for k, v in sorted(by_topic.items(), key=lambda x: -x[1]):
        print(f"  {v:4d}  {k}")
    print("\nby matched rule (first-hit):")
    for k, v in sorted(by_rule.items(), key=lambda x: -x[1]):
        print(f"  {v:4d}  {k}")

    geo_topic = sum(1 for r in matched if r["topic"] == "Geography")
    non_geo = len(matched) - geo_topic
    print(f"\nGeography-topic rows in slice: {geo_topic} "
          f"({geo_topic / 424 * 100:.1f}% of the 424 Geography rows)")
    print(f"non-Geography-topic rows pulled in by the place-anchor filter: {non_geo}")

    rnd = random.Random(SEED)
    sample_audit = rnd.sample(matched, min(15, len(matched)))
    print(f"\nseeded (seed={SEED}) 15-row audit sample:")
    for r in sample_audit:
        print(f"  [{r['matched_rule']}:{r['matched_span']!r}] ({r['topic']}) "
              f"Q: {r['q'][:90]!r} A: {r['gold'][:40]!r}")

    with OUT.open("w") as f:
        for r in matched:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(matched)} rows -> {OUT}")


if __name__ == "__main__":
    main()
