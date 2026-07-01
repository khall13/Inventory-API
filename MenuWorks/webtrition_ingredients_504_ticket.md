# Webtrition support ticket — /ingredients 504 (draft)

**To:** _DL_US_WebtritionSupport@compass-usa.com
**Subject:** Web Services 3.0 ingredients endpoint times out (504) on Production

> Draft for review. Copy the body below. Do not paste any credential values; the WT-Client-Id
> account is referenced, not disclosed. (Email copy intentionally avoids dashes per house style;
> technical identifiers such as X-IBM-Client-Id are kept verbatim because they are literal tokens.)

---

Hello Webtrition Team,

We are a Web Services 3.0 client on the Production environment (our WT-Client-Id account was added
in June 2026, business unit 4990). We are integrating menu and recipe data and most endpoints work
well, but the ingredients endpoint consistently times out.

Endpoints that return 200 for us:
  GET /v3/units_of_measure
  GET /v3/business_units
  GET /v3/business_units/-1/products
  GET /v3/business_units/-1/recipes/{mrn}
  GET /v3/business_units/-1/menu_items   (with an options.filter date window)

Endpoint that fails:
  GET /v3/business_units/-1/ingredients   returns 504 Endpoint request timed out on every call.

This is not a filter problem. The endpoint accepts the filter (we no longer receive the 4000
"Filter is a required parameter" error), but the gateway times out. We reproduced the 504 three
ways, each after five retries with exponential backoff:
  1. one mrn, no include flags:    options={"filter":{"mrns":["100079"]}}
  2. one mrn with allergens only:  options={"filter":{"mrns":["100079"]},"include":{"allergens":true}}
  3. two mrns, no include flags:   options={"filter":{"mrns":["100079","100088"]}}
We see the same timeout when scoping to business unit 4990 instead of -1.

Because recipe detail via /recipes/{mrn} works reliably, the problem appears specific to the
ingredients endpoint rather than to detail calls in general.

Could you please investigate why the ingredients endpoint times out for our client, and advise the
supported way to retrieve ingredient detail? For example, is there a maximum mrns batch size, a
required filter field we are missing, or an alternative endpoint we should use?

Example request and response:
  GET https://api.compass-usa.com/stg/v3/business_units/-1/ingredients?options={"filter":{"mrns":["100079"]}}
  Response: 504 Endpoint request timed out

Thank you for your help,
Kenneth Hallwachs
PreciTaste
