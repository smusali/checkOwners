# False positives

A false positive is a case where the scored owner and the maintainer's judgment disagree. The prediction and the repository evidence stay in the record. The correction sits beside them.

Submit a correction with the false-positive issue form. Say whether the anonymized report may be added here. A fixed entry names the regression test that locks the correction. An open entry names the issue that tracks it.

## formatting-reindent

Prediction: Bob is the owner of foo.py because a reformat commit touches the file.

Judgment: Alice remains the primary owner. Bob's blame share is 0.

Reason: A whitespace reformat does not confer ownership.

Fix: Blame share for the reformat is 0, so Bob scores below Alice.

Category: recommendation_wrong. Status: fixed. Test: `test_golden_formatting`.

Evidence: Alice creates and edits foo.py. Bob reindents it. Alice reviewed that change. The reformat commit and Bob's zero blame share stay in the record.
