# Ending statistics

A Cloudflare Worker that counts which endings readers reached in branching
fiction. Readers send their ending only if they choose to, and anyone can read
the totals. It stores the work, the ending, the chapters passed if the reader
sent them, and the day. It stores no IP address and sets no cookie.

The site's `/fiction/index.json` lists each work's chapters and endings, and
the Worker accepts only those. The repository is public, so the Worker holds no
secrets. Other sites' pages are kept out by the allowed origins, and scripts by
a limit of 5 uploads per IP address per minute.

## Deploy

Run these in this folder. The first command opens a browser to sign in to your
Cloudflare account.

```sh
npx wrangler@4 login
npx wrangler@4 d1 create ending-stats
```

Put the `database_id` it prints into `wrangler.jsonc`, then:

```sh
npx wrangler@4 d1 execute ending-stats --remote --file schema.sql
npx wrangler@4 deploy
```

`deploy` prints the address, such as `https://ending-stats.<you>.workers.dev`.
Put it in `endingsApi` in `config/_default/params.toml`. Ending chapters show the statistics from the next site build.

## Read the data

```sh
npx wrangler@4 d1 execute ending-stats --remote --command \
  "SELECT work, ending, COUNT(*) FROM readings GROUP BY work, ending"
npx wrangler@4 d1 execute ending-stats --remote --command \
  "SELECT path, COUNT(*) FROM readings WHERE path IS NOT NULL GROUP BY path"
```

A path is the chapters the reader opened in that read-through, so a reader who
jumps from the contents to a later chapter leaves a gap in it. The Worker checks
that each chapter exists, but not that one leads to the next. A new ending is
refused for up to 5 minutes after it's published, while the Worker still has
the older chapter list.

## Test

```sh
node --test test/
```

To try it with the site, run the Worker and Hugo side by side. The Worker uses a
local database, so nothing reaches Cloudflare. A branching work as a draft gives
it endings to count.

```sh
npx wrangler@4 d1 execute ending-stats --local --file schema.sql
npx wrangler@4 dev --port 8788 --var ALLOWED_ORIGINS:http://localhost:1313 \
  --var MANIFEST_URL:http://localhost:1313/fiction/index.json
HUGO_PARAMS_ENDINGSAPI=http://localhost:8788 hugo server -D
```
