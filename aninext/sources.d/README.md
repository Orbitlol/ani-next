# Scraper rules

Drop `*.json` files in this folder (or in `~/.aninext/sources/`) and aninext
loads them as extra providers — no code required.

```bash
aninext sources list                 # what is loaded
aninext sources validate my.json     # check a rule file
aninext sources path                 # where your own rules live
```

`example.json` documents every field. A minimal rule looks like this:

```json
{
  "id": "mysite",
  "name": "My Site",
  "base": "https://mysite.example",
  "kinds": ["anime"],
  "search": {
    "url": "/search?q={query}&page={page}",
    "items": "div.card",
    "fields": {
      "title": "h3 a ::text",
      "url": "h3 a @href",
      "poster": "img @data-src | img @src"
    }
  },
  "details": {
    "fields": { "synopsis": "div.description ::text" },
    "episodes": {
      "items": "ul.episodes li",
      "fields": { "number": "span.num ::text", "url": "a @href" }
    }
  },
  "episode": {
    "servers": {
      "items": "div.server",
      "fields": { "label": "::text", "embed": "@data-embed" }
    }
  }
}
```

Rules you write are your responsibility: only scrape sites whose terms allow
it, and keep the built-in rate limit (`aninext config set rate_limit 1`) on
for sites that ask for it.
