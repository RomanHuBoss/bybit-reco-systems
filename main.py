from __future__ import annotations

# Root entrypoint for local run:
#   python main.py
# This delegates to the FastAPI app launcher in app/main.py

from app.main import main

if __name__ == "__main__":
    main()
