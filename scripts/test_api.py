import requests
import json

BASE_URL = "http://localhost:8000"

def test_endpoint(name, url):
    print(f"Testando {name}: {url}")
    try:
        response = requests.get(url)
        if response.status_code == 200:
            print(f"  [OK] Status 200")
            data = response.json()
            print(f"  [INFO] Retornou {len(data)} itens")
            return data
        else:
            print(f"  [ERRO] Status {response.status_code}")
            return None
    except Exception as e:
        print(f"  [EXCEÇÃO] {e}")
        return None

def run_all_tests():
    print("=== INICIANDO BATERIA DE TESTES DA API ===\n")
    
    # 1. Testar /v4/sports
    sports = test_endpoint("Listar Esportes", f"{BASE_URL}/v4/sports")
    
    if sports:
        for sport in sports:
            sport_key = sport['key']
            print(f"\n--- Testando dados para {sport_key} ---")
            
            # 2. Testar /v4/sports/{sport}/odds
            odds = test_endpoint(f"Odds para {sport_key}", f"{BASE_URL}/v4/sports/{sport_key}/odds")
            
            if odds and len(odds) > 0:
                event_id = odds[0]['id']
                
                # 3. Testar /v4/sports/{sport}/events/{eventId}/odds
                test_endpoint(f"Odds do Evento {event_id}", f"{BASE_URL}/v4/sports/{sport_key}/events/{event_id}/odds")
                
                # 4. Testar /v4/sports/{sport}/scores
                test_endpoint(f"Scores para {sport_key}", f"{BASE_URL}/v4/sports/{sport_key}/scores")
    
    # 5. Testar Historical
    test_endpoint("Odds Históricas", f"{BASE_URL}/v4/historical/sports/soccer_laliga/odds?date=2026-02-15T10:00:00Z")

    print("\n=== BATERIA DE TESTES CONCLUÍDA ===")

if __name__ == "__main__":
    run_all_tests()
