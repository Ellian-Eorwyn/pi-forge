
## bulk: `chat-27b` -> `moe-35a3b`

| Case | chat-27b | moe-35a3b | silent | s/item | verdict |
| --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | 10/12 | 6/12 | 0->0 | 0.7->0.3 (-0.4) | worse |
| `abstention-grounded` | 12/12 | 12/12 | 0->0 | 2.2->0.3 (-1.9) | holds |
| `braindump-split` | 4/8 | 2/8 | 0->0 | 8.2->4.8 (-3.4) | worse |
| `classify-hard` | 3/8 | 0/8 | 0->0 | 6.6->1.2 (-5.4) | worse |
| `classify-notes` | 3/8 | 3/8 | 0->0 | 1.9->1.2 (-0.7) | holds |
| `connection-judgment` | 14/16 | 15/16 | 0->0 | 1.7->0.5 (-1.2) | holds |
| `doc-cleanup-ocr` | 2/8 | 3/8 | 0->0 | 10.1->7.7 (-2.4) | holds |
| `enumeration-breadth` | 3/8 | 0/8 | 0->0 | 51.5->39.6 (-11.9) | worse |
| `grounding-draft` | 3/8 | 0/8 | 0->0 | 9.0->2.9 (-6.1) | worse |
| `lcr-48k` | 10/10 | 9/10 | 0->0 | 1.8->0.6 (-1.2) | holds |
| `lcr-60k` | 9/10 | 8/10 | 0->0 | 1.8->0.6 (-1.2) | holds |
| `lcr-80k` | 9/10 | 8/10 | 0->0 | 2.3->0.7 (-1.6) | holds |
| `meeting-brief` | 6/8 | 1/8 | 2->1 | 28.2->6.3 (-21.8) | worse |
| `summary-report` | 8/8 | 8/8 | 2->3 | 8.0->1.2 (-6.8) | **silent failures** |
| `summary-transcript` | 8/8 | 8/8 | 1->1 | 3.9->0.9 (-3.1) | holds |
| `transcript-cleanup-meeting` | 1/8 | 0/8 | 0->0 | 26.9->10.0 (-17.0) | holds |
| `transcript-cleanup-memo` | 2/8 | 1/8 | 0->0 | 6.4->2.6 (-3.8) | holds |
| `verifier-seeded` | 7/8 | 2/8 | 0->0 | 4.1->1.9 (-2.1) | worse |

  moe-35a3b: better on 0, worse on 8.

## verify: `think-27b` -> `moe-35a3b-think`

| Case | think-27b | moe-35a3b-think | silent | s/item | verdict |
| --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | 5/12 | 6/12 | ?->0 | 9.8->8.0 (-1.8) | holds |
| `abstention-grounded` | 12/12 | 11/12 | ?->0 | 15.2->7.1 (-8.1) | holds |
| `braindump-split` | 7/8 | 7/8 | ?->0 | 61.9->28.7 (-33.2) | holds |
| `classify-hard` | 0/8 | 1/8 | ?->0 | 47.0->22.9 (-24.1) | holds |
| `classify-notes` | 5/8 | 4/8 | ?->0 | 38.1->20.9 (-17.2) | holds |
| `connection-judgment` | 15/16 | 15/16 | ?->0 | 31.5->12.8 (-18.7) | holds |
| `doc-cleanup-ocr` | 2/8 | 2/8 | ?->0 | 123.2->78.4 (-44.8) | holds |
| `enumeration-breadth` | 0/8 | 0/8 | ?->0 | 97.4->56.3 (-41.1) | holds |
| `grounding-draft` | 3/8 | 0/8 | ?->0 | 68.5->35.3 (-33.3) | worse |
| `lcr-48k` | 10/10 | 9/10 | ?->0 | 18.7->10.2 (-8.5) | holds |
| `lcr-60k` | 10/10 | 10/10 | ?->0 | 16.3->12.3 (-3.9) | holds |
| `lcr-80k` | 10/10 | 10/10 | ?->0 | 18.4->13.0 (-5.3) | holds |
| `meeting-brief` | 5/8 | 4/8 | ?->1 | 105.9->67.6 (-38.3) | **silent failures** |
| `summary-report` | 8/8 | 8/8 | ?->5 | 62.0->30.5 (-31.6) | **silent failures** |
| `summary-transcript` | 8/8 | 8/8 | ?->3 | 47.1->22.1 (-25.0) | **silent failures** |
| `transcript-cleanup-meeting` | 0/8 | 0/8 | ?->0 | 125.3->80.1 (-45.2) | holds |
| `transcript-cleanup-memo` | 8/8 | 2/8 | ?->1 | 60.6->48.3 (-12.3) | **silent failures** |
| `verifier-seeded` | 7/8 | 7/8 | ?->0 | 33.0->13.7 (-19.3) | holds |

  moe-35a3b-think: better on 0, worse on 5.
  `?` — not graded yet: `think-27b`. An ungraded
  model has no silent failures the way an unopened envelope has no bad news.

