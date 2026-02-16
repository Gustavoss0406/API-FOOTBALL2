import os
import logging
from fastapi import FastAPI, Depends, HTTPException, Query
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
    "CL": "soccer_uefa_champs_league",      # UEFA Champions League
    "EC": "soccer_uefa_europa_league",      # European Championship / Europa League mapping
    "WC": "soccer_fifa_world_cup",          # FIFA World Cup (if present)
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
