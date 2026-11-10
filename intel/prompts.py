"""Prompts and JSON schemas for the brief. The model only sees verified articles and real market data."""
from .config import READER, THEMES

S = {"type": "string"}
SA = {"type": "array", "items": {"type": "string"}}


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


ITEM = _obj(dict(headline=S, summary=S, why_it_matters=S, economic_impact=S, market_impact=S,
                 investment_implications=S, opportunities=S, risks=S, tag=S, themes=SA, sources=SA))
SECTION_SCHEMA = _obj(dict(overview=S, items={"type": "array", "items": ITEM}))
POINT = _obj(dict(text=S, sources=SA))
CEO_SCHEMA = _obj(dict(know={"type": "array", "items": POINT}, opportunities={"type": "array", "items": POINT},
                       risks={"type": "array", "items": POINT}))
WEEKLY_SCHEMA = _obj(dict(overview=S, items={"type": "array", "items": ITEM},
                          rising_themes={"type": "array", "items": _obj(dict(theme=S, why=S))},
                          ceo=CEO_SCHEMA))

SYSTEM = f"""You are the editor-in-chief of the "Global Intelligence & Wealth Brief", a private daily briefing for {READER}.
You combine the judgment of a senior financial analyst, economist and geopolitical analyst.

Hard rules:
1. Facts come ONLY from the articles and market data provided in the message. Never add facts, numbers, names, dates or
   quotes from memory. If the articles don't support a point, leave it out.
2. Every item cites the article ids it relies on in "sources" (ids look like A1b2c3d4e5f6). Use only ids that appear in
   the message. An item without a valid source will be deleted.
3. Separate reporting from analysis: "summary" is what was reported; "why_it_matters", impacts, implications,
   opportunities and risks are your analysis - reasoned, specific and hedged where uncertain ("could", "may").
4. Prefer factual reporting over opinion; ignore promotional or trivial stories, celebrity news, sports and crime unless
   they have clear economic or policy consequences.
5. No personal investment advice ("buy X", "sell Y"). Describe opportunities, risks and what to watch instead.
6. Write in clear, plain English (translate French sources). Be concise: summary 2-3 sentences, other fields 1-2
   sentences. Use "" for fields that don't apply to the section.
7. "themes": 1-3 short lowercase tags; prefer these when they fit: {", ".join(THEMES)}.
8. Merge stories that are the same event. Rank by importance to the reader, not by how recent they are."""


def story_block(clusters, max_excerpt=700):
    """Stories as the model sees them: one block per cluster with its verified articles."""
    out = []
    for c in clusters:
        lines = [f"STORY: {c['title']}  (covered by {len(c['outlets'])} outlet(s): {', '.join(c['outlets'][:6])})"]
        for a in c["show"][:3]:
            when = a["published"].strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"  [{a['aid']}] {a['source']} - \"{a['title']}\" ({when})")
        ex = next((a["excerpt"] for a in c["show"] if a.get("excerpt")), "")
        if ex:
            lines.append(f"  Excerpt: {ex[:max_excerpt]}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def section_prompt(section, clusters, date_str, market_text=""):
    n = section.get("n", 6)
    m = f"\nMARKET DATA (real numbers you may quote):\n{market_text}\n" if market_text else ""
    return f"""Date: {date_str}. Write the "{section['title']}" section.
Cover: {section.get('ask', '')}.
Choose up to {n} of the most important stories below for this reader (fewer if the material is thin; never pad).
Fields: headline (your own, max 12 words); summary; why_it_matters; economic_impact; market_impact;
investment_implications; opportunities; risks; tag ""; themes; sources.
Fill the fields that fit this section and leave the others as "". "overview" = 2-3 sentences on the big picture
of this section today.
{m}
STORIES:
{story_block(clusters)}"""


def exec_prompt(clusters, date_str, market_text):
    return f"""Date: {date_str}. Write the EXECUTIVE SUMMARY: the 10 most important developments in the world today for
this reader (global importance first, then the reader's focus areas). For each: headline, summary, why_it_matters,
economic_impact, market_impact (fill all five), themes, sources; leave investment_implications, opportunities, risks
and tag as "". "overview" = 2-3 sentences: the state of the world this morning.

MARKET DATA (real numbers you may quote):
{market_text or "not available"}

STORIES:
{story_block(clusters, 500)}"""


def watchlist_prompt(done_items, clusters, date_str):
    return f"""Date: {date_str}. Write the CANADA OPPORTUNITIES WATCHLIST: up to 8 concrete opportunities that could
generate business, partnerships, consulting work or investment returns for the reader. Categories (put one in "tag"):
Quebec, Ontario, Banking, Wealth management, Real estate, Small business, AI, Digital transformation, Technology,
Consulting. For each: headline, summary (the development behind it), opportunities (who could benefit and how,
practical), risks, why_it_matters, themes, sources. Leave economic_impact, market_impact, investment_implications as "".
Only opportunities grounded in today's articles. "overview" = 1-2 sentences.

TODAY'S BRIEF SO FAR (with source ids):
{done_items}

MORE CANADIAN STORIES:
{story_block(clusters, 400)}"""


def ceo_prompt(done_items, date_str):
    return f"""Date: {date_str}. Write the CEO BRIEF that closes today's report, from the brief below:
exactly 3 "know" (things the reader should know today), 3 "opportunities" worth watching, 3 "risks" worth monitoring.
Each is one or two sharp sentences with its source ids.

TODAY'S BRIEF:
{done_items}"""


def weekly_prompt(week_items, start, end, theme_counts):
    return f"""Write the WEEKLY INTELLIGENCE SUMMARY for {start} to {end} from this week's daily briefs.
"overview": 4-6 sentences on what defined the week. "items": the 10 developments that mattered most this week, with
headline, summary (how the story evolved over the week), why_it_matters, economic_impact, market_impact,
investment_implications, opportunities, risks, themes, sources (ids from the items below), tag "".
"rising_themes": up to 5 themes gaining importance, with why. "ceo": 3 know / 3 opportunities / 3 risks for next week.

THEME COUNTS THIS WEEK VS THE 4 WEEKS BEFORE:
{theme_counts}

THIS WEEK'S ITEMS:
{week_items}"""
