"""允许 `python -m service_pilot`。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
