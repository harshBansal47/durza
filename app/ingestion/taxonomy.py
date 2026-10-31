"""Map provider category labels into the site's existing category taxonomy.

Provider taxonomies are not the public category list. Keep aliases explicit
and resolve them to existing category slugs instead of creating categories
from external labels or requiring exact display-name matches.
"""

import re

_CATEGORY_ALIASES = {
    "computers-electronics": "electronics",
    "computer-electronics": "electronics",
    "electronics-computers": "electronics",
    "technology": "electronics",
    "tech": "electronics",
    "clothing": "fashion",
    "apparel": "fashion",
    "clothing-apparel": "fashion",
    "health-beauty": "beauty",
    "health-and-beauty": "beauty",
    "cosmetics": "beauty",
    "personal-care": "beauty",
    "online-grocery": "grocery",
    "grocery-delivery": "grocery",
    "food-delivery-services": "food-delivery",
    "travel-and-hotels": "travel",
    "hotels-and-travel": "travel",
}


def category_key(value: str) -> str:
    """Normalize punctuation and case in a provider label for alias lookup."""
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def canonical_category_slug(value: str) -> str:
    """Map a known provider label to a site slug, without creating new rows."""
    key = category_key(value)
    return _CATEGORY_ALIASES.get(key, key)
