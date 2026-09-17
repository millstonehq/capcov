\* A list of distinct table names, each a string.  X is the literal head of
   the cons pattern; the tail is walked before the membership test. *\
(datatype table-list
  ______________________
  [] : table-list;

  if (string? X)
  if (not (element? X (tt.value Xs)))
  Xs : table-list;
  ______________________
  (cons X Xs) : table-list;)
