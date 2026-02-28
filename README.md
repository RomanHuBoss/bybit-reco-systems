# Bybit Recommender System (Local MVP)

See `SPEC.md` for full specification.

## Run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

UI:
- http://127.0.0.1:8000/

Swagger:
- http://127.0.0.1:8000/docs


UI: status filters (recommended/blocked/no_trade/suppressed), buttons: Риски, Журнал.
