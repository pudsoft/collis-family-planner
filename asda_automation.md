# ASDA Grocery Automation — Research & Plan

## Goal

Automate grocery shopping via ASDA's website — ultimately: take a shopping list, push it to an ASDA basket, ready to review and checkout.

---

## Terms of Service & robots.txt

### robots.txt
No general `Disallow` rules. Only blocks AI training crawlers (GPTBot, ClaudeBot etc.) under EU Directive 2019/790. A personal automation script using a normal browser context is unaffected.

### Terms of Service
> *"You must not collect any content or information from other users using automated methods (such as bots, robots, spiders or scrapers) without ASDA's prior consent, and you must not use these methods to access the service in any other way."*

**Assessment:** Personal script logging into your own account and adding items to your own basket is not a meaningful enforcement risk.

---

## Architecture

ASDA's frontend uses two distinct API layers:

| Layer | Base URL | Auth | Used for |
|-------|----------|------|---------|
| SFCC (Salesforce Commerce Cloud) | `www.asda.com/mobify/proxy/ghs-api/` | Bearer JWT (session) | Basket, customer, products |
| api2 | `api2.asda.com/external/` | `ocp-apim-subscription-key` + cookies | Orders, regulars, profile |
| Algolia | `8i6wskccnv-dsn.algolia.net/` | Static API key | Product search |

---

## Discovered Endpoints

### Authentication
Auth is session-based. The Bearer JWT and cookies are obtained by logging in through a real browser session (Playwright with Edge profile). The JWT expires in ~30 minutes; session cookies last longer.

Key header for SFCC calls:
```
Authorization: Bearer <JWT>
```

Key header for api2 calls:
```
ocp-apim-subscription-key: bc042eff107c4bca87dccb19ae707d16
```

Algolia headers (static, public search key):
```
x-algolia-application-id: 8I6WSKCCNV
x-algolia-api-key: 03e4272048dd17f771da37b57ff8a75e
```

---

### Constants (from captured session)

| Name | Value |
|------|-------|
| Organization ID | `f_ecom_bjgs_prd` |
| Customer ID | `ab1m1Gk9LPeEpiEMMlE3P4BCbx` |
| Store ID | `4383` |
| Basket ID | `eec4669732541b3870c3c6a211` *(changes per session)* |
| Algolia App ID | `8I6WSKCCNV` |
| Algolia Index | `ASDA_PRODUCTS` |

---

### Product Search (Algolia)

```
POST https://8i6wskccnv-dsn.algolia.net/1/indexes/*/queries
```

Request body:
```json
{
  "requests": [
    {
      "indexName": "ASDA_PRODUCTS",
      "query": "doritos",
      "params": "hitsPerPage=10&clickAnalytics=true&optionalFilters=[]&filters=(STATUS:A OR STATUS:I) AND NOT DISPLAY_ONLINE:false AND NOT UNTRAITED_STORES:4383 AND STOCK.4383 > 0 AND NOT PRODUCT_TYPE:Bundle&attributesToRetrieve=[\"STATUS\",\"BRAND\",\"CIN\",\"NAME\",\"AVG_RATING\",\"RATING_COUNT\",\"PRICES.EN\",\"SALES_TYPE\",\"MAX_QTY\",\"STOCK.4383\",\"IS_FROZEN\",\"IS_BWS\",\"PACK_SIZE\",\"IMAGE_ID\"]"
    }
  ]
}
```

Response hits contain: `NAME`, `PRICES.EN` (price), `CIN` (product ID used for add-to-basket), `PACK_SIZE`, `BRAND`, `STOCK.4383`.

**Note:** The timestamp-based filters (`START_DATE`, `END_DATE`, `PURCHASE_END_DATE_FTO`) use Unix epoch seconds and must be set to `Date.now() / 1000` at call time.

---

### Get Customer's Basket ID

```
GET https://www.asda.com/mobify/proxy/ghs-api/customer/shopper-customers/v1/organizations/f_ecom_bjgs_prd/customers/{customerId}/baskets?siteId=ASDA_GROCERIES
```

Returns `baskets[0].basketId` — needed for add-to-basket calls.

---

### Add Item to Basket

```
POST https://www.asda.com/mobify/proxy/ghs-api/checkout/shopper-baskets/v1/organizations/f_ecom_bjgs_prd/baskets/{basketId}/items?siteId=ASDA_GROCERIES
```

Request body (array — can add multiple items at once):
```json
[
  {
    "productId": "5779405",
    "quantity": 2,
    "price": 1.47
  }
]
```

`productId` = `CIN` from Algolia search results. `price` must match the current price from Algolia.

---

### Past Orders

List:
```
GET https://api2.asda.com/external/ghs/order/v1/list?olderOrderLimit=6
```

Detail:
```
GET https://api2.asda.com/external/ghs/order/v1/detail/{orderId}
```

---

### Regulars (Frequently Bought)

```
GET https://api2.asda.com/external/subs/v1/product/regulars
```

Returns `products[]` with `product_id` and `quantity` — a ready-made "usual shop" list.

---

### Customer Profile

```
GET https://api2.asda.com/external/customers/v1/profile
```

---

### Product Lists (Favourites / Wishlists)

```
GET https://www.asda.com/mobify/proxy/ghs-api/customer/shopper-customers/v1/organizations/f_ecom_bjgs_prd/customers/{customerId}/product-lists?siteId=ASDA_GROCERIES
```

---

### Shopper Context (Store Selection)

```
PATCH https://www.asda.com/mobify/proxy/ghs-api/shopper/shopper-context/v1/organizations/f_ecom_bjgs_prd/shopper-context/{sessionId}?siteId=ASDA_GROCERIES
```

Body:
```json
{
  "assignmentQualifiers": { "storeId": "4383" },
  "customQualifiers": { "storeId": "4383", "is_colleague": false }
}
```

---

## Discovery Infrastructure

| File | Purpose |
|------|---------|
| `asda_discover.js` | Opens Edge with real profile, captures all API calls while you browse, appends to `asda_api_calls.json` |
| `asda_api_calls.json` | Accumulated captures from all discovery runs |

### Running the Discovery Script

**Close Edge first:**
```powershell
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
```

```
node asda_discover.js
```

Captures calls to `asda.com`, `algolia.net`, and `algolianet.com`. Results are **appended** to the existing JSON file (not overwritten).

---

## Next Steps

- [ ] Build `asda_shop.js` — the actual automation script:
  - Launch Edge with persistent profile (same approach as discovery)
  - Wait for page to load, extract Bearer JWT from intercepted request headers
  - Accept a shopping list (text file or Family Planner integration)
  - For each item: search Algolia → pick best match → add to basket
  - Print basket summary
- [ ] Session persistence — cache the JWT so we don't need a browser open for every run (JWT lasts ~30 min; cookies last longer)
- [ ] Integrate with Family Planner shopping list feature
