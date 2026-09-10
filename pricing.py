"""Per-carrier pricing engine implemented from the B2C Logistics Commercials sheets."""
from __future__ import annotations

import math

CARRIER_BILLING_NAME = {
    "delhivery": "delhivery",
    "bluedart": "bluedart plus",
    "dtdc": "dtdc",
    "ekart": "ekart",
    "shadowfax": "shadowfax",
    "velocity": None,
    "amazon": None,
    "elastic": None,
}


def resolve_zones(carrier_id, zone, data):
    """Returns (system_zone, final_zone) for a carrier zone using the
    Billing Zone Mapping + Actual Zone Name sheets."""
    if not zone:
        return None, None
    billing = data.get("billing_zone_mapping", {})
    actual = data.get("actual_zone_name", {})
    system = None
    key = CARRIER_BILLING_NAME.get(carrier_id)
    if key and key in billing:
        table = billing[key]
        system = table.get(zone)
        if system is None:
            for k, v in table.items():
                if k.lower() == zone.lower():
                    system = v
                    break
    final = actual.get(system) if system else None
    if final is None:
        final = actual.get(zone)
        if final is None:
            for k, v in actual.items():
                if k.lower() == zone.lower():
                    final = v
                    break
    return (system, final) if (system or final) else (None, None)


def r2(x):
    return round(float(x) + 1e-9, 2)


def _grams(weight_kg):
    return max(0, int(round(float(weight_kg) * 1000)))


# ---------------------------------------------------------------------------
# Delhivery Surface (W.E.F 01-02-2024)
# ---------------------------------------------------------------------------
def price_delhivery(zone, weight_kg, movement, vol_tier, params):
    movement = (movement or "Fwd").upper()
    if zone == "B_SPL":
        if movement == "DTO":
            return None, "B_SPL has no DTO rate on the card"
        if movement == "RTO":
            return 0.0, "RTO at Rs 0 (included)"
        return params["b_spl"], "Guwahati B_SPL special: Rs 42 / shipment"

    if movement == "RTO":
        # RTO row on the surface card is Rs 0 for all zones
        return 0.0, f"RTO at Rs 0 (included), zone {zone}"

    if movement == "DTO":
        # DTO rates are printed explicitly (independent of the X/Y volume slabs)
        b500 = params["dto"]["base_500"].get(zone)
        a500 = params["dto"]["addl_500"].get(zone)
        if b500 is None or a500 is None:
            return None, f"No DTO rate for zone {zone!r}"
        grams = _grams(weight_kg)
        extra = max(0, grams - 500)
        n_500 = math.ceil(extra / 500.0) if extra > 0 else 0
        total = b500 + n_500 * a500
        return r2(total), f"DTO first 500g Rs {b500} + {n_500}x Rs {a500}/500g"

    # Fwd: flat rate per zone; A/B use X/Y which depend on the monthly volume slab.
    xy = params["xy"].get(vol_tier, params["xy"]["1-3 Lakh"])
    base = params["base_forward"].get(zone)
    if base is None:
        return None, f"No rate for zone {zone!r}"
    if base in ("X", "Y"):
        xy_val = xy.get(base)
        if xy_val is None:
            return None, (
                f"Rate for zone {zone} ({base}) at vol slab {vol_tier} is "
                f"'Existing Rate Card, Table B' which is not printed in the workbook "
                f"- choose a volume slab of 1-3 Lakh or >=3 Lakh to quote"
            )
        base = xy_val
    return r2(base), f"Flat forward Rs {base} (zone {zone}, vol slab {vol_tier}); additional slabs Rs 0"


# ---------------------------------------------------------------------------
# Blue Dart Dart Plus
# ---------------------------------------------------------------------------
def price_bluedart(zone, weight_kg, movement, vol_tier, params):
    movement = (movement or "Fwd").upper()
    rate_zone = params["master_to_rate"].get(zone, zone)
    if rate_zone not in params["first_1kg"]:
        return None, f"No Blue Dart rate for zone {zone!r}"
    base = params["first_1kg"][rate_zone]
    addl = params["addl_500"][rate_zone]
    grams = max(1000, _grams(weight_kg))
    n_500 = math.ceil(max(0, grams - 1000) / 500.0)
    fwd = base + n_500 * addl
    if movement == "RTO":
        fwd = fwd * params["rto_factor"]
        rate_label = f"RTO (70% of forward) zone {rate_zone}"
    else:
        rate_label = f"Forward zone {rate_zone}"
    disc = params["vol_discount"].get(vol_tier, 0.0)
    total = fwd * (1 - disc)
    return r2(total), (
        f"{rate_label}: Rs {base} first 1kg + {n_500}x Rs {addl}/500g"
        f"{' - ' + str(disc * 100).rstrip('0').rstrip('.') + '% vol disc' if disc else ''}"
    )


