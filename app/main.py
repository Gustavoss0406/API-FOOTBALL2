import os
import logging
import time
import unicodedata
import re
from fastapi import FastAPI, Depends, HTTPException, Query, Body
from pydantic import BaseModel
import uuid
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import datetime
from . import models
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from scripts.scraper import ensure_event_markets
import requests
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/odds.db")
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

models.Base.metadata.create_all(bind=engine)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
app = FastAPI(title="Football Odds API (v4 Clone)", description="A 100% free football odds API, functionally identical to The Odds API v4.")

_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
allow_origins = ["*"] if _origins.strip() == "*" else [o.strip() for o in _origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Map codes from football-data.org to internal sport keys
FD_CODE_TO_INTERNAL = {
    "PL": "soccer_epl",                      # Premier League
    "PD": "soccer_spain_la_liga",           # Primera Division
    "ELC": "soccer_efl_champ",              # Championship
    "PPL": "soccer_portugal_primeira_liga", # Primeira Liga
    "BL1": "soccer_germany_bundesliga",     # Bundesliga
    "DED": "soccer_netherlands_eredivisie", # Eredivisie
    "BSA": "soccer_brazil_campeonato",      # Brasileirão Série A
    "SA": "soccer_italy_serie_a",           # Serie A
    "FL1": "soccer_france_ligue_one",       # Ligue 1
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_europa_conference",
    "EC": "soccer_uefa_euro_championship",
    "WC": "soccer_fifa_world_cup",          # FIFA World Cup (if present)
    # Domestic cups
    "FAC": "soccer_eng_fa_cup",
    "EFL": "soccer_eng_efl_cup",
    "CS": "soccer_eng_community_shield",
    "CDR": "soccer_esp_copa_del_rey",
    "COPPA": "soccer_ita_coppa_italia",
    "DFB": "soccer_ger_dfb_pokal",
    "COUPE": "soccer_fra_coupe",
    "TACA": "soccer_por_taca_portugal",
    "KNVB": "soccer_ned_knvb_beker",
    "CBR": "soccer_bra_copa_do_brasil",
}

def resolve_sport_key(sport: str) -> str:
    code = sport.upper()
    if code in FD_CODE_TO_INTERNAL:
        return FD_CODE_TO_INTERNAL[code]
    return sport

@app.get("/v4/sports")
def get_sports(all: bool = False, db: Session = Depends(get_db)):
    sports = db.query(models.Sport).all()
    if not all:
        sports = [s for s in sports if s.active]
    return sports

@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="unhealthy")

@app.get("/v4/sports/{sport}/odds")
def get_odds(
    sport: str,
    regions: str = "eu",
    markets: str = "h2h",
    oddsFormat: str = "decimal",
    db: Session = Depends(get_db)
):
    resolved = resolve_sport_key(sport)
    events = db.query(models.Event).filter(models.Event.sport_key == resolved).all()
    
    response = []
    for event in events:
        event_data = {
            "id": event.id,
            "sport_key": event.sport_key,
            "sport_title": event.sport_title, # Adicionado para paridade
            "commence_time": event.commence_time.strftime('%Y-%m-%dT%H:%M:%SZ'), # Formato exato
            "home_team": event.home_team,
            "away_team": event.away_team,
            "bookmakers": []
        }
        
        # Agrupar odds por bookmaker
        bookmakers_dict = {}
        for odd in event.odds:
            if odd.bookmaker_key not in bookmakers_dict:
                bookmakers_dict[odd.bookmaker_key] = {
                    "key": odd.bookmaker_key,
                    "title": odd.bookmaker_key.replace("_", " ").title(), # Melhorado para paridade
                    "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    "markets": []
                }
            
            # Agrupar por mercado
            market_found = False
            for m in bookmakers_dict[odd.bookmaker_key]["markets"]:
                if m["key"] == odd.market_key:
                    outcome = {"name": odd.outcome_name, "price": odd.price}
                    if odd.point is not None:
                        outcome["point"] = odd.point
                    m["outcomes"].append(outcome)
                    market_found = True
                    break
            
            if not market_found:
                outcome = {"name": odd.outcome_name, "price": odd.price}
                if odd.point is not None:
                    outcome["point"] = odd.point
                bookmakers_dict[odd.bookmaker_key]["markets"].append({
                    "key": odd.market_key,
                    "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    "outcomes": [outcome]
                })
        
        event_data["bookmakers"] = list(bookmakers_dict.values())
        response.append(event_data)
        
    return response

@app.get("/v4/sports/{sport}/scores")
def get_scores(sport: str, daysFrom: int = 3, db: Session = Depends(get_db)):
    resolved = resolve_sport_key(sport)
    events = db.query(models.Event).filter(models.Event.sport_key == resolved).all()
    response = []
    for event in events:
        score_data = {
            "id": event.id,
            "sport_key": event.sport_key,
            "sport_title": event.sport_title,
            "commence_time": event.commence_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "completed": event.scores is not None,
            "home_team": event.home_team,
            "away_team": event.away_team,
            "scores": None,
            "last_update": None
        }
        if event.scores:
            score_data["scores"] = [
                {"name": event.home_team, "score": str(event.scores.score_home)}, # String para paridade
                {"name": event.away_team, "score": str(event.scores.score_away)}
            ]
            score_data["last_update"] = event.scores.last_update.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            # Se não tiver score, usa a hora do evento ou agora
            score_data["last_update"] = event.commence_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        response.append(score_data)
    return response

@app.get("/v4/sports/{sport}/events/{eventId}/odds")
def get_event_odds(sport: str, eventId: str, regions: str = "eu", markets: str = "h2h", refresh: bool = Query(False), db: Session = Depends(get_db)):
    resolved = resolve_sport_key(sport)
    event = db.query(models.Event).filter(models.Event.id == eventId, models.Event.sport_key == resolved).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if refresh:
        try:
            ensure_event_markets(db, event)
            db.commit()
            db.refresh(event)
        except Exception:
            db.rollback()
    
    # Lógica de formatação idêntica ao endpoint de odds geral
    event_data = {
        "id": event.id,
        "sport_key": event.sport_key,
        "sport_title": event.sport_title,
        "commence_time": event.commence_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "home_team": event.home_team,
        "away_team": event.away_team,
        "bookmakers": []
    }
    
    bookmakers_dict = {}
    for odd in event.odds:
        if odd.bookmaker_key not in bookmakers_dict:
            bookmakers_dict[odd.bookmaker_key] = {
                "key": odd.bookmaker_key,
                "title": odd.bookmaker_key.replace("_", " ").title(),
                "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "markets": []
            }
        
        market_found = False
        for m in bookmakers_dict[odd.bookmaker_key]["markets"]:
            if m["key"] == odd.market_key:
                outcome = {"name": odd.outcome_name, "price": odd.price}
                if odd.point is not None:
                    outcome["point"] = odd.point
                m["outcomes"].append(outcome)
                market_found = True
                break
        
        if not market_found:
            outcome = {"name": odd.outcome_name, "price": odd.price}
            if odd.point is not None:
                outcome["point"] = odd.point
            bookmakers_dict[odd.bookmaker_key]["markets"].append({
                "key": odd.market_key,
                "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "outcomes": [outcome]
            })
    
    event_data["bookmakers"] = list(bookmakers_dict.values())
    return event_data

