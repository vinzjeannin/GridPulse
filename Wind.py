"""
GridPulse Scotland data refresh script.

Writes grid_metrics.json for index.html using Elexon Insights and the Carbon
Intensity API. If a source fails, the previous value is carried forward and
"data_quality" in the output says so. Nothing is silently replaced by a made-up
number. A zero is treated as a real value.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from google import genai

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:
    UK_TZ = timezone.utc

ELEXON = "https://data.elexon.co.uk/bmrs/api/v1"
CARBON = "https://api.carbonintensity.org.uk"
TIMEOUT = 30
OUTPUT_FILE = "grid_metrics.json"

# Equivalency assumptions (not live data)
KWH_PER_HOME_PER_DAY = 9
KWH_PER_LITRE_ALCOHOL = 7
GAS_M3_PER_MWH = 190
CO2_T_PER_MWH = 0.35
MEGAPACK_MWH = 3.9
FALLBACK_GBP_PER_MWH = 135        # only if Elexon cashflows are unavailable
SCOTTISH_DEMAND_SHARE = 0.09      # ESTIMATE: no live public Scottish demand feed

CURTAILABLE_FUELS = {"WIND", "SOLAR", "NUCLEAR"}
NON_GENERATION_FUELS = {"INTERCONNECTOR", "SUPPLIER", "VIRTUAL"}

# Used ONLY for BM units whose register entry has no region at all.
SCOTTISH_NAME_HINTS = (
    "whitelee", "beatrice", "moray", "seagreen", "neart na gaoithe", "viking", "dunmaglass",
    "stronelairg", "kype muir", "hywind", "aberdeen", "fallago", "black law", "hadyard",
    "arecleoch", "dersalloch", "kilgallioch", "pogbie", "gordonbush", "strathy", "andershaw",
    "harestanes", "crystal rig", "dun law", "longpark", "toddleburn", "clyde", "corriegarth",
    "lochluichart", "berry burn", "rothes", "tom nan clach", "glenglass", "dorenell",
    "edinbane", "fairburn", "achany", "torness", "hunterston",
)

WIND_FARMS = [
    ("Whitelee Wind Farm", ("whitelee",), 55.68, -4.22),
    ("Beatrice Offshore", ("beatrice",), 58.12, -3.08),
    ("Moray Firth Offshore", ("moray",), 58.15, -2.95),
    ("Seagreen Offshore", ("seagreen",), 56.60, -1.75),
    ("Neart na Gaoithe", ("neart na gaoithe",), 56.25, -2.10),
    ("Viking Wind Farm (Shetland)", ("viking",), 60.30, -1.27),
    ("Dunmaglass", ("dunmaglass",), 57.30, -4.30),
    ("Stronelairg", ("stronelairg",), 57.12, -4.50),
    ("Clyde Wind Farm", ("clyde",), 55.45, -3.65),
    ("Kype Muir", ("kype muir",), 55.62, -3.97),
    ("Hywind Scotland", ("hywind",), 57.48, -1.35),
    ("Aberdeen Bay Offshore", ("aberdeen",), 57.22, -2.05),
]

# Only used on the very first run, when there is no previous grid_metrics.json.
FIRST_RUN = {
    "total_generation_gw": 0.0,
    "total_wind_gw": 0.0,
    "uk_carbon_intensity": 0,
    "scottish_carbon_intensity": 0,
    "generation_mix": {},
    "scottish_generation_mix": {},
}


def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def rows(payload):
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def attempt(label, fn, fallback, quality, ok_label):
    try:
        result = fn()
        quality[label] = ok_label
        return result
    except Exception as exc:
        print(f"WARN [{label}]: {type(exc).__name__}: {exc}")
        quality[label] = "fallback: previous value carried forward"
        return fallback


def load_previous():
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def settlement_clock(now_utc):
    now_uk = now_utc.astimezone(UK_TZ)
    midnight_uk = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now_utc - midnight_uk.astimezone(timezone.utc)
    period = int(elapsed.total_seconds() // 1800) + 1
    return now_uk.strftime("%Y-%m-%d"), period


# ---------------------------------------------------------------------------
# Carbon Intensity API
# ---------------------------------------------------------------------------
def get_gb_mix_ci():
    items = get_json(f"{CARBON}/generation")["data"]["generationmix"]
    mix = {i["fuel"]: round(num(i["perc"]), 1) for i in items}
    return {f: v for f, v in sorted(mix.items(), key=lambda kv: -kv[1]) if v > 0}


def get_uk_intensity():
    inten = get_json(f"{CARBON}/intensity")["data"][0]["intensity"]
    value = inten.get("actual") if inten.get("actual") is not None else inten.get("forecast")
    return int(value)


def get_scottish_mix_ci():
    """Average of North Scotland (1) and South Scotland (2). Zero intensity is valid when the
    mix is entirely wind, nuclear and solar."""
    mixes, intensities = [], []
    for region_id in (1, 2):
        period = get_json(f"{CARBON}/regional/regionid/{region_id}")["data"][0]["data"][0]
        mixes.append({i["fuel"]: num(i["perc"]) for i in period["generationmix"]})
        forecast = period["intensity"].get("forecast")
        if forecast is not None:
            intensities.append(float(forecast))
    if not intensities:
        raise RuntimeError("No regional intensity returned")
    fuels = set().union(*mixes)
    avg = {f: round(sum(m.get(f, 0.0) for m in mixes) / len(mixes), 1) for f in fuels}
    avg = {f: v for f, v in sorted(avg.items(), key=lambda kv: -kv[1]) if v > 0}
    return avg, int(round(sum(intensities) / len(intensities)))


# ---------------------------------------------------------------------------
# Elexon
# ---------------------------------------------------------------------------
def get_fuelinst(now_utc):
    params = {
        "publishDateTimeFrom": (now_utc - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "publishDateTimeTo": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "format": "json",
    }
    data = rows(get_json(f"{ELEXON}/datasets/FUELINST", params))
    if not data:
        raise RuntimeError("FUELINST returned no rows")
    latest = max(r.get("startTime", "") for r in data)
    total_mw, wind_mw = 0.0, 0.0
    for r in data:
        if r.get("startTime") != latest:
            continue
        mw = num(r.get("generation"))
        total_mw += mw
        if str(r.get("fuelType", "")).upper() == "WIND":
            wind_mw += mw
    if total_mw <= 0:
        raise RuntimeError("FUELINST total was zero")
    return round(total_mw / 1000.0, 1), round(wind_mw / 1000.0, 2)


def classify_scottish(gsp_id, gsp_name, name):
    """Returns (is_scottish, method). Region field wins; name hints only if region is blank."""
    gid = gsp_id.strip().upper().lstrip("_")
    gname = gsp_name.strip().lower()
    if gid or gname:
        return (gid in {"N", "P"} or "scotland" in gname), "region"
    if any(h in name for h in SCOTTISH_NAME_HINTS):
        return True, "name_hint"
    return False, "none"


def load_bmu_reference():
    units = {}
    for u in rows(get_json(f"{ELEXON}/reference/bmunits/all")):
        bmu = u.get("elexonBmUnit")
        if not bmu:
            continue
        gsp_id = str(u.get("gspGroupId") or "")
        gsp_name = str(u.get("gspGroupName") or "")
        name = f"{u.get('bmUnitName') or ''} {u.get('leadPartyName') or ''}".lower()
        scottish, method = classify_scottish(gsp_id, gsp_name, name)
        units[bmu] = {
            "id": bmu,
            "fuel": str(u.get("fuelType") or "").upper(),
            "name": name,
            "capacity_mw": num(u.get("generationCapacity")),
            "gsp_id": gsp_id.strip(),
            "gsp_name": gsp_name.strip(),
            "scottish": scottish,
            "method": method,
        }
    if not units:
        raise RuntimeError("BM unit reference returned no units")
    log_unit_diagnostics(units)
    if not any(u["scottish"] for u in units.values()):
        raise RuntimeError("No Scottish BM units identified")
    return units


def log_unit_diagnostics(units):
    """Printed in the Actions log so classification problems are visible."""
    wind = [u for u in units.values() if u["fuel"] == "WIND"]
    print(f"DIAG wind units: {len(wind)}, total {sum(u['capacity_mw'] for u in wind) / 1000:.1f} GW")

    by_region = defaultdict(lambda: [0, 0.0])
    by_prefix = defaultdict(lambda: [0, 0.0])
    for u in wind:
        key = f"{u['gsp_id'] or '-'} / {u['gsp_name'] or '-'}"
        by_region[key][0] += 1
        by_region[key][1] += u["capacity_mw"]
        prefix = u["id"].split("_")[0] if "_" in u["id"] else u["id"][:2]
        by_prefix[prefix][0] += 1
        by_prefix[prefix][1] += u["capacity_mw"]
    print("DIAG wind capacity by region label (units, GW):")
    for k, (n, mw) in sorted(by_region.items(), key=lambda kv: -kv[1][1])[:12]:
        print(f"   {k}: {n} units, {mw / 1000:.1f} GW")
    print("DIAG wind capacity by BM unit prefix (units, GW):")
    for k, (n, mw) in sorted(by_prefix.items(), key=lambda kv: -kv[1][1])[:6]:
        print(f"   {k}: {n} units, {mw / 1000:.1f} GW")

    via = defaultdict(float)
    for u in wind:
        if u["scottish"]:
            via[u["method"]] += u["capacity_mw"]
    print("DIAG Scottish wind GW via region field: "
          f"{via.get('region', 0) / 1000:.1f}, via name hints: {via.get('name_hint', 0) / 1000:.1f}")
    sample = next((u for u in units.values() if "whitelee" in u["name"]), None)
    if sample:
        print(f"DIAG sample Whitelee unit: {sample}")


def fetch_pn_levels(settlement_date, period):
    params = {"settlementDate": settlement_date, "settlementPeriod": period, "format": "json"}
    data = rows(get_json(f"{ELEXON}/datasets/PN", params))
    levels = defaultdict(list)
    for r in data:
        bmu = r.get("bmUnit")
        if not bmu:
            continue
        for key in ("levelFrom", "levelTo"):
            if r.get(key) is not None:
                levels[bmu].append(num(r.get(key)))
    if not levels:
        raise RuntimeError("PN returned no rows")
    print(f"DIAG PN rows: {len(data)}, BM units: {len(levels)}")
    return levels


def summarise_pn(units, levels):
    gb_wind_mw = scot_wind_mw = 0.0
    matched = 0
    for bmu, pts in levels.items():
        u = units.get(bmu)
        if not u or not pts:
            continue
        matched += 1
        mw = sum(pts) / len(pts)
        if mw <= 0 or u["fuel"] in NON_GENERATION_FUELS:
            continue
        if u["fuel"] == "WIND":
            gb_wind_mw += mw
            if u["scottish"]:
                scot_wind_mw += mw
    print(f"DIAG PN matched to register: {matched}; wind MW GB {gb_wind_mw:.0f}, Scotland {scot_wind_mw:.0f}")
    if gb_wind_mw <= 0:
        raise RuntimeError("No wind output found in PN data")
    return {"gb_wind_mw": gb_wind_mw, "scot_wind_mw": scot_wind_mw}


def wind_capacity_mw(units):
    gb = sum(u["capacity_mw"] for u in units.values() if u["fuel"] == "WIND")
    scot = sum(u["capacity_mw"] for u in units.values() if u["fuel"] == "WIND" and u["scottish"])
    return gb, scot


def fetch_curtailment(units, settlement_date):
    """Accepted bids (turn-downs) on Scottish wind, solar and nuclear units, UK day so far.
    An empty result for Scottish units is a valid zero. Missing data raises instead."""
    vol_rows = rows(get_json(f"{ELEXON}/balancing/settlement/acceptance/volumes/all/bid/{settlement_date}"))
    if not vol_rows:
        raise RuntimeError("Acceptance volumes returned no rows")
    if not any("totalVolumeAccepted" in r for r in vol_rows):
        raise RuntimeError(f"Unexpected volume fields: {list(vol_rows[0].keys())}")

    def eligible(row):
        u = units.get(row.get("bmUnit"))
        return u is not None and u["scottish"] and u["fuel"] in CURTAILABLE_FUELS

    mwh_by_unit = defaultdict(float)
    registered = eligible_rows = 0
    for r in vol_rows:
        if r.get("bmUnit") in units:
            registered += 1
        if not eligible(r):
            continue
        eligible_rows += 1
        # This endpoint is bids only, so magnitude is the curtailed volume whatever the sign.
        mwh_by_unit[r["bmUnit"]] += abs(num(r.get("totalVolumeAccepted")))
    print(f"DIAG acceptances: {len(vol_rows)} rows, {registered} match register, "
          f"{eligible_rows} Scottish wind/solar/nuclear, {sum(mwh_by_unit.values()):.1f} MWh")

    cost_by_unit, cost_live = {}, False
    try:
        cash_rows = rows(get_json(
            f"{ELEXON}/balancing/settlement/indicative/cashflows/all/bid/{settlement_date}"))
        tmp = defaultdict(float)
        for r in cash_rows:
            if eligible(r):
                tmp[r["bmUnit"]] += num(r.get("totalCashflow"))
        cost_by_unit = {k: abs(v) for k, v in tmp.items()}
        cost_live = bool(cash_rows)
    except Exception as exc:
        print(f"WARN [curtailment_cost]: {type(exc).__name__}: {exc}")
    return mwh_by_unit, cost_by_unit, cost_live


def build_curtailment_blocks(units, mwh_by_unit, cost_by_unit, cost_live):
    fuel_mwh, fuel_cost = defaultdict(float), defaultdict(float)
    farm_mwh, farm_cost = defaultdict(float), defaultdict(float)
    for bmu, mwh in mwh_by_unit.items():
        u = units[bmu]
        cost = cost_by_unit.get(bmu, 0.0) if cost_live else mwh * FALLBACK_GBP_PER_MWH
        fuel_mwh[u["fuel"]] += mwh
        fuel_cost[u["fuel"]] += cost
        if u["fuel"] == "WIND":
            for farm_name, keywords, _, _ in WIND_FARMS:
                if any(k in u["name"] for k in keywords):
                    farm_mwh[farm_name] += mwh
                    farm_cost[farm_name] += cost
                    break

    def block(fuel):
        mwh = round(fuel_mwh.get(fuel, 0.0), 1)
        return {"mwh": mwh, "cost_gbp": round(fuel_cost.get(fuel, 0.0), 2),
                "homes_powered": int(mwh * 1000 / KWH_PER_HOME_PER_DAY)}

    breakdown = {"wind": block("WIND"), "solar": block("SOLAR"), "nuclear": block("NUCLEAR")}
    coords = {name: (lat, lon) for name, _, lat, lon in WIND_FARMS}
    top = sorted(farm_mwh.items(), key=lambda kv: -kv[1])[:6]
    locations = [
        {"name": n, "lat": coords[n][0], "lon": coords[n][1], "fuel": "wind",
         "wasted_mwh": round(m, 1), "cost_gbp": int(round(farm_cost[n]))}
        for n, m in top if m > 0
    ]
    return breakdown, locations


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------
def fetch_grid_data(prev):
    now_utc = datetime.now(timezone.utc)
    settlement_date, period = settlement_clock(now_utc)
    q = {}

    gb_mix = attempt("gb_generation_mix", get_gb_mix_ci,
                     prev.get("generation_mix", FIRST_RUN["generation_mix"]), q,
                     "live (Carbon Intensity API)")
    uk_intensity = attempt("uk_carbon_intensity", get_uk_intensity,
                           prev.get("uk_carbon_intensity", FIRST_RUN["uk_carbon_intensity"]), q,
                           "live (Carbon Intensity API)")
    scot_mix, scot_intensity = attempt(
        "scottish_mix_and_intensity", get_scottish_mix_ci,
        (prev.get("scottish_generation_mix", FIRST_RUN["scottish_generation_mix"]),
         prev.get("scottish_carbon_intensity", FIRST_RUN["scottish_carbon_intensity"])),
        q, "live (Carbon Intensity API, avg of regions 1 and 2)")

    gb_total_gw, gb_wind_gw = attempt(
        "gb_totals", lambda: get_fuelinst(now_utc),
        (prev.get("total_generation_gw", FIRST_RUN["total_generation_gw"]),
         prev.get("total_wind_gw", FIRST_RUN["total_wind_gw"])),
        q, "live (Elexon FUELINST)")

    units = attempt("bm_unit_reference", load_bmu_reference, {}, q, "live (Elexon reference data)")
    pn_stats = None
    if units:
        pn_stats = attempt("scottish_wind_share",
                           lambda: summarise_pn(units, fetch_pn_levels(settlement_date, period)),
                           None, q, "live (Elexon Physical Notifications)")

    # Capacity comes from the register. No hardcoded replacement if it looks odd.
    prev_cap = prev.get("wind_capacity", {})
    cap_gb_mw, cap_scot_mw = wind_capacity_mw(units) if units else (0.0, 0.0)
    if cap_gb_mw > 0:
        cap_gb, cap_scot = round(cap_gb_mw / 1000.0, 1), round(cap_scot_mw / 1000.0, 1)
        q["wind_capacity"] = "live (BM unit register, BM-registered wind only)"
    else:
        cap_gb = prev_cap.get("gb_total", {}).get("capacity_gw", 0.0)
        cap_scot = prev_cap.get("scotland", {}).get("capacity_gw", 0.0)
        q["wind_capacity"] = "fallback: previous value carried forward"
    cap_ew = round(max(cap_gb - cap_scot, 0.0), 1)

    if pn_stats and pn_stats["scot_wind_mw"] > 0:
        scot_share = pn_stats["scot_wind_mw"] / pn_stats["gb_wind_mw"]
    elif cap_gb:
        scot_share = cap_scot / cap_gb
        q["scottish_wind_share"] = "estimate: Scottish share of installed capacity"
    else:
        scot_share = prev.get("wind_country_split", {}).get("Scotland", 0.0) / 100.0
        q["scottish_wind_share"] = "fallback: previous value carried forward"
    scot_share = min(max(scot_share, 0.0), 1.0)

    scot_wind_gw = round(gb_wind_gw * scot_share, 1)
    ew_wind_gw = round(max(gb_wind_gw - scot_wind_gw, 0.0), 1)

    wind_pct = scot_mix.get("wind", 0.0)
    if wind_pct > 5:
        scot_total_gw = round(scot_wind_gw / (wind_pct / 100.0), 1)
        q["scottish_total_generation"] = "derived: Scottish wind output divided by wind % of regional mix"
    else:
        scot_total_gw = prev.get("scot_total_generation_gw", scot_wind_gw)
        q["scottish_total_generation"] = "fallback: wind share of Scottish mix too small to derive total"

    scot_demand_gw = round(gb_total_gw * SCOTTISH_DEMAND_SHARE, 1)
    q["scottish_demand"] = f"ESTIMATE: {int(SCOTTISH_DEMAND_SHARE * 100)}% of GB demand (no live feed)"
    net_export = round(scot_total_gw - scot_demand_gw, 1)

    zero = {"mwh": 0.0, "cost_gbp": 0.0, "homes_powered": 0}
    breakdown = prev.get("fuel_breakdown") or {k: dict(zero) for k in ("wind", "solar", "nuclear")}
    locations = prev.get("locations", [])
    curtail = attempt("curtailment", lambda: fetch_curtailment(units, settlement_date), None, q,
                      "live (Elexon accepted bids, Scottish units, UK day so far)")
    if curtail:
        mwh_by_unit, cost_by_unit, cost_live = curtail
        breakdown, locations = build_curtailment_blocks(units, mwh_by_unit, cost_by_unit, cost_live)
        q["constraint_cost"] = ("live (Elexon bid cashflows)" if cost_live
                                else f"ESTIMATE: GBP {FALLBACK_GBP_PER_MWH}/MWh flat rate")

    wasted_wind = breakdown["wind"]["mwh"]
    total_wasted = round(sum(b["mwh"] for b in breakdown.values()), 1)

    q["substations_interconnectors_map"] = "static reference geography and labels"
    q["gas_co2_homes_whisky_bess"] = "derived from curtailed MWh using fixed assumptions"

    def usage(gen, cap):
        return round(100.0 * gen / cap, 1) if cap else 0.0

    return {
        "timestamp": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "settlement_date": settlement_date,
        "settlement_period": period,
        "total_generation_gw": gb_total_gw,
        "scot_total_generation_gw": scot_total_gw,
        "uk_carbon_intensity": uk_intensity,
        "scottish_carbon_intensity": scot_intensity,
        "total_wind_gw": round(gb_wind_gw, 1),
        "generation_mix": gb_mix,
        "scottish_generation_mix": scot_mix,
        "wind_country_split": {"Scotland": round(scot_share * 100, 1),
                               "England & Wales": round((1 - scot_share) * 100, 1)},
        "wind_capacity": {
            "gb_total": {"gen_gw": round(gb_wind_gw, 1), "capacity_gw": cap_gb,
                         "usage_perc": usage(gb_wind_gw, cap_gb)},
            "scotland": {"gen_gw": scot_wind_gw, "capacity_gw": cap_scot,
                         "usage_perc": usage(scot_wind_gw, cap_scot)},
            "england_wales": {"gen_gw": ew_wind_gw, "capacity_gw": cap_ew,
                              "usage_perc": usage(ew_wind_gw, cap_ew)},
        },
        "scottish_balance": {"demand_gw": scot_demand_gw, "generation_gw": scot_total_gw,
                             "net_export_gw": round(abs(net_export), 1),
                             "is_exporter": net_export >= 0},
        "total_wasted_mwh": total_wasted,
        "fuel_breakdown": breakdown,
        "whisky_pure_alcohol_litres_total": int(wasted_wind * 1000 / KWH_PER_LITRE_ALCOHOL),
        "whisky_litres": int(wasted_wind * 1000 / KWH_PER_LITRE_ALCOHOL),
        "fossil_displacement": {"gas_burned_m3": round(total_wasted * GAS_M3_PER_MWH, 1),
                                "co2_tonnes": round(total_wasted * CO2_T_PER_MWH, 1)},
        "bess_storage": {"megapacks_needed": int(total_wasted / MEGAPACK_MWH),
                         "total_gwh": round(total_wasted / 1000, 2)},
        "interconnectors": [
            {"name": "Western HVDC Link (Hunterston to Deeside)",
             "path": [[55.73, -4.88], [54.5, -3.5], [53.22, -3.05]],
             "status": "HVDC corridor"},
            {"name": "Eastern Green Link 1 (Torness to Hawthorn Pit)",
             "path": [[56.0, -2.4], [55.3, -1.8], [54.8, -1.3]],
             "status": "Planned corridor (not yet operational)"},
        ],
        "substations": [
            {"name": "Beauly Substation (Highlands)", "lat": 57.48, "lon": -4.42, "status": "Highlands export node"},
            {"name": "Denny North (Central Belt)", "lat": 56.02, "lon": -3.88, "status": "Central Belt node"},
            {"name": "Wishaw Substation (Lanarkshire)", "lat": 55.77, "lon": -3.92, "status": "Lanarkshire node"},
            {"name": "Harker Substation (Border/Carlisle)", "lat": 54.93, "lon": -2.98, "status": "B6 Gateway Node"},
        ],
        "locations": locations,
        "data_quality": q,
    }


def fallback_summary(m):
    cost = sum(b["cost_gbp"] for b in m["fuel_breakdown"].values())
    return (
        f"Scottish wind is currently supplying an estimated {m['wind_capacity']['scotland']['gen_gw']} GW "
        f"into the grid, with Scottish carbon intensity at {m['scottish_carbon_intensity']} gCO2/kWh. "
        f"So far today {m['total_wasted_mwh']} MWh of Scottish generation has been turned down through "
        f"Balancing Mechanism bids, at a constraint cost of about £{cost:,.0f}. Transmission limits across "
        "the B6 boundary stop this low-carbon power reaching English demand centres, so gas plant further "
        "south must replace it, and the cost is recovered from bill payers through BSUoS charges."
    )


def generate_ai_insights(m):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("WARN [ai_summary]: GEMINI_API_KEY missing, using fallback text.")
        return fallback_summary(m)

    cost = sum(b["cost_gbp"] for b in m["fuel_breakdown"].values())
    prompt = (
        "Write a clean ~200-word executive analysis for the GridPulse Scotland dashboard. "
        "Use ONLY these figures and do not invent any others: "
        f"GB wind output {m['total_wind_gw']} GW; estimated Scottish wind output "
        f"{m['wind_capacity']['scotland']['gen_gw']} GW "
        f"({m['wind_capacity']['scotland']['usage_perc']}% of installed Scottish wind capacity); "
        f"Scottish carbon intensity {m['scottish_carbon_intensity']} gCO2/kWh versus "
        f"{m['uk_carbon_intensity']} gCO2/kWh GB-wide; Scottish generation curtailed so far today "
        f"{m['total_wasted_mwh']} MWh at a constraint cost of about £{cost:,.0f}; "
        f"estimated gas burned elsewhere to replace it {m['fossil_displacement']['gas_burned_m3']} m3 "
        f"and {m['fossil_displacement']['co2_tonnes']} tonnes CO2. "
        "STRICT RULES: no markdown headers, no titles or labels, no bullet points. "
        "Start directly with the narrative analysis from the first word."
        "If you quote the wasted energy curtailed so far today, round it to to the nearest MWh, use coma as separator for thousands. If it's greater than 10000 MWh, change unit to GWh by dividing by 1000, one decimal".   
        "If you quote the volume of gas burned elsewhere, round it th the nearest cubic meter, use coma as separator for thousands. The additional carbon should be rounded to the nearst tonne and use coma as separator for thousands."
    )
    client = genai.Client(api_key=api_key)
    for model in [c for c in (os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash") if c]:
        try:
            text = (client.models.generate_content(model=model, contents=prompt).text or "").strip()
            lines = [ln for ln in text.splitlines()
                     if not ln.strip().startswith("#") and not ln.strip().startswith("**LIVE")]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                print(f"AI summary generated with model: {model}")
                return cleaned
        except Exception as exc:
            print(f"WARN [ai_summary] model {model} failed: {type(exc).__name__}: {exc}")
    return fallback_summary(m)


def main():
    print("Fetching live grid metrics...")
    metrics = fetch_grid_data(load_previous())
    print("Data quality report:")
    for key, status in metrics["data_quality"].items():
        print(f"  - {key}: {status}")
    print("Generating AI insight block...")
    metrics["ai_summary"] = generate_ai_insights(metrics)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"Successfully updated {OUTPUT_FILE} at {metrics['timestamp']}")


if __name__ == "__main__":
    main()