# ---------------------------------------------------------------------------
# DTDC (w.e.f 21-May-25)
# ---------------------------------------------------------------------------
def price_dtdc(zone, weight_kg, movement, vol_tier, params):
    return r2(params["first_5000g"]), (
        f"Flat Rs {params['first_5000g']} per shipment (first 5000g); "
        f"additional 1000g Rs {params['addl_1000g']}"
    )


# ---------------------------------------------------------------------------
# Ekart
# ---------------------------------------------------------------------------
def price_ekart(zone, weight_kg, movement, vol_tier, params):
    plan = params["vol_discount"].get(vol_tier) or params["vol_discount"]["Base rates (Rs 41)"]
    first, addl = plan["first_5kg"], plan["addl_500"]
    grams = _grams(weight_kg)
    if grams <= 5000:
        return r2(first), f"0-5kg flat Rs {first} (incl fwd/COD/RTO){'  [' + vol_tier + ']' if vol_tier else ''}"
    n_500 = math.ceil((grams - 5000) / 500.0)
    total = first + n_500 * addl
    return r2(total), (
        f"Rs {first} (0-5kg) + {n_500}x Rs {addl}/500g thereafter"
        f"{'  [' + vol_tier + ']' if vol_tier else ''}"
    )


# ---------------------------------------------------------------------------
# Shadowfax (W.E.F 01-06-2025)
# ---------------------------------------------------------------------------
def price_shadowfax(zone, weight_kg, movement, vol_tier, params):
    key = None
    for k in params["rates"]:
        if k.lower() == (zone or "").lower():
            key = k
            break
    if key is None:
        return None, f"No Shadowfax rate for zone {zone!r}"
    high = vol_tier == ">=4L"
    rate = params["rates"][key][1 if high else 0]
    tier_label = ">=4L vol" if high else "<4L vol"
    return r2(rate), f"Flat Rs {rate} / shipment ({key}, {tier_label})"


def price_shadowfax_rvp(zone, params):
    """Shadowfax 'RVP with QC' (W.E.F 01-04-2025) — reverse pickup product.

    CPS per RVP category (Local/Regional/Metro/ROI/SZ) plus a flat QC fee of
    Rs 15 per shipment (pass or fail). Zone categories are derived from the
    zone master's Shadowfax forward zone via params['rvp']['zone_map'].
    Liability terms are informational only (not a charge).
    """
    rvp = params.get("rvp") or {}
    zmap = rvp.get("zone_map") or {}
    key = None
    for k in zmap:
        if k.lower() == (zone or "").lower():
            key = k
            break
    if key is None:
        return None, f"No RVP (reverse pickup) rate for Shadowfax zone {zone!r}"
    cat = zmap[key]
    cps = rvp.get("cps", {}).get(cat)
    if cps is None:
        return None, f"No RVP CPS for zone {cat!r}"
    qc = rvp.get("qc", 0.0)
    total = float(cps) + float(qc)
    detail = f"RVP with QC (W.E.F 01-04-2025): CPS Rs {cps} ({cat}) + QC Rs {qc} = Rs {r2(total)}"
    if rvp.get("liability_note"):
        detail += "; " + rvp["liability_note"]
    return r2(total), detail


# ---------------------------------------------------------------------------
# Velocity Express
# ---------------------------------------------------------------------------
def price_velocity(zone, weight_kg, movement, vol_tier, params):
    for k in params["rates"]:
        if k.lower() == (zone or "").lower() or (
            zone and "regional" in k.lower() and "regional" in zone.lower()
        ):
            rate = params["rates"][k]
            return r2(rate), f"Flat Rs {rate} / shipment ({k})"
    return None, f"No Velocity Express rate for zone {zone!r}"


# ---------------------------------------------------------------------------
# Amazon CPS
# ---------------------------------------------------------------------------
def price_amazon(zone, weight_kg, movement, vol_tier, params):
    cps = params["cps"].get(vol_tier, params["cps"]["<2 Lakh"])
    disc = params["disc"].get(vol_tier, 0.0)
    grams = _grams(weight_kg)
    # "Additional INR 15/Kg for additional 1kg above 2kg" -> billed per full kg above 2 kg.
    chargeable_kg = max(params["base_kg_inclusive"], math.ceil(grams / 1000.0))
    extra_kg = chargeable_kg - params["base_kg_inclusive"]
    addl_rate = params["addl_per_kg"] * (1 - disc)
    total = cps + addl_rate * extra_kg
    note = f"CPS Rs {cps} ({vol_tier})" + (
        f" + {extra_kg}kg x Rs {addl_rate:.2f}" if extra_kg > 0 else ""
    )
    return r2(total), note