class ResolveRequest(BaseModel):
    home_team: str
    away_team: str
    commence_time: Optional[str] = None

def _norm_name(s: str) -> str:
    return ''.join(ch for ch in s.lower() if ch.isalnum())

@app.post("/v4/sports/{sport}/events/resolve")
def resolve_event(sport: str, req: ResolveRequest, refresh: bool = Query(True), db: Session = Depends(get_db)):
    resolved = resolve_sport_key(sport)
    sp = db.query(models.Sport).filter(models.Sport.key == resolved).first()
    if not sp:
        sp = models.Sport(key=resolved, group="Soccer", title=resolved, description=resolved)
        db.add(sp)
        db.commit()
    for bkey, btitle in [("bet365", "Bet365"), ("betano", "Betano"), ("pinnacle", "Pinnacle")]:
        if not db.query(models.Bookmaker).filter(models.Bookmaker.key == bkey).first():
            db.add(models.Bookmaker(key=bkey, title=btitle))
    db.commit()
    hn = _norm_name(req.home_team)
    an = _norm_name(req.away_team)
    existing = None
    for ev in db.query(models.Event).filter(models.Event.sport_key == resolved).all():
        if _norm_name(ev.home_team) in hn or hn in _norm_name(ev.home_team):
            if _norm_name(ev.away_team) in an or an in _norm_name(ev.away_team):
                existing = ev
                break
    if not existing:
        when = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
        if req.commence_time:
            try:
                when = datetime.datetime.fromisoformat(req.commence_time.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                when = when
        ev = models.Event(
            id=str(uuid.uuid4()).replace("-", ""),
            sport_key=resolved,
            sport_title=sp.title,
            commence_time=when,
            home_team=req.home_team,
            away_team=req.away_team,
        )
        db.add(ev)
        db.commit()
        existing = ev
    if refresh:
        try:
            ensure_event_markets(db, existing)
            db.commit()
            db.refresh(existing)
        except Exception:
            db.rollback()
    bookmakers_dict = {}
    for odd in existing.odds:
        if odd.bookmaker_key not in bookmakers_dict:
            bookmakers_dict[odd.bookmaker_key] = {
                "key": odd.bookmaker_key,
                "title": odd.bookmaker_key.replace("_", " ").title(),
                "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "markets": []
            }
        market_found = False
        for m in bookmakers_dict[odd.bookmaker_key]["markets"]:
            if m["key"] == odd.market_key:
                outcome = {"name": odd.outcome_name, "price": odd.price}
                if odd.point is not None:
                    outcome["point"] = odd.point
                m["outcomes"].append(outcome)
                market_found = True
                break
        if not market_found:
            outcome = {"name": odd.outcome_name, "price": odd.price}
            if odd.point is not None:
                outcome["point"] = odd.point
            bookmakers_dict[odd.bookmaker_key]["markets"].append({
                "key": odd.market_key,
                "last_update": odd.last_update.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "outcomes": [outcome]
            })
    return {
        "id": existing.id,
        "sport_key": existing.sport_key,
        "sport_title": existing.sport_title,
        "commence_time": existing.commence_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "home_team": existing.home_team,
        "away_team": existing.away_team,
        "bookmakers": list(bookmakers_dict.values()),
    }

@app.get("/v4/historical/sports/{sport}/odds")
def get_historical_odds(sport: str, date: str, db: Session = Depends(get_db)):
    # Simulação de histórico retornando o estado atual para a data solicitada
    resolved = resolve_sport_key(sport)
    data = get_odds(sport=resolved, db=db)
    return {
        "timestamp": date, 
        "previous_timestamp": None, 
        "next_timestamp": None, 
        "data": data
    }

ESPN_LEAGUE = {
    # legacy internal keys
    "soccer_france_ligue_one": "fra.1",
    "soccer_epl": "eng.1",
    "soccer_spain_la_liga": "esp.1",
    "soccer_italy_serie_a": "ita.1",
    "soccer_germany_bundesliga": "ger.1",
    # Football-Data short codes
    "PL": "eng.1",   # Premier League
    "SA": "ita.1",   # Serie A
    "PD": "esp.1",   # Primera Division (LaLiga)
    "BL1": "ger.1",  # Bundesliga
    "FL1": "fra.1",  # Ligue 1
    "PPL": "por.1",  # Primeira Liga
    "DED": "ned.1",  # Eredivisie
    "ELC": "eng.2",  # Championship
    "BSA": "bra.1",  # Campeonato Brasileiro Série A
    "CL": "uefa.champions",
    "EL": "uefa.europa",
    "ECL": "uefa.europa.conf",
    "EC": "uefa.euro",
    "WC": "fifa.world",
    "FAC": "eng.fa",
    "EFL": "eng.efl",
    "CS": "eng.community",
    "CDR": "esp.copa_del_rey",
    "COPPA": "ita.coppa_italia",
    "DFB": "ger.dfb_pokal",
    "COUPE": "fra.cup",
    "TACA": "por.taca",
    "KNVB": "ned.knvb_beker",
    "CBR": "bra.cup",
}

CANDIDATE_LEAGUES = [
    "fifa.world", "uefa.euro",
    "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "eng.1", "eng.2", "esp.1", "ita.1", "ger.1", "fra.1", "por.1", "ned.1", "bra.1",
    "eng.fa", "eng.efl", "eng.community",
    "esp.copa_del_rey",
    "ita.coppa_italia",
    "ger.dfb_pokal",
    "fra.cup", "fra.scup", "fra.sper",
    "ned.knvb_beker",
    "por.taca",
    "bra.cup",
]

from typing import Optional

_CACHE: dict = {}
def _fetch_json(url: str, headers: Optional[dict] = None, tries: int = None, timeout: int = None, ttl: Optional[int] = None):
    now = time.time()
    if url in _CACHE:
        exp, data = _CACHE[url]
        if now < exp:
            return data
        else:
            del _CACHE[url]
    last_err = None
    h = headers or {}
    if "User-Agent" not in h:
        h["User-Agent"] = "Mozilla/5.0"
    if tries is None:
        tries = INSIGHTS_FETCH_TRIES
    if timeout is None:
        timeout = INSIGHTS_HTTP_TIMEOUT
    if ttl is None:
        tl = 120
        if "/teams" in url or "/schedule" in url:
            tl = 300
        elif "/scoreboard" in url:
            tl = 60
        elif "/summary" in url:
            tl = 120
        ttl = tl
    for i in range(tries):
        try:
            r = _SESSION.get(url, headers=h, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                _CACHE[url] = (now + ttl, data)
                return data
            last_err = Exception(f"status {r.status_code}")
        except Exception as e:
            last_err = e
        time.sleep(0.25 * (i + 1))
    raise last_err

def _espn_teams(league_code: str):
    urls = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/teams?limit=200",
        f"https://site.api.espn.com/apis/v2/sports/soccer/{league_code}/teams?limit=200",
    ]
    data = None
    for u in urls:
        try:
            data = _fetch_json(u)
            break
        except Exception:
            data = None
    if data is None:
        return {}
    out = {}
    try:
        teams_arr = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    except Exception:
        teams_arr = []
    for it in teams_arr:
        t = it.get("team", {})
        slug = t.get("slug", "").lower()
        key = t.get("abbreviation", "").lower()
        name = t.get("displayName", "").lower()
        short = t.get("shortDisplayName", "").lower()
        for k in {slug, key, name, short}:
            if k:
                out[k] = {"id": t.get("id"), "name": t.get("displayName"), "slug": t.get("slug")}
    if not out:
        try:
            ua = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(urls[0], headers=ua, timeout=INSIGHTS_HTTP_TIMEOUT)
            if r.status_code == 200:
                j = r.json()
                for it in j.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
                    t = it.get("team", {})
                    slug = t.get("slug", "").lower()
                    key = t.get("abbreviation", "").lower()
                    name = t.get("displayName", "").lower()
                    short = t.get("shortDisplayName", "").lower()
                    for k in {slug, key, name, short}:
                        if k:
                            out[k] = {"id": t.get("id"), "name": t.get("displayName"), "slug": t.get("slug")}
        except Exception:
            pass
    return out

def _espn_last_events_ids(league_code: str, team_id: str, limit: int = 10):
    urls = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/teams/{team_id}/schedule?limit=50",
        f"https://site.api.espn.com/apis/v2/sports/soccer/{league_code}/teams/{team_id}/schedule?limit=50",
    ]
    data = None
    for u in urls:
        try:
            data = _fetch_json(u)
            break
        except Exception:
            data = None
    if data is None:
        return []
    evs = []
    for e in data.get("events", []):
        comp = False
        comps = e.get("competitions") or []
        if comps:
            st = comps[0].get("status", {}).get("type", {})
            if isinstance(st, dict):
                comp = bool(st.get("completed")) or str(st.get("state", "")).lower() in {"post", "final", "complete", "end"}
        else:
            st = e.get("status", {}).get("type", {})
            if isinstance(st, dict):
                comp = bool(st.get("completed")) or str(st.get("state", "")).lower() in {"post", "final", "complete", "end"}
        if comp and e.get("id"):
            evs.append(str(e.get("id")))
        if len(evs) >= limit:
            break
    return evs

