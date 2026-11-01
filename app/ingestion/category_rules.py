"""Keyword rules: which of our categories a provider's wording belongs to.

This is the *starting* knowledge, not the final word. A rule's result is recorded
as an alias row (`method="rule"`), visible in the admin, where a person can
override it; their choice then beats every rule. So the rules only have to be
right most of the time, and cheap to be wrong.

How a label is matched:

* Wording is normalised first ("Electronics & COMPUTERS ACCESORIES" becomes
  "electronics and computers accesories"), so case, punctuation and "&" do not
  matter.
* A keyword matches whole words, with an optional plural ("computer" matches
  "computers"). A longer phrase scores more than a shorter one, so "home
  appliance" outranks "appliance" when a Home & Kitchen category exists.
* The highest-scoring rule whose target category *exists* wins. Ties go to the
  earlier rule in `RULES`.

`targets` are tried in order, each as a slug or a name. If none exists in the
database the rule is skipped, which is how the rules for categories you have not
created yet stay dormant and start working the day you add the category.

To teach it something new, add a keyword. To handle one odd wording, do not touch
the code: map it in the admin (Category Aliases).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    name: str
    targets: tuple[str, ...]
    keywords: tuple[str, ...]


# Order matters only for ties. Grocery sits before food delivery so that
# "Grocery & Gourmet Food" is groceries; e-commerce comes last as the broadest.
RULES: tuple[Rule, ...] = (
    Rule(
        "electronics",
        ("electronics", "consumer-electronics"),
        (
            "electronics",
            "electronic",
            "consumer electronics",
            "computer",
            "laptop",
            "notebook",
            "desktop",
            "pc",
            "tablet",
            "mobile phone",
            "smartphone",
            "phone",
            "camera",
            "headphone",
            "earphone",
            "earbud",
            "speaker",
            "audio",
            "gadget",
            "wearable",
            "smartwatch",
            "television",
            "tv",
            "appliance",
            "console",
            "printer",
            "monitor",
            "router",
            "power bank",
            "charger",
            "peripheral",
            "keyboard",
            "mouse",
        ),
    ),
    Rule(
        "fashion",
        ("fashion", "apparel", "clothing"),
        (
            "fashion",
            "apparel",
            "clothing",
            "clothes",
            "garment",
            "footwear",
            "shoe",
            "sneaker",
            "sandal",
            "jewellery",
            "jewelry",
            "watch",
            "bag",
            "handbag",
            "backpack",
            "ethnic wear",
            "mens wear",
            "womens wear",
            "kids wear",
            "innerwear",
            "lingerie",
            "eyewear",
            "sunglass",
            "saree",
            "kurta",
            "denim",
            "lifestyle",
        ),
    ),
    Rule(
        "beauty",
        ("beauty", "beauty-personal-care", "personal-care"),
        (
            "beauty",
            "cosmetic",
            "makeup",
            "make up",
            "skincare",
            "skin care",
            "haircare",
            "hair care",
            "fragrance",
            "perfume",
            "grooming",
            "personal care",
            "salon",
            "lipstick",
            "nail",
            "spa",
        ),
    ),
    Rule(
        "grocery",
        ("grocery", "groceries"),
        (
            "grocery",
            "groceries",
            "supermarket",
            "hypermarket",
            "fruit",
            "vegetable",
            "staple",
            "daily needs",
            "kirana",
            "dairy",
            "pantry",
            "fresh produce",
            "gourmet",
            "household essentials",
            "packaged food",
        ),
    ),
    Rule(
        "food-delivery",
        ("food-delivery", "food", "restaurants"),
        (
            "food delivery",
            "food ordering",
            "restaurant",
            "dining",
            "dine",
            "pizza",
            "burger",
            "cafe",
            "takeaway",
            "meal",
            "biryani",
            "food and beverage",
            "food beverage",
            "cloud kitchen",
            "bakery",
            "food",
        ),
    ),
    Rule(
        "travel",
        ("travel", "travel-hotels"),
        (
            "travel",
            "flight",
            "airline",
            "airfare",
            "air ticket",
            "hotel",
            "resort",
            "holiday",
            "vacation",
            "tour",
            "trip",
            "bus",
            "train",
            "railway",
            "cab",
            "taxi",
            "car rental",
            "cruise",
            "visa",
            "accommodation",
            "hostel",
            "stay",
            "homestay",
        ),
    ),
    Rule(
        "home-kitchen",
        ("home-kitchen", "home-and-kitchen", "home-appliances", "home", "home-decor"),
        (
            "home and kitchen",
            "home kitchen",
            "kitchen",
            "furniture",
            "furnishing",
            "home decor",
            "decor",
            "mattress",
            "bedding",
            "home appliance",
            "kitchen appliance",
            "cookware",
            "home improvement",
            "home",
            "garden",
            "lighting",
        ),
    ),
    Rule(
        "health",
        ("health", "health-wellness", "wellness", "pharmacy"),
        (
            "health",
            "wellness",
            "pharmacy",
            "medicine",
            "medical",
            "fitness",
            "nutrition",
            "supplement",
            "vitamin",
            "ayurveda",
            "ayurvedic",
            "diagnostic",
            "healthcare",
            "gym",
            "yoga",
        ),
    ),
    Rule(
        "books-education",
        ("books-education", "education", "books"),
        (
            "book",
            "ebook",
            "education",
            "course",
            "learning",
            "stationery",
            "exam",
            "tuition",
            "school",
            "college",
            "university",
        ),
    ),
    Rule(
        "entertainment",
        ("entertainment", "movies-entertainment"),
        ("entertainment", "movie", "music", "streaming", "ott", "game", "gaming", "concert"),
    ),
    Rule(
        "finance",
        ("finance", "financial-services", "insurance"),
        (
            "finance",
            "financial",
            "insurance",
            "loan",
            "credit card",
            "investment",
            "banking",
            "bank",
            "mutual fund",
            "demat",
            "trading",
            "payment",
            "wallet",
        ),
    ),
    Rule(
        "gifts",
        ("gifts", "gifts-flowers", "flowers"),
        ("gift", "flower", "cake", "hamper", "personalised", "personalized"),
    ),
    Rule(
        "automotive",
        ("automotive", "auto"),
        ("automotive", "auto", "car", "bike", "motorcycle", "tyre", "tire", "vehicle", "helmet"),
    ),
    Rule(
        "kids-baby",
        ("kids-baby", "baby-kids", "kids", "toys"),
        ("baby", "kids", "toy", "infant", "maternity", "diaper"),
    ),
    Rule(
        "e-commerce",
        ("e-commerce", "ecommerce", "shopping", "general", "online-shopping"),
        (
            "e commerce",
            "ecommerce",
            "marketplace",
            "online shopping",
            "online shop",
            "online store",
            "shopping",
            "general merchandise",
            "department store",
            "multi category",
            "all categories",
            "general",
            "retail",
        ),
    ),
)

# Wording that says nothing about the category. Skipped rather than recorded, or the
# review queue would fill with "Other" from every provider.
NOISE: frozenset[str] = frozenset(
    {
        "other", "others", "misc", "miscellaneous", "all", "uncategorized",
        "uncategorised", "default", "none", "na", "n a", "null", "unknown",
    }
)  # fmt: skip
