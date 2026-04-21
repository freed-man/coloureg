# Coloureg — VIN Lookup API Research & Integration

**Date:** 21 April 2026
**Author:** Roland Freedman
**Purpose:** Complete record of the research, provider comparison, negotiation, and integration work for resolving UK vehicle registration numbers (VRM/reg) to VINs for Coloureg.

---

## Background

Coloureg is a UK vehicle paint code lookup service for private vehicle owners. The user enters their UK registration number, and the service resolves the corresponding OEM paint code so they can purchase touch-up paint or arrange resprays in the correct factory colour.

To do this, the service needs the **full 17-character VIN**, because paint codes vary by trim, production plant, and production batch within the same make and model — the VIN is the only reliable identifier to resolve the correct code. The VIN is then used with Partslink24 (or an equivalent manufacturer parts catalogue) to retrieve the OEM paint code.

**Key constraint:** In the UK, full VIN access from a VRM is tightly controlled by DVLA licensing. Most commercial APIs either don't return the VIN at all, return it in a restricted form (`LICENSE_RESTRICTED` or last-5-digits only), or require a permitted-purpose application.

---

## Providers Investigated

### 1. DVLA Vehicle Enquiry Service (VES) — Free, official

- **URL:** https://developer-portal.driver-vehicle-licensing.api.gov.uk/apis/vehicle-enquiry-service/vehicle-enquiry-service-description.html
- **Returns:** Make, model, colour, fuel type, MOT status, tax status, year of manufacture
- **Does NOT return:** VIN
- **Verdict:** Useless for Coloureg's primary need. Could be used as a secondary confirmation display ("We found your 2018 Audi A3 in Mythos Black") but doesn't solve the paint code problem.

### 2. DVLA KADOE API — Official, permitted purpose required

- **URL:** https://developer-portal.driver-vehicle-licensing.api.gov.uk/apis/kadoe/kadoe-description.html
- **Accepts:** VRM or VIN as input (useful — supports both directions)
- **Returns:** Registration, make, model, tax status, **and keeper PII** (name, address)
- **Application:** https://register-for-kadoe.driver-vehicle-licensing.api.gov.uk
- **Status:** APPLIED AND REJECTED on 21 April 2026.

#### Rejection reply (from `kadoe-interest@dvla.gov.uk`)

> Unfortunately, you will not be eligible to apply for KADOE API. The service has a set of permitted purposes and potential users have to demonstrate reasonable cause of why the data is required in adherence to legislative and regulatory requirements and compliance to data protection guidance.
>
> — Andrea, Vehicle Enquiries, Service Management, DVLA

KADOE is designed for entities with statutory or contractual reasons to identify a vehicle's keeper (parking enforcement, insurance claims, finance recovery, police, etc.). Coloureg's use case (paint code lookup for consumer users) does not qualify as a permitted purpose.

### 3. Vehicle Data Global (VDG) / ukvehicledata.co.uk — **CHOSEN (secondary)**

- **URL:** https://ukvehicledata.co.uk / https://vehicledataglobal.com
- **Control Panel:** https://cp.ukvehicledata.co.uk
- **Account:** Active (Roland Freedman, Coloureg, Support Code `C68F3B`)
- **Tier:** PayGo Tier 1

#### VIN activation
VIN field on the VDG account is **activated**. By default VDG returns `"Vin": "LICENSE_RESTRICTED"` and `"VinLast5"`, but on request and use-case approval, full VIN is unlocked. A human account manager reviewed the use case and approved.

Use case submitted (60-char fields):
- Data required: `VIN only — no keeper data, not stored or cached`
- Reason: `VIN used to look up OEM paint code (not stored or logged)`
- Annual volume: `Under 5,000 per year, initially`

#### Pricing (PayGo Tier 1)
- Vehicle Details (incl. VIN): **£0.15 per lookup**
- Minimum top-up: £50 credit (lasts 12 months)
- No monthly minimum, no subscription