def _espn_event_summary(league_code: str, event_id: str):
    urls = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/summary?event={event_id}",
        f"https://site.api.espn.com/apis/v2/sports/soccer/{league_code}/summary?event={event_id}",
    ]
    for u in urls:
        try:
            return _fetch_json(u)
        except Exception:
            pass
    raise HTTPException(status_code=502, detail="summary fetch failed")

# --- API-FOOTBALL comparison helpers (no data copied into main payload) ---
def _apif_get(url: str, api_key: str, timeout: int = 10):
    h = {"x-apisports-key": api_key}
    return _fetch_json(url, headers=h, tries=2, timeout=timeout, ttl=60)

def _apif_search_team_id(name: str, api_key: str):
    q = name.strip()
    url = f"https://v3.football.api-sports.io/teams?search={requests.utils.quote(q)}"
    data = _apif_get(url, api_key)
    for it in data.get("response", []):
        n = (it.get("team", {}) or {}).get("name", "")
        if n.lower() == name.lower():
            return it.get("team", {}).get("id")
    if data.get("response"):
        return data["response"][0].get("team", {}).get("id")
    return None

def _apif_last_fixtures(team_id: int, api_key: str, last: int = 2):
    for n in [last, 5]:
        url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&last={n}"
        data = _apif_get(url, api_key)
        fids = [f.get("fixture", {}).get("id") for f in data.get("response", []) if f.get("fixture", {}).get("id")]
        if fids:
            return fids
    return []

def _apif_fixture_stats(fid: int, api_key: str):
    url = f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fid}"
    data = _apif_get(url, api_key)
    return data.get("response", [])

def _apif_team_metrics_from_fixtures(team_name: str, team_id: int, fids: list[int], api_key: str):
    vals_corners = []
    vals_offsides = []
    vals_cards = []
    # xG and woodwork not universally available in API-Football
    for fid in fids:
        try:
            stats = _apif_fixture_stats(fid, api_key)
        except Exception:
            continue
        # find team block
        for block in stats:
            tname = (block.get("team", {}) or {}).get("name", "")
            if tname.lower() != team_name.lower():
                continue
            for item in block.get("statistics", []) or []:
                k = (item.get("type") or "").lower()
                v = item.get("value")
                if v is None:
                    continue
                try:
                    num = float(v)
                except Exception:
                    continue
                if k == "corner kicks":
                    vals_corners.append(num)
                elif k == "offsides":
                    vals_offsides.append(num)
                elif k == "yellow cards":
                    # Will combine yellow+red as cards
                    yc = num
                    rc = 0.0
                    # look ahead for red in same block
                    for it in block.get("statistics", []) or []:
                        if (it.get("type") or "").lower() == "red cards" and it.get("value") is not None:
                            try:
                                rc = float(it.get("value"))
                            except Exception:
                                rc = 0.0
                            break
                    vals_cards.append(yc + rc)
                elif k == "red cards":
                    # handled when yellow encountered; skip direct
                    pass
            break
    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else None
    samples = {"corners_avg": {"n": len(vals_corners), "sum": sum(vals_corners)},
               "offsides_avg": {"n": len(vals_offsides), "sum": sum(vals_offsides)},
               "cards_avg": {"n": len(vals_cards), "sum": sum(vals_cards)}}
    return {"corners_avg": avg(vals_corners), "offsides_avg": avg(vals_offsides), "cards_avg": avg(vals_cards)}, samples

def _summaries_for_events(league_code: str, events: list[str], max_workers: int = None, deadline: Optional[float] = None) -> dict:
    results = {}
    if not events:
        return results
    if max_workers is None:
        max_workers = INSIGHTS_MAX_WORKERS
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_espn_event_summary, league_code, eid): eid for eid in events}
        for f in as_completed(futs):
            if deadline is not None and time.time() > deadline:
                break
            eid = futs[f]
            try:
                results[eid] = f.result()
            except Exception:
                continue
    return results

