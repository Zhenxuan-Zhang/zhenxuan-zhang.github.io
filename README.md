# zhenxuan-zhang.github.io

Personal academic homepage. One HTML file, no build step, no framework.
Content loads from flat JSON files in `data/`, and the publication list is
rebuilt weekly by a GitHub Action.

```
index.html                        the whole page: markup, CSS, JS
data/publications.json            THE paper list. Written by the Action, do not hand-edit
data/publications.manual.json     your edits: under-review papers, badges, thumbnails
data/scholar.json                 citations, h-index, i10-index, citations per year
data/news.json                    the News section
scripts/update_publications.py    Google Scholar + OpenAlex + Crossref fetcher
scripts/fetch_thumbnails.py       pulls a figure from open-access PDFs
scripts/requirements.txt          three dependencies
.github/workflows/update-publications.yml   runs it every Monday
img_journal/                      paper thumbnails, referenced from the data files
logo/                             portrait and institution marks
```

## How the automatic update works

Every Monday at 06:00 UTC the Action runs `scripts/update_publications.py`,
which does three things and then commits the result:

1. Reads your Google Scholar profile once, for the paper list and the
   per-paper citation counts, plus h-index and i10-index.
2. Looks up each paper on OpenAlex, falling back to Crossref, for the author
   list, journal, volume and pages, and DOI.
3. Applies `data/publications.manual.json` on top. Anything you set there wins.

You can also press **Actions → Update publications → Run workflow** at any
time, or run it locally:

```bash
pip install -r scripts/requirements.txt
python3 scripts/update_publications.py            # full run
python3 scripts/update_publications.py --dry-run  # print, write nothing
python3 scripts/update_publications.py --no-scholar
```

### Google Scholar is the fragile part

Scholar has no public API and blocks automated traffic. The `scholarly`
maintainers run their own CI behind a paid proxy for this reason. Expect the
Scholar step to fail some weeks with a CAPTCHA.

When it fails, the script logs the failure, keeps every paper already in
`data/publications.json`, and leaves `data/scholar.json` untouched, so the page
keeps showing the last good numbers rather than going blank. OpenAlex and
Crossref are stable and need no key, so the metadata half keeps working either
way. Nothing is ever written unless at least one paper survives the merge.

If the Scholar step fails for weeks on end, two options: add a paid proxy
(`ScraperAPI`, supported by `scholarly` directly), or set the repository
variable `OPENALEX_AUTHOR_ID` to your OpenAlex author ID and let OpenAlex
discover papers on its own. Citation counts will then come from OpenAlex, which
are lower than Scholar's because it counts a narrower corpus.

## Editing content

**A paper that is under review or in press.** Add it to the `entries` array in
`data/publications.manual.json`. These are written to the page verbatim while
no index knows about them.

Once the paper is accepted and turns up on Scholar or OpenAlex with a real
venue, the indexed record takes over and the manual entry is retired, so the
card stops saying "Under review" on its own. The job prints the retired titles
in its log; delete them from `entries` when you next edit the file. A hit on
arXiv alone does not count as acceptance, since a paper can be a preprint and
under review at the same time. Add `"sticky": true` to an entry to keep it
verbatim regardless.

Acceptance often lands weeks before Scholar indexes it. To flip a paper
immediately, delete it from `entries` and add an `overrides` block with the
real venue and badge.

**A badge, a thumbnail, an author string, a corrected venue.** Add it to the
`overrides` object in the same file, keyed by the paper title in lower case
with all punctuation removed. For example:

```json
"gema score granular explainable multi agent scoring framework for radiology report evaluation": {
  "badges": [{ "text": "CCF-A", "kind": "rank" }],
  "thumb": "img_journal/gema-score.png",
  "highlight": true
}
```

Badge kinds are `top`, `rank`, `oral`, `review`, `plain`. `highlight: true` puts
the paper in the Selected tab. `status` is `published`, `review` or `preprint`.
In `authors`, wrap your own name in `**` to bold it, and write a literal
asterisk as `\*` for co-first author marks.

