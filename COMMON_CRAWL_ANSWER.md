# "Use Common Crawl so changing site structure stops breaking us"

**Short answer: it would not solve that problem, because it solves a different
one.** Worth being able to explain why, since the suggestion sounds right.

---

## What Common Crawl actually is

A non-profit that crawls the web every month or two and publishes the **raw
HTML** as a public archive — petabytes of WARC files on S3. It exists so that
researchers don't each have to crawl the web themselves.

The key word is **raw**. It gives you the same HTML your scraper would have
fetched. It does not give you structured records.

## Why it doesn't fix structural drift

Our pipeline has two halves:

```
FETCHING          ->   PARSING
get the HTML           work out which text is the title,
                       which is the deadline, which is a link
```

**Common Crawl replaces the fetching half.** Parsing is untouched — you still
receive HTML and still have to decide what a deadline looks like in it.

So when Clean Air Fund redesigns its funding page, the archived HTML changes in
exactly the same way the live HTML does, and the parser breaks in exactly the
same way. You've changed where the bytes come from, not what they look like.

## The blocking problem: it would be badly out of date

This is the part that decides it for a leads tool.

| | Live scraping | Common Crawl |
|---|---|---|
| Age of data | minutes | **weeks to months** |
| Crawl frequency | whenever we run it | roughly monthly |
| Coverage of niche funder sites | complete, we choose them | partial, we don't choose |
| Pages behind a login | with our session | **never included** |

A funding call with a 30-day window can open and close entirely between two
Common Crawl snapshots. For a tool whose whole job is "tell me about this
before the deadline", stale data is not a smaller version of the benefit — it is
the failure mode. **Yesterday's grant list is not a lead list.**

## The coverage problem

Common Crawl samples the web broadly, weighted toward popular pages. It has no
obligation to include:

- `bond.org.uk/funding-opportunities` on any given month
- every page of a 32-page paginated result set
- anything behind DevelopmentAid's paywall (4 of our sources need a login)

We currently choose 85 sources deliberately and walk them completely. Switching
to Common Crawl means accepting whatever it happened to capture.

## What WOULD reduce structural fragility

Your senior's underlying concern is right — parser drift is a real cost. These
actually address it:

**1. We already avoid per-site selectors.** 73 of 85 sources use heuristics
("does this look like an opportunity?"), not CSS selectors. A redesign that
moves a `<div>` doesn't break them, because they were never anchored to that
`<div>`. This is the single biggest reason the codebase isn't in constant
maintenance.

**2. Detect drift instead of waiting to hear about it.** `scripts/audit_sources.py`
flags any source where more than 60% of stored rows look like junk — which is
what a drifted parser produces. That turns "the team noticed Clean Air Fund is
wrong" into something you see first.

**3. Prefer a site's own API where one exists.** An API is a contract; HTML is
not. This is the real answer for ADB and DevelopmentAid, and it's a procurement
conversation rather than an engineering one.

**4. LLM extraction as a fallback, not a default.** Send only the pages that
parse to nothing. Costs money per page and isn't deterministic, so it's a
backstop for the tail, not a replacement for the 73 that work.

## What Common Crawl is genuinely good for

Not nothing — just not this:

- **Discovery**: mining the archive to *find* funding sites we don't yet track,
  by searching for pages containing "call for proposals" and a deadline. That is
  a real use, and a nice one-off exercise.
- **Backfill**: reconstructing history for a site we started tracking recently.
- **Research**: measuring how funding language has changed over years.

If the goal is "find more sources", Common Crawl is a reasonable tool. If the
goal is "stop parsers breaking", it doesn't touch the problem.

## How to put it to your senior

> "Common Crawl replaces the fetching, not the parsing — and parsing is the part
> that breaks when a site is redesigned, because the archived HTML changes the
> same way the live HTML does. It's also weeks to months stale and doesn't cover
> pages behind a login, which rules it out for deadline-driven leads. What does
> reduce drift is what we already do: heuristics rather than per-site selectors,
> for 73 of 85 sources. I've added a script that detects a drifted parser
> automatically instead of waiting for someone to spot it. Where I'd genuinely
> use Common Crawl is discovering *new* funder sites we don't track yet."

That agrees with the concern, shows you evaluated the suggestion properly, and
offers where it does fit.
