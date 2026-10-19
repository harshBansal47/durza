# BachatBase Smart Savings Platform

## Product intent

BachatBase should help people make a better-value purchase decision, not only
find a code at checkout. Preserve the live coupons, deals, stores, products,
price history, affiliate routing, click attribution, accounts, alerts, and
verification systems. Add editorial content and connect it to that existing
commerce data.

The public website is a separate Next.js project. This backend provides the
editorial and store-destination API foundation; the homepage and page layouts
below are the frontend implementation contract, not claims that those screens
have shipped in this repository.

## 1. Sitemap and URL rules

```text
/
├── /deals/                         Existing deal discovery and detail URLs
│   ├── /deals/{existing-deal-path}
│   ├── /deals/category/{category-slug}
│   └── /deals/store/{store-slug}
├── /coupons/                       Existing coupon discovery and reveal URLs
│   ├── /coupons/{existing-coupon-path}
│   ├── /coupons/category/{category-slug}
│   └── /coupons/store/{store-slug}
├── /stores/
│   └── /store/{store-slug}
├── /categories/
│   └── /categories/{category-slug}
├── /guides/
│   └── /guides/{guide-slug}
├── /reviews/
│   └── /reviews/{review-slug}
├── /compare/
│   └── /compare/{comparison-slug}
├── /collections/
│   └── /collections/{collection-slug}
├── /products/{product-slug}        Optional, when price history adds value
├── /search/?q={query}              Noindex
├── /account/                       Noindex
│   ├── /account/saved
│   └── /account/alerts
├── /about/
├── /how-we-work/
├── /affiliate-disclosure/
├── /privacy/
└── /terms/
```

The exact legacy paths must be read from the live frontend and search indexes
before deployment. Keep existing coupon/deal URLs stable wherever possible.
Use lowercase hyphenated editorial slugs. Index only canonical, useful,
published pages; noindex search results, account pages, tracking redirects, and
thin filter combinations. Include only canonical indexable URLs in type-specific
XML sitemaps. A retired URL gets a one-to-one 301 to its closest useful
successor, never a blanket redirect to `/`.

## 2. Database and content architecture

Existing `coupons`, `stores`, `products`, `price_points`, tracking, affiliate
destination, and attribution tables remain the source of truth for offers,
merchants, observed prices, and outbound clicks. A code-less offer remains a
coupon record with its existing deal semantics.

The additive migration `f3a41b7c9d20` introduces:

| Table | Purpose |
|---|---|
| `editorial_contents` | Guides, reviews, comparisons, and collections; shared publication, SEO, market, author, body-block, structured-data, featured, and canonical fields |
| `editorial_relations` | Curated editorial-to-editorial links with relation type and display order |
| `editorial_entity_links` | Typed links to existing stores, products, coupons/deals, and categories; database check requires exactly one target |
| `editorial_faqs` | Ordered page-specific FAQs |
| `store_faqs` | Ordered merchant-specific FAQs |
| `store_relationships` | Curated alternatives / related stores |
| `stores.pricing_summary`, `stores.pricing_checked_at` | Optional merchant pricing summary with freshness metadata |

`structured_data` stores content-type-specific review verdict, comparison
feature/pricing rows, guide methodology, and collection curation metadata. Use
stable keys validated by the editorial authoring workflow; move fields to
dedicated relational columns if reporting or filtering begins to depend on them.
`content_blocks` is structured JSON, not trusted HTML; the frontend must render
known block types and sanitize any rich text. `Page` remains for static/legal
pages rather than becoming the editorial CMS.

Public API:

- `GET /api/v1/editorial?content_type=guide` lists published indexable editorial
  items.
- `GET /api/v1/editorial/{content_type}/{slug}` returns a published editorial
  page, including curated links and FAQs.
- `GET /api/v1/editorial/homepage` returns featured guide, comparison, review,
  and collection cards. Existing category, store, coupon/deal APIs supply the
  remaining homepage modules.
- `GET /api/v1/stores/by-slug/{slug}/destination` aggregates the existing store,
  active deals/coupons, FAQs, alternatives, and linked editorial content.

Admins/editors author with `POST /api/v1/editorial`, `PATCH
/api/v1/editorial/id/{id}`, and the store create/update API. Only admins delete
editorial records. Draft/in-review content is not exposed by public list/detail
routes; `GET /api/v1/editorial/manage` is the role-protected editorial queue.
Publishing requires a summary and at least one content block. Editorial
reads include derived `adsense_eligible`: true only for published, indexable
guides, reviews, and comparisons. This is a frontend placement guard as well as
a content contract; never rely on ad code embedded in an article body.

## 3. Navigation

**Desktop:** logo; universal search (“Search deals, stores, products and
guides”); Deals, Coupons, Stores, Categories, Guides, Reviews, Comparisons,
Collections; account/saved items. If the header is crowded, put all sections in
an Explore menu while keeping Deals, Coupons, Guides, Compare, and Stores
directly visible.

**Mobile:** compact logo/search/account header and bottom Home, Search, Deals,
Saved navigation. Remaining sections are available in the menu. Search
suggestions label result type (store, product, offer, guide) and search result
pages use noindex.

## 4. Homepage wireframe

```text
[Header + universal search]
[Hero: “Save money before you buy.” Supporting copy + search + Explore guides]
[Popular categories]
[Featured buying guides: benefit, audience, updated date]
[Trending comparisons: A vs B, concise best-for description]
[Latest deals: existing offer feed and tracked links]
[Popular stores: current offers + destination links]
[Best coupons: verification and expiry visible]
[Recent reviews: verdict context + checked date]
[Newsletter: consent and frequency expectations]
[Footer: About, How we work, disclosure, help, legal, sitemap]
```

