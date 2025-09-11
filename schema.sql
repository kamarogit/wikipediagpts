PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=3000;


create table if not exists users(
id integer primary key autoincrement,
handle text unique not null,
created_at text default (datetime('now'))
);


create table if not exists articles(
id integer primary key autoincrement,
lang text not null, -- 'ja'
page_id integer not null, -- MediaWiki pageid
title text not null,
url text not null,
unique(lang, page_id)
);


create table if not exists user_articles(
user_id integer not null,
article_id integer not null,
shown_at text default (datetime('now')),
reacted integer default 0, -- 0/1
reaction text, -- 'like'|'skip'|'block'
primary key(user_id, article_id),
foreign key(user_id) references users(id),
foreign key(article_id) references articles(id)
);


create index if not exists idx_user_articles_user on user_articles(user_id);
create index if not exists idx_articles_lang_page on articles(lang, page_id);