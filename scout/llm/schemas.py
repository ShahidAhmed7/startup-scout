"""Validated structured outputs for every AI job."""

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

Sector = Literal[
    "fintech",
    "commerce and retail",
    "logistics and mobility",
    "health",
    "education",
    "agriculture and food",
    "travel and hospitality",
    "real estate and construction",
    "jobs and HR",
    "media and creators",
    "marketing and sales",
    "productivity and business software",
    "developer tools and AI infrastructure",
    "security",
    "legal and government",
    "climate and energy",
    "manufacturing and industry",
    "consumer and social",
    "other",
]
BusinessModel = Literal[
    "software_subscription",
    "marketplace",
    "financial_product",
    "commerce",
    "tech_enabled_service",
    "hardware_light",
    "deep_tech",
    "infrastructure_api",
    "media_content",
    "other",
]
Customer = Literal[
    "consumers",
    "small_businesses",
    "large_businesses",
    "government",
    "developers",
    "mixed",
]


class ExtractedCompany(BaseModel):  # extract: today's fields plus three
    name: str
    website: str
    country: str
    stage: str
    amount_text: str
    amount_usd: float
    investors: list[str]
    what_they_do: str
    business_model_hint: str  # how it makes money, if the item says; else ""
    customer_hint: str  # who pays, if the item says; else ""
    traction: str  # numbers stated in the item (users, revenue, growth); else ""


class Classified(BaseModel):  # classify, one per startup
    startup_id: int
    product_known: bool  # false if the material does not say what they sell
    one_line: str  # what they do, max 15 words; "" if unknown
    sector: Sector
    business_model: BusinessModel
    customer: Customer
    ai_native: bool  # the product itself is AI (agent, model, AI-first software)
    skip: bool
    skip_reason: str  # skip list category, or ""
    search_phrase: str  # 3-6 plain words for web search, no company name


class Dim(BaseModel):
    score: int = Field(ge=1, le=10)
    reason: str  # one line


class BangladeshDims(BaseModel):
    demand: Dim
    readiness: Dim
    competition_gap: Dim
    regulatory_path: Dim
    economics: Dim


class GlobalDims(BaseModel):
    demand_abroad: Dim
    remote_delivery: Dim
    cost_advantage: Dim
    trust_burden: Dim
    competition_gap: Dim


class ServiceDims(BaseModel):
    buyer_breadth: Dim  # how many potential clients worldwide have this need (no prices needed)
    pain_budget: Dim  # do they already pay people or tools to get this done
    cost_advantage: Dim  # can a Bangladesh team do the work well for much less
    reachability: Dim  # can a remote team find and contact the buyers
    repeatable_revenue: Dim  # the same job every month for many clients, sold as a package
    remote_delivery: Dim  # delivered entirely online through software (the gate)
    trust_burden: Dim  # contracts, data rules, references needed
    competition_gap: Dim  # room for a focused newcomer


class WhatItTakes(BaseModel):
    build_type: Literal[
        "software",
        "tech_enabled_service",
        "marketplace",
        "regulated_product",
        "hardware_light",
        "operations_heavy",
    ]
    capital: Literal[
        "bootstrap", "seed", "venture"
    ]  # bootstrap < $50k, seed $50k-$1M, venture > $1M
    team: Literal["2-5", "5-15", "15+"]  # to reach first paying customers
    licence: str  # "none", or which licence/registration
    time_to_revenue: Literal["under 3 months", "3-12 months", "over 12 months"]


class Scored(BaseModel):
    model_config = ConfigDict(populate_by_name=True)  # score, one per startup
    startup_id: int
    concept_bd: str  # the Bangladesh version in one sentence, or "none"
    concept_global: str  # the version sold abroad from Bangladesh, or "none"
    concept_service: str  # done-for-you work for clients, delivered through software from Bangladesh, or "none"
    bangladesh: BangladeshDims
    global_: GlobalDims = Field(alias="global")  # model_config: populate_by_name=True
    service: ServiceDims
    timing: Dim  # why now
    traction: str  # stated numbers, else "unknown"
    investor_signal: str  # named lead investors / accelerator, else "unknown"
    what_it_takes: WhatItTakes
    service_what_it_takes: WhatItTakes  # the same labels for the service version
    ai_angle: str  # "" unless AI changes the economics
    confidence: Literal["high", "medium", "low"]


class Competitor(BaseModel):
    name: str
    url: str


class CardUpdate(BaseModel):  # after research, only these two may change
    competition_gap: int = Field(ge=1, le=10)
    regulatory_path: int = Field(ge=1, le=10)
    note: str  # "" if nothing changed


class Card(BaseModel):
    one_liner: str  # the idea for THIS card's market, max 15 words
    what_they_do: str  # fact: 1-2 sentences
    evidence: str  # fact: traction, investors, why funded; "unknown" if none
    bangladesh_play: str  # 2-4 sentences
    global_play: str  # 2-3 sentences, honest if weak
    service_play: str  # 2-3 sentences, or "" if there is no service version
    main_risk: str  # 1 sentence
    already_here: str  # 1-2 sentences, from search results only
    competitors: list[Competitor]
    ai_angle: str  # "" unless it changes the economics
    update: CardUpdate
    source_urls: list[str]


class DeepDive(BaseModel):
    landscape: str
    competitors: list[Competitor]
    failure_check: str
    economics: str
    regulatory_route: str
    first_customers: str
    validation_30_days: str
    risks: list[str]
    source_urls: list[str]


class ExtractedArticle(BaseModel):
    item_id: int
    kind: Literal["funding", "launch", "other"]
    companies: list[ExtractedCompany]


class Extraction(BaseModel):
    articles: list[ExtractedArticle]


class Classification(BaseModel):
    startups: list[Classified]


class Scoring(BaseModel):
    startups: list[Scored]


class Interview(BaseModel):
    bangla: str
    english: str


class Kit(BaseModel):
    interview: list[Interview]
    headline: str
    subhead: str
    call_to_action: str
    prices: str
    plan_7_days: str


class Ask(BaseModel):
    on_topic: bool
    answer: str


class Assumption(BaseModel):
    text: str
    status: Literal["holding", "at_risk", "untested"]
    evidence_for: int = Field(ge=0)
    evidence_against: int = Field(ge=0)


class Assumptions(BaseModel):
    assumptions: list[Assumption]
    summary: str
