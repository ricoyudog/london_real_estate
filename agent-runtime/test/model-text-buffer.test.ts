import assert from "node:assert/strict";
import test from "node:test";

import { ModelTextBuffer } from "../src/finalizer.ts";

test("ModelTextBuffer filters numeric tokens from streamed prose", () => {
  const buffer = new ModelTextBuffer();
  buffer.append("The rate is ");
  buffer.append("5");
  buffer.append(".25");
  buffer.append("%");
  buffer.append(" today.");
  assert.equal(buffer.flush(), "The rate is  today.");
});

test("ModelTextBuffer returns safe streamed prose after flush", () => {
  const buffer = new ModelTextBuffer();
  buffer.append("Market conditions remain ");
  buffer.append("resilient.");
  assert.equal(buffer.flush(), "Market conditions remain resilient.");
});

test("ModelTextBuffer filters a numeric token that appears after safe prose", () => {
  const buffer = new ModelTextBuffer();
  buffer.append("Market conditions remain resilient. ");
  buffer.append("The number is ");
  buffer.append("42");
  buffer.append(" ");
  buffer.append("today.");
  assert.equal(buffer.flush(), "Market conditions remain resilient. The number is today.");
});

test("ModelTextBuffer retains incomplete tokens that have not hit a boundary", () => {
  const buffer = new ModelTextBuffer();
  buffer.append("five");
  assert.equal(buffer.flush(), "five");
});
