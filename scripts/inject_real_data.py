import json
import datetime
import uuid
import sys
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Adicionar o diretório pai ao path para importar modelos
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import models

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/odds.db")
engine = create_engine(DATABASE_URL)
models.Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(bind=engine)

def inject_data():
    with open('/home/ubuntu/real_odds_data.json', 'r') as f:
        data = json.load(f)
    
    db = SessionLocal()
    
    # Limpar dados antigos
    db.query(models.Odd).delete()
    db.query(models.Event).delete()
    db.query(models.Sport).delete()
    db.query(models.Bookmaker).delete()
    db.commit()

    # Adicionar Bookmaker padrão
    bm = models.Bookmaker(key="bet365", title="Bet365")
    db.add(bm)
    
    for item in data:
        sport_key = item['sport_key']
        sport_title = sport_key.replace("soccer_", "").replace("_", " ").title()
        
        sport = db.query(models.Sport).filter(models.Sport.key == sport_key).first()
        if not sport:
            sport = models.Sport(
                key=sport_key, 
                group="Soccer", 
                title=sport_title,
                description=f"Football matches in {sport_key}"
            )
            db.add(sport)
            db.commit()

        event_id = str(uuid.uuid4()).replace("-", "")
        event = models.Event(
            id=event_id,
            sport_key=sport_key,
            sport_title=sport_title, # Adicionado para paridade
            commence_time=datetime.datetime.fromisoformat(item['commence_time'].replace('Z', '')),
            home_team=item['home_team'],
            away_team=item['away_team']
        )
        db.add(event)
        
        # Adicionar Odds H2H
        outcomes = [item['home_team'], "Draw", item['away_team']]
        prices = item['odds']['h2h']
        for i, price in enumerate(prices):
            odd = models.Odd(
                event_id=event_id,
                bookmaker_key="bet365",
                market_key="h2h",
                outcome_name=outcomes[i],
                price=price
            )
            db.add(odd)
    
    db.commit()
    print(f"Sucesso: {len(data)} jogos reais injetados com sport_title.")
    db.close()

if __name__ == "__main__":
    inject_data()
