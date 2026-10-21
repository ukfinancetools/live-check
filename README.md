# UK Finance Tools live check

An automated check of [ukfinancetools.co.uk](https://www.ukfinancetools.co.uk). It runs on GitHub Actions and
commits each result to this repository, so the history of outcomes is public.

## What it checks

| Check | Schedule | What it finds |
|---|---|---|
| Indexing | hourly | a page that should appear in search results but is served with a noindex tag or header, a robots.txt block, a canonical that points elsewhere, a redirect or an error; a page missing from the sitemap; a noindex page listed in the sitemap |
| Sitemap | hourly | a sitemap URL that does not answer 200 |
| Calculators | hourly | a calculator that gives no result, or does not show the expected figure for one fixed input |
| Figures | daily | a rate or threshold in the calculators' code that differs from the site's published table of tax rates and thresholds (`/data/tax-params.json`), or a page that does not show the table's figure where it states that value |

Each calculator's expected figure comes from a source outside the site, named in `calculators.json`: an HMRC
worked example, a calculation by hand from the published GOV.UK rates, or an independent model. It is never
taken from the site's own code, data or pages.

## Results

- `results/latest.json`: the last hourly result, with every finding.
- `results/history.jsonl`: one line per hourly run.
- `results/values-latest.json`: the last daily figures check.
- `results/planted-tests.jsonl`: one line per monthly planted test. A known fault is put on the staging copy of the site, never the live site, and the check must report it and nothing else.

A failed run means the check found a problem (exit code 1) or could not run (exit code 2). A run that could
not run never counts as a pass.

## Where the files come from

The site's own repository is private. `indexing_outcome.py` and `live_fetch.py` are exact copies of the
modules the site's deploy checks use. `expected-urls.json` and `stated-values.json` are generated from the
site's source. `sync-manifest.json` records the sha256 of each copied file. Changes are made in the site
repository and copied here, never edited here.

## Run it yourself

```
python3 live_check.py --skip-calculators    # indexing and sitemap checks (Python 3 only)
python3 live_check.py                       # all hourly checks (also needs Node, puppeteer-core and Chrome)
python3 compare_values.py                   # the daily figures check
```