def _find_next_h2h_event(home_team_id: str, away_team_id: str):
    now = datetime.datetime.utcnow().replace(tzinfo=None)
    best = None
    for code in CANDIDATE_LEAGUES:
        try:
            home_sched = _fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/teams/{home_team_id}/schedule?limit=50")
            for e in home_sched.get("events", []):
                comps = (e.get("competitions") or [{}])[0].get("competitors", [])
                ids = [c.get("team", {}).get("id") for c in comps]
                if away_team_id in ids:
                    # upcoming or today
                    try:
                        d = (e.get("date") or (e.get("competitions") or [{}])[0].get("date"))
                        when = datetime.datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        when = now
                    st = (e.get("competitions") or [{}])[0].get("status", {}).get("type", {})
                    is_future = str(st.get("state", "")).lower() in {"pre", "scheduled", "future", "postponed"} or when >= now
                    if is_future:
                        cand = (when, code, str(e.get("id")))
                        if best is None or cand[0] < best[0]:
                            best = cand
        except Exception:
            continue
    if best:
        return best[1], best[2]
    return None, None

def _find_h2h_by_scoreboard(home_team_id: str, away_team_id: str, days_ahead: int = 7):
    ua = {"User-Agent": "Mozilla/5.0"}
    today = datetime.datetime.utcnow().date()
    for d in range(0, days_ahead + 1):
        date_str = (today + datetime.timedelta(days=d)).strftime('%Y%m%d')
        for code in CANDIDATE_LEAGUES:
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={date_str}"
                data = _fetch_json(url, headers=ua)
                for e in data.get("events", []):
                    comp = (e.get("competitions") or [{}])[0]
                    ids = [c.get("team", {}).get("id") for c in comp.get("competitors", [])]
                    if home_team_id in ids and away_team_id in ids:
                        return code, str(e.get("id"))
            except Exception:
                continue
    return None, None

def _parse_date_str(s: str) -> Optional[str]:
    try:
        if not s:
            return None
        s = s.strip()
        if len(s) == 8 and s.isdigit():
            return s
        if len(s) >= 10:
            y, m, d = s[:10].split("-")
            return f"{y}{m}{d}"
    except Exception:
        return None
    return None

def _find_event_by_names_on_date(date_str: str, home_name: str, away_name: str):
    ua = {"User-Agent": "Mozilla/5.0"}
    def _norm_str(s: str) -> str:
        if not s:
            return ""
        s = unicodedata.normalize('NFD', s)
        s = ''.join(ch for ch in s if not unicodedata.combining(ch))
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s
    hn = _norm_str(home_name)
    an = _norm_str(away_name)
    for code in CANDIDATE_LEAGUES:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={date_str}"
            data = _fetch_json(url, headers=ua, ttl=60)
            for e in data.get("events", []):
                comp = (e.get("competitions") or [{}])[0]
                comps = comp.get("competitors", [])
                names = [ _norm_str(c.get("team", {}).get("displayName", "")) for c in comps]
                if any(hn in n or n in hn for n in names) and any(an in n or n in an for n in names):
                    # Build structured competitors preserving home/away
                    compo = []
                    for c in comps:
                        compo.append({
                            "id": c.get("team", {}).get("id"),
                            "name": c.get("team", {}).get("displayName"),
                            "homeAway": c.get("homeAway")
                        })
                    return code, str(e.get("id")), compo
        except Exception:
            continue
    return None, None, None

def _find_event_by_names_around_date(date_str: str, home_name: str, away_name: str, window_days: int = 1):
    try:
        base = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    except Exception:
        return None, None, None
    offs = list(range(-window_days, window_days + 1))
    for d in offs:
        t = base + datetime.timedelta(days=d)
        ds = t.strftime('%Y%m%d')
        dl, de, comp = _find_event_by_names_on_date(ds, home_name, away_name)
        if dl and de and comp:
            return dl, de, comp
    return None, None, None

def _resolve_team_across_candidates(names: list[str]):
    found = {}
    def _norm_str(s: str) -> str:
        if not s:
            return ""
        s = unicodedata.normalize('NFD', s)
        s = ''.join(ch for ch in s if not unicodedata.combining(ch))
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s
    for code in CANDIDATE_LEAGUES + ["esp.1", "por.1", "eng.1", "ita.1", "ger.1"]:
        try:
            tm = _espn_teams(code)
            try:
                logging.warning(f"insights.cross scan code={code} teams={len(tm)}")
            except Exception:
                pass
        except Exception:
            continue
        for name in names:
            k = _norm_str(name)
            # direct match
            if k in tm and name not in found:
                found[name] = tm[k]
                try:
                    logging.warning(f"insights.cross direct name='{name}' code={code} -> id={found[name]['id']}")
                except Exception:
                    pass
                continue
            # fuzzy
            for mk, mv in tm.items():
                mk_norm = _norm_str(mk)
                if k in mk_norm or mk_norm in k:
                    if name not in found:
                        found[name] = mv
                        try:
                            logging.warning(f"insights.cross fuzzy name='{name}' code={code} match='{mk}' id={mv.get('id')}")
                        except Exception:
                            pass
                        break
        if all(n in found for n in names):
            break
    return found

def _find_event_on_date_by_ids(date_str: str, home_id: str, away_id: str):
    ua = {"User-Agent": "Mozilla/5.0"}
    for code in CANDIDATE_LEAGUES:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={date_str}"
            data = _fetch_json(url, headers=ua, ttl=60)
            for e in data.get("events", []):
                comp = (e.get("competitions") or [{}])[0]
                comps = comp.get("competitors", [])
                ids = [c.get("team", {}).get("id") for c in comps]
                if str(home_id) in map(str, ids) and str(away_id) in map(str, ids):
                    compo = []
                    for c in comps:
                        compo.append({
                            "id": c.get("team", {}).get("id"),
                            "name": c.get("team", {}).get("displayName"),
                            "homeAway": c.get("homeAway")
                        })
                    return code, str(e.get("id")), compo
        except Exception:
            continue
    return None, None, None

