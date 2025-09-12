from typing import Literal

import os

import aiosqlite
import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel


LANG = "ja"
DB_PATH = os.environ.get("DB_PATH", "wikipediagpts.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


class ReactIn(BaseModel):
    article_id: int
    reaction: Literal["like", "skip", "block"]


app = FastAPI()


async def get_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    # スキーマ適用（存在しない場合のみ作成）
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            await conn.executescript(f.read())
    except FileNotFoundError:
        # スキーマファイルが無い場合でも動作を継続
        pass
    return conn


async def get_or_create_user(conn: aiosqlite.Connection, handle: str) -> int:
    await conn.execute(
        "insert or ignore into users(handle) values(?)",
        (handle,),
    )
    await conn.commit()
    cur = await conn.execute(
        "select id from users where handle=?",
        (handle,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=500, detail="failed to get user id")
    return int(row["id"])  # type: ignore[index]


async def fetch_random_summary() -> dict:
    url = "https://ja.wikipedia.org/api/rest_v1/page/random/summary"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/next_article")
async def next_article(user: str = Query(alias="user")):
    conn = await get_db()
    try:
        uid = await get_or_create_user(conn, user)
        for _ in range(12):
            js = await fetch_random_summary()

            # 除外: あいまいさ回避・標準以外
            if js.get("type") not in (None, "standard"):
                continue

            pageid = js.get("pageid")
            title = js.get("title")
            url = (js.get("content_urls") or {}).get("desktop", {}).get("page")
            if not (pageid and title and url):
                continue

            # articles upsert
            await conn.execute(
                "insert or ignore into articles(lang, page_id, title, url) values(?,?,?,?)",
                (LANG, pageid, title, url),
            )
            await conn.commit()

            cur = await conn.execute(
                "select id from articles where lang=? and page_id=?",
                (LANG, pageid),
            )
            ar = await cur.fetchone()
            if not ar:
                continue
            article_id = ar["id"]

            # 既紹介？
            cur = await conn.execute(
                "select 1 from user_articles where user_id=? and article_id=?",
                (uid, article_id),
            )
            if await cur.fetchone():
                continue

            # 未紹介なら即時記録
            await conn.execute(
                "insert into user_articles(user_id, article_id) values(?,?)",
                (uid, article_id),
            )
            await conn.commit()

            return {
                "article_id": article_id,
                "title": title,
                "url": url,
                "summary": {
                    "extract": js.get("extract"),
                    "thumbnail": (js.get("thumbnail") or {}).get("source"),
                },
            }

        raise HTTPException(status_code=404, detail="No unseen article found (try again)")
    finally:
        await conn.close()


@app.post("/react")
async def react(inp: ReactIn, user: str = Query(alias="user")):
    conn = await get_db()
    try:
        uid = await get_or_create_user(conn, user)
        await conn.execute(
            """
update user_articles
set reacted=1, reaction=?
where user_id=? and article_id=?
""",
            (inp.reaction, uid, inp.article_id),
        )
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()