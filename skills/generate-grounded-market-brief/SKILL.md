---
name: generate-grounded-market-brief
description: Assemble a citation-grounded `market_brief_draft.v1` for `finalize_market_brief` from facts already obtained through the agent facade.
type: skill
---

# Generate a Grounded Market Brief

After gathering facts and resolving their citations, your FINAL action MUST be exactly one `finalize_market_brief` call with the complete `market_brief_draft.v1`. Without that call no brief is delivered and the turn has failed.

Use `finalize_market_brief` only after gathering the facts and resolving the citations that support them. A `complete`, `partial`, or `unavailable` brief is a successful delivery; a partial brief with explicit limitations is preferable to refusing to finalize. Submit a bounded `market_brief_draft.v1` with this shape:

```text
title
status: complete | partial | unavailable
facts[]: maximum 12
  claim_id
  kind: numeric | qualitative
  confidence: high | medium | low
  text: qualitative only
  supporting_citation_refs[]: qualitative only
  numeric_citation_ref: numeric only
inferences[]: maximum 8
  claim_id
  text
  confidence: high | medium | low
  supporting_fact_ids[]
  caveat
limitations[]
```

## Facts and citations

For a numeric fact, submit only `numeric_citation_ref`. Never submit a value, unit, definition, date, source metadata, or numeric display text with it. Don't write numbers in the title, qualitative fact text, inference text, caveat, or limitation text. The host hydrates numeric content from the cited observation.

For a qualitative fact, submit its text and only the resolved `supporting_citation_refs` that support it. Don't invent citation refs. A fact without a resolved citation cannot become a numeric fact.

Use unique claim IDs. Keep numeric and qualitative fields separate. The draft must stay within the fact limit.

## Inferences and confidence

Every inference must reference existing fact IDs in `supporting_fact_ids`. Every inference must include a caveat. Keep inference count within its limit.

There are three independent confidence fields:

- Datasource confidence comes from citation metadata.
- Fact confidence describes the fact in this brief.
- Inference confidence describes the inference in this brief.

Don't derive one confidence from another or merge them.

## Coverage and dates

`published_at` may be null. Surface a publication-date warning when it is null, and never invent a date.

When coverage is insufficient, set status to `partial` or `unavailable` and name the limitation. Don't fill unavailable coverage with model prose that implies a number. Use only facts supported by resolved citations.

For a capability reported as blocked or unavailable, submit `status: unavailable` with `facts: []` and `inferences: []`. Put the coverage reason only in `limitations`; do not create a qualitative coverage fact without a resolved citation.