def _team_stats_from_events(league_code: str, team_id: str, team_name_norm: str, events: list[str], summaries: Optional[dict] = None):
    goals_for = []
    goals_against = []
    corners = []
    cards = []
    offsides = []
    wood = []
    xg_for_vals = []
    xg_against_vals = []
    form = []
    for eid in events:
        try:
            s = summaries.get(eid) if summaries is not None else _espn_event_summary(league_code, eid)
            if s is None:
                continue
        except Exception:
            continue
        comps = s.get("boxscore", {}).get("teams", [])
        gf = None
        ga = None
        r = s.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
        for c in r:
            nm = c.get("team", {}).get("displayName", "").lower()
            sc = int(c.get("score", "0")) if c.get("score") is not None else 0
            if team_name_norm in nm or nm in team_name_norm:
                gf = sc
            else:
                ga = sc
        if gf is not None and ga is not None:
            goals_for.append(gf)
            goals_against.append(ga)
            if gf > ga:
                form.append("W")
            elif gf == ga:
                form.append("D")
            else:
                form.append("L")
        xg_self = None
        xg_opp = None
        for tm in comps:
            nm = tm.get("team", {}).get("displayName", "").lower()
            statistics = tm.get("statistics", [])
            from typing import Optional
            def get_stat(keys: set[str]) -> Optional[float]:
                for st in statistics:
                    name = st.get("name", "")
                    if name in keys:
                        val = st.get("displayValue")
                        try:
                            return float(val)
                        except Exception:
                            try:
                                return float(st.get("value")) if st.get("value") is not None else None
                            except Exception:
                                return None
                return None
            is_self = team_name_norm in nm or nm in team_name_norm
            if is_self:
                c = get_stat({"wonCorners", "cornerKicks", "cornersWon"})
                if c is not None:
                    corners.append(c)
                y = get_stat({"yellowCards"}) or 0.0
                rcard = get_stat({"redCards"}) or 0.0
                cards.append(y + rcard)
                o = get_stat({"offsides"})
                if o is not None:
                    offsides.append(o)
                w = get_stat({"shotsOnWoodwork", "hitWoodwork"})
                if w is not None:
                    wood.append(w)
                xg_self = get_stat({"expectedGoals", "xg", "xG"})
            else:
                # attempt to capture opponent xG
                def get_stat_opp(keys: set[str]) -> Optional[float]:
                    for st in statistics:
                        name = st.get("name", "")
                        if name in keys:
                            try:
                                return float(st.get("displayValue"))
                            except Exception:
                                try:
                                    return float(st.get("value")) if st.get("value") is not None else None
                                except Exception:
                                    return None
                    return None
                xg_opp = get_stat_opp({"expectedGoals", "xg", "xG"})
        if xg_self is not None:
            xg_for_vals.append(xg_self)
        if xg_opp is not None:
            xg_against_vals.append(xg_opp)
    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else None
    stats = {
        "goals_for_avg": avg(goals_for),
        "goals_against_avg": avg(goals_against),
        "corners_avg": avg(corners),
        "cards_avg": avg(cards),
        "offsides_avg": avg(offsides),
        "woodwork_hit_avg": avg(wood),
    }
    if xg_for_vals:
        stats["xg_for_avg"] = avg(xg_for_vals)
    if xg_against_vals:
        stats["xg_against_avg"] = avg(xg_against_vals)
    samples = {
        "goals_for_avg": {"n": len(goals_for), "sum": sum(goals_for)},
        "goals_against_avg": {"n": len(goals_against), "sum": sum(goals_against)},
        "corners_avg": {"n": len(corners), "sum": sum(corners)},
        "cards_avg": {"n": len(cards), "sum": sum(cards)},
        "offsides_avg": {"n": len(offsides), "sum": sum(offsides)},
        "woodwork_hit_avg": {"n": len(wood), "sum": sum(wood)},
    }
    if xg_for_vals:
        samples["xg_for_avg"] = {"n": len(xg_for_vals), "sum": sum(xg_for_vals)}
    if xg_against_vals:
        samples["xg_against_avg"] = {"n": len(xg_against_vals), "sum": sum(xg_against_vals)}
    return stats, form[:5], samples

def _h2h_events_for_teams(league_code: str, home_team_id: str, away_team_id: str, limit: int = 5):
    home_sched = _espn_last_events_ids(league_code, home_team_id, 50)
    events = []
    for eid in home_sched:
        try:
            s = _espn_event_summary(league_code, eid)
        except Exception:
            continue
        comps = s.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
        ids = [c.get("team", {}).get("id") for c in comps]
        if home_team_id in ids and away_team_id in ids:
            events.append(s)
        if len(events) >= limit:
            break
    return events

def _btts_over_rates(league_code: str, team_id: str, events: list[str], summaries: Optional[dict] = None):
    both = 0
    over = 0
    n = 0
    for eid in events:
        try:
            s = summaries.get(eid) if summaries is not None else _espn_event_summary(league_code, eid)
            if s is None:
                continue
        except Exception:
            continue
        r = s.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
        if len(r) != 2:
            continue
        try:
            a = int(r[0].get("score", "0"))
            b = int(r[1].get("score", "0"))
        except Exception:
            continue
        n += 1
        if a > 0 and b > 0:
            both += 1
        if a + b > 2:
            over += 1
    rates = {
        "btts_rate_percent": int(round(100 * both / n)) if n else 0,
        "over_2_5_rate_percent": int(round(100 * over / n)) if n else 0,
    }
    return rates, n

def _resolve_team_key(mapper: dict, team_id: str):
    def _norm_str(s: str) -> str:
        if not s:
            return ""
        s = unicodedata.normalize('NFD', s)
        s = ''.join(ch for ch in s if not unicodedata.combining(ch))
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return s
    k = _norm_str(team_id)
    if k in mapper:
        return mapper[k]
    for m in mapper.keys():
        if k in _norm_str(m):
            return mapper[m]
    raise HTTPException(status_code=404, detail="team not found")

