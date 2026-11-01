---
name: blocked-page-recovery
description: Use when web_fetch hits a 403, 429, bot wall or paywall.
version: 0.1.0
author: Chelus
---

# Blocked-Page Recovery

When a page won't come through `web_fetch`, try a stored copy before giving up, and never
loop on the same URL. Work down the ladder, cheapest first. This finds copies; it does not
get past logins, and a copy is not the live page.

## When to Use

- `web_fetch` returns Status 403 or 429, or a "Just a moment", "Attention Required" or
  "Access denied" page.
- The text is a paywall teaser, a cookie wall or a few lines where an article should be.
- The page came back with no readable text (it needs JavaScript) and the owner needs the body.

## Procedure

1. **Wayback Machine.** `web_fetch` with
   `https://archive.org/wayback/available?url=<the URL>`. The JSON gives
   `archived_snapshots.closest.url` and a `timestamp` (YYYYMMDDhhmmss). `web_fetch` that
   snapshot URL. No `closest` means it was never archived; move on.
2. **Older or deleted pages.** `web_fetch`
   `https://web.archive.org/cdx/search/cdx?url=<the URL>&output=json&limit=10` lists
   snapshots with timestamps; build `https://web.archive.org/web/<timestamp>/<the URL>`.
   If CDX answers 503, go back to step 1 instead of retrying.
3. **archive.today** (best for paywalled news). `web_fetch`
   `https://archive.ph/newest/<the URL>`. On 429, try `archive.md`, then `archive.li`,
   once each, then stop.
4. **Same site, other door.** After two blocked tries on one site, stop fighting its HTML:
   look for an RSS feed (`/feed`, `/rss`), `/sitemap.xml`, a `.json` or `/api/` version of
   the page, or an `/html/` or text edition (arXiv has `arxiv.org/html/<id>`).
5. **Search for another source.** `web_search` the headline in quotes; the same story or
   press release often sits on an unblocked site.
6. **Ask.** If nothing works, tell the owner what you tried and ask them to paste the text.

## Pitfalls

- Validate the body, not the status: a 200 can still be a rate-limit page, a redirect stub
  or a challenge page. Check that the text holds the words you expect from the title.
- Cite a snapshot with its date ("as archived on 2026-08-06"), never as the live page,
  because it may be stale. For prices, availability or breaking news, a snapshot is
  context, not an answer; say how old it is.
- Google's cache is gone and AMP caches mostly return a redirect back to the blocked page;
  don't use either.
- `r.jina.ai` and similar readers now need an API key and return 403 without one.
- Don't use public web proxies: they see everything and their copies can't be trusted.
- Archives are public sites; never put a token, session link or private URL into an
  archive lookup, since the lookup itself can make the archive save the page.

## Verification

The recovered text contains the article's own title or opening words, and the reply says
which route it came from and, for an archive, the snapshot date.