Lead with helpful discovery, not a wall of coupon cards. Editorial recommendations
must be distinguishable from paid placements; offer modules must use existing
freshness and tracking behavior.

## 5. Store destination wireframe

```text
[Breadcrumbs]
[Store overview: logo, description, official site, market, disclosure]
[Current deals] [Current coupons: code, verification and expiry state]
[Store information and relevant categories]
[Pricing summary, currency and last-checked date, when maintained]
[FAQs]
[Alternatives]
[Related guides] [Related comparisons] [Reviews]
```

Use the destination API and keep outbound journeys on the existing tracked
redirect path. Store pages are conversion-oriented and are not AdSense-eligible.
Do not imply stale offers or pricing are current.

## 6. Guide wireframe

```text
[Breadcrumbs]
[H1 + summary + intended audience]
[Author/reviewer + publication/update dates + methodology + disclosure]
[Table of contents]
[Quick answer / top picks linked to existing product/store records]
[Guide sections: criteria, trade-offs, recommendations, pricing context]
[Comparison table or links]
[Relevant current offers, secondary to the advice]
[FAQs]
[Related guides, reviews, comparisons, stores]
[AdSense placements in the article body only]
```

## 7. Review wireframe

```text
[Breadcrumbs]
[H1 + concise verdict + best for / not for]
[Reviewer + publication/update dates + pricing checked date + disclosure]
[Key takeaways]
[Pricing, currency/market, billing basis, checked date]
[Pros and cons]
[Features, usability, support, limitations, alternatives]
[Methodology; disclose hands-on versus research-based review]
[Current relevant offers] [Alternatives] [Comparisons] [Related guides]
[FAQs]
[AdSense placements in the article body only]
```

Do not invent ratings or add review structured data without a real, documented
review basis.

## 8. Comparison wireframe

```text
[Breadcrumbs]
[H1: A vs B + summary verdict and best-for guidance]
[Compared/checked date + affiliate disclosure]
[At-a-glance summary]
[Accessible feature table with explicit values and units]
[Pricing table: currency, plan/billing basis, checked date]
[Detailed feature, usability and limitation sections]
[Who should choose each option]
[Reviews] [Related guides] [Current relevant offers]
[FAQs]
[AdSense placements in the article body only]
```

Comparison data should be structured and dated. Avoid arbitrary overall scores
where the attributes cannot fairly be compared.

## 9. Internal linking and monetization

Create topical clusters, not isolated articles. In a hosting cluster, the
“best hosting” guide links to hosting reviews, comparisons, store destinations,
and relevant offers. Each review links to the guide, comparisons, alternatives,
and merchant page. Comparisons link to both reviews and their related offers.
Store destinations link to current coupons/deals, alternatives, reviews,
guides, and comparisons. Offers link to their merchant destination and, when
useful, the supporting buying advice. Collections link to their member content,
and members link back when relevant.

Drive “Related” modules, breadcrumbs, and contextual links from curated
`editorial_relations` and `editorial_entity_links`; do not generate irrelevant
boilerplate links. Editorial disclosure must be visible near affiliate
recommendations and in the site-wide “How we work” and disclosure pages.

AdSense is permitted only for published, indexable guides, reviews, and
comparisons, within informational article bodies. It is prohibited on
collections, store pages, coupon reveal pages, deal pages, tracking redirects,
and checkout-oriented flows. Paid inventory must be clearly labeled. The
existing affiliate click path remains the monetization path on commerce pages.

## 10. Incremental migration and release plan

1. Inventory live frontend URLs, canonicals, backlinks, organic traffic, and
   redirects. Establish a URL preservation map before touching templates.
2. Apply the additive editorial migration. Keep coupons, deals, store IDs,
   outbound tracking, attribution, and existing APIs intact.
3. Build a vertical slice with one guide, review, comparison, and destination
   page connected to existing store/product/offer records. Validate editor
   publishing and public API responses.
4. Add shared frontend navigation, breadcrumbs, cards, disclosures, and the
   four editorial layouts. Compose the homepage from the new editorial
   homepage endpoint and existing category/store/offer endpoints.
5. Launch one focused topical cluster before adding breadth. Use the cluster to
   validate accuracy, internal links, content maintenance, and offer freshness.
6. Preserve current coupon/deal routes. Add one-to-one 301 redirects only for
   URLs that genuinely change; do not create redirect chains.
7. Gate AdSense rendering on the API's eligibility flag and separately test
   that ineligible pages never load ad placements. Validate canonical tags,
   noindex directives, disclosure, sitemap membership, and structured data.
8. Monitor indexed pages, organic entrances, user engagement, offer freshness,
   tracked outbound clicks, conversion attribution, and revenue by page type.
   Expand clusters based on reader usefulness and observed demand.

### Release checks

- Run the new migration up/down/up against Postgres before production; test
  model and API behavior under SQLite as part of the normal suite.
- Verify public endpoints never expose unpublished content or raw affiliate
  destinations.
- Verify AdSense gates use `adsense_eligible`, not URL substring heuristics.
- Confirm frontend keeps existing reveal, verification, click tracking, and
  affiliate attribution behavior unchanged.
- Submit only canonical indexable URLs to the sitemap and spot-check redirect
  targets and Search Console coverage after rollout.