@app.post("/v4/insights/preview")
def match_insights(payload: dict = Body(...)):
    league = payload.get("meta", {}).get("league") or payload.get("league")
    espn_league = ESPN_LEAGUE.get(league, None) if league else None
    teams = payload.get("teams", {})
    home = teams.get("home", {})
    away = teams.get("away", {})
    home_id = home.get("id") or home.get("name", "").lower()
    away_id = away.get("id") or away.get("name", "").lower()
    # If payload carries a target date, try to pinpoint the exact event fast
    target_date = payload.get("meta", {}).get("date") or payload.get("date")
    date_token = _parse_date_str(target_date) if target_date else None
    det_league = None
    det_event = None
    resolved_home = None
    resolved_away = None
    if date_token and (home.get("name") or home.get("id")) and (away.get("name") or away.get("id")):
        hn = home.get("name") or str(home.get("id"))
        an = away.get("name") or str(away.get("id"))
        dl, de, compo = _find_event_by_names_on_date(date_token, hn, an)
        if dl and de and compo:
            det_league, det_event = dl, de
            # assign by true home/away
            for c in compo:
                if c.get("homeAway") == "home":
                    resolved_home = {"id": c.get("id"), "name": c.get("name")}
                elif c.get("homeAway") == "away":
                    resolved_away = {"id": c.get("id"), "name": c.get("name")}
        # If not found by names on the date, proceed to generic detection below
        if not det_event:
            # Try to resolve both teams across candidate leagues by name, then locate by ids on date
            cross = _resolve_team_across_candidates([hn, an])
            if hn in cross and an in cross:
                dl2, de2, comp2 = _find_event_on_date_by_ids(date_token, cross[hn]["id"], cross[an]["id"])
                if dl2 and de2 and comp2:
                    det_league, det_event = dl2, de2
                    for c in comp2:
                        if c.get("homeAway") == "home":
                            resolved_home = {"id": c.get("id"), "name": c.get("name")}
                        elif c.get("homeAway") == "away":
                            resolved_away = {"id": c.get("id"), "name": c.get("name")}
            # As última tentativa por nomes ±1 dia
            if not det_event:
                dl3, de3, comp3 = _find_event_by_names_around_date(date_token, hn, an, window_days=1)
                if dl3 and de3 and comp3:
                    det_league, det_event = dl3, de3
                    for c in comp3:
                        if c.get("homeAway") == "home":
                            resolved_home = {"id": c.get("id"), "name": c.get("name")}
                        elif c.get("homeAway") == "away":
                            resolved_away = {"id": c.get("id"), "name": c.get("name")}
    # Resolve team ids/names
    if resolved_home and resolved_away:
        home_res = resolved_home
        away_res = resolved_away
        use_league = det_league or espn_league or "uefa.champions"
    else:
        # Try cross-league resolution first to avoid league bias
        hn = home.get("name") or str(home.get("id") or "")
        an = away.get("name") or str(away.get("id") or "")
        cross = _resolve_team_across_candidates([hn, an])
        try:
            logging.warning(f"insights.cross_resolution query hn='{hn}' an='{an}' -> keys={list(cross.keys())}")
        except Exception:
            pass
        if hn in cross and an in cross:
            home_res = cross[hn]
            away_res = cross[an]
            use_league = det_league or espn_league or "uefa.champions"
        else:
            # Fallback to league-bound teams list
            if not espn_league:
                espn_league = "fra.1"
            team_map = _espn_teams(espn_league)
            try:
                logging.warning(f"insights.league_fallback league={espn_league} keys={list(team_map.keys())[:5]}")
            except Exception:
                pass
            home_res = _resolve_team_key(team_map, home_id)
            away_res = _resolve_team_key(team_map, away_id)
            use_league = espn_league
    # Determine correct league/event for the next H2H if not already found via date
    if not det_event and payload.get("status") == "SCHEDULED":
        dl, de = _find_next_h2h_event(home_res.get("id"), away_res.get("id"))
        if not dl or not de:
            dl, de = _find_h2h_by_scoreboard(home_res.get("id"), away_res.get("id"), 7)
        if dl and de:
            det_league, det_event = dl, de
            use_league = det_league
    cache_key = None
    meta_in = payload.get("meta") or {}
    if not meta_in.get("strict") and not meta_in.get("compare_api_football"):
        k_date = date_token or "-"
        cache_key = f"{k_date}|{home_res['id']}|{away_res['id']}|{use_league}"
        if cache_key in _CACHE:
            exp, cached = _CACHE[cache_key]
            if time.time() < exp:
                clone = dict(cached)
                clone["match_id"] = payload.get("match_id") or str(uuid.uuid4()).replace("-", "")
                return clone
    ev_limit = INSIGHTS_EV_LIMIT_DATE if date_token else INSIGHTS_EV_LIMIT_DEFAULT
    home_events = _espn_last_events_ids(use_league, home_res["id"], ev_limit)
    away_events = _espn_last_events_ids(use_league, away_res["id"], ev_limit)
    all_eids = list(dict.fromkeys(home_events + away_events))
    deadline = time.time() + INSIGHTS_BUDGET_SECS
    summaries_cache = _summaries_for_events(use_league, all_eids, max_workers=INSIGHTS_MAX_WORKERS, deadline=deadline)
    home_stats, home_form, home_samples = _team_stats_from_events(use_league, home_res["id"], (home_res.get("name") or "").lower(), home_events, summaries=summaries_cache)
    away_stats, away_form, away_samples = _team_stats_from_events(use_league, away_res["id"], (away_res.get("name") or "").lower(), away_events, summaries=summaries_cache)
    rates, h2h_n = _btts_over_rates(use_league, home_res["id"], all_eids, summaries=summaries_cache)
    h2h_summaries = _h2h_events_for_teams(use_league, home_res["id"], away_res["id"], INSIGHTS_H2H_LIMIT)
    # Try to enrich from the detected upcoming event
    venue_val = None
    referee_name = None
    kickoff_val = None
    lineup_info = None
    if det_event and det_league:
        try:
            s = _espn_event_summary(det_league, det_event)
            gi = s.get("gameInfo", {})
            venue_val = (gi.get("venue") or {}).get("fullName") or venue_val
            offs = (gi.get("officials") or [])
            if offs:
                referee_name = offs[0].get("displayName")
            hdr_comp = (s.get("header", {}).get("competitions") or [{}])[0]
            kickoff_val = hdr_comp.get("date") or kickoff_val
            # lineups with positions when available
            lns = s.get("lineups") or []
            home_players = []
            away_players = []
            confirmed = False
            for block in lns:
                team = (block.get("team") or {})
                tside = team.get("id")
                confirmed = confirmed or bool(block.get("confirmed"))
                def collect(pls):
                    out = []
                    for p in pls or []:
                        ath = p.get("athlete") or {}
                        pos = (p.get("position") or {}).get("abbreviation")
                        out.append({
                            "name": ath.get("displayName") or ath.get("shortName"),
                            "pos": pos,
                            "starter": bool(p.get("starter")),
                        })
                    return out
                players = collect(block.get("starters")) + collect(block.get("substitutes"))
                if str(tside) == str((resolved_home or {}).get("id") or home_res.get("id")):
                    home_players.extend(players)
                elif str(tside) == str((resolved_away or {}).get("id") or away_res.get("id")):
                    away_players.extend(players)
            if home_players or away_players:
                lineup_info = {"confirmed": confirmed, "home_players": home_players, "away_players": away_players}
        except Exception:
            pass
    last_matches = []
    for s in h2h_summaries:
        hdr = s.get("header", {}).get("competitions", [{}])[0]
        date = hdr.get("date")
        comps = hdr.get("competitors", [])
        if len(comps) == 2:
            a = comps[0]
            b = comps[1]
            try:
                ascore = int(a.get("score", "0"))
                bscore = int(b.get("score", "0"))
            except Exception:
                ascore = bscore = 0
            last_matches.append({
                "date": date,
                "home": a.get("team", {}).get("displayName"),
                "away": b.get("team", {}).get("displayName"),
                "score": f"{ascore}-{bscore}",
            })
        if not venue_val:
            v = hdr.get("venue", {}).get("fullName")
            if v:
                venue_val = v
    def _merge_stats(existing, computed):
        if not isinstance(existing, dict):
            return computed
        out = dict(existing)
        for k, v in computed.items():
            if out.get(k) is None:
                out[k] = v
        return out
    home_stats_filled = _merge_stats(home.get("stats_season"), home_stats)
    away_stats_filled = _merge_stats(away.get("stats_season"), away_stats)
    home_form_out = home.get("form_last_5") if isinstance(home.get("form_last_5"), list) and len(home.get("form_last_5")) > 0 else home_form[:5]
    away_form_out = away.get("form_last_5") if isinstance(away.get("form_last_5"), list) and len(away.get("form_last_5")) > 0 else away_form[:5]
    missing_metrics = []
    for k, v in home_samples.items():
        if v["n"] == 0 and home_stats.get(k) is None:
            missing_metrics.append(f"home.{k}")
    for k, v in away_samples.items():
        if v["n"] == 0 and away_stats.get(k) is None:
            missing_metrics.append(f"away.{k}")
    if missing_metrics:
        logging.warning(f"insights.veracity.missing={missing_metrics}")
    zero_confirmed = []
    for k, v in home_samples.items():
        if home_stats.get(k) == 0 and v["n"] > 0 and v["sum"] == 0:
            zero_confirmed.append(f"home.{k}")
    for k, v in away_samples.items():
        if away_stats.get(k) == 0 and v["n"] > 0 and v["sum"] == 0:
            zero_confirmed.append(f"away.{k}")
    if zero_confirmed:
        logging.info(f"insights.veracity.zero_confirmed={zero_confirmed}")
    # choose final team identity: if event-resolved, use that to preserve true home/away
    final_home_name = (resolved_home or {}).get("name") or home.get("name") or home_res["name"]
    final_away_name = (resolved_away or {}).get("name") or away.get("name") or away_res["name"]
    final_home_id = (resolved_home or {}).get("id") or str(home_res.get("id") or home_id)
    final_away_id = (resolved_away or {}).get("id") or str(away_res.get("id") or away_id)
    # standings (best effort)
    standings_map = {}
    try:
        standings_map = _espn_standings(use_league)
    except Exception:
        standings_map = {}
    # compute referee averages from cached summaries
    ref_avg_cards = None
    ref_avg_reds = None
    if referee_name:
        try:
            ref_avg_cards, ref_avg_reds = _ref_avgs_from_summaries(referee_name, summaries_cache)
        except Exception:
            ref_avg_cards = ref_avg_reds = None
    # attempt logos
    league_logo_val = None
    try:
        league_logo_val = _league_logo(use_league, date_token, kickoff_val)
    except Exception:
        league_logo_val = None
    home_logo_val = None
    away_logo_val = None
    try:
        if det_event and det_league:
            evsum = _espn_event_summary(det_league, det_event)
            home_logo_val = _extract_team_logo_from_summary(evsum, (resolved_home or {}).get("id") or home_res.get("id"))
            away_logo_val = _extract_team_logo_from_summary(evsum, (resolved_away or {}).get("id") or away_res.get("id"))
    except Exception:
        pass
    out = {
        "match_id": payload.get("match_id") or str(uuid.uuid4()).replace("-", ""),
        "status": payload.get("status") or "SCHEDULED",
        "meta": {
            "league": det_league or (league if league else use_league),
            "league_logo": league_logo_val,
            "venue": payload.get("meta", {}).get("venue") or venue_val,
            "commence_time": kickoff_val,
            "referee": ({"name": referee_name, "avg_cards": ref_avg_cards, "avg_reds": ref_avg_reds} if referee_name else {}),
        },
        "teams": {
            "home": {
                "name": final_home_name,
                "id": final_home_id,
                "logo": home_logo_val,
                "rank": (standings_map.get(str(final_home_id)) or {}).get("rank"),
                "points": (standings_map.get(str(final_home_id)) or {}).get("points"),
                "stats_season": home_stats_filled,
                "form_last_5": home_form_out,
            },
            "away": {
                "name": final_away_name,
                "id": final_away_id,
                "logo": away_logo_val,
                "rank": (standings_map.get(str(final_away_id)) or {}).get("rank"),
                "points": (standings_map.get(str(final_away_id)) or {}).get("points"),
                "stats_season": away_stats_filled,
                "form_last_5": away_form_out,
            },
        },
        "lineups": lineup_info or (payload.get("lineups") or {"confirmed": False, "home_players": [], "away_players": []}),
        "h2h": {
            "last_matches": last_matches,
            "btts_rate_percent": rates["btts_rate_percent"],
            "over_2_5_rate_percent": rates["over_2_5_rate_percent"],
        },
        "veracity": {
            "home": {
                "stats_season": {k: {"sample_size": v["n"], "zero_is_real": (home_stats.get(k) == 0 and v["n"] > 0 and v["sum"] == 0)} for k, v in home_samples.items()},
                "form_last_5": {"sample_size": len(home_form_out)},
            },
            "away": {
                "stats_season": {k: {"sample_size": v["n"], "zero_is_real": (away_stats.get(k) == 0 and v["n"] > 0 and v["sum"] == 0)} for k, v in away_samples.items()},
                "form_last_5": {"sample_size": len(away_form_out)},
            },
            "h2h": {"sample_size": h2h_n},
            "sources": ["espn.summary", "espn.scoreboard", "espn.teams.schedule"],
        },
    }
    # Optional comparison against API-Football for audit only (no data copied into core payload)
    meta = payload.get("meta") or {}
    apif_key = meta.get("apif_key") or os.getenv("APIFOOTBALL_KEY")
    if meta.get("compare_api_football") and apif_key:
        try:
            last_n = 2
            try:
                last_n = int(meta.get("compare_last", 2))
            except Exception:
                last_n = 2
            # Resolve API-Football team ids by name
            apif_home_id = _apif_search_team_id(final_home_name, apif_key)
            apif_away_id = _apif_search_team_id(final_away_name, apif_key)
            comp = {}
            if apif_home_id:
                fids = _apif_last_fixtures(apif_home_id, apif_key, last=last_n)
                vals, smp = _apif_team_metrics_from_fixtures(final_home_name, apif_home_id, fids, apif_key)
                comp["home"] = {"metrics": vals, "samples": smp}
            if apif_away_id:
                fids = _apif_last_fixtures(apif_away_id, apif_key, last=last_n)
                vals, smp = _apif_team_metrics_from_fixtures(final_away_name, apif_away_id, fids, apif_key)
                comp["away"] = {"metrics": vals, "samples": smp}
            # Build diffs for comparable metrics
            def diff_pair(ours: dict, theirs: dict, key: str):
                o = ours.get(key)
                t = theirs.get(key)
                if o is None or t is None:
                    return None
                return round(o - t, 2)
            diffs = {}
            if "home" in comp:
                diffs["home"] = {k: diff_pair(home_stats_filled, comp["home"]["metrics"], k) for k in ["corners_avg", "offsides_avg", "cards_avg"] if comp["home"]["metrics"].get(k) is not None}
            if "away" in comp:
                diffs["away"] = {k: diff_pair(away_stats_filled, comp["away"]["metrics"], k) for k in ["corners_avg", "offsides_avg", "cards_avg"] if comp["away"]["metrics"].get(k) is not None}
            out["comparison"] = {"apifootball": comp, "diffs": diffs}
            out["veracity"]["sources"].append("apifootball.compare")
        except Exception:
            # Do not fail main response on comparison issues
            pass
    # Remove nulls and fields with explicit None to honor 'no nulls' and no invented values
    def _strip_nulls(obj):
        if isinstance(obj, dict):
            return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [ _strip_nulls(v) for v in obj ]
        return obj
    out = _strip_nulls(out)
    if cache_key:
        _CACHE[cache_key] = (time.time() + INSIGHTS_CACHE_TTL, out)
    strict = bool((payload.get("meta") or {}).get("strict"))
    if strict:
        missing = []
        for side_key, samples in [("home", home_samples), ("away", away_samples)]:
            for mk, meta in samples.items():
                if meta["n"] == 0 and mk in (home_stats if side_key == "home" else away_stats) and (home_stats if side_key == "home" else away_stats).get(mk) is None:
                    missing.append(f"{side_key}.{mk}")
        if (payload.get("meta") or {}).get("date") and not kickoff_val:
            missing.append("meta.commence_time")
        if missing:
            raise HTTPException(status_code=422, detail={"missing": missing})
    return out

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
INSIGHTS_HTTP_TIMEOUT = int(os.getenv("INSIGHTS_HTTP_TIMEOUT", "5"))
INSIGHTS_FETCH_TRIES = int(os.getenv("INSIGHTS_FETCH_TRIES", "2"))
INSIGHTS_MAX_WORKERS = int(os.getenv("INSIGHTS_MAX_WORKERS", "6"))
INSIGHTS_CACHE_TTL = int(os.getenv("INSIGHTS_CACHE_TTL", "60"))
INSIGHTS_BUDGET_SECS = float(os.getenv("INSIGHTS_BUDGET_SECS", "5"))
INSIGHTS_EV_LIMIT_DATE = int(os.getenv("INSIGHTS_EV_LIMIT_DATE", "2"))
INSIGHTS_EV_LIMIT_DEFAULT = int(os.getenv("INSIGHTS_EV_LIMIT_DEFAULT", "6"))
INSIGHTS_H2H_LIMIT = int(os.getenv("INSIGHTS_H2H_LIMIT", "3"))

