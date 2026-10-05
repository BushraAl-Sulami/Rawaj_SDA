from typing import Any, Literal

from pydantic import BaseModel, Field



# =========================================================
# SHARED TYPES
# =========================================================

AnalysisStatus = Literal["complete", "partial", "failed"]
EvidenceSource = Literal["caption", "visual", "transcript", "metadata", "combined"]
ContentPillar = Literal[
    "food_product",
    "offers_campaigns",
    "brand_storytelling",
    "entertainment_humor",
    "behind_the_scenes",
    "people_staff",
    "dining_experience",
    "educational",
    "social_proof",
    "community_engagement",
]

class CountPct(BaseModel):
    """Reusable count + percentage pair."""

    count: int = 0
    pct: float = Field(default=0, ge=0, le=100)


# =========================================================
# RESTAURANT INFORMATION
# =========================================================

class RestaurantInfo(BaseModel):
    restaurant_id: int
    name: str

    instagram_username: str
    instagram_url: str | None = None

    email: str | None = None
    location: str | None = None
    category: str | None = None


# =========================================================
# ANALYSIS METADATA
# =========================================================

class AnalysisMetadata(BaseModel):
    status: AnalysisStatus
    analyzed_at: str
    scraped_at: str | None = None
    source: str = "instagram"
    analysis_version: str = "3.0"


# =========================================================
# INSTAGRAM PROFILE
# =========================================================

class InstagramProfile(BaseModel):
    username: str | None = None
    full_name: str | None = None
    bio: str | None = None

    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None

    website: str | None = None

    verified: bool | None = None
    business_category: str | None = None
    is_business: bool | None = None
    is_private: bool | None = None


# =========================================================
# PROFILE ANALYSIS
# =========================================================

class BioAnalysis(BaseModel):
    cuisine_identified: bool = False
    cuisine: str | None = None
    location_present: bool = False
    cta_present: bool = False
    value_proposition_present: bool = False


class ContactAccessibility(BaseModel):
    website_available: bool = False
    email_available: bool = False
    phone_available: bool = False
    external_link_available: bool = False


class ProfileCompleteness(BaseModel):
    has_clear_business_description: bool = False
    has_location: bool = False
    has_contact_method: bool = False
    has_external_link: bool = False


class ProfileAnalysis(BaseModel):
    bio: BioAnalysis = Field(default_factory=BioAnalysis)
    contact_accessibility: ContactAccessibility = Field(
        default_factory=ContactAccessibility
    )
    profile_completeness: ProfileCompleteness = Field(
        default_factory=ProfileCompleteness
    )


# =========================================================
# CONTENT / REEL RAW DATA
# =========================================================

class ContentRawData(BaseModel):
    content_url: str | None = None
    timestamp: str | None = None
    caption: str = ""

    # Raw Instagram / Apify media labels, e.g. Image, Video, Sidecar.
    content_type: str | None = None
    product_type: str | None = None

    image_urls: list[str] = Field(default_factory=list)
    video_url: str | None = None

    likes: int | None = None
    comments: int | None = None
    views: int | None = None

    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    alt_text: str | None = None
    music_info: dict[str, Any] | None = None


class ReelDetails(BaseModel):
    """Extra data added only when a recent content item matches a Reel."""

    matched: bool = True
    reel_url: str | None = None

    transcript: str | None = None
    duration_seconds: float | None = None
    shares: int | None = None
    plays: int | None = None
    views: int | None = None
    
    enrichment_success: bool = True


class ScrapedContent(BaseModel):
    content_id: str | None = None
    short_code: str | None = None
    raw_data: ContentRawData
    reel_details: ReelDetails | None = None


# =========================================================
# PART 1 OUTPUT — SCRAPED INSTAGRAM DATA
# =========================================================

class ScrapedInstagramData(BaseModel):
    """
    Output of Part 1 (scrape): raw Instagram data exactly as collected.

    Contains no LLM output and no interpretation. It is the only input
    Part 2 (analyze) needs about the restaurant's Instagram account.
    """

    scraped_at: str
    profile: InstagramProfile
    content: list[ScrapedContent] = Field(default_factory=list)

    # Scrape settings, kept so coverage can be reported against the request.
    content_limit: int
    lookback_days: int

    # Non-fatal guardrail findings from the scrape (e.g. private account).
    scrape_warnings: list[str] = Field(default_factory=list)