#### API details
- **Base URL:** `https://uk.api.vehicledataglobal.com/r2/lookup`
- **Method:** `GET`
- **Auth:** API key as query parameter `apikey` (lowercase), confirmed from the portal's own generated Direct API Link. (The PHP sample showed Bearer header; the OpenAPI spec listed `ApiKey` capitalised. The portal URL is the authoritative format.)
- **Parameters (lowercase):** `apikey`, `packagename`, `vrm` (or `vin`, `ukvdid`, `uvc`)

#### Keys
- **Live key:** `ccd7432d-45be-45ab-9d58-ec1f029c88f3`
- **Sandbox key:** `b1640f13-c6dd-4dd9-b24b-82dda1f5062a`

#### Sandbox restrictions
- Only VRMs/VINs/postcodes containing the letter 'A' can be searched
- Data is up to 12 months out of date
- VINs cannot be used as input in sandbox
- Data may not be used in production / for profit
- No billing occurs — `TransactionCost: null`, balance unchanged

#### Confirmed working response
Test lookup of `RE22MKA` returned:
- VRM: `RE22MKA`
- **VIN: `WMW32DH0302S62292`** (full 17 chars, real MINI WMI)
- Make/Model: `MINI COOPER CLASSIC`
- Year: `2022`
- Colour: `GREY`
- Plus full Model Details, Safety (NCAP), Emissions, Performance, etc.

#### Paint code follow-up
Live chat with Tracy (VDG) on 21 April 2026:

> Me: Any chance you provide a colour code for each VRN rather than just a generic name "black" but e.g. LY9B/A2 (Brilliant Black)?
>
> Tracy: We don't have paint codes at the moment, however this data will be available in May.

Follow-up email sent to `help@vehicledataglobal.com` requesting written confirmation and a more specific date. **If confirmed, this would remove the need for a separate Partslink24 step entirely — VDG would become a single-call solution (reg → paint code).**

### 4. VehicleSmart — Declined (contract sent but not signed)

- **URL:** https://vehiclesmart.com/vehicle-smart-api.html
- **Contact:** James, `info@vehiclesmart.com`
- **Status:** Use case approved, DocuSign API Agreement sent, NOT signed.

#### Approval exchange
I emailed describing the use case. James replied:

> Thanks Roland, that's a valid use-case and will likely be approved. If you let me know your full name I'll send over our API Agreement for you to review, sign and return. We can then set you up with a free 30-day trial.

#### Pricing (Extended Vehicle Data — includes VIN)
- 8p per lookup up to 2,000/month, dropping by volume
- **Minimum service charge: £50 + VAT (£60/month)** if usage generates less than £50 payable
- Free 30-day trial: 50 free VIN lookups + 1,000 free Basic lookups

#### Why declined
CheckCarDetails came back with £0.03/lookup and £20/month minimum — substantially cheaper at Coloureg's expected volumes. VehicleSmart only becomes competitive above ~2,000 lookups/month.

#### Notable agreement clauses (for reference if revisited)
- **Clause 5.6:** Extended Vehicle Data (VIN) cannot be cached longer than 24 hours
- **Clause 2.2 / 3.2:** Must give written notice before trial ends, auto-terminates otherwise; 30-day notice post-trial
- **Clause 8.5:** Client pays for all lookups on the API key, including unauthorised use if the key leaks
- **Clause 13.8:** VehicleSmart's liability capped at £500 or 3 months' fees
- **Clause 13.4 / 13.7:** Client indemnities to VehicleSmart are unlimited
- Permitted Purpose and Permitted Platforms fields on the Commercial Terms must be filled in — blank fields mean no licence

**No response sent to James since the decline — can come back later if needed.**

### 5. CheckCarDetails — **CHOSEN (primary)**

- **URL:** https://www.checkcardetails.co.uk/api/vehicledata
- **Contact:** `support@checkcardetails.co.uk`
- **Status:** Approved, custom endpoint to be built.

#### Their reply
> Yes, we can build something like this so that you can enter a registration number and receive the full VIN in return. Please note that the full VIN must not be displayed to the general public in any way. Under DVLA requirements, this data can only be used for your internal purposes. The cost for this would be £0.03 per lookup.