**A wrong or duplicated paper.** Add its normalised title to the `exclude`
array. Scholar profiles collect stray records and this is how you drop them.

**News, About, Research, Teaching, Awards, Education.** News lives in
`data/news.json`. The rest is written directly in `index.html`, each string as
a pair of `data-en` and `data-zh` attributes on the same element. Add both or
the language switch will show a blank.

## Thumbnails

`scripts/fetch_thumbnails.py` fills gaps in the thumbnail column. It runs after
the data rebuild, and only for papers with no image. Your hand-picked images in
`img_journal/` are never overwritten.

For each paper without a thumbnail it looks for an open-access PDF, in this
order: an arXiv ID read from the paper's url, an `oa_pdf` link recorded from
OpenAlex, then Unpaywall by DOI. It opens the PDF, takes the largest raster
figure from the first three pages, and if the figures are vector art instead,
renders page one cropped to the drawn region. The result is cropped square to
512 px and written to `img_journal/<id>.png`.

```bash
python3 scripts/fetch_thumbnails.py --dry-run          # show what it would fetch
python3 scripts/fetch_thumbnails.py --only hipath      # one paper
python3 scripts/fetch_thumbnails.py --only hipath --force
```

What it cannot do: IEEE and Elsevier PDFs are behind a paywall, so a journal
paper with no preprint produces nothing and the card keeps its generated
k-space pattern. Of your current list that affects the TMI, TIP, MedIA and
Frontiers entries, all of which already have images you chose yourself.

An automatically extracted figure is a first page teaser, picked by size rather
than by meaning. It is a reasonable placeholder for a new preprint, not a
replacement for choosing the right panel. When one looks wrong, drop a better
file into `img_journal/` and point at it from the `overrides` block.

## Repository settings to confirm

- **Settings → Actions → General → Workflow permissions** must be
  **Read and write**. Without it the job runs, finds new data, then fails to
  push, and the failure is easy to miss.
- **Settings → Pages** should show the site as live from `main`, folder `/ (root)`.
- Optional repository variables under **Settings → Secrets and variables →
  Actions → Variables**: `GOOGLE_SCHOLAR_ID` (defaults to the ID already in the
  script), `OPENALEX_MAILTO` (an address puts you in OpenAlex's faster pool),
  `OPENALEX_AUTHOR_ID`.

## Traps worth knowing

- **`.github/` vanishes on browser upload.** GitHub's drag-and-drop picker skips
  dot-prefixed names and macOS Finder hides them (Command Shift period to
  reveal). Use `git push`, or **Add file → Create new file** and type the full
  path, which creates the folders as you go.
- **Opening `index.html` by double-clicking works, but shows stale content.**
  Browsers block file reads from `file://`, so the JSON files never load and the
  page falls back to a snapshot built into `index.html`. It says so under the
  publication list. For a true local preview run `python3 -m http.server` in the
  folder.
- **That built-in snapshot is a last resort, not a second copy to maintain.**
  Leave it alone. It only appears when every data file is unreachable.
- **All paths are relative**, so the page works as a user site at
  `<owner>.github.io` or a project site at `<owner>.github.io/<repo>/` with no
  configuration.
- Missing thumbnails and a missing portrait fall back to a generated k-space
  pattern seeded by the paper title, so the page never shows a broken image.

## Not yet verified

The updater was written without network access to `scholar.google.com`,
`api.openalex.org`, `api.crossref.org`, `api.unpaywall.org` or `arxiv.org`. The
merge logic is tested against constructed responses, including the cases where
Scholar is blocked and where both metadata sources are down, and the figure
extraction is tested on synthetic PDFs covering raster figures, vector figures
of several shapes, and a page with no figure at all. What has never been tried
is the response parsing against the live APIs and against a real paper PDF. Run
both scripts once from the Actions tab and read the log before trusting them.