## small: `task-4b` -> `task-9b`

| Case | task-4b | task-9b | silent | s/item | verdict |
| --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | 5/12 | 7/12 | ?->0 | 0.3->0.2 (-0.1) | better |
| `abstention-grounded` | 10/12 | 11/12 | ?->0 | 3.9->5.4 (+1.5) | holds |
| `braindump-split` | 5/8 | 6/8 | ?->0 | 4.1->4.2 (+0.1) | holds |
| `classify-hard` | 2/8 | 2/8 | ?->0 | 2.3->3.0 (+0.7) | holds |
| `classify-notes` | 4/8 | 2/8 | ?->0 | 2.4->3.0 (+0.6) | worse |
| `connection-judgment` | 16/16 | 16/16 | ?->0 | 0.5->0.6 (+0.1) | holds |
| `doc-cleanup-ocr` | 3/8 | 0/8 | ?->0 | 7.5->9.3 (+1.8) | worse |
| `enumeration-breadth` | 1/8 | 0/8 | ?->0 | 100.6->87.8 (-12.8) | holds |
| `grounding-draft` | 5/8 | 0/8 | ?->0 | 2.7->4.0 (+1.2) | worse |
| `lcr-48k` | 8/10 | 9/10 | ?->0 | 9.7->12.9 (+3.2) | holds |
| `lcr-60k` | 8/10 | 10/10 | ?->0 | 12.2->16.1 (+3.9) | better |
| `lcr-80k` | n/a | n/a | | | not comparable |
| `meeting-brief` | 2/8 | 2/8 | ?->1 | 9.6->16.6 (+7.0) | **silent failures** |
| `summary-report` | 6/8 | 7/8 | ?->5 | 2.5->3.0 (+0.5) | **silent failures** |
| `summary-transcript` | 8/8 | 8/8 | ?->1 | 1.0->1.2 (+0.1) | **silent failures** |
| `transcript-cleanup-meeting` | 7/8 | 5/8 | ?->0 | 16.6->18.8 (+2.3) | worse |
| `transcript-cleanup-memo` | 5/8 | 7/8 | ?->0 | 3.2->3.9 (+0.7) | better |
| `verifier-seeded` | 6/8 | 7/8 | ?->0 | 3.0->4.0 (+1.0) | holds |

  task-9b: better on 3, worse on 7.
  `?` — not graded yet: `task-4b`. An ungraded
  model has no silent failures the way an unopened envelope has no bad news.

## within the MoE: `moe-35a3b` -> `moe-35a3b-think`

