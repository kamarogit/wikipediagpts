from typing import Literal, Optional

import os

import aiosqlite
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md
import re
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel


LANG = "ja"
DB_PATH = os.environ.get("DB_PATH", "wikipediagpts.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
USER_AGENT = os.environ.get(
    "WIKI_USER_AGENT",
    "wikipediagpts/1.0 (+https://github.com/; contact: unknown)",
)


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


def _extract_wikipedia_main_html(html: str) -> tuple[Optional[str], str]:
    """
    Wikipediaの記事HTMLから本体部分のHTMLを抽出する。
    見出しタイトルと本文HTML（必要要素のみ）を返却。
    """
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#firstHeading") or soup.title
    title = title_el.get_text(strip=True) if title_el else None

    content = soup.select_one("div#mw-content-text")
    if not content:
        # 取得できない場合は全体HTMLを返す（フォールバック）
        return title, html

    # Wikipedia特有のノイズを削除
    selectors_to_remove = [
        "table.infobox",
        "table.vertical-navbox",
        "table.navbox",
        "div.hatnote",
        "div.reflist",
        "ol.references",
        "div.sidebar",
        "div.toc",
        "span.mw-editsection",
        "div.mw-kartographer-container",
        "figure[role='navigation']",
        "script",
        "style",
        "link",
        "noscript",
    ]
    for selector in selectors_to_remove:
        for el in content.select(selector):
            el.decompose()

    for el in content.select("sup.reference"):
        el.decompose()

    # 一部の空要素や冗長な改行を減らすためにクリーンアップ
    # ここではHTML文字列に戻すだけに留める
    return title, str(content)


def _html_to_markdown(html: str) -> str:
    markdown = md(html, heading_style="ATX")
    # 連続改行の正規化
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 行末の余計な空白を削除
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


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


@app.get("/article_content")
async def article_content(
    url: str = Query(alias="url"),
    format: Literal["markdown", "text"] = Query("markdown", alias="format"),
):
    """指定URL（Wikipedia想定）をサーバ側で取得し、本文をMarkdown/テキストに正規化して返す。"""
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"failed to fetch url: {e}")

    title, main_html = _extract_wikipedia_main_html(r.text)
    if format == "markdown":
        content = _html_to_markdown(main_html)
    else:
        content = _html_to_text(main_html)

    return {
        "title": title,
        "url": str(r.url),
        "format": format,
        "content": content,
    }


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