\* One admissibility cell [Outcome Liveness Verdict Count HasPre HasSuccessor]
   with the documented
   shape: committed on a non-live target is refused; committed on a live
   target admits at least one state; aborted admits exactly the pre-state;
   unknown admits the pre-state or the pre-state and the successor. *\
(datatype cell
  if (= Out committed) if (= L nonlive) if (= V refuses) if (= N 0)
  if (= Pre false) if (= Succ false)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;

  if (= Out committed) if (= L live) if (= V admits) if (= N 1)
  if (= Succ true)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;

  if (= Out aborted) if (element? L [live nonlive]) if (= V admits) if (= N 1)
  if (= Pre true)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;

  if (= Out unknown) if (= L live) if (= V admits) if (= N 2)
  if (= Pre true) if (= Succ true)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;

  if (= Out unknown) if (= L live) if (= V admits) if (= N 1)
  if (= Pre true) if (= Succ true)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;

  if (= Out unknown) if (= L nonlive) if (= V admits) if (= N 1)
  if (= Pre true)
  ______________________
  (cons Out (cons L (cons V (cons N (cons Pre (cons Succ [])))))) : cell;)
