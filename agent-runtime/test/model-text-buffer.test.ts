import assert from "node:assert/strict";
import test from "node:test";

import { ModelTextBuffer, NumericGuardViolation } from "../src/finalizer.ts";

test("ModelTextBuffer rejects a numeric token split across chunks and closes the buffer", () => {
  // Given: a streaming buffer
  const buffer = new ModelTextBuffer();

  // When: a decimal percentage arrives split across chunks
  buffer.append("4");
  buffer.append(".5");

  // Then: the boundary detects it and later appends cannot resume streaming
  assert.throws(() => buffer.append("%"), NumericGuardViolation);
  assert.equal(buffer.guardRejected, true);
  assert.throws(() => buffer.append("safe prose"), NumericGuardViolation);
});

test("ModelTextBuffer returns safe streamed prose after flush", () => {
  // Given: prose split over two chunks
  const buffer = new ModelTextBuffer();

  // When: it is appended and flushed
  buffer.append("Market conditions remain ");
  buffer.append("resilient.");

  // Then: the original safe text is emitted
  assert.equal(buffer.flush(), "Market conditions remain resilient.");
});

test("ModelTextBuffer validates a completed numeric token after safe prose", () => {
  // Given: a safe completed sentence followed by a second streamed token
  const buffer = new ModelTextBuffer();
  buffer.append("Market conditions remain resilient. ");
  buffer.append("4");

  // When/Then: the later percentage boundary is also guarded
  assert.throws(() => buffer.append("%"), NumericGuardViolation);
});

test("ModelTextBuffer rejects a forbidden token discovered on flush", () => {
  // Given: a buffer containing a word that is not complete until flush
  const buffer = new ModelTextBuffer();
  buffer.append("five");

  // When/Then: final validation refuses the buffered text
  assert.throws(() => buffer.flush(), NumericGuardViolation);
});
