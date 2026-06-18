# Intentionally empty: importing this package must NOT pull in the scrapers'
# heavy runtime deps (requests, feedparser, beautifulsoup4, ...). The prediction
# microservice imports `src.scrapers.config` and `src.scrapers.db`; eagerly doing
# `from .main import main` here made those imports fail on the lean service image
# (and locally). Run scrapers as modules instead: `python -m src.scrapers.<name>`.