from requests.adapters import HTTPAdapter
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=20))
_SESSION.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
def _espn_standings(league_code: str):
    # Best effort: cache for 10 minutes
    urls = [
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/standings",
        f"https://site.api.espn.com/apis/v2/sports/soccer/{league_code}/standings",
    ]
    data = None
    for u in urls:
        try:
            data = _fetch_json(u, ttl=600)
            break
        except Exception:
            data = None
    if not data:
        return {}
    # Build a map team_id -> {rank, points}
    table = {}
    # ESPN returns children for groups/divisions; iterate all items
    standings = data.get("children") or data.get("standings") or []
    stack = list(standings)
    while stack:
        node = stack.pop(0)
        # dive into nested
        for k in ("children", "standings", "entries"):
            if isinstance(node.get(k), list):
                # If entries present, parse entries here
                if k == "entries":
                    for ent in node.get(k):
                        team = (ent.get("team") or {})
                        tid = str(team.get("id")) if team.get("id") is not None else None
                        stats = ent.get("stats", [])
                        rank = None
                        pts = None
                        for st in stats:
                            abbr = st.get("abbreviation") or st.get("name")
                            if (abbr or "").lower() in {"pts", "points"}:
                                try:
                                    pts = int(float(st.get("value")))
                                except Exception:
                                    pts = None
                            if (abbr or "").lower() in {"rk", "rank"}:
                                try:
                                    rank = int(float(st.get("value")))
                                except Exception:
                                    rank = None
                        if tid and (rank is not None or pts is not None):
                            table[tid] = {"rank": rank, "points": pts}
                else:
                    stack.extend(node.get(k))
    return table

