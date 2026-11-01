---
name: arxiv
description: Use when finding or reading arXiv research papers.
version: 0.1.0
author: Chelus
---

# arXiv Papers

Search arXiv's free API and read papers with `web_fetch`. No key and no shell needed. The
API answers in Atom XML, which `web_fetch` returns as text; pick out the fields by reading.

## When to Use

- "Find papers on", "what's new in", "who wrote", "summarize this paper".
- An arXiv ID (`2201.11903`) or an `arxiv.org/abs/...` link in the conversation.

## Procedure

1. **Search.** `web_fetch`
   `https://export.arxiv.org/api/query?search_query=<query>&max_results=5`. Join words with
   `+`. Prefixes: `ti:` title, `au:` author, `abs:` abstract, `cat:` category, `all:` all.
   Combine with `+AND+`, `+OR+`, `+ANDNOT+`; quote a phrase as `%22chain+of+thought%22`.
   For newest first, add `&sortBy=submittedDate&sortOrder=descending`.
2. **Read the results.** Each `<entry>` has `<id>` (the versioned abs URL), `<title>`,
   `<published>`, `<author><name>`, `<summary>` (the abstract) and `<category term=...>`.
   List them as: ID, title, first author et al., date, one line from the abstract.
3. **One paper by ID.** `web_fetch`
   `https://export.arxiv.org/api/query?id_list=2201.11903` (comma-separate several).
4. **Read the paper.** `web_fetch https://arxiv.org/html/<id>` for the full text as HTML;
   it is long, so the tool saves it and you page it with `read_file`. Read the abstract,
   introduction and conclusion first. If there's no HTML version, the abs page
   `https://arxiv.org/abs/<id>` gives the abstract; the PDF is binary and `web_fetch`
   can't read it.
5. Cite papers as `Author et al., Title, arXiv:<id>vN (year)`, keeping the version you
   actually read. Use the `grounded-citations` skill for multi-source answers.

## Common categories

`cs.AI` AI, `cs.CL` language, `cs.LG` machine learning, `cs.CV` vision, `cs.CR` security,
`stat.ML` statistical ML, `q-bio` biology, `astro-ph` astrophysics. Full list:
`https://arxiv.org/category_taxonomy`.

## Pitfalls

- Space requests about 3 seconds apart; arXiv throttles fast callers.
- `arxiv.org/abs/<id>` always shows the latest version; quote the `vN` you read, because a
  later version can change the content.
- A summary that says "withdrawn" or "retracted" means don't present it as a valid result.
- Semantic Scholar (citation counts) refuses anonymous calls from this phone's address
  with 429; don't promise citation counts without a key.
- arXiv preprints aren't peer reviewed; say so when it matters.

## Verification

Each paper you mention has an ID that came from an API or page result in this
conversation, and its title matches that result.
