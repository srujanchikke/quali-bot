"""
db.py — Neo4j driver factory.

Single source of truth for opening an authenticated Neo4j connection.
All modules should use ``get_driver()`` instead of constructing a driver
themselves.

Usage::

    from hs_indexer.db import get_driver

    with get_driver() as driver:
        with driver.session() as session:
            ...
"""

from __future__ import annotations

from neo4j import GraphDatabase

from hs_indexer.config import cfg


def get_driver():
    """Return an authenticated Neo4j driver configured from ``config.toml`` / env."""
    return GraphDatabase.driver(cfg.neo4j.uri, auth=(cfg.neo4j.user, cfg.neo4j.password))
