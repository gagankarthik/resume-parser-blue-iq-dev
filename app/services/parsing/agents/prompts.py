"""Shared prompt fragments for the multi-agent healthcare parser.

Kept in one place so the date/no-fabrication/credential rules stay identical to
the single-shot parser (`app/services/parsing/ai_parser.py`) across every agent.
"""

CORE_RULES = """- Extract ONLY what is explicitly stated. NEVER infer, guess, expand, or hallucinate. If a value is not written, use null/empty.
- Dates — keep the precision written; never invent a missing day or month:
  • Full date  → MM/DD/YYYY   • Month+year → MM/YYYY   • Year only → YYYY   • Current → "Present".
  • In a date range, a trailing 2-digit number is a YEAR: "August 2018 – April 19" → start "08/2018", end "04/2019" (never drop that end date, never read it as a day).
- Copy geographic values exactly as written — "England, UK" stays "England, UK"; never shorten or expand a place name.
- Spelling: when the résumé misspells an ordinary word or a standard clinical term, write it correctly ("Registerd Nurse" → "Registered Nurse", "Telemtry" → "Telemetry", "Pediatic" → "Pediatric"). Fix clear typos only; never reword, paraphrase, or change meaning. NEVER alter the spelling of people's names, employer/facility/school names, place names, email addresses, URLs, or licence/credential numbers.
- Do not repeat yourself: never output the same role, degree, certification, licence, skill, or bullet twice, even when the résumé lists it in more than one place.
- Preserve credential abbreviations EXACTLY (RN, LPN, CRT, RRT, OT, PT, SLP, RT(R), RT(CT), RT(M), ARRT…). Do NOT expand them.
- This is NOT a nurse-only task: handle allied health (radiologic/CT/MRI/mammography techs, respiratory, OT/PT/SLP, surgical/lab, social work) with the same care as nurses."""
