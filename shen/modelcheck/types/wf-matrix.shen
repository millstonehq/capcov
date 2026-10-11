\* [Op Cells] is a well-formed admissibility matrix when its cells are a
   cell-list covering all six (Outcome, Liveness) keys: total and disjoint. *\
(datatype wf-matrix
  if (= 6 (length (tt.value Cs)))
  Cs : cell-list;
  ______________________
  (cons Op (cons Cs [])) : wf-matrix;)
