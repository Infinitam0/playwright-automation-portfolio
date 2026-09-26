"""Scraper atoms. One file per source. Add a new scraper by dropping a module here
and decorating its class with `@register_scraper("name")` from scanner.registry.
The convention loader in scanner.registry picks it up automatically — no edits
required elsewhere."""
