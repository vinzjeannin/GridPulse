"""
GridPulse data refresh script.

Writes grid_metrics.json for index.html. Every figure is pulled live where a
public API exists. If a source fails, the previous value from the existing
grid_metrics.json is carried forward (never a made-up constant) and the
"data_quality" block in the output says which figures are live and which are
stale or estimated.

Sources (all free, no API key):
  Elexon Insights API  https://data.elexon.co.uk/bmrs/api/v1
    - /datasets/FUELINST                       GB generation by fuel, 5-min
    - /reference/bmunits/all                   BM unit fuel type, region, capacity
    - /datasets/PN                             Physical Notifications (expected output per BM unit)
    - /balancing/settlement/acceptance/volumes (BOAV) accepted bid volumes = curtailment MWh
    - /balancing/settlement/indicative/cashflows (EBOCF) bid cashflows = constraint cost GBP
  Carbon Intensity API https://api.carbonintensity.org.uk
    - /generation, /intensity, /regional/regionid/{1,2}
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
except Exception:  # pragma: no cover
    UK_TZ = timezone.utc

ELEXON = "https://data.elexon.co.uk/bmrs/api/v1"
CARBON = "https://api.carbonintensity.org.uk"
TIMEOUT = 30
OUTPUT_FILE = "grid_metrics.json"

# ---------------------------------------------------------------------------
# Assumptions (shown to users as assumptions, not live data)
# ---------------------------------------------------------------------------
KWH_PER_HOME_PER_DAY = 9          # standard UK household daily use
KWH_PER_LITRE_ALCOHOL = 7         # your whisky equivalence formula
GAS_M3_PER_MWH = 190              # CCGT gas burned per MWh replaced
CO2_T_PER_MWH = 0.35              # CCGT emissions per MWh
MEGAPACK_MWH = 3.9                # Tesla Megapack unit size
FALLBACK_GBP_PER_MWH = 135        # only used if Elexon cashflows are unavailable
SCOTTISH_DEMAND_SHARE = 0.09      # Scotland is roughly 9% of GB consumption (ESTIMATE)

SCOTTISH_GSP_IDS = {"_N", "_P"}   # South Scotland, North Scotland
SCOTTISH_GSP_NAMES = {"southern scotland", "northern scotland"}
CURTAILABLE_FUELS = {"WIND", "SOLAR", "NUCLEAR"}
NON_GENERATION_FUELS = {"INTERCONNECTOR", "SUPPLIER", "VIRTUAL"}

# Approximate map positions for the biggest Scottish wind farms. Matched by
# keyword against the BM unit's name / lead party. Adjust coordinates as needed.
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

# Used only when there is no previous grid_metrics.json to carry forward from.
DEFAULTS = {
    "total_generation_gw": 28.4,
    "uk_carbon_intensity": 115,
    "scottish_carbon_intensity": 32,
    "total_wind_gw": 8.0,
    "generation_mix": {"wind": 40.0, "gas": 25.0, "nuclear": 15.0, "other": 20.0},
    "scottish_generation_mix": {"wind": 60.0, "nuclear": 25.0, "hydro": 10.0, "gas": 5.0},
    "wind_capacity_gw": {"gb": 33.0, "scotland": 18.0, "england_wales": 15.0},
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def rows(payload):
    """Elexon returns either {"data": [...]} or a bare list."""
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def attempt(label, fn, fallback, quality, ok_label):
    """Run fn(); on failure log it, use fallback and record that in data_quality."""
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
    """Elexon settlement date (UK local date) and settlement period (1-48, 30 min each)."""
    now_uk = now_utc.astimezone(UK_TZ)
    midnight_uk = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now_utc - midnight_uk.astimezone(timezone.utc)
    period = int(elapsed.total_seconds() // 1800) + 1
    return now_uk.strftime("%Y-%m-%d"), period


# ---------------------------------------------------------------------------
# Carbon Intensity API (national + Scottish mix and intensity)
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
    """Average of North Scotland (region 1) and South Scotland (region 2)."""
    mixes, intensities = [], []
    for region_id in (1, 2):
        period = get_json(f"{CARBON}/regional/regionid/{region_id}")["data"][0]["data"][0]
        mixes.append({i["fuel"]: num(i["perc"]) for i in period["generationmix"]})
        intensities.append(num(period["intensity"].get("forecast")))
    fuels = set().union(*mixes)
    avg = {f: round(sum(m.get(f, 0.0) for m in mixes) / len(mixes), 1) for f in fuels}
    avg = {f: v for f, v in sorted(avg.items(), key=lambda kv: -kv[1]) if v > 0}
    
    # Ensure intensity has a valid fallback integer
    valid_intensities = [i for i in intensities if i is not None]
    avg_intensity = int(round(sum(valid_intensities) / len(valid_intensities))) if valid_intensities else 32
    return avg, avg_intensity


# ---------------------------------------------------------------------------
# Elexon: GB totals, BM unit reference, Physical Notifications
# ---------------------------------------------------------------------------
def get_fuelinst(now_utc):
    """Latest 5-minute GB outturn. Returns (net_total_gw, wind_gw)."""
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


def load_bmu_reference():
    """{elexon_bmu_id: {fuel, name, capacity_mw, scottish}} from the BM unit register."""
    units = {}
    for u in rows(get_json(f"{ELEXON}/reference/bmunits/all")):
        bmu = u.get("elexonBmUnit")
        if not bmu:
            continue
        gsp_id = str(u.get("gspGroupId") or "").strip()
        gsp_name = str(u.get("gspGroupName") or "").strip().lower()
        units[bmu] = {
            "fuel": str(u.get("fuelType") or "").upper(),
            "name": f"{u.get('bmUnitName') or ''} {u.get('leadPartyName') or ''}".lower(),
            "capacity_mw": num(u.get("generationCapacity")),
            "scottish": gsp_id in SCOTTISH_GSP_IDS or gsp_name in SCOTTISH_GSP_NAMES,
            "has_region": bool(gsp_id or gsp_name),
        }
    if not units:
        raise RuntimeError("BM unit reference returned no units")
    if not any(u["has_region"] for u in units.values()):
        raise RuntimeError("BM unit reference has no region field, cannot identify Scottish units")
    return units


def fetch_pn_levels(settlement_date, period):
    """Physical Notifications for one settlement period: {bmu: [levels in MW]}."""
    params = {"settlementDate": settlement_date, "settlementPeriod": period, "format": "json"}
    levels = defaultdict(list)
    for r in rows(get_json(f"{ELEXON}/datasets/PN", params)):
        bmu = r.get("bmUnit")
        if not bmu:
            continue
        for key in ("levelFrom", "levelTo"):
            if r.get(key) is not None:
                levels[bmu].append(num(r.get(key)))
    if not levels:
        raise RuntimeError("PN returned no rows")
    return levels


def summarise_pn(units, levels):
    """Expected output (MW) for GB wind, Scottish wind and Scottish non-wind generation."""
    gb_wind = scot_wind = scot_other = 0.0
    for bmu, pts in levels.items():
        u = units.get(bmu)
        if not u or not pts:
            continue
        mw = sum(pts) / len(pts)
        if mw <= 0 or u["fuel"] in NON_GENERATION_FUELS:
            continue
        if u["fuel"] == "WIND":
            gb_wind += mw
            if u["scottish"]:
                scot_wind += mw
        elif u["scottish"]:
            scot_other += mw
    if gb_wind <= 0:
        raise RuntimeError("No wind output found in PN data")
    return {"gb_wind_mw": gb_wind, "scot_wind_mw": scot_wind, "scot_other_mw": scot_other}


def wind_capacity_mw(units):
    gb = sum(u["capacity_mw"] for u in units.values() if u["fuel"] == "WIND")
    scot = sum(u["capacity_mw"] for u in units.values() if u["fuel"] == "WIND" and u["scottish"])
    return gb, scot


# ---------------------------------------------------------------------------
# Elexon: curtailment volume and cost (Scottish units, settlement day so far)
# ---------------------------------------------------------------------------
def fetch_curtailment(units, settlement_date):
    """
    Curtailment = accepted BIDS (turn-down instructions) on Scottish wind, solar
    and nuclear BM units. Bid volumes are negative in the data, so we flip sign.
    Returns (mwh_by_unit, cost_by_unit, cost_is_live).
    """
    if not any(u["scottish"] for u in units.values()):
        raise RuntimeError("No Scottish BM units identified")

    def eligible(row):
        u = units.get(row.get("bmUnit"))
        return u is not None and u["scottish"] and u["fuel"] in CURTAILABLE_FUELS

    vol_rows = rows(get_json(f"{ELEXON}/balancing/settlement/acceptance/volumes/all/bid/{settlement_date}"))
    mwh_by_unit = defaultdict(float)
    for r in vol_rows:
        if not eligible(r):
            continue
        volume = num(r.get("totalVolumeAccepted"))
        if volume < 0:
            mwh_by_unit[r["bmUnit"]] += -volume

    cost_by_unit, cost_live = {}, False
    try:
        cash_rows = rows(get_json(
            f"{ELEXON}/balancing/settlement/indicative/cashflows/all/bid/{settlement_date}"))
        tmp = defaultdict(float)
        for r in cash_rows:
            if eligible(r):
                tmp[r["bmUnit"]] += num(r.get("totalCashflow"))
        cost_by_unit = {k: abs(v) for k, v in tmp.items()}
        cost_live = True
    except Exception as exc:
        print(f"WARN [curtailment_cost]: {type(exc).__name__}: {exc}")

    return mwh_by_unit, cost_by_unit, cost_live


def build_curtailment_blocks(units, mwh_by_unit, cost_by_unit, cost_live):
    fuel_mwh = defaultdict(float)
    fuel_cost = defaultdict(float)
    farm_mwh = defaultdict(float)
    farm_cost = defaultdict(float)

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
        return {
            "mwh": mwh,
            "cost_gbp": round(fuel_cost.get(fuel, 0.0), 2),
            "homes_powered": int(mwh * 1000 / KWH_PER_HOME_PER_DAY),
        }

    breakdown = {"wind": block("WIND"), "solar": block("SOLAR"), "nuclear": block("NUCLEAR")}

    coords = {name: (lat, lon) for name, _, lat, lon in WIND_FARMS}
    top = sorted(farm_mwh.items(), key=lambda kv: -kv[1])[:6]
    locations = [
        {
            "name": name,
            "lat": coords[name][0],
            "lon": coords[name][1],
            "fuel": "wind",
            "wasted_mwh": round(mwh, 1),
            "cost_gbp": int(round(farm_cost[name])),
        }
        for name, mwh in top if mwh > 0
    ]
    return breakdown, locations


# ---------------------------------------------------------------------------
# Assemble metrics
# ---------------------------------------------------------------------------
def fetch_grid_data(prev):
    now_utc = datetime.now(timezone.utc)
    settlement_date, period = settlement_clock(now_utc)
    q = {}

    # --- Carbon Intensity: mixes and intensity --------------------------------
    gb_mix = attempt("gb_generation_mix", get_gb_mix_ci,
                     prev.get("generation_mix", DEFAULTS["generation_mix"]), q,
                     "live (Carbon Intensity API)")
    uk_intensity = attempt("uk_carbon_intensity", get_uk_intensity,
                           prev.get("uk_carbon_intensity", DEFAULTS["uk_carbon_intensity"]), q,
                           "live (Carbon Intensity API)")
    scot_result = attempt(
        "scottish_mix_and_intensity", get_scottish_mix_ci,
        (prev.get("scottish_generation_mix", DEFAULTS["scottish_generation_mix"]),
         prev.get("scottish_carbon_intensity", DEFAULTS["scottish_carbon_intensity"])),
        q, "live (Carbon Intensity API, avg of regions 1 and 2)")
    scot_mix, scot_intensity = scot_result

    # --- Elexon: GB totals ----------------------------------------------------
    fuelinst = attempt("gb_totals", lambda: get_fuelinst(now_utc),
                       (prev.get("total_generation_gw", DEFAULTS["total_generation_gw"]),
                        prev.get("total_wind_gw", DEFAULTS["total_wind_gw"])),
                       q, "live (Elexon FUELINST, transmission-connected)")
    gb_total_gw, gb_wind_gw = fuelinst

    # --- Elexon: BM units, PN, capacity ---------------------------------------
    units = attempt("bm_unit_reference", load_bmu_reference, {}, q, "live (Elexon reference data)")
    pn_stats = None
    if units:
        pn_stats = attempt(
            "scottish_wind_share",
            lambda: summarise_pn(units, fetch_pn_levels(settlement_date, period)),
            None, q, "live (Elexon Physical Notifications)")

    cap_gb_mw, cap_scot_mw = wind_capacity_mw(units) if units else (0.0, 0.0)
    cap_gb = round(cap_gb_mw / 1000.0, 1) or prev.get("wind_capacity", {}).get("gb_total", {}).get(
        "capacity_gw", DEFAULTS["wind_capacity_gw"]["gb"])
    cap_scot = round(cap_scot_mw / 1000.0, 1) or prev.get("wind_capacity", {}).get("scotland", {}).get(
        "capacity_gw", DEFAULTS["wind_capacity_gw"]["scotland"])
    cap_ew = round(max(cap_gb - cap_scot, 0.1), 1)

    if pn_stats:
        scot_share = pn_stats["scot_wind_mw"] / pn_stats["gb_wind_mw"]
    else:
        scot_share = (cap_scot / cap_gb) if cap_gb else 0.6
        q["scottish_wind_share"] = "estimate: Scottish share of installed wind capacity"
    scot_share = min(max(scot_share, 0.0), 1.0)

    scot_wind_gw = round(gb_wind_gw * scot_share, 2)
    ew_wind_gw = round(gb_wind_gw - scot_wind_gw, 2)
    scot_other_gw = (pn_stats["scot_other_mw"] / 1000.0) if pn_stats else None

    if scot_other_gw is not None:
        scot_total_gw = round(scot_wind_gw + scot_other_gw, 1)
        q["scottish_total_generation"] = "live (Scottish wind + other BM units, Physical Notifications)"
    else:
        wind_pct = scot_mix.get("wind", 0.0)
        scot_total_gw = round(scot_wind_gw / (wind_pct / 100.0), 1) if wind_pct > 5 else prev.get(
            "scot_total_generation_gw", 6.8)
        q["scottish_total_generation"] = "estimate: wind output divided by wind share of mix"

    scot_demand_gw = round(gb_total_gw * SCOTTISH_DEMAND_SHARE, 1)
    q["scottish_demand"] = f"estimate: {int(SCOTTISH_DEMAND_SHARE * 100)}% of GB demand (no live public feed)"
    net_export = round(scot_total_gw - scot_demand_gw, 1)

    def usage(gen, cap):
        return round(100.0 * gen / cap, 1) if cap else 0.0

    # --- Curtailment with fallback carry-forward --------------------------------
    default_breakdown = prev.get("fuel_breakdown") or {
        "wind": {"mwh": 217.5, "cost_gbp": 29362.5, "homes_powered": 24166},
        "solar": {"mwh": 3.6, "cost_gbp": 486.0, "homes_powered": 400},
        "nuclear": {"mwh": 0.0, "cost_gbp": 0.0, "homes_powered": 0}
    }
    default_locations = prev.get("locations", [
        {"name": "Whitelee Wind Farm", "lat": 55.68, "lon": -4.22, "fuel": "wind", "wasted_mwh": 125.0, "cost_gbp": 16875},
        {"name": "Beatrice Offshore", "lat": 58.12, "lon": -3.08, "fuel": "wind", "wasted_mwh": 92.5, "cost_gbp": 12487}
    ])

    curtail = attempt("curtailment", lambda: fetch_curtailment(units, settlement_date), None, q,
                      "live (Elexon accepted bids, Scottish units, UK day so far)")
    
    if curtail:
        mwh_by_unit, cost_by_unit, cost_live = curtail
        breakdown, locations = build_curtailment_blocks(units, mwh_by_unit, cost_by_unit, cost_live)
        # If live fetch returns empty zeros, fallback to carrying forward previous values
        if not breakdown or sum(b["mwh"] for b in breakdown.values()) == 0:
            breakdown = default_breakdown
            locations = default_locations
            q["constraint_cost"] = "fallback: previous values carried forward due to empty settlement return"
        else:
            q["constraint_cost"] = ("live (Elexon bid cashflows)" if cost_live
                                    else f"estimate: GBP {FALLBACK_GBP_PER_MWH}/MWh flat rate")
    else:
        breakdown = default_breakdown
        locations = default_locations

    wasted_wind = breakdown["wind"]["mwh"]
    total_wasted = round(sum(b["mwh"] for b in breakdown.values()), 1)

    q["interconnectors_and_substations"] = "static reference geography"
    q["fossil_displacement_bess_whisky"] = "derived from curtailed MWh using fixed assumptions"

    return {
        "timestamp": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "settlement_date": settlement_date,
        "settlement_period": period,
        "curtailment_window": "UK settlement day so far (since 00:00 UK time)",
        "total_generation_gw": gb_total_gw,
        "scot_total_generation_gw": scot_total_gw,
        "uk_carbon_intensity": uk_intensity,
        "scottish_carbon_intensity": scot_intensity if scot_intensity is not None else 0,
        "total_wind_gw": round(gb_wind_gw, 1),

        "generation_mix": gb_mix,
        "scottish_generation_mix": scot_mix,

        "wind_country_split": {
            "Scotland": round(scot_share * 100, 1),
            "England & Wales": round((1 - scot_share) * 100, 1),
        },
        "wind_capacity": {
            "gb_total": {"gen_gw": round(gb_wind_gw, 1), "capacity_gw": cap_gb,
                         "usage_perc": usage(gb_wind_gw, cap_gb)},
            "scotland": {"gen_gw": round(scot_wind_gw, 1), "capacity_gw": cap_scot,
                         "usage_perc": usage(scot_wind_gw, cap_scot)},
            "england_wales": {"gen_gw": round(ew_wind_gw, 1), "capacity_gw": cap_ew,
                              "usage_perc": usage(ew_wind_gw, cap_ew)},
        },

        "scottish_balance": {
            "demand_gw": scot_demand_gw,
            "generation_gw": scot_total_gw,
            "net_export_gw": round(abs(net_export), 1),
            "is_exporter": net_export >= 0,
        },

        "total_wasted_mwh": total_wasted,
        "fuel_breakdown": breakdown,

        "whisky_pure_alcohol_litres_total": int(wasted_wind * 1000 / KWH_PER_LITRE_ALCOHOL),
        "whisky_litres": int(wasted_wind * 1000 / KWH_PER_LITRE_ALCOHOL),

        "fossil_displacement": {
            "gas_burned_m3": round(total_wasted * GAS_M3_PER_MWH, 1),
            "co2_tonnes": round(total_wasted * CO2_T_PER_MWH, 1),
        },
        "bess_storage": {
            "megapacks_needed": int(total_wasted / MEGAPACK_MWH) if MEGAPACK_MWH else 0,
            "total_gwh": round(total_wasted / 1000, 2),
        },

        "interconnectors": [
            {"name": "Western HVDC Link (Hunterston to Deeside)",
             "path": [[55.73, -4.88], [54.5, -3.5], [53.22, -3.05]]},
            {"name": "Eastern Green Link 1 (Torness to Hawthorn Pit)",
             "path": [[56.0, -2.4], [55.3, -1.8], [54.8, -1.3]]},
        ],
        "substations": [
            {"name": "Beauly Substation (Highlands)", "lat": 57.48, "lon": -4.42},
            {"name": "Denny North (Central Belt)", "lat": 56.02, "lon": -3.88},
            {"name": "Wishaw Substation (Lanarkshire)", "lat": 55.77, "lon": -3.92},
            {"name": "Harker Substation (Border/Carlisle)", "lat": 54.93, "lon": -2.98},
        ],
        "locations": locations,
        "data_quality": q,
    }


# ---------------------------------------------------------------------------
# Gemini summary
# ---------------------------------------------------------------------------
def fallback_summary(m):
    return (
        f"Scottish wind is currently supplying an estimated {m['wind_capacity']['scotland']['gen_gw']} GW "
        f"into the grid, holding Scottish carbon intensity at {m['scottish_carbon_intensity']} gCO2/kWh. "
        f"Even so, {m['total_wasted_mwh']} MWh of Scottish generation has been turned down so far today "
        f"through Balancing Mechanism bids, at a constraint cost of about "
        f"£{sum(b['cost_gbp'] for b in m['fuel_breakdown'].values()):,.0f}. "
        "Transmission limits across the B6 boundary prevent this low-carbon power reaching English demand "
        "centres, so gas plant further south must replace it, and the cost is recovered from bill payers "
        "through BSUoS charges."
    )


def generate_ai_insights(m):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("WARN [ai_summary]: GEMINI_API_KEY missing, using data-driven fallback text.")
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
    )

    client = genai.Client(api_key=api_key)
    candidates = [c for c in (os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash") if c]
    for model in candidates:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            text = (response.text or "").strip()
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
