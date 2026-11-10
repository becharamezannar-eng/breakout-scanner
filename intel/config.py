"""Settings for the Global Intelligence & Wealth Brief.

Everything secret or deployment-specific comes from environment variables (see deploy/.env.example).
Editorial settings (sections, focus, sources) live here and in sources.yaml.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
TZ = "America/Toronto"                       # Eastern Time (same clock as New York)


def _env(name, default=""):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


@dataclass
class Settings:
    # AI
    anthropic_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openai_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    provider: str = field(default_factory=lambda: _env("INTEL_PROVIDER", "anthropic"))      # anthropic | openai
    model: str = field(default_factory=lambda: _env("INTEL_MODEL", "claude-sonnet-5-5"))
    effort: str = field(default_factory=lambda: _env("INTEL_EFFORT", "medium"))            # low | medium | high
    openai_model: str = field(default_factory=lambda: _env("INTEL_OPENAI_MODEL", "gpt-5"))
    max_daily_cost: float = field(default_factory=lambda: float(_env("INTEL_MAX_DAILY_COST_USD", "3")))
    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_full: bool = field(default_factory=lambda: _env("INTEL_TELEGRAM_FULL", "0") == "1")
    # Optional extra source
    newsapi_key: str = field(default_factory=lambda: _env("NEWSAPI_KEY"))
    # GDELT (Reuters/AP discovery): rate-limits GitHub's shared servers, so on by default only on a server
    gdelt: bool = field(default_factory=lambda: _env("INTEL_GDELT", "on" if _env("DATABASE_URL") else "off").lower()
                        in ("on", "1", "true", "yes"))
    # Storage: DATABASE_URL (postgresql://... or sqlite:///path) on a server; JSON files in reports/ on GitHub
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL"))
    outdir: str = field(default_factory=lambda: _env("INTEL_OUTDIR", "reports"))
    public_url: str = field(default_factory=lambda: _env(
        "INTEL_PUBLIC_URL", "https://becharamezannar-eng.github.io/breakout-scanner/intel/"))
    # Collection
    lookback_hours: int = field(default_factory=lambda: int(_env("INTEL_LOOKBACK_HOURS", "30")))
    max_verify: int = field(default_factory=lambda: int(_env("INTEL_MAX_VERIFY", "420")))
    workers: int = field(default_factory=lambda: int(_env("INTEL_WORKERS", "12")))
    # Schedule (server mode; GitHub mode uses the workflow's cron + a time gate)
    brief_time: str = field(default_factory=lambda: _env("INTEL_BRIEF_TIME", "05:40"))      # start so it's ready by 6:00
    weekly_day: str = field(default_factory=lambda: _env("INTEL_WEEKLY_DAY", "sun"))
    health_port: int = field(default_factory=lambda: int(_env("INTEL_HEALTH_PORT", "8080")))
    # Server: "on" = the server builds and sends the 6 am brief; "off" = GitHub builds it and the server only answers
    # Telegram commands (it reads the published brief). Use one producer, not both.
    schedule: bool = field(default_factory=lambda: _env("INTEL_SCHEDULE", "on").lower() in ("on", "1", "true", "yes"))

    @property
    def ai_enabled(self):
        return bool(self.anthropic_key if self.provider == "anthropic" else self.openai_key)


# Prices per million tokens (input, output), checked Nov 2026 on platform.claude.com/docs/en/about-claude/pricing.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0)}

READER = ("a Canadian investor and business professional who follows Canada (especially Quebec and Ontario), the "
          "United States, the Middle East and the UAE/Dubai, global markets, banking, wealth management, ETFs, interest "
          "rates, inflation, real estate, AI and automation, digital marketing, small business and entrepreneurship")

# Sections in report order. kind: which LLM schema; keys: keyword routing (English + French).
SECTIONS = [
    dict(id="executive_summary", title="Executive Summary", kind="exec", n=10),
    dict(id="geopolitics", title="Geopolitics", kind="items", n=7, keys=[
        "war", "conflict", "sanction", "election", "summit", "diplomat", "treaty", "nato", "united nations", "ukraine",
        "russia", "china", "taiwan", "military", "ceasefire", "tariff", "trade war", "trade deal", "missile", "border",
        "president", "prime minister", "foreign minister", "coup", "protest", "g7", "g20", "eu ", "european union"],
        ask="what happened, why it matters, potential economic impact"),
    dict(id="global_economy", title="Global Economy", kind="items", n=7, keys=[
        "gdp", "inflation", "cpi", "consumer prices", "jobs report", "employment", "unemployment", "payrolls", "wages",
        "retail sales", "central bank", "interest rate", "rate cut", "rate hike", "ecb", "bank of england", "bank of japan",
        "pmi", "manufacturing", "recession", "growth forecast", "imf", "world bank", "oecd", "budget", "deficit", "fiscal",
        "trade balance", "exports", "imports", "tariff", "economy", "économie", "taux directeur"],
        ask="what happened and the implications for investors and businesses"),
    dict(id="financial_markets", title="Financial Markets", kind="items", n=7, keys=[
        "stocks", "shares", "s&p 500", "nasdaq", "dow", "tsx", "wall street", "bond", "yield", "treasuries", "dollar",
        "currency", "loonie", "yen", "euro", "oil", "crude", "brent", "opec", "gold", "silver", "copper", "commodit",
        "earnings", "profit", "ipo", "merger", "acquisition", "takeover", "bitcoin", "market", "bourse"],
        ask="key market movements, investor implications, risks and opportunities"),
    dict(id="canada", title="Canada", kind="items", n=7, keys=[
        "canada", "canadian", "ottawa", "bank of canada", "carney", "cmhc", "statistics canada", "statcan", "tsx",
        "royal bank", "rbc", "td bank", "scotiabank", "bmo", "cibc", "national bank", "desjardins", "manulife", "sun life",
        "shopify", "alberta", "british columbia", "osfi", "cra", "loonie", "housing starts", "crea"],
        ask="federal developments, Bank of Canada, indicators, housing, banks, major companies, tax and regulation, "
            "investment implications"),
    dict(id="quebec", title="Quebec", kind="items", n=6, keys=[
        "quebec", "québec", "montreal", "montréal", "legault", "caq", "hydro-québec", "hydro-quebec", "laval", "gatineau",
        "sherbrooke", "trois-rivières", "saguenay", "desjardins", "caisse de dépôt", "cdpq", "national assembly",
        "assemblée nationale", "investissement québec", "bombardier", "couche-tard", "cgi inc", "national bank"],
        ask="provincial policy, economy, business, labour market, infrastructure, technology, opportunities for Quebec "
            "residents and businesses"),
    dict(id="ontario", title="Ontario", kind="items", n=6, keys=[
        "ontario", "toronto", "doug ford", "queen's park", "mississauga", "brampton", "hamilton", "waterloo", "kitchener",
        "london, ont", "ottawa city", "gta", "bay street", "tmx group", "metrolinx", "ttc", "ontario place",
        "greenbelt", "ring of fire"],
        ask="what happened, why it matters, economic impact, investment implications, business opportunities"),
    dict(id="united_states", title="United States", kind="items", n=7, keys=[
        "federal reserve", "the fed", "fomc", "white house", "congress", "senate", "treasury secretary", "u.s.", "us economy",
        "american", "trump", "washington", "wall street", "irs", "sec ", "jobs report", "nonfarm"],
        ask="Fed, indicators, policy, corporate and market events, with implications for Canadian investors and "
            "businesses"),
    dict(id="middle_east", title="Middle East & UAE", kind="items", n=6, keys=[
        "dubai", "uae", "emirates", "abu dhabi", "saudi", "riyadh", "qatar", "doha", "kuwait", "bahrain", "oman", "israel",
        "iran", "gaza", "lebanon", "syria", "iraq", "yemen", "jordan", "egypt", "gulf", "opec", "aramco", "adnoc",
        "mubadala", "adia", "emaar", "dp world", "difc", "adgm", "neom", "pif"],
        ask="regional developments, economic initiatives, Dubai, real estate, regulation, investment and business "
            "opportunities"),
    dict(id="ai_technology", title="AI & Technology", kind="items", n=7, keys=[
        "artificial intelligence", " ai ", "ai-", "openai", "anthropic", "google", "microsoft", "nvidia", "meta", "apple",
        "chip", "semiconductor", "software", "cyber", "hack", "ransomware", "breach", "cloud", "data center",
        "automation", "robot", "quantum", "startup", "llm", "chatbot", "intelligence artificielle"],
        ask="what happened, why it matters, business applications, opportunities for entrepreneurs and investors"),
    dict(id="wealth", title="Wealth Management Brief", kind="items", n=6, keys=[
        "etf", "fund", "portfolio", "asset manager", "wealth", "pension", "rrsp", "tfsa", "annuit", "mortgage", "bank",
        "banking", "dividend", "bond", "interest rate", "private credit", "private equity", "asset allocation",
        "brokerage", "advisor", "fintech"],
        ask="why it matters to investors, key opportunities, key risks, long-term implications"),
    dict(id="business", title="Business Opportunities", kind="items", n=6, keys=[
        "startup", "small business", "entrepreneur", "funding round", "venture", "raises", "marketing", "advertising",
        "e-commerce", "retail", "creator", "social media", "seo", "franchise", "consulting", "growth", "launch",
        "market gap", "demand", "shortage", "subsidy", "grant", "program", "petite entreprise"],
        ask="emerging industries, AI opportunities, marketing trends, market gaps, growth sectors, practical ideas"),
    dict(id="watchlist", title="Canada Opportunities Watchlist", kind="watch", n=8),
    dict(id="ceo_brief", title="CEO Brief", kind="ceo"),
]
SECTION_BY_ID = {s["id"]: s for s in SECTIONS}
FOCUS_BOOST = ["canada", "canadian", "quebec", "québec", "montreal", "montréal", "ontario", "toronto", "dubai", "uae",
               "emirates", "bank of canada", "tsx", "etf", "wealth", "interest rate", "inflation", "mortgage", "housing"]

# Bot command -> sections it returns
COMMANDS = {
    "brief": None, "markets": ["financial_markets", "wealth"], "geopolitics": ["geopolitics", "middle_east"],
    "business": ["business", "watchlist"], "wealth": ["wealth", "financial_markets"],
    "canada": ["canada", "quebec", "ontario"], "technology": ["ai_technology"], "watchlist": ["watchlist", "ceo_brief"],
}

# Themes the model should prefer when tagging (keeps trends comparable day to day)
THEMES = [
    "bank of canada rates", "fed rates", "inflation", "canada economy", "us economy", "housing", "canadian banks",
    "us tariffs", "trade", "oil", "gold", "currencies", "bond yields", "equity markets", "earnings", "etfs",
    "private credit", "ai", "ai regulation", "semiconductors", "cybersecurity", "big tech", "startups", "small business",
    "digital marketing", "real estate", "dubai real estate", "gulf economy", "middle east conflict", "ukraine war",
    "china", "elections", "quebec politics", "ontario politics", "infrastructure", "energy transition", "labour market",
    "immigration", "crypto", "wealth management", "pensions", "m&a",
]