def _extract_team_logo_from_summary(summary: dict, team_id: str):
    try:
        comp = (summary.get("header", {}).get("competitions") or [{}])[0]
        comps = comp.get("competitors", [])
        for c in comps:
            tid = str((c.get("team") or {}).get("id"))
            if str(team_id) == tid:
                t = c.get("team") or {}
                logos = t.get("logos") or []
                for lg in logos:
                    href = lg.get("href")
                    if href:
                        return href
                if t.get("logo"):
                    return t.get("logo")
    except Exception:
        pass
    return None

def _league_logo(league_code: str, date_token: Optional[str] = None, kickoff_iso: Optional[str] = None):
    try:
        d = date_token
        if not d and kickoff_iso:
            d = _parse_date_str(kickoff_iso)
        if not d:
            d = datetime.datetime.utcnow().strftime('%Y%m%d')
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/scoreboard?dates={d}"
        data = _fetch_json(url, ttl=120)
        leagues = data.get("leagues", [])
        if leagues:
            logos = leagues[0].get("logos") or []
            for lg in logos:
                href = lg.get("href")
                if href:
                    return href
    except Exception:
        pass
    return None

def _ref_avgs_from_summaries(ref_name: str, summaries: dict):
    tot_cards = 0.0
    tot_reds = 0.0
    matches = 0
    if not ref_name:
        return None, None
    for s in summaries.values():
        gi = s.get("gameInfo") or {}
        offs = gi.get("officials") or []
        if not offs:
            continue
        name = (offs[0].get("displayName") or "").strip().lower()
        if name != ref_name.strip().lower():
            continue
        comps = (s.get("boxscore") or {}).get("teams", [])
        yc = 0.0
        rc = 0.0
        for tm in comps:
            statistics = tm.get("statistics", [])
            for st in statistics:
                nm = st.get("name")
                if nm == "yellowCards":
                    try:
                        yc += float(st.get("displayValue")) if st.get("displayValue") is not None else float(st.get("value"))
                    except Exception:
                        pass
                elif nm == "redCards":
                    try:
                        rc += float(st.get("displayValue")) if st.get("displayValue") is not None else float(st.get("value"))
                    except Exception:
                        pass
        tot_cards += yc + rc
        tot_reds += rc
        matches += 1
    if matches == 0:
        return None, None
    return round(tot_cards / matches, 2), round(tot_reds / matches, 2)
# temporary debug route to inspect ESPN teams availability
@app.get("/__debug/teams/{code}")
def _debug_teams(code: str):
    tm = _espn_teams(code)
    return {"count": len(tm), "sample": list(tm.keys())[:5]}
