from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LIDARAI_", extra="ignore")

    app_name: str = "LidarAI Local Processor"
    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"
    port: int = 8000
    storage_dir: str = "./backend_storage"
    auth_token: str = ""
    cors_origins: str = "*"
    job_timeout_seconds: int = 1200
    job_retention_days: int = 0
    job_retention_max_jobs: int = 0
    job_retention_min_free_mb: int = 0
    job_delete_terminal_after_seconds: int = 0
    default_processing_profile: str = "fast_onboarding"
    ai_provider: str = "openai"
    openai_api_key: str = ""
    openai_organization: str = ""
    openai_project: str = ""
    openai_model: str = "gpt-5.5"
    openai_fallback_model: str = ""
    openai_reasoning_effort: str = "medium"
    openai_request_timeout_seconds: int = 45
    openai_max_images_per_request: int = 1

    # --- Anthropic provider (LIDARAI_AI_PROVIDER=anthropic) -----------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Adaptive thinking depth: low | medium | high | xhigh | max.
    # Benchmarked Aug 26 2026: low matches medium on all flow-quality checks
    # (slot capture, scan gate, grounding on real photos) at ~30% lower
    # latency — the beta configuration of record.
    anthropic_effort: str = "low"
    anthropic_request_timeout_seconds: int = 60
    anthropic_max_tokens: int = 8192

    # --- Flow agent (API_CONTRACT_V1) ---------------------------------
    # Signed flow-state tokens; falls back to auth_token so a fresh deploy
    # is never unsigned-by-accident. Set explicitly in production.
    flow_token_secret: str = ""
    # The opening turn (steps 1-2) may attach more keyframe images.
    opening_max_images: int = 4
    # Which flag means "the first scan's processing is complete" for the
    # SOW §3 gate (docs/SCAN_SCOPE.md): "processor_job" -- the processor
    # pipeline's processingState, server-verified when the job is known --
    # or "device_bake" -- scanContext.localModelReady, the phone's own
    # textured bake. TakeShape has not decided which the beta uses, so it is
    # configuration; under either, no scan prompt happens before the flag.
    scan_complete_signal: str = "processor_job"
    # Homeowner identity: Supabase JWT verification (X-Homeowner-Token).
    supabase_jwt_secret: str = ""
    # Supabase persistence (service role) — Week 2; unset = local JSONL only.
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    # Baked room models from a whole-home export are copied to Supabase
    # Storage at ingest so a lead package can link to them. Files over this
    # size are recorded on the index as skipped rather than attempted:
    # Supabase's free plan rejects objects over 50 MB, and a real kitchen
    # bake is ~70 MB. Raise it on a plan whose bucket allows larger objects.
    model_upload_max_mb: int = 50
    # Operations handoff (lead packages / results return).
    ops_token: str = ""
    ops_webhook_url: str = ""
    # --- Ops email loop (provider-finder scope, agreed Sep 1) ----------
    # Lead packages are emailed here on submission (Quintin in production;
    # a test inbox during development). Unset = webhook/API only.
    ops_email: str = ""
    ops_email_from: str = ""  # defaults to smtp_username / Resend onboarding sender
    # Preferred transport: Resend HTTPS API (hosts like Railway and Render
    # block outbound SMTP ports at the network level; HTTPS always works).
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    # Public base URL of this deployment (no trailing slash) — used to build
    # the signed quote-entry link inside ops emails.
    public_base_url: str = ""
    # Suggested-provider section of the ops email: web-researched nearby
    # companies (one search per lead, cached) added to the ranked provider
    # table. Off by default like every research flag; the demo env enables it.
    provider_finder_enabled: bool = False
    # --- Provider discovery + ranking (extends the provider finder) --------
    # Google Places is the only source of ratings and review counts: its
    # terms allow display with attribution and a 30-day cache ceiling on
    # everything but the place id (docs/adr/provider-ranking.md). Yelp,
    # Facebook, Instagram and Nextdoor are link-only -- never scraped, and a
    # missing count is stored as null, never zero. Off by default like every
    # research flag; the key lives in a TakeShape-owned account (SOW §8).
    provider_discovery_enabled: bool = False
    google_places_api_key: str = ""
    provider_discovery_ttl_days: int = 30
    provider_discovery_max_results: int = 10
    # Trade category -> Places text query, as a JSON object. A category not
    # listed here falls back to "<category> contractor"; adding a trade is
    # configuration, not code.
    provider_discovery_queries: str = (
        '{"Painting": "painting contractor", "Flooring": "flooring contractor", '
        '"Interior Remodeling": "home remodeling contractor", '
        '"Window & Door Install": "window and door installer", '
        '"Handyman": "handyman service", '
        '"Interior Cleaning": "house cleaning service", "Decking": "deck builder", '
        '"Roofing & Siding": "roofing and siding contractor", '
        '"Window Cleaning": "window cleaning service", '
        '"Gutter Cleaning": "gutter cleaning service", '
        '"Power Washing": "pressure washing service", '
        '"Moving": "moving company", '
        '"Junk Removal": "junk removal service"}'
    )
    # Profile links on the link-only platforms come from the existing
    # web-search path (search results, not the platforms' pages).
    provider_profile_links_enabled: bool = True
    # Ranking weights (app/flow/provider_ranking.py) -- every term is tunable.
    rank_platform_weights: str = "google=1.0,yelp=0.6,facebook=0.4,instagram=0.4,nextdoor=0.4"
    rank_volume_weight: float = 0.6
    rank_rating_weight: float = 0.4
    rank_volume_percentile_weight: float = 0.6
    rank_volume_log_weight: float = 0.4
    rank_volume_reference_count: int = 200
    rank_rating_prior: float = 4.0
    rank_rating_shrinkage_count: int = 10
    rank_multi_platform_bonus: float = 0.05
    rank_multi_platform_bonus_cap: float = 0.15
    rank_quoted_boost: float = 0.25
    rank_quoted_boost_saturation: int = 5
    rank_no_data_baseline: float = 0.0
    # DEPRECATED (Sep 9 2026): the pre-ranking "partners first, then previous
    # quoters by count" ordering. Kept behind this flag for one release so a
    # rollback is a config change; the ranked list carries the same
    # note_quoted signal as its named quotedBoost term.
    preferred_partner_ordering_enabled: bool = False
    # --- Reply-by-email quote entry (equal alternative to the entry page) --
    # Ops replies to the lead email in plain words; the reply is read from
    # the ops mailbox over IMAP, parsed into a structured quote, uploaded,
    # and confirmed back by email.
    ops_reply_enabled: bool = False
    ops_imap_host: str = "imap.gmail.com"
    ops_imap_username: str = ""  # falls back to smtp_username
    ops_imap_password: str = ""  # falls back to smtp_password
    ops_reply_poll_seconds: int = 90
    # Where the lead email asks replies to go. Unset = the ops address
    # itself, which is right when we can read that mailbox over IMAP. Set
    # it to a mailbox we *can* read when the ops address cannot be polled
    # (a Google Workspace account whose admin does not hand out app
    # passwords, for instance): ops still receives the lead at their own
    # address and replies normally, and the reply lands where the poller
    # is looking.
    ops_reply_to: str = ""
    # Addresses a reply may come from, comma-separated. Unset = the ops
    # address alone. Widen it when ops has more than one account (a
    # personal address and a work one) so a reply from either is read.
    ops_reply_senders: str = ""
    # Wide-band guidance BEFORE a contractor prices the job (Noah's Aug 25
    # email). ON by default as of Sep 16 2026: with it off, every "what's
    # this going to run me?" got "I can't give you a number myself", which
    # reads as a refusal rather than a policy — 19 cost questions across the
    # Sep 16 eval battery, not one answered, and homeowners left. The
    # capability existed the whole time and was switched off.
    #
    # This is a ballpark, never a quote: `flow/enforcement.py` still holds
    # the model to the card's own band (`allowed_price_range`), and a real
    # price still only comes back from a human-reviewed request.
    #
    # Note this does NOT turn on spending: web-grounded rates are
    # `price_research_enabled`, which stays off and is opted into per deploy.
    agent_price_guidance_enabled: bool = True
    # Ground the range in web-searched rates rather than the static national
    # table: near the homeowner's zip when we have one, nationally when we
    # don't. ON by default as of Sep 16 2026 — a static table is a guess about
    # a market it has never seen, and "go and look it up" is the whole point
    # of the question. Cached 30 days per service+zip (or service+national),
    # so this is one search per area per service, not per conversation, and
    # the static table is still the silent fallback on any failure.
    price_research_enabled: bool = True
    # Web-grounded local context (style trends, seasonal/permit notes) for
    # any zip — pure engagement, no business risk.
    local_context_enabled: bool = False
    # BETA: web-searched local providers for zips with no TakeShape partner.
    # Off by default — surfaces unvetted third parties; a product decision.
    local_provider_research_enabled: bool = False
    # SOW §12 logging discipline.
    log_pii_masking_enabled: bool = True
    log_retention_days: int = 90
    # Debug-only raw request/response log (ai_threads/*/messages.jsonl) —
    # UNMASKED, so it stays off outside a specific debugging task (§12).
    raw_turn_log_enabled: bool = False
    # Refuse to start when acceptance-critical config is missing (set on
    # TakeShape's production service; dev and tests run permissive).
    strict_config: bool = False
    # Input guard (app/flow/input_guard.py): screens the homeowner's message
    # before generation. On by default; a kill switch because a bad pattern
    # would block real conversation.
    guard_enabled: bool = True

    # --- Production armor (app/armor.py) ------------------------------
    # Serialize turns per conversation thread: concurrent turns on one
    # thread are a lost-update on flow state (a captured slot vanishing).
    turn_lock_enabled: bool = True
    turn_lock_timeout_seconds: float = 25.0
    # Cap total wall time for one turn (the model call can regenerate, so
    # the worst case is a multiple of the provider timeout). On breach the
    # homeowner gets deterministic safe copy instead of a hung spinner.
    turn_deadline_enabled: bool = True
    turn_deadline_seconds: float = 100.0
    # Sliding-window rate limit on the endpoints that cost a model call.
    rate_limit_enabled: bool = True
    rate_limit_per_identity_per_minute: int = 20
    rate_limit_per_address_per_minute: int = 120
    # Largest request body accepted. A chat turn carries up to four keyframe
    # JPEGs inline as base64 (~1 MB typical), so the ceiling is generous;
    # what it stops is a body big enough to exhaust the instance's memory,
    # which no amount of rate limiting bounds.
    max_request_bytes: int = 16_000_000


settings = Settings()