#### Pricing
- **£0.03 per VIN lookup** (custom endpoint)
- **£20/month minimum commitment** (usage comes out of that balance first, extras billed above)
- £20 covers ~666 VIN lookups

#### Paint code question (outstanding)
Follow-up email sent asking whether any package returns the paint code directly from the reg, or whether it could be built as a custom data point. Awaiting reply.

#### Compliance note
The "VIN must not be displayed to the public" clause aligns with Coloureg's intended architecture — VIN is an internal resolver field only; the end user only ever sees the paint code.

---

## Cost Comparison at Typical Volumes

| Monthly usage | VDG (£0.15) | CheckCarDetails (£0.03) | VehicleSmart (8p + min) |
|---|---|---|---|
| 10 lookups | £1.50 | £20 (minimum) | £60 (min + VAT) |
| 100 lookups | £15.00 | £20 (minimum) | £60 (min + VAT) |
| 500 lookups | £75.00 | £20 (covers 666) | £60 (min + VAT) |
| 1,000 lookups | £150.00 | £30 (£20 + 334 × £0.03) | ~£96 (incl. VAT) |
| 2,000 lookups | £300.00 | £60 (£20 + 1,334 × £0.03) | ~£192 (incl. VAT) |

### Break-even analysis
- **Under 133 lookups/month:** VDG is cheapest (£20 ÷ £0.15 = 133)
- **133–2,000 lookups/month:** CheckCarDetails wins clearly
- **Above 2,000 lookups/month:** Reconsider VehicleSmart

---

## Final Decision

1. **Primary provider: CheckCarDetails** — £0.03 per VIN lookup, £20/month minimum, custom endpoint being built.
2. **Fallback: VDG** — PayGo account active with £49.70 balance, full VIN activated, sandbox working. Zero ongoing cost if unused.
3. **VehicleSmart: declined** — too expensive at Coloureg's stage/volume. Contract saved for reference if ever revisited.
4. **KADOE: unavailable** — DVLA rejected the application; permitted purposes don't cover consumer paint code lookup.

### Pragmatic development path
Because CCD's custom endpoint is still being built, **development work starts on VDG's sandbox** (free, same response structure, already accessible). Once CCD's endpoint is live, swap the base URL and API client. Architecture keeps provider-specific code in one module so swapping is a small change.

---

## VDG API Integration (Python / Django)

### Authoritative request format
From the VDG portal's own generated Direct API Link:

```
https://uk.api.vehicledataglobal.com/r2/lookup?apikey=<KEY>&packagename=VehicleDetails&vrm=<VRM>
```

- Parameters are **lowercase**: `apikey`, `packagename`, `vrm`
- Method is `GET`
- No headers required — auth is via query parameter

### Working Python client

**File: `coloureg/services/vdg.py`**

```python
"""Vehicle Data Global API client for VRM -> VIN lookups."""
import os
import requests

VDG_BASE_URL = "https://uk.api.vehicledataglobal.com/r2/lookup"
VDG_API_KEY = os.environ["VDG_API_KEY"]
VDG_PACKAGE = "VehicleDetails"

STATUS_SUCCESS = 0
STATUS_SUCCESS_WITH_WARNINGS = 1


class VdgError(Exception):
    """Raised when a VDG lookup fails."""


def lookup_by_vrm(vrm: str, timeout: int = 10) -> dict:
    """Look up a vehicle by UK registration mark."""
    params = {
        "apikey": VDG_API_KEY,
        "packagename": VDG_PACKAGE,
        "vrm": vrm.upper().replace(" ", ""),
    }

    try:
        resp = requests.get(VDG_BASE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise VdgError(f"Network error: {e}") from e

    data = resp.json()
    status = data.get("ResponseInformation", {}).get("StatusCode")

    if status not in (STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS):
        msg = data.get("ResponseInformation", {}).get("StatusMessage", "Unknown")
        raise VdgError(f"VDG lookup failed ({status}): {msg}")

    return data


def extract_vehicle_info(response: dict) -> dict:
    """Pull the fields we care about out of the VDG response."""
    results = response.get("Results", {}) or {}
    vd = results.get("VehicleDetails", {}) or {}
    ident = vd.get("VehicleIdentification", {}) or {}
    colour = vd.get("VehicleHistory", {}).get("ColourDetails", {}) or {}
    model = results.get("ModelDetails", {}).get("ModelIdentification", {}) or {}

    return {
        "vrm": ident.get("Vrm"),
        "vin": ident.get("Vin"),
        "make": ident.get("DvlaMake"),
        "model_dvla": ident.get("DvlaModel"),
        "model_full": model.get("Model"),
        "model_variant": model.get("ModelVariant"),
        "series": model.get("Series"),
        "year": ident.get("YearOfManufacture"),
        "current_colour": colour.get("CurrentColour"),
        "original_colour": colour.get("OriginalColour"),
    }
```

