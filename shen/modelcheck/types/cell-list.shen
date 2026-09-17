\* Cells with pairwise distinct (Outcome, Liveness) keys. *\
(datatype cell-list
  ______________________
  [] : cell-list;

  if (not (element? (mc.cell-key (tt.value C)) (map (/. X (mc.cell-key X)) (tt.value Cs))))
  C : cell;
  Cs : cell-list;
  ______________________
  (cons C Cs) : cell-list;)
