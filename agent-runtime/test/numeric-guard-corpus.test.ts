import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { NumericGuard } from "../src/finalizer.ts";

type CorpusCase = { readonly category: string; readonly text: string };
type Corpus = { readonly reject: readonly CorpusCase[]; readonly accept: readonly CorpusCase[] };

const corpus: Corpus = JSON.parse(
  readFileSync(new URL("./fixtures/numeric-guard/corpus.json", import.meta.url), "utf8"),
);

test("NumericGuard rejects every adversarial corpus entry", () => {
  // Given: the fixed adversarial corpus
  // When/Then: each forbidden form is checked
  for (const entry of corpus.reject) {
    assert.notEqual(NumericGuard.check(entry.text), null, entry.category);
  }
});

test("NumericGuard accepts every safe corpus entry", () => {
  // Given: the fixed safe corpus controls
  // When/Then: each genuinely non-numeric form is checked
  for (const entry of corpus.accept) {
    assert.equal(NumericGuard.check(entry.text), null, entry.category);
  }
});

test("NumericGuard catches full-width digits before and after NFKC normalization", () => {
  // Given: a full-width numeric character
  const raw = "５";

  // When/Then: both representations are guarded
  assert.notEqual(NumericGuard.check(raw), null);
  assert.notEqual(NumericGuard.check(raw.normalize("NFKC")), null);
});