### Example Django view

**File: `coloureg/views.py`**

```python
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .services.vdg import lookup_by_vrm, extract_vehicle_info, VdgError


@require_GET
def lookup_reg(request):
    vrm = request.GET.get("vrm", "").strip()
    if not vrm:
        return JsonResponse({"error": "Registration required"}, status=400)

    try:
        raw = lookup_by_vrm(vrm)
        info = extract_vehicle_info(raw)
    except VdgError as e:
        return JsonResponse({"error": str(e)}, status=502)

    # VIN is used server-side only to resolve paint code.
    # Do NOT return VIN to the frontend.
    return JsonResponse({
        "make": info["make"],
        "model": info["model_full"] or info["model_dvla"],
        "year": info["year"],
        "colour": info["current_colour"],
        # paint_code: resolved server-side via Partslink24 using info["vin"]
    })
```

### Environment configuration

Add to `env.py` (matching mp4-autocommit workflow, gitignored):

```python
import os
# Use sandbox key during development
os.environ["VDG_API_KEY"] = "b1640f13-c6dd-4dd9-b24b-82dda1f5062a"
# Switch to live for production:
# os.environ["VDG_API_KEY"] = "ccd7432d-45be-45ab-9d58-ec1f029c88f3"
```

### Sample confirmed-working response

Test of `RE22MKA` on sandbox returned (abridged):

```json
{
  "RequestInformation": {
    "PackageName": "VehicleDetails",
    "SearchTerm": "RE22MKA",
    "SearchType": "VRM"
  },
  "ResponseInformation": {
    "StatusCode": 0,
    "StatusMessage": "Success",
    "IsSuccessStatusCode": true,
    "QueryTimeMs": 7
  },
  "BillingInformation": {
    "AccountType": 1,
    "AccountBalance": 49.70,
    "TransactionCost": null,
    "BillingResult": 0
  },
  "Results": {
    "VehicleDetails": {
      "UkvdId": "V-BBBCMJ",
      "VehicleIdentification": {
        "Vrm": "RE22MKA",
        "Vin": "WMW32DH0302S62292",
        "VinLast5": "62292",
        "DvlaMake": "MINI",
        "DvlaModel": "COOPER CLASSIC",
        "YearOfManufacture": 2022,
        "DvlaFuelType": "PETROL"
      },
      "VehicleHistory": {
        "ColourDetails": {
          "CurrentColour": "GREY",
          "OriginalColour": "GREY",
          "NumberOfColourChanges": 0
        }
      }
    },
    "ModelDetails": {
      "ModelIdentification": {
        "Make": "Mini",
        "Range": "Hatch",
        "Model": "Cooper Classic",
        "ModelVariant": "Cooper Classic",
        "Series": "F55",
        "Mark": 3
      }
    }
  }
}
```

### Status codes reference

From the OpenAPI spec — full list of StatusCode values:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | SuccessWithResultsBlockWarnings |
| 2 | NoResultsFound |
| 3 | BillingFailure |
| 4 | FilterIpFailure |
| 5 | FilterCustomFailure |
| 6 | SandboxValidationFailure |
| 7 | UnknownApiKey |
| 8 | UnknownPackage |
| 13 | InvalidSearchTerm |
| 14 | NoSearchTermFound |
| 17 | InformationTemporarilyUnavailable |
| 19 | ApiKeyReachedConcurrentLimit |
| 20 | ApiKeyReachedDailyLimit |
| 25 | NoDvlaRegistrationDataAvailable |
| 26 | NoMatchFound |