# =========================================================
# ANALYSIS OF ONE CONTENT ITEM
# =========================================================

class EvidenceItem(BaseModel):
    source: EvidenceSource
    supports: list[str] = Field(default_factory=list)
    text: str


class ContentCTAAnalysis(BaseModel):
    present: bool = False

    # FREE TEXT. No fixed CTA taxonomy.
    intent: str | None = None
    description: str | None = None


class ContentPromotionAnalysis(BaseModel):
    present: bool = False

    # FREE TEXT. No fixed promotion taxonomy.
    intent: str | None = None
    description: str | None = None


class VisualSignals(BaseModel):
    food_visible: bool = False
    menu_visible: bool = False
    price_visible: bool = False
    offer_visible: bool = False
    logo_visible: bool = False
    branding_visible: bool = False
    people_visible: bool = False


class ContentAnalysis(BaseModel):
    # Human-readable description.
    content_type: str

    # Dynamic labels inferred from the content. No predefined list.
    # Ask the LLM to keep them concise and normalized for aggregation.
    content_categories: list[str] = Field(default_factory=list)
    content_pillar: ContentPillar
    content_themes: list[str] = Field(default_factory=list)

    cta: ContentCTAAnalysis = Field(default_factory=ContentCTAAnalysis)
    promotion: ContentPromotionAnalysis = Field(
        default_factory=ContentPromotionAnalysis
    )

    visual_signals: VisualSignals = Field(default_factory=VisualSignals)

    text_in_visual: str | None = None
    visual_summary: str
    caption_summary: str

    # Populated when a Reel transcript exists.
    transcript_summary: str | None = None
    spoken_topics: list[str] = Field(default_factory=list)

    evidence: list[EvidenceItem] = Field(default_factory=list)


class AnalyzedContent(ScrapedContent):
    analysis: ContentAnalysis | None = None
    analysis_error: str | None = None


# =========================================================
# AGGREGATED RESEARCH METRICS
# =========================================================

class PostingActivity(BaseModel):
    content_last_30_days: int = 0
    content_per_week: float = 0
    days_since_last_content: int | None = None


class EngagementMetrics(BaseModel):
    average_likes: float | None = None
    median_likes: float | None = None
    average_comments: float | None = None

    # Percentage points, e.g. 0.18 means 0.18%.
    engagement_rate_pct: float | None = None
    sample_size: int = 0


class CTAMetrics(BaseModel):
    content_with_cta: int = 0
    any_cta_pct: float = Field(default=0, ge=0, le=100)

    # Dynamic free-text intent labels inferred by the LLM.
    cta_intents: dict[str, int] = Field(default_factory=dict)


class PromotionMetrics(BaseModel):
    promotional_content: int = 0
    promotional_content_pct: float = Field(default=0, ge=0, le=100)

    # Dynamic free-text intent labels inferred by the LLM.
    promotion_intents: dict[str, int] = Field(default_factory=dict)


class BrandingMetrics(BaseModel):
    logo_presence_pct: float = Field(default=0, ge=0, le=100)
    branding_presence_pct: float = Field(default=0, ge=0, le=100)


class CommerceVisibilityMetrics(BaseModel):
    menu_visible_pct: float = Field(default=0, ge=0, le=100)
    price_visible_pct: float = Field(default=0, ge=0, le=100)
    offer_visible_pct: float = Field(default=0, ge=0, le=100)


class ReelMetrics(BaseModel):
    reels_detected: int = 0
    reels_enriched: int = 0
    reel_content_pct: float = Field(default=0, ge=0, le=100)

    average_views: float | None = None
    median_views: float | None = None
    average_duration_seconds: float | None = None
    transcript_available_pct: float = Field(default=0, ge=0, le=100)


