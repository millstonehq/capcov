\* Registry ids: symbols, pairwise distinct, none malformed. *\
(datatype id-list
  ______________________
  [] : id-list;

  if (symbol? X)
  if (not (= X malformed))
  if (not (element? X (tt.value Xs)))
  Xs : id-list;
  ______________________
  (cons X Xs) : id-list;)