For UX: map codes 2, 25, 26 to a user-friendly "We couldn't find that registration" message; everything else is a generic error.

### Rate limits (sandbox key)
- 100 requests per day
- 10 requests per second

---

## Architecture Notes

### What to store vs. what to discard

The safest pattern, regardless of which provider is active:

1. **Do not store the VIN long-term.** Look it up, use it to resolve the paint code, discard it.
2. **Cache the paint code against the reg** (`reg → paint_code`) if re-query avoidance is worthwhile. Paint codes don't change, so this is cheap and valid. Reg and paint code are not personal data.
3. **Keep call logs** (timestamp, reg, success/fail) for billing reconciliation and debugging, but scrub the VIN from them.

This aligns with:
- The statements made to VehicleSmart and VDG during use-case approval ("not stored or cached")
- CheckCarDetails' "internal use only, not displayed to the public" clause
- VehicleSmart's 24-hour cache cap (clause 5.6) — not relevant now but good habit

### API key protection
- Keys go in `env.py` (gitignored), never committed
- Sandbox key for dev, live key only in production environment variables
- If a live key is ever exposed, rotate it immediately — the client is billed for any use of the key, authorised or not

### Provider abstraction
Keep the VDG-specific logic in one module (`services/vdg.py`). If/when CheckCarDetails' custom endpoint is ready, add `services/ccd.py` with the same `lookup_by_vrm` / `extract_vehicle_info` interface, and swap at the view level via a settings flag.

---

## Outstanding Items

- [ ] **Paint code availability from VDG in May 2026** — awaiting written confirmation from `help@vehicledataglobal.com` with a specific date. If confirmed, this potentially simplifies the whole architecture (reg → paint code in one call, Partslink24 step removed).
- [ ] **Paint code availability from CheckCarDetails** — follow-up email sent asking if any package returns the paint code, or if it can be built as a custom data point. Awaiting reply.
- [ ] **CheckCarDetails custom VRN→VIN endpoint** — build timeline and confirmation of when £20/month billing starts (dev phase or go-live only) not yet confirmed. Awaiting reply.
- [ ] **Partslink24 account** — Coloureg will have one but it is not yet set up / purchased. Once available, VIN → paint code resolution step can be fully tested.

---

## Email Trail (summary)

1. **KADOE application** → submitted 21 Apr → rejected 21 Apr (Andrea, DVLA)
2. **VehicleSmart initial enquiry** → approved in principle → API Agreement sent → **not signed**
3. **VDG VIN activation request** → approved → live at £0.15/lookup
4. **VDG paint code chat with Tracy** → "available in May" → email confirmation requested
5. **CheckCarDetails custom VIN endpoint** → approved, £0.03/lookup → clarifications requested
6. **CheckCarDetails paint code enquiry** → awaiting reply

---

## Reference Links

- DVLA VES API: https://developer-portal.driver-vehicle-licensing.api.gov.uk/apis/vehicle-enquiry-service/vehicle-enquiry-service-description.html
- DVLA KADOE API: https://developer-portal.driver-vehicle-licensing.api.gov.uk/apis/kadoe/kadoe-description.html
- VDG pricing: https://vehicledataglobal.com/Pricing
- VDG DVLA data schema: https://vehicledataglobal.com/DataSources/DVLA
- VDG control panel: https://cp.ukvehicledata.co.uk
- VehicleSmart API: https://vehiclesmart.com/vehicle-smart-api.html
- VehicleSmart Extended Data example: https://vehiclesmart.com/api-example/extended.html
- CheckCarDetails API: https://www.checkcardetails.co.uk/api/vehicledata

---

*End of record — Coloureg VIN lookup research & integration, 21 April 2026.*
