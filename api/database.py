"""
database.py — asyncpg pool management with JSON codecs.
"""

import json
import logging

import asyncpg

from config import get_settings

logger = logging.getLogger(__name__)

db_pool: asyncpg.Pool = None


async def init_pool() -> asyncpg.Pool:
    global db_pool
    settings = get_settings()

    async def init_conn(conn):
        await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')
        await conn.set_type_codec('json', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')

    db_pool = await asyncpg.create_pool(
        settings.database_url, min_size=2, max_size=10, init=init_conn,
    )
    logger.info("Database pool created")
    return db_pool


async def close_pool():
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed")


def get_pool() -> asyncpg.Pool:
    return db_pool