# ---------------------------------------------------------------------------
# Elastic Run
# W.E.F 01-07-2025: SDD (local) Rs 37 / delivered order, WH 2, 4, 10, 12, 28.
# W.E.F 20-02-2026: NDD - Regional Rs 40 / delivered order (no return freight,
#                   bill on delivered shipments only).
# The pre-2025 flat Rs 33/32/31 volume scheme is retired. Elastic Run has no
# lane column in the zone master, so local vs regional is inferred from the
# destination pin prefix against the warehouse SDD metro.
# ---------------------------------------------------------------------------
def price_elastic(zone, weight_kg, movement, vol_tier, params, whid, pin=None, service="standard"):
    sdd_whs = params.get("wh_sdd_37_ids") or []
    metro = params.get("metro_prefixes") or {}
    whid = int(whid)

    if service == "sdd":
        if whid not in sdd_whs:
            return None, (
                f"No Elastic Run SDD (local) rate for WH {whid}"
                f" (SDD WHs: {', '.join(map(str, sdd_whs))})"
            )
        return r2(params["sdd_rate"]), (
            f"SDD (local) Rs {params['sdd_rate']} / delivered order "
            f"(W.E.F 01-07-2025, WH {whid})"
        )

    if service == "ndd_regional":
        return r2(params["ndd_regional"]), (
            f"NDD - Regional Rs {params['ndd_regional']} / delivered order "
            "(W.E.F 20-02-2026); no return freight, bill on delivered only"
        )

    # Default: auto local/regional from the destination pin (WH-based coverage).
    if whid not in sdd_whs:
        return None, (
            f"Elastic Run has no rate scheme for WH {whid}"
            f" (SDD WHs: {', '.join(map(str, sdd_whs))})"
        )
    local = False
    rates = metro.get(whid) or metro.get(str(whid))
    if rates and pin:
        try:
            local = int(str(int(pin))[:3]) in rates
        except (TypeError, ValueError):
            local = False
    if local:
        return r2(params["sdd_rate"]), (
            f"SDD (local) Rs {params['sdd_rate']} / delivered order "
            f"(W.E.F 01-07-2025, WH {whid}, dest {pin} in SDD metro)"
        )
    return r2(params["ndd_regional"]), (
        f"NDD - Regional Rs {params['ndd_regional']} / delivered order "
        f"(W.E.F 20-02-2026, WH {whid}, dest {pin} outside SDD metro); "
        "no return freight, bill on delivered only"
    )


PRICERS = {
    "delhivery": price_delhivery,
    "bluedart": price_bluedart,
    "dtdc": price_dtdc,
    "ekart": price_ekart,
    "shadowfax": price_shadowfax,
    "velocity": price_velocity,
    "amazon": price_amazon,
    "elastic": price_elastic,
}


def quote_carrier(carrier, zone, weight_kg, movement, opts, data):
    """Compute cost for one carrier. `zone` is the carrier zone (None = not served)."""
    params = data["price_cards"][carrier["id"]]
    movement = (movement or "Fwd").upper()
    base = {
        "id": carrier["id"],
        "name": carrier["name"],
        "master_zone": zone,
        "cost": None,
        "served": False,
        "details": "",
    }

    # A carrier with no lane zone is simply not served on this lane.
    if not zone and carrier["id"] != "elastic":
        base["system_zone"] = base["final_zone"] = None
        base["rate_basis"] = "Not served on this lane"
        return base

    # Only quote movements that are actually on the carrier's commercial card.
    supported = [str(m).upper() for m in (params.get("movements") or ["Fwd"])]
    if movement not in supported:
        base["system_zone"], base["final_zone"] = resolve_zones(carrier["id"], zone, data)
        base["rate_basis"] = (
            f"{movement} not quoted on the {carrier['name']} card"
            f" (card covers: {', '.join(supported).lower()})"
        )
        return base
    fn = PRICERS[carrier["id"]]
    vol_tier = opts.get("volume", {}).get(carrier["id"])
    for label in ("xy", "vol_discount", "cps", "vol_rates"):
        params_vol = params.get(label)
        if params_vol and vol_tier is None:
            vol_tier = next(iter(params_vol), None)
            break
    if carrier["id"] == "elastic":
        cost, detail = fn(zone, weight_kg, movement, vol_tier, params,
                          whid=opts.get("whid"), pin=opts.get("pin"),
                          service=opts.get("elastic_service", "standard"))
    else:
        cost, detail = fn(zone, weight_kg, movement, vol_tier, params)
    system_zone, final_zone = resolve_zones(carrier["id"], zone, data)
    base.update({
        "system_zone": system_zone,
        "final_zone": final_zone,
        "served": cost is not None,
        "cost": cost,
        "rate_basis": detail if detail else ("Not served on this lane" if cost is None else None),
    })
    return base