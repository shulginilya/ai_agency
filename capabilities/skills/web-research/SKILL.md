---
name: web-research
description: Method for answering a question from web sources when the repository cannot answer it. Use when a task depends on facts that are not in the codebase - upstream release notes, an API's current behaviour, a CVE, a version's actual ship date. Not for anything a grep would settle.
---

# Web research

## Before you search

Check the repository first. A question about how *this* code behaves is answered by
reading it, and a search that confirms what the code already says has cost turns and
bought nothing. Search when the answer lives outside the repo: upstream behaviour,
versions, advisories, another project's contract.

## Method

1. **State the question as a claim you could falsify.** "Does SDK 0.2.132 apply the
   skills default when setting_sources is `[]`?" beats "how do skills work".
2. **Prefer the primary source.** Release notes over a blog post about them; the source
   file over the documentation describing it; the advisory over the summary.
3. **Quote the line that supports each claim.** A paraphrase cannot be checked, and a
   paraphrase of something you skimmed is how a wrong fact enters with confidence.
4. **Say when sources disagree** rather than picking one silently. Two dated sources
   disagreeing usually means something changed between them, which is itself the answer.
5. **Date what you find.** "As of the 0.2.132 release notes" is a fact; "the SDK does X"
   is a claim with a hidden expiry.

## Reporting

Lead with the answer. Then the evidence, each with its source. Then what you could not
establish -- an admitted gap is information, and a confident guess is not.
