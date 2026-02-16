import os
import logging
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import datetime
from . import models
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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
    events = db.query(models.Event).filter(models.Event.sport_key == sport).all()
    
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
    events = db.query(models.Event).filter(models.Event.sport_key == sport).all()
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
def get_event_odds(sport: str, eventId: str, regions: str = "eu", markets: str = "h2h", db: Session = Depends(get_db)):
    event = db.query(models.Event).filter(models.Event.id == eventId, models.Event.sport_key == sport).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    
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

@app.get("/v4/historical/sports/{sport}/odds")
def get_historical_odds(sport: str, date: str, db: Session = Depends(get_db)):
    # Simulação de histórico retornando o estado atual para a data solicitada
    data = get_odds(sport=sport, db=db)
    return {
        "timestamp": date, 
        "previous_timestamp": None, 
        "next_timestamp": None, 
        "data": data
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
