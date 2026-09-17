\* One admissibility cell [Outcome Liveness Verdict Count] with the documented
   shape: committed on a non-live target is refused; committed on a live
   target admits at least one state; aborted admits exactly the pre-state;
   unknown admits the pre-state or the pre-state and the successor. *\
(datatype cell
  if (= Out committed) if (= L nonlive) if (= V refuses)
  ______________________
  (cons Out (cons L (cons V (cons N [])))) : cell;

  if (= Out committed) if (= L live) if (= V admits) if (>= N 1)
  ______________________
  (cons Out (cons L (cons V (cons N [])))) : cell;

  if (= Out aborted) if (element? L [live nonlive]) if (= V admits) if (= N 1)
  ______________________
  (cons Out (cons L (cons V (cons N [])))) : cell;

  if (= Out unknown) if (element? L [live nonlive]) if (= V admits) if (element? N [1 2])
  ______________________
  (cons Out (cons L (cons V (cons N [])))) : cell;)
