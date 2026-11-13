# Sample app (bubble-fix checks)

`app.py`: a page and `/company/<id>`. Tests: `uv run --with pytest pytest -q`. Keep changes small.
An unknown company should show a plain "No such company" page with status 404, not crash.