| Case | moe-35a3b | moe-35a3b-think | silent | s/item | verdict |
| --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | 6/12 | 6/12 | 0->0 | 0.3->8.0 (+7.7) | holds |
| `abstention-grounded` | 12/12 | 11/12 | 0->0 | 0.3->7.1 (+6.8) | holds |
| `braindump-split` | 2/8 | 7/8 | 0->0 | 4.8->28.7 (+23.9) | better |
| `classify-hard` | 0/8 | 1/8 | 0->0 | 1.2->22.9 (+21.8) | holds |
| `classify-notes` | 3/8 | 4/8 | 0->0 | 1.2->20.9 (+19.7) | holds |
| `connection-judgment` | 15/16 | 15/16 | 0->0 | 0.5->12.8 (+12.3) | holds |
| `doc-cleanup-ocr` | 3/8 | 2/8 | 0->0 | 7.7->78.4 (+70.7) | holds |
| `enumeration-breadth` | 0/8 | 0/8 | 0->0 | 39.6->56.3 (+16.7) | holds |
| `grounding-draft` | 0/8 | 0/8 | 0->0 | 2.9->35.3 (+32.3) | holds |
| `lcr-48k` | 9/10 | 9/10 | 0->0 | 0.6->10.2 (+9.6) | holds |
| `lcr-60k` | 8/10 | 10/10 | 0->0 | 0.6->12.3 (+11.7) | better |
| `lcr-80k` | 8/10 | 10/10 | 0->0 | 0.7->13.0 (+12.3) | better |
| `meeting-brief` | 1/8 | 4/8 | 1->1 | 6.3->67.6 (+61.2) | better |
| `summary-report` | 8/8 | 8/8 | 3->5 | 1.2->30.5 (+29.3) | **silent failures** |
| `summary-transcript` | 8/8 | 8/8 | 1->3 | 0.9->22.1 (+21.2) | **silent failures** |
| `transcript-cleanup-meeting` | 0/8 | 0/8 | 0->0 | 10.0->80.1 (+70.1) | holds |
| `transcript-cleanup-memo` | 1/8 | 2/8 | 0->1 | 2.6->48.3 (+45.7) | **silent failures** |
| `verifier-seeded` | 2/8 | 7/8 | 0->0 | 1.9->13.7 (+11.8) | better |

  moe-35a3b-think: better on 5, worse on 3.

## within the 27B (shipped table): `chat-27b` -> `think-27b`

| Case | chat-27b | think-27b | silent | s/item | verdict |
| --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | 10/12 | 5/12 | 0->? | 0.7->9.8 (+9.2) | worse |
| `abstention-grounded` | 12/12 | 12/12 | 0->? | 2.2->15.2 (+13.0) | holds |
| `braindump-split` | 4/8 | 7/8 | 0->? | 8.2->61.9 (+53.7) | better |
| `classify-hard` | 3/8 | 0/8 | 0->? | 6.6->47.0 (+40.4) | worse |
| `classify-notes` | 3/8 | 5/8 | 0->? | 1.9->38.1 (+36.2) | better |
| `connection-judgment` | 14/16 | 15/16 | 0->? | 1.7->31.5 (+29.8) | holds |
| `doc-cleanup-ocr` | 2/8 | 2/8 | 0->? | 10.1->123.2 (+113.1) | holds |
| `enumeration-breadth` | 3/8 | 0/8 | 0->? | 51.5->97.4 (+45.9) | worse |
| `grounding-draft` | 3/8 | 3/8 | 0->? | 9.0->68.5 (+59.5) | holds |
| `lcr-48k` | 10/10 | 10/10 | 0->? | 1.8->18.7 (+16.9) | holds |
| `lcr-60k` | 9/10 | 10/10 | 0->? | 1.8->16.3 (+14.4) | holds |
| `lcr-80k` | 9/10 | 10/10 | 0->? | 2.3->18.4 (+16.0) | holds |
| `meeting-brief` | 6/8 | 5/8 | 2->? | 28.2->105.9 (+77.7) | holds |
| `summary-report` | 8/8 | 8/8 | 2->? | 8.0->62.0 (+54.0) | holds |
| `summary-transcript` | 8/8 | 8/8 | 1->? | 3.9->47.1 (+43.1) | holds |
| `transcript-cleanup-meeting` | 1/8 | 0/8 | 0->? | 26.9->125.3 (+98.4) | holds |
| `transcript-cleanup-memo` | 2/8 | 8/8 | 0->? | 6.4->60.6 (+54.2) | better |
| `verifier-seeded` | 7/8 | 7/8 | 0->? | 4.1->33.0 (+28.9) | holds |

  think-27b: better on 3, worse on 3.
  `?` — not graded yet: `think-27b`. An ungraded
  model has no silent failures the way an unopened envelope has no bad news.
