\* [Op Declared ObservedAsIs Vocabulary] is a well-formed declared write-set
   when Declared is a table-list, every declared table is one the effect
   vocabulary can write for this op, every as-is write observed on a witness
   is declared, and every declared table is observed on some witness. *\
(datatype wf-writes
  if (tt.subset? (tt.value D) (tt.value V))
  if (tt.subset? (tt.value O) (tt.value D))
  if (tt.subset? (tt.value D) (tt.value O))
  D : table-list;
  ______________________
  (cons Op (cons D (cons O (cons V [])))) : wf-writes;)
