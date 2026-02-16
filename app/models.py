from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import datetime

Base = declarative_base()

class Sport(Base):
    __tablename__ = 'sports'
    key = Column(String, primary_key=True)
    group = Column(String)
    title = Column(String)
    description = Column(String)
    active = Column(Boolean, default=True)
    has_outrights = Column(Boolean, default=False)

class Event(Base):
    __tablename__ = 'events'
    id = Column(String, primary_key=True)
    sport_key = Column(String, ForeignKey('sports.key'))
    sport_title = Column(String) # Novo campo para paridade
    commence_time = Column(DateTime)
    home_team = Column(String)
    away_team = Column(String)
    
    odds = relationship("Odd", back_populates="event")
    scores = relationship("Score", back_populates="event", uselist=False)

class Bookmaker(Base):
    __tablename__ = 'bookmakers'
    key = Column(String, primary_key=True)
    title = Column(String)

class Odd(Base):
    __tablename__ = 'odds'
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, ForeignKey('events.id'))
    bookmaker_key = Column(String, ForeignKey('bookmakers.key'))
    market_key = Column(String) # e.g., h2h, spreads, totals
    outcome_name = Column(String)
    price = Column(Float)
    point = Column(Float, nullable=True) # for spreads and totals
    last_update = Column(DateTime, default=datetime.datetime.utcnow)
    
    event = relationship("Event", back_populates="odds")

class Score(Base):
    __tablename__ = 'scores'
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, ForeignKey('events.id'))
    score_home = Column(Integer)
    score_away = Column(Integer)
    last_update = Column(DateTime, default=datetime.datetime.utcnow)
    
    event = relationship("Event", back_populates="scores")
