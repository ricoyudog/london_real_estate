"""Small, framework-independent datasource functions."""

from .esg import fetch_non_domestic_epc_ratings
from .geography import lookup_postcode, query_town_centres
from .hybrid import fetch_hybrid_working
from .macro import (
    fetch_bank_rate,
    fetch_latest_mpc_decision,
    fetch_london_labour_market,
    fetch_uk_gdp,
    fetch_uk_inflation,
    fetch_uk_labour_market,
)
from .market import fetch_public_market_report, fetch_voa_office_stock
from .news import fetch_content_item, search_market_news
from .planning import fetch_planning_application, search_planning_applications

__all__ = [
    "fetch_bank_rate",
    "fetch_content_item",
    "fetch_hybrid_working",
    "fetch_latest_mpc_decision",
    "fetch_london_labour_market",
    "fetch_non_domestic_epc_ratings",
    "fetch_planning_application",
    "fetch_public_market_report",
    "fetch_uk_gdp",
    "fetch_uk_inflation",
    "fetch_uk_labour_market",
    "fetch_voa_office_stock",
    "lookup_postcode",
    "query_town_centres",
    "search_market_news",
    "search_planning_applications",
]
