# Parser fixtures

Sanitized fragments of what each priority source actually returns, so a parser
has a contract that fails when the source changes shape.

**What may live here:** the smallest fragment that exercises the parser — a
listing row, an API record, the "no results" container. Values are replaced
with plausible substitutes.

**What must never live here**, per the brief: credentials, cookies, session
tokens, full private pages, or anything a logged-in account can see that an
anonymous visitor cannot. Every file here is safe to publish, and this
repository is public.

Each fixture is paired with a test asserting the parser extracts the fields the
manifest says the source yields. When a source's markup drifts, the fixture
test is what turns "PARSE_ZERO, cause unknown" into a named, reproducible
failure with a diff.