class ResearchMetrics(BaseModel):
    activity: PostingActivity = Field(default_factory=PostingActivity)
    engagement: EngagementMetrics = Field(default_factory=EngagementMetrics)

    # Dynamic keys such as image, carousel, video, reel.
    format_mix: dict[str, CountPct] = Field(default_factory=dict)

    # Dynamic LLM-inferred content categories.
    content_mix: dict[str, CountPct] = Field(default_factory=dict)

    cta: CTAMetrics = Field(default_factory=CTAMetrics)
    promotion: PromotionMetrics = Field(default_factory=PromotionMetrics)
    branding: BrandingMetrics = Field(default_factory=BrandingMetrics)
    commerce_visibility: CommerceVisibilityMetrics = Field(
        default_factory=CommerceVisibilityMetrics
    )
    reels: ReelMetrics = Field(default_factory=ReelMetrics)


# =========================================================
# RESEARCH SIGNALS
# =========================================================

class ResearchSignal(BaseModel):
    """
    Evidence-backed observation produced by Research.

    A signal describes what was observed. It does NOT decide whether that
    observation is good/bad or whether the restaurant is a qualified prospect.
    """

    signal_id: str
    dimension: str
    observation: str

    metric_path: str | None = None
    value: Any | None = None

    evidence_content_ids: list[str] = Field(default_factory=list)
    


# =========================================================
# ANALYSIS COVERAGE + DATA QUALITY
# =========================================================

class AnalysisDateRange(BaseModel):
    oldest_content: str | None = None
    newest_content: str | None = None


class AnalysisCoverage(BaseModel):
    content_requested: int = 0
    content_scraped: int = 0
    content_analyzed: int = 0
    content_failed: int = 0
    analysis_success_pct: float = Field(default=0, ge=0, le=100)

    date_range: AnalysisDateRange = Field(default_factory=AnalysisDateRange)
    multimodal_analysis: bool = False

    reels_detected: int = 0
    reels_enriched: int = 0
    reel_transcripts_available: int = 0


class FailedAnalysis(BaseModel):
    content_id: str | None = None
    stage: str
    reason: str


class DataQuality(BaseModel):
    status: AnalysisStatus = "partial"

    profile_scrape_success: bool = False
    content_scrape_success: bool = False
    multimodal_analysis_completed: bool = False

    reel_enrichment_attempted: bool = False
    reel_enrichment_completed: bool = False

    warnings: list[str] = Field(default_factory=list)
    failed_analyses: list[FailedAnalysis] = Field(default_factory=list)


# =========================================================
# FULL RESEARCH AGENT OUTPUT
# =========================================================

class ResearchProfile(BaseModel):
    """
    Full Research Agent output.

    Save this object as JSON / DB for traceability, evidence retrieval,
    debugging, UI details, and downstream use.
    """

    schema_version: str = "3.0"

    restaurant: RestaurantInfo
    analysis_metadata: AnalysisMetadata

    profile: InstagramProfile
    profile_analysis: ProfileAnalysis = Field(default_factory=ProfileAnalysis)

    metrics: ResearchMetrics
    research_signals: list[ResearchSignal] = Field(default_factory=list)

    # Full evidence. Includes images, videos and Reels among the same
    # latest-N content window.
    content: list[AnalyzedContent] = Field(default_factory=list)

    analysis_coverage: AnalysisCoverage = Field(default_factory=AnalysisCoverage)
    data_quality: DataQuality = Field(default_factory=DataQuality)


# =========================================================
# QUALIFICATION AGENT INPUT
# =========================================================

class QualificationRestaurantContext(BaseModel):
    restaurant_id: int
    name: str
    category: str | None = None
    location: str | None = None
    followers: int | None = None



class QualificationInput(BaseModel):
    restaurant: RestaurantInfo
    profile: InstagramProfile
    profile_analysis: ProfileAnalysis
    metrics: ResearchMetrics
    research_signals: list[ResearchSignal]
    analysis_coverage: AnalysisCoverage
    data_quality: DataQuality

def build_qualification_input(
    report: ResearchProfile
) -> QualificationInput:

    return QualificationInput(
        restaurant=report.restaurant,
        profile=report.profile,
        profile_analysis=report.profile_analysis,
        metrics=report.metrics,
        research_signals=report.research_signals,
        analysis_coverage=report.analysis_coverage,
        data_quality=report.data_quality,
    